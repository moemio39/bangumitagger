import collections
import difflib
import hashlib
from pathlib import Path
import re
import time
import uuid
import xml.etree.ElementTree as ET
from . import metadata as meta
from .common import (VIDEO, SIDECAR, EXTRA_DIRS, atomic_json, episode_number, guard,
                     is_extra, key, lp, nfo_id, normal, query_name, roots_from,
                     reparse, safe, snapshot, walk_files)

def valid_nfo(path):
    try:
        return ET.parse(lp(path)).getroot().tag in ('tvshow', 'movie', 'episodedetails', 'season')
    except (OSError, ET.ParseError):
        return False

def scan(config):
    roots = roots_from(config)
    groups = {}; stats = {'videos_seen':0, 'already_scraped':0, 'still_downloading':0}
    for root, kind in zip(roots, ('show', 'movie')):
        for entry in lp(root).iterdir():
            entry = normal(entry)
            if entry.name.startswith('.') or reparse(entry):
                continue
            all_files = list(walk_files(entry)) if lp(entry).is_dir() else [entry]
            managed = lp(entry).is_dir() and valid_nfo(entry / ('tvshow.nfo' if kind == 'show' else 'movie.nfo'))
            root_id = nfo_id(entry / ('tvshow.nfo' if kind == 'show' else 'movie.nfo')) if managed else None
            videos = [p for p in all_files if p.suffix.lower() in VIDEO]
            for p in videos:
                stats['videos_seen'] += 1
                canonical_movie = kind=='movie' and managed and p.parent==entry and (
                    p.stem.casefold()==entry.name.casefold() or any(p.stem.casefold().startswith(entry.name.casefold()+s)
                                                                  for s in (' -','_','.',' [')))
                if valid_nfo(p.with_suffix('.nfo')) or (canonical_movie and not is_extra(p, entry)):
                    stats['already_scraped'] += 1
                    continue
                if lp(p).stat().st_size == 0 or time.time() - lp(p).stat().st_mtime < int(config.get('stable_seconds', 120)):
                    stats['still_downloading'] += 1
                    continue
                # Flat releases form one group. Separate nested seasons/releases
                # rather than guessing one Bangumi subject for a franchise pack.
                group_path = p.parent
                while group_path != entry and group_path.name.casefold() in EXTRA_DIRS:
                    group_path = group_path.parent
                if not lp(entry).is_dir():
                    group_path = root
                # Different movie files are separate confirmation units. Do not
                # assume a collection directory is multiple encodes of one film.
                label_path = p if kind == 'movie' or group_path == root else group_path
                if kind == 'movie' and is_extra(p, entry):
                    main = [v for v in videos if not is_extra(v, entry) and p.is_relative_to(v.parent)]
                    if len(main) == 1:
                        label_path = main[0]; group_path = main[0].parent
                gid = hashlib.sha256((key(label_path) + kind).encode()).hexdigest()[:16]
                if gid not in groups:
                    group_managed = managed and (kind=='show' or canonical_movie or is_extra(p,entry))
                    season = 1
                    for parent in [p.parent, *p.parents]:
                        if parent == root:
                            break
                        m = re.fullmatch(r'(?i)(?:Season\s*|S)(\d+)', parent.name)
                        if m:
                            season = int(m[1]); break
                    hint_id = (nfo_id(group_path / 'season.nfo') or root_id) if group_managed else None
                    groups[gid] = {'id':gid, 'path':str(group_path), 'name':label_path.name,
                        'query':query_name(entry.name if group_managed else label_path.stem if kind=='movie' or group_path == root else label_path.name),
                        'kind':kind, 'videos':[], 'managed':group_managed, 'owner':str(entry) if group_managed else None,
                        'hint_subject_id':hint_id, 'hint_root_id':root_id if group_managed else None, 'hint_season':season,
                        'unsupported_disc':any(part.upper() in ('BDMV','VIDEO_TS') for part in p.relative_to(root).parts)}
                groups[gid]['videos'].append(str(p))
    return {'groups':list(groups.values()), 'stats':stats}

