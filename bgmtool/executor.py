from contextlib import contextmanager
import ctypes
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET
from .common import atomic_json, digest, guard, key, lp, read_json, roots_from, unchanged

def alive(pid):
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = ctypes.c_ulong()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

@contextmanager
def library_lock(data, run):
    path = Path(data) / 'execution.lock'
    with path.open('x', encoding='utf-8') as f:
        json.dump({'pid':os.getpid(), 'run':str(run)}, f)
        f.flush(); os.fsync(f.fileno())
    try:
        yield
    finally:
        path.unlink()

def unlock_stale(data):
    path = Path(data) / 'execution.lock'
    e = read_json(path)
    if e and alive(e['pid']):
        raise ValueError('执行进程仍在运行，不允许解除锁')
    if path.exists():
        path.unlink()

def service_command(command):
    p = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                        "$ErrorActionPreference='Stop'; " + command],
                       capture_output=True, text=True, timeout=60,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if p.returncode:
        raise RuntimeError('Jellyfin 服务操作失败；请使用管理员权限运行，或手动停止/启动服务')
    return p.stdout.strip()

@contextmanager
def pause_jellyfin(enabled, emit):
    restart = False
    try:
        if enabled:
            if os.name != 'nt' or not ctypes.windll.shell32.IsUserAnAdmin():
                raise ValueError('临时停止服务需要 Windows 管理员权限；可取消选项后重试')
            status = service_command('(Get-Service -Name JellyfinServer).Status.ToString()')
            if status == 'Running':
                restart = True
                service_command("Stop-Service -Name JellyfinServer -Force; (Get-Service -Name JellyfinServer).WaitForStatus('Stopped',[TimeSpan]::FromSeconds(30))")
                emit('Jellyfin 已临时停止；无论成功或失败，都会尝试恢复原运行状态。')
        yield
    finally:
        if restart:
            service_command("Start-Service -Name JellyfinServer; (Get-Service -Name JellyfinServer).WaitForStatus('Running',[TimeSpan]::FromSeconds(30))")
            emit('Jellyfin 已恢复运行；未调用媒体库刷新接口。')

