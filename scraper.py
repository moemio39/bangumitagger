"""Run without arguments for the GUI; --help lists automation CLI commands."""
import argparse
import json
from pathlib import Path
import sys
from bgmtool.api import Client
from bgmtool.common import atomic_json, load_config, read_json, save_config
from bgmtool.executor import Executor, unlock_stale
from bgmtool.planner import Planner, scan, suggest

HERE = Path(__file__).resolve().parent

def main(argv=None):
    parser = argparse.ArgumentParser(description='Bangumi → Jellyfin 原位刮削；默认 GUI / 预览，不复制视频')
    parser.add_argument('--data',type=Path,default=HERE/'data',help='缓存、绑定和执行记录目录（不存 Token）')
    subs = parser.add_subparsers(dest='command')
    common = argparse.ArgumentParser(add_help=False)
    for key in ('shows-root','movies-root','proxy'):
        common.add_argument('--'+key)
    gui = subs.add_parser('gui'); gui.add_argument('--smoke',action='store_true',help=argparse.SUPPRESS)
    scan_parser = subs.add_parser('scan',parents=[common]); scan_parser.add_argument('--output',type=Path)
    search = subs.add_parser('search',parents=[common]); search.add_argument('keyword')
    plan = subs.add_parser('plan',parents=[common]); plan.add_argument('--selections',type=Path,required=True)
    plan.add_argument('--replace-nfo',action='store_true',help='按预览删除旧 NFO，不备份')
    plan.add_argument('--no-resources',action='store_true')
    for command in ('apply','resume','rollback','verify'):
        sub = subs.add_parser(command,parents=[common]); sub.add_argument('--run',type=Path)
        if command != 'verify':
            sub.add_argument('--yes',action='store_true',help='确认当前运行记录中的媒体修改')
            sub.add_argument('--pause-jellyfin',action='store_true',help='临时停止并恢复服务原状态，需管理员')
    subs.add_parser('unlock')
    args = parser.parse_args(argv); data = args.data.absolute(); data.mkdir(parents=True,exist_ok=True)
    if args.command in (None,'gui'):
        from bgmtool.gui import launch
        launch(data,getattr(args,'smoke',False)); return
    config = load_config(data)
    for key in ('shows_root','movies_root','proxy'):
        if getattr(args,key,None) is not None: config[key] = getattr(args,key)
    if args.command=='unlock': unlock_stale(data); print('失效执行锁已清理'); return
    if args.command=='scan':
        result = scan(config); destination = args.output or data/'scan.json'
        atomic_json(destination,result)
        print(json.dumps(result['stats'],ensure_ascii=False)); print('待处理组：',len(result['groups'])); print(destination)
    elif args.command=='search':
        rows = suggest(args.keyword,Client(data/'cache',config['proxy']).search(args.keyword))
        for row in rows: print(row['id'],row.get('name_cn') or row.get('name'),row.get('date'),row.get('platform'),f"{row['match_score']:.0%}")
    elif args.command=='plan':
        if args.no_resources: config['collect_resources'] = False
        result = scan(config)
        run, planned = Planner(config,data,Client(data/'cache',config['proxy'])).build(
            result['groups'],read_json(args.selections),args.replace_nfo)
        save_config(data,config); atomic_json(data/'last-run.json',{'run':str(run)})
        print('预览完成；媒体未改动。',json.dumps(planned['summary'],ensure_ascii=False)); print(run)
    else:
        run = args.run or read_json(data/'last-run.json',{}).get('run')
        if not run: parser.error('请指定 --run，或先生成预览')
        executor = Executor(config,data,run)
        if args.command!='verify' and not args.yes:
            parser.error('修改媒体需要 --yes；请先检查 plan.json，旧 NFO 删除不会备份')
        if args.command=='verify': print(json.dumps(executor.verify(),ensure_ascii=False,indent=2))
        elif args.command=='rollback': executor.rollback(args.pause_jellyfin)
        else: executor.apply(resume=args.command=='resume',pause=args.pause_jellyfin)

if __name__=='__main__':
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'): stream.reconfigure(encoding='utf-8',errors='replace')
    if sys.version_info < (3,10):
        raise SystemExit('需要 Python 3.10 或以上（含 tkinter）')
    try: main()
    except Exception as e:
        print('停止：'+str(e),file=sys.stderr); sys.exit(1)