def suggest(keyword, candidates):
    def normalized(text):
        return re.sub(r'[^\w\u3040-\u30ff\u4e00-\u9fff]', '', text.casefold())
    q = normalized(keyword)
    rows = []
    for c in candidates:
        aliases = [c.get('name', ''), c.get('name_cn', '')]
        for field in c.get('infobox', []):
            if field.get('key') in ('别名', '中文名'):
                values = field.get('value', [])
                aliases += [v.get('v', '') for v in values if isinstance(v, dict)] if isinstance(values, list) else [str(values)]
        score = max((difflib.SequenceMatcher(None, q, normalized(a)).ratio() for a in aliases if a), default=0)
        rows.append(dict(c, match_score=round(score, 4)))
    return sorted(rows, key=lambda x:x['match_score'], reverse=True)

def choose_episode(episodes, number, mode='auto', ep_type=0):
    rows = [e for e in episodes if e.get('type', 0) == ep_type]
    matched = {}
    fields = ('ep', 'sort') if mode == 'auto' else (mode,)
    for field in fields:
        for e in rows:
            try:
                if float(e.get(field, -999)) == number:
                    matched[e['id']] = e
            except (TypeError, ValueError):
                pass
    if len(matched) != 1:
        raise ValueError(f'章节 {number} 未唯一匹配（{len(matched)} 个结果）；指定 ep/sort 模式或编号偏移')
    return next(iter(matched.values()))

def integer(value, label):
    if isinstance(value,bool) or not re.fullmatch(r'[+-]?\d+',str(value).strip()):
        raise ValueError(label+'必须是整数，不会自动截断小数')
    return int(value)