class Executor:
    def __init__(self, config, data, run, emit=print):
        self.roots = roots_from(config); self.data = Path(data).absolute()
        self.run = Path(run).absolute(); self.emit = emit
        if not self.run.resolve().is_relative_to((self.data / 'runs').resolve()):
            raise ValueError('运行记录须在工具 data/runs 内')
        self.plan = read_json(self.run / 'plan.json')
        checksum = (self.run / 'plan.sha256').read_text(encoding='ascii').strip()
        if checksum != hashlib.sha256((self.run / 'plan.json').read_bytes()).hexdigest():
            raise ValueError('预览计划被修改；请重新生成，不要手工改已生成的 plan.json')
        if self.plan['roots'] != [str(r) for r in self.roots]:
            raise ValueError('媒体根目录已变更；请使用生成计划时的路径或重新预览')
        self.journal = self.run / 'journal.jsonl'

    def events(self):
        return [json.loads(s) for s in self.journal.read_text(encoding='utf-8').splitlines() if s.strip()] if self.journal.exists() else []

    def log(self, **e):
        e['time'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.journal.open('a', encoding='utf-8') as f:
            f.write(json.dumps(e, ensure_ascii=False) + '\n'); f.flush(); os.fsync(f.fileno())

    def valid_temp(self, temp, e):
        """A crash may leave only a prefix of our own staged metadata."""
        data = lp(temp).read_bytes()
        expected = Path(e['src']).read_bytes()
        return len(data) <= len(expected) and expected.startswith(data)

    def preflight(self, resume=False):
        events = self.events()
        if any(e['event'] == 'rollback_complete' for e in events):
            raise ValueError('此计划已回滚，不能继续执行；请重新扫描')
        if events and not resume:
            raise ValueError('此计划已有执行记录；使用“恢复中断”，不要再次普通执行')
        intents = {(e.get('action'), e.get('path') or e.get('src'), e.get('dst'))
                   for e in events if e['event'] == 'intent'}
        writes = {key(e['dst']):e for e in self.plan['writes']}
        deletions = {key(e['path']):e for e in self.plan['deletions']}
        for e in self.plan['moves']:
            src = guard(e['src'], self.roots); dst = guard(e['dst'], self.roots)
            a, b = lp(src).exists(), lp(dst).exists()
            if a == b:
                raise ValueError('移动状态冲突：' + str(src))
            if b and (not resume or ('move', str(src), str(dst)) not in intents):
                raise ValueError('目标存在但没有已记录的移动意图：' + str(dst))
            unchanged(src if a else dst, e)
            parent = dst.parent
            while not lp(parent).exists():
                parent = parent.parent
            if e['device'] != lp(parent).stat().st_dev:
                raise ValueError('跨卷移动被禁止')
        for e in self.plan['deletions']:
            p = guard(e['path'], self.roots)
            if p.suffix.lower() != '.nfo':
                raise ValueError('删除目标不是 NFO')
            rewritten = writes.get(key(p))
            written_here = rewritten and ('write', str(p), None) in intents and lp(p).exists() and digest(p) == rewritten['sha256']
            if written_here:
                continue
            if not lp(p).exists():
                if not resume or ('delete_nfo', str(p), None) not in intents:
                    raise ValueError('旧 NFO 已变化或丢失：' + str(p))
            else:
                unchanged(p, e)
                if digest(p) != e['sha256']:
                    raise ValueError('旧 NFO 内容发生变化：' + str(p))
        for e in self.plan['writes']:
            dst = guard(e['dst'], self.roots); stage = Path(e['src'])
            if not stage.resolve().is_relative_to((self.run / 'staged').resolve()):
                raise ValueError('暂存文件不在当前运行记录内')
            if dst.suffix.lower() not in ('.nfo', '.jpg', '.png', '.webp') and dst.name != '.ignore':
                raise ValueError('禁止写入视频或其他非元数据文件')
            if digest(stage) != e['sha256']:
                raise ValueError('暂存元数据校验失败')
            if dst.suffix.lower() == '.nfo':
                ET.parse(lp(stage))
            if lp(dst).exists():
                was_written = resume and ('write', str(dst), None) in intents and digest(dst) == e['sha256']
                expected_old = deletions.get(key(dst))
                if not was_written and not expected_old:
                    raise ValueError('新元数据目标已存在，拒绝覆盖：' + str(dst))
            temp = guard(dst.with_name(dst.name + '.' + self.plan['run_id'] + '.tmp'), self.roots)
            if lp(temp).exists() and (not resume or ('write', str(dst), None) not in intents or not self.valid_temp(temp, e)):
                raise ValueError('存在未识别或不完整的临时文件：' + str(temp))
        return intents

    def mkdirs(self, parent):
        missing = []
        while not lp(parent).exists():
            missing.append(guard(parent, self.roots)); parent = parent.parent
        for p in reversed(missing):
            self.log(event='intent', action='mkdir', path=str(p))
            lp(p).mkdir(); self.log(event='done', action='mkdir', path=str(p))

    def apply(self, resume=False, pause=False):
        with library_lock(self.data, self.run), pause_jellyfin(pause, self.emit):
            intents = self.preflight(resume)
            self.log(event='resume' if resume else 'start')
            for e in self.plan['deletions']:
                p = guard(e['path'], self.roots)
                rewritten = next((w for w in self.plan['writes'] if key(w['dst']) == key(p)), None)
                if rewritten and ('write', str(p), None) in intents and lp(p).exists() and digest(p) == rewritten['sha256']:
                    continue
                if lp(p).exists():
                    unchanged(p, e)
                    if digest(p) != e['sha256']:
                        raise ValueError('旧 NFO 在执行期间变化，已停止')
                    self.log(event='intent', action='delete_nfo', path=str(p), sha256=e['sha256'])
                    lp(p).unlink(); self.log(event='done', action='delete_nfo', path=str(p))
            for i, e in enumerate(self.plan['moves'], 1):
                src = guard(e['src'], self.roots); dst = guard(e['dst'], self.roots)
                if not lp(src).exists():
                    unchanged(dst, e); continue
                unchanged(src, e)
                if lp(dst).exists():
                    raise ValueError('移动目标在执行期间出现，已停止')
                self.mkdirs(dst.parent)
                self.log(event='intent', action='move', src=str(src), dst=str(dst))
                os.rename(lp(src), lp(dst)); unchanged(dst, e)
                self.log(event='done', action='move', src=str(src), dst=str(dst))
                if i % 25 == 0:
                    self.emit(f'已移动 {i}/{len(self.plan["moves"])} 个文件')
            for e in self.plan['writes']:
                dst = guard(e['dst'], self.roots)
                if lp(dst).exists():
                    if digest(dst) == e['sha256'] and ('write', str(dst), None) in intents:
                        continue
                    raise ValueError('元数据目标在执行期间出现，已停止：' + str(dst))
                self.mkdirs(dst.parent)
                temp = guard(dst.with_name(dst.name + '.' + self.plan['run_id'] + '.tmp'), self.roots)
                self.log(event='intent', action='write', path=str(dst), temp=str(temp), sha256=e['sha256'])
                if lp(temp).exists() and digest(temp) != e['sha256']:
                    if not self.valid_temp(temp, e):
                        raise ValueError('临时元数据文件发生变化，已停止')
                    self.log(event='intent', action='discard_partial_temp', path=str(temp))
                    lp(temp).unlink()
                if not lp(temp).exists():
                    data = Path(e['src']).read_bytes()
                    if hashlib.sha256(data).hexdigest() != e['sha256']:
                        raise ValueError('暂存文件变化，已停止')
                    with lp(temp).open('xb') as f:
                        f.write(data); f.flush(); os.fsync(f.fileno())
                os.rename(lp(temp), lp(dst))
                self.log(event='done', action='write', path=str(dst), sha256=e['sha256'])
            for path in self.plan['cleanup']:
                p = guard(path, self.roots)
                if lp(p).exists() and lp(p).is_dir() and not any(lp(p).iterdir()):
                    self.log(event='intent', action='empty_rmdir', path=str(p))
                    lp(p).rmdir(); self.log(event='done', action='empty_rmdir', path=str(p))
            report = self.verify()
            self.log(event='complete'); self.emit('执行及校验完成。未复制视频，未触发 Jellyfin 刷新。')
            return report

    def verify(self):
        for e in self.plan['moves']:
            unchanged(guard(e['dst'], self.roots), e)
            if lp(e['src']).exists():
                raise ValueError('原路径仍存在：' + e['src'])
        for e in self.plan['writes']:
            p = guard(e['dst'], self.roots)
            if digest(p) != e['sha256']:
                raise ValueError('元数据内容已变化：' + str(p))
            if p.suffix.lower() == '.nfo':
                ET.parse(lp(p))
        report = {'verified':True, 'run_id':self.plan['run_id'], 'summary':self.plan['summary'],
                  'video_files_copied':0, 'video_bytes_modified':0,
                  'old_nfo_backup_created':False, 'jellyfin_scan_triggered':False}
        atomic_json(self.run / 'verification.json', report)
        return report

    def rollback(self, pause=False):
        with library_lock(self.data, self.run), pause_jellyfin(pause, self.emit):
            events = self.events()
            if not events:
                raise ValueError('没有执行记录，不需要回滚')
            if any(e['event'] == 'rollback_complete' for e in events):
                raise ValueError('此运行已回滚')
            intents = [e for e in events if e['event'] == 'intent']
            moves = {key(e['dst']):e for e in self.plan['moves']}
            writes = {key(e['dst']):e for e in self.plan['writes']}
            # Validate everything before making the first rollback change.
            for e in intents:
                if e['action'] == 'move':
                    src = guard(e['src'], self.roots); dst = guard(e['dst'], self.roots)
                    if lp(dst).exists():
                        if lp(src).exists():
                            raise ValueError('回滚原位置已有文件，拒绝覆盖')
                        unchanged(dst, moves[key(dst)])
                    elif not lp(src).exists():
                        raise ValueError('回滚源和目标均丢失')
                    else:
                        unchanged(src, moves[key(dst)])
                elif e['action'] == 'write':
                    for name in ('path', 'temp'):
                        p = guard(e[name], self.roots)
                        if lp(p).exists() and digest(p) != e['sha256'] and not (name == 'temp' and self.valid_temp(p, writes[key(e['path'])])):
                            raise ValueError('新元数据被修改，拒绝删除：' + str(p))
            handled = set()
            for e in reversed(intents):
                action = e['action']; identity = (action, e.get('path') or e.get('dst'))
                if identity in handled:
                    continue
                handled.add(identity)
                if action == 'write':
                    for name in ('path', 'temp'):
                        p = guard(e[name], self.roots)
                        if lp(p).exists():
                            if digest(p) != e['sha256'] and not (name == 'temp' and self.valid_temp(p, writes[key(e['path'])])):
                                raise ValueError('回滚期间元数据发生变化，已停止')
                            lp(p).unlink()
                elif action == 'move':
                    src = guard(e['src'], self.roots); dst = guard(e['dst'], self.roots)
                    if lp(dst).exists():
                        unchanged(dst, moves[key(dst)])
                        if lp(src).exists():
                            raise ValueError('回滚原位置出现文件，已停止')
                        lp(src.parent).mkdir(parents=True, exist_ok=True)
                        os.rename(lp(dst), lp(src))
                elif action == 'empty_rmdir':
                    lp(guard(e['path'], self.roots)).mkdir(parents=True, exist_ok=True)
                elif action == 'mkdir':
                    p = guard(e['path'], self.roots)
                    if lp(p).exists() and not any(lp(p).iterdir()):
                        lp(p).rmdir()
            # Destinations could still contain moved videos at the time their
            # mkdir event was reversed. Revisit only our own empty directories.
            created = sorted({e['path'] for e in intents if e['action']=='mkdir'},
                             key=lambda p:len(Path(p).parts), reverse=True)
            for path in created:
                p = guard(path, self.roots)
                if lp(p).exists() and lp(p).is_dir() and not any(lp(p).iterdir()):
                    lp(p).rmdir()
            self.log(event='rollback_complete')
            self.emit('回滚完成；按选项删除的旧 NFO 不会恢复（未备份）。')