class Planner:
    def __init__(self, config, data, client, emit=print):
        self.config = config; self.data = Path(data); self.client = client; self.emit = emit
        self.roots = roots_from(config)
        self.bundles = {}; self.covers = {}

    def bundle(self, sid):
        sid = int(sid)
        if sid not in self.bundles:
            self.emit(f'读取 Bangumi 条目 {sid} …')
            self.bundles[sid] = self.client.bundle(sid)
        return self.bundles[sid]

    def owner(self, subject, kind, group):
        root = self.roots[1 if kind == 'movie' else 0]
        sid = subject['id']
        if group['managed'] and group.get('owner'):
            p = guard(group['owner'], self.roots)
            existing = nfo_id(p / ('movie.nfo' if kind == 'movie' else 'tvshow.nfo'))
            if existing != sid:
                raise ValueError('已整理作品的根条目 ID 不同；拒绝把新集加入错误作品')
            return p
        matches = [normal(p) for p in lp(root).iterdir() if p.is_dir() and
                   nfo_id(normal(p) / ('movie.nfo' if kind == 'movie' else 'tvshow.nfo')) == sid]
        if len(matches) > 1:
            raise ValueError('同一 Bangumi 条目存在多个作品目录，请先解决重复归属')
        if matches:
            return guard(matches[0], self.roots)
        year = (subject.get('date') or '')[:4]
        name = safe(meta.title(subject)) + (f' ({year})' if year else '') + f' [bangumiid-{sid}]'
        return guard(root / name, self.roots)

    def build(self, groups, selections, replace_nfo=False):
        run = self.data / 'runs' / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
        stage = run / 'staged'; stage.mkdir(parents=True)
        plan = {'version':1, 'run_id':run.name, 'roots':[str(r) for r in self.roots],
                'moves':[], 'deletions':[], 'writes':[], 'review':[], 'warnings':[], 'cleanup':[],
                'replace_nfo_without_backup':bool(replace_nfo)}
        targets = {}; source_keys = set(); delete_keys = set(); write_keys = {}; protected = set()

        def deletion(path):
            path = guard(path, self.roots)
            if path.suffix.lower() != '.nfo':
                raise ValueError('只允许删除已预览的旧 NFO')
            if lp(path).exists() and key(path) not in delete_keys:
                from .common import digest
                plan['deletions'].append(dict(path=str(path), sha256=digest(path), **snapshot(path)))
                delete_keys.add(key(path))

        def write(path, data):
            path = guard(path, self.roots); k = key(path)
            sha = hashlib.sha256(data).hexdigest()
            if k in write_keys:
                if write_keys[k] != sha:
                    raise ValueError('两个选择生成了冲突元数据：' + str(path))
                return
            if k in targets:
                raise ValueError('元数据与移动目标冲突：' + str(path))
            if lp(path).exists():
                if path.suffix.lower() == '.nfo' and replace_nfo:
                    deletion(path)
                else:
                    protected.add(k)
                    return  # Keep existing NFO and images by default.
            staged = stage / (sha + path.suffix)
            if not staged.exists():
                staged.write_bytes(data)
            plan['writes'].append({'src':str(staged.absolute()), 'dst':str(path), 'sha256':sha, 'size':len(data)})
            write_keys[k] = sha

        def move(src, dst, role):
            src = guard(src, self.roots); dst = guard(dst, self.roots)
            if key(src) == key(dst):
                return
            if key(src) in source_keys:
                raise ValueError('重复移动源文件：' + str(src))
            if key(dst) in targets or key(dst) in write_keys or lp(dst).exists():
                raise ValueError('目标已存在，拒绝覆盖：' + str(dst))
            parent = dst.parent
            while not lp(parent).exists():
                parent = parent.parent
            if lp(src).stat().st_dev != lp(parent).stat().st_dev:
                raise ValueError('跨卷移动被禁止；两个媒体库须在同一卷')
            plan['moves'].append(dict(src=str(src), dst=str(dst), role=role, **snapshot(src)))
            source_keys.add(key(src)); targets[key(dst)] = src

        def cover(bundle, parent):
            for ext in ('.jpg', '.png', '.webp'):
                if lp(parent / ('poster' + ext)).exists():
                    protected.add(key(parent / ('poster' + ext)))
                    return 'poster' + ext
            sid = bundle['subject']['id']
            if sid not in self.covers:
                try:
                    self.covers[sid] = self.client.poster(bundle['subject'])
                except (OSError, ValueError, RuntimeError) as e:
                    self.covers[sid] = None
                    plan['warnings'].append(f'条目 {sid} 封面未下载：{type(e).__name__}；NFO 仍可生成')
            result = self.covers[sid]
            if result:
                data, ext = result; write(parent / ('poster' + ext), data)
                return 'poster' + ext
            return None

        for group in groups:
            if group.get('unsupported_disc'):
                plan['review'].append({'group':group['name'],'reason':'原盘 BDMV/VIDEO_TS 不自动处理，避免把分段流当成独立影片'})
                continue
            selected = selections.get(group['id'])
            if not selected or not selected.get('confirmed'):
                plan['review'].append({'group':group['name'], 'reason':'尚未确认 Bangumi 条目，未修改'})
                continue
            sid = integer(selected['subject_id'],'条目 ID'); kind = selected.get('kind', group['kind'])
            if kind not in ('show', 'movie'):
                raise ValueError('作品类型必须为 show 或 movie')
            season = integer(selected.get('season', group.get('hint_season', 1)),'季号')
            if not 0 <= season <= 99:
                raise ValueError('季号须在 0–99 之间')
            mode = selected.get('numbering', 'auto')
            if mode not in ('auto', 'ep', 'sort'):
                raise ValueError('编号模式须为 auto、ep 或 sort')
            offset = integer(selected.get('offset', 0),'编号偏移')
            bundle = self.bundle(sid); s = bundle['subject']
            platform = str(s.get('platform', ''))
            if kind == 'movie' and platform in ('TV', 'TV动画') and not selected.get('allow_kind_override'):
                raise ValueError('选择的是 TV 动画，不应当作独立电影；请更正类型/条目')
            root_id = integer(selected.get('root_subject_id') or sid,'根作品 ID') if kind == 'show' else sid
            root_bundle = bundle if root_id == sid else self.bundle(root_id)
            root_subject = root_bundle['subject']
            owner = self.owner(root_subject, kind, group)
            source_dir = Path(group['path'])
            videos = [guard(p, self.roots) for p in group['videos']]
            video_destinations = {}; mapped = 0
            for p in videos:
                relative = str(p.relative_to(source_dir))
                override = selected.get('overrides', {}).get(relative, {})
                if not isinstance(override,dict):
                    raise ValueError('逐文件覆盖的值须为 JSON 对象：'+relative)
                role = override.get('role') or ('extra' if is_extra(p, source_dir) else 'movie' if kind == 'movie' else 'episode')
                if role not in ('extra', 'movie', 'episode') or (role == 'movie' and kind != 'movie'):
                    raise ValueError('逐文件 role 仅允许 episode、extra；movie 仅用于电影组')
                ep = None; end = None
                version = hashlib.sha256(key(p).encode()).hexdigest()[:8]
                quality = re.search(r'(?i)\b(\d{3,4}p)\b', p.stem)
                version_name = (quality.group(1) + '-' if quality else '') + version
                if role == 'extra':
                    dst = owner / 'extras' / (safe(p.stem, 120) + '-' + version + p.suffix.lower())
                elif role == 'movie':
                    dst = owner / (owner.name + ' - ' + version_name + p.suffix.lower())
                else:
                    number, end, explicit_season = episode_number(p.name)
                    number = override.get('number', number); end = override.get('end', end)
                    ep_type = integer(override.get('episode_type', 0),'章节类型')
                    is_special = bool(re.search(r'(?i)(?:^|[\[ _])(?:SP|OVA|OAD)(?:\d*|$)(?:[\] _.-]|$)', p.stem))
                    if is_special and 'episode_type' not in override and season != 0 and platform not in ('OVA', 'OAD'):
                        plan['review'].append({'src':str(p), 'reason':'SP/OVA/OAD 与正片区别不明确；指定特别篇类型或 Season 00 后再处理'})
                        continue
                    if number is None and len([e for e in bundle['episodes'] if e.get('type', 0) == ep_type]) == 1:
                        # One explicitly selected single-episode subject, not a
                        # filename guessed to belong to a multi-episode show.
                        only = [e for e in bundle['episodes'] if e.get('type', 0) == ep_type][0]
                        value = only.get('ep') or only.get('sort')
                        if value is not None and float(value).is_integer():
                            number = int(value)
                    if number is None:
                        plan['review'].append({'src':str(p), 'reason':'未识别到可靠集号；使用逐文件覆盖指定 number'})
                        continue
                    number = integer(number,'集号') - offset
                    end = integer(end,'合集结束集号') - offset if end is not None else None
                    if number < 1 or (end is not None and (end < number or end-number > 100)):
                        plan['review'].append({'src':str(p), 'reason':'集号/合集范围不合理，未移动'})
                        continue
                    if explicit_season is not None and explicit_season != season and not override:
                        plan['review'].append({'src':str(p), 'reason':f'文件季号 {explicit_season} 与选择的 {season} 不同'})
                        continue
                    try:
                        ep = choose_episode(bundle['episodes'], number, mode, ep_type)
                        if end is not None:
                            for n in range(number+1, end+1):
                                choose_episode(bundle['episodes'], n, mode, ep_type)
                    except ValueError as e:
                        plan['review'].append({'src':str(p), 'reason':str(e)}); continue
                    part = f'S{season:02}E{number:02}' + (f'-E{end:02}' if end is not None else '')
                    name = safe(meta.title(root_subject), 65) + ' ' + part + ' - ' + safe(ep.get('name_cn') or ep.get('name') or '', 55)
                    dst = owner / f'Season {season:02}' / (name + ' - ' + version_name + p.suffix.lower())
                move(p, dst, role); mapped += 1; video_destinations[key(p)] = (p, dst)
                if role == 'episode':
                    write(dst.with_suffix('.nfo'), meta.episode(s, root_subject, ep, season, number, end))
                elif role == 'extra':
                    extra = ET.Element('movie'); meta.add(extra, 'title', p.stem)
                    meta.add(extra, 'plot', '本地附加视频；不作为正片计入集数。')
                    meta.add(extra, 'lockdata', 'true'); write(dst.with_suffix('.nfo'), meta.xml(extra))
            if not mapped:
                continue
            poster = cover(root_bundle, owner)
            write(owner / ('movie.nfo' if kind == 'movie' else 'tvshow.nfo'), meta.xml(meta.base('movie' if kind == 'movie' else 'tvshow', root_bundle, poster)))
            if kind == 'show' and any(e['role']=='episode' and Path(e['dst']).parent == owner / f'Season {season:02}' for e in plan['moves']):
                season_dir = owner / f'Season {season:02}'
                existing_sid = nfo_id(season_dir / 'season.nfo')
                if existing_sid and existing_sid != sid:
                    raise ValueError('季目录已属于另一个 Bangumi 条目：' + str(season_dir))
                n = meta.base('season', bundle, cover(bundle, season_dir)); meta.add(n, 'seasonnumber', season)
                write(season_dir / 'season.nfo', meta.xml(n))
            # Prefix-exact subtitle/audio pairing only. Do not guess matching
            # episodes based on an unrelated numeric token or release group.
            all_files = list(walk_files(source_dir)) if source_dir not in self.roots else [
                normal(p) for p in lp(source_dir).iterdir() if p.is_file()]
            for p in all_files:
                if p.suffix.lower() not in SIDECAR:
                    continue
                candidates = [(v, d) for v, d in video_destinations.values() if p.parent == v.parent and
                              (p.stem.casefold() == v.stem.casefold() or p.stem.casefold().startswith(v.stem.casefold()+'.'))]
                if len(candidates) == 1:
                    v, d = candidates[0]; tail = p.stem[len(v.stem):]
                    move(p, d.with_name(d.stem + tail + p.suffix.lower()), 'companion')
                else:
                    plan['review'].append({'src':str(p), 'reason':'外挂字幕/音轨前缀未唯一匹配；保留文件（完整发布包整理时可归入资料）'})
            all_video_keys = {key(p) for p in all_files if p.suffix.lower() in VIDEO}
            full_package = all_video_keys == set(video_destinations) and source_dir not in self.roots and not group['managed']
            if full_package and self.config.get('collect_resources', True):
                resources = owner / '资料' / ('发布包-' + hashlib.sha256(key(source_dir).encode()).hexdigest()[:12])
                for p in all_files:
                    if key(p) in source_keys or key(p) in protected or p.suffix.lower() in VIDEO:
                        continue
                    if p.suffix.lower() == '.nfo' and replace_nfo:
                        deletion(p); continue
                    move(p, resources / p.relative_to(source_dir), 'resource')
                if any(e['role']=='resource' and Path(e['dst']).is_relative_to(resources) for e in plan['moves']):
                    write(resources.parent / '.ignore', b'')
                for p in all_files:
                    for parent in [p.parent, *p.parents]:
                        if not parent.is_relative_to(source_dir):
                            break
                        if parent != owner and not owner.is_relative_to(parent):
                            plan['cleanup'].append(str(guard(parent, self.roots)))
        plan['cleanup'] = sorted(set(plan['cleanup']), key=lambda p:len(Path(p).parts), reverse=True)
        plan['summary'] = dict(collections.Counter(e['role'] for e in plan['moves']))
        plan['summary'].update(writes=len(plan['writes']), old_nfo_deleted=len(plan['deletions']),
                               review=len(plan['review']), video_copies=0)
        atomic_json(run / 'plan.json', plan)
        (run / 'plan.sha256').write_text(hashlib.sha256((run / 'plan.json').read_bytes()).hexdigest(), encoding='ascii')
        return run, plan
