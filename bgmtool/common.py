import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid
import xml.etree.ElementTree as ET

VIDEO = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.m2ts', '.ts', '.webm'}
SIDECAR = {'.ass', '.ssa', '.srt', '.sup', '.sub', '.idx', '.vtt', '.mka', '.aac', '.ac3', '.dts'}
EXTRA_DIRS = {'extras', 'other', 'trailers', 'samples', 'interviews', 'featurettes',
              'behind the scenes', 'deleted scenes', 'clips', 'shorts', 'backdrops', 'theme-music'}
DEFAULTS = {'shows_root':'', 'movies_root':'', 'proxy':'',
            'stable_seconds':120, 'collect_resources':True}

def lp(p):
    s = str(Path(p).absolute())
    return Path('\\\\?\\' + s) if os.name == 'nt' and not s.startswith('\\\\?\\') else Path(s)

def normal(p):
    s = str(p)
    if s.startswith('\\\\?\\'):
        s = s[4:]
    return Path(s)

def key(p):
    return str(normal(p).absolute()).casefold()

def reparse(p):
    s = lp(p).lstat()
    return stat.S_ISLNK(s.st_mode) or bool(getattr(s, 'st_file_attributes', 0) & 0x400)

def guard(p, roots):
    p = Path(os.path.abspath(normal(p)))
    root = next((r for r in roots if p.is_relative_to(r)), None)
    if root is None or p == root:
        raise ValueError('路径超出媒体根目录或指向根目录本身：' + str(p))
    for part in [p, *p.parents]:
        if lp(part).exists() and reparse(part):
            raise ValueError('拒绝操作符号链接、联接或重解析点：' + str(part))
        if part == root:
            break
    return p

def roots_from(config):
    values = [config[n] for n in ('shows_root', 'movies_root')]
    if any(not isinstance(v, str) or not v.strip() for v in values):
        raise ValueError('请先设置剧集和电影目录；CLI 可用 --shows-root 与 --movies-root')
    roots = [Path(os.path.abspath(v.strip())) for v in values]
    if any(not lp(r).is_dir() or reparse(r) for r in roots):
        raise ValueError('两个媒体根目录必须已存在且不是联接/符号链接')
    if roots[0].is_relative_to(roots[1]) or roots[1].is_relative_to(roots[0]):
        raise ValueError('剧集和电影根目录不能相同或互相嵌套')
    if lp(roots[0]).stat().st_dev != lp(roots[1]).stat().st_dev:
        raise ValueError('两个媒体根目录须在同一卷；工具不执行跨卷复制')
    return roots

def safe(s, limit=80):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', ' ', str(s))
    s = re.sub(r'\s+', ' ', s).strip(' .')[:limit].rstrip(' .') or 'Untitled'
    if re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', s):
        s = '_' + s
    return s

def digest(p):
    h = hashlib.sha256()
    with lp(p).open('rb') as f:
        for b in iter(lambda:f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()

def snapshot(p):
    s = lp(p).stat()
    return {'size':s.st_size, 'inode':s.st_ino, 'mtime_ns':s.st_mtime_ns, 'device':s.st_dev}

def unchanged(p, e):
    if snapshot(p) != {k:e[k] for k in ('size', 'inode', 'mtime_ns', 'device')}:
        raise ValueError('文件身份、大小或修改时间已变化：' + str(p))

def atomic_json(p, value):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    temp = p.with_name(p.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, p)

def read_json(p, default=None):
    p = Path(p)
    return json.loads(p.read_text(encoding='utf-8-sig')) if p.exists() else default

def load_config(data):
    config = dict(DEFAULTS)
    config.update(read_json(Path(data) / 'config.json', {}))
    return {k:config[k] for k in DEFAULTS}

def save_config(data, config):
    # No token, cookies, credentials or arbitrary keys are persisted.
    atomic_json(Path(data) / 'config.json', {k:config[k] for k in DEFAULTS})

def nfo_id(p):
    try:
        root = ET.parse(lp(p)).getroot()
        value = root.findtext('bangumiid') or root.findtext("uniqueid[@type='bangumi']")
        return int(value) if value else None
    except (OSError, ValueError, ET.ParseError):
        return None

def walk_files(root):
    """No symlink/junction traversal; honor Jellyfin's .ignore boundary."""
    stack = [Path(root)]
    while stack:
        base = stack.pop()
        if lp(base / '.ignore').exists():
            continue
        for p in lp(base).iterdir():
            p = normal(p)
            if reparse(p):
                continue
            if lp(p).is_dir():
                stack.append(p)
            elif lp(p).is_file():
                yield p

def query_name(name):
    bracket_titles = [text for text in re.findall(r'\[([^\]]+)\]',name)
                      if re.search(r'[\u3040-\u30ff\u4e00-\u9fff]',text) and
                      not re.search(r'字幕|组|組|双语|雙語|国语|國語|简体|簡體|繁体|繁體|特别篇|特別篇|新番',text)]
    name = re.sub(r'(?i)\[bangumiid-\d+\]', '', name)
    name = re.sub(r'\[[^\]]*\]', '', name)
    name = re.sub(r'(?i)\((?:19|20)\d{2}\)', '', name)
    name = re.sub(r'(?i)\b(?:BDRip|DVDRip|BluRay|WEB[- ]?DL|HEVC|AVC|x26[45]|FLAC|AAC|Ma10p?|Hi10p?|\d{3,4}p)\b.*', '', name)
    result = re.sub(r'[._]+', ' ', name).strip(' -_')
    return result if result else (bracket_titles[0] if bracket_titles else '')

def episode_number(name):
    """Return (number, end, explicit season). Refuse uncertain bare digits."""
    patterns = [
        r'(?i)S(?P<season>\d+)E(?P<n>\d+)(?:[-~](?:E)?(?P<end>\d+))?',
        r'第\s*(?P<n>\d+)(?:[-~至](?P<end>\d+))?\s*[話话集]',
        r'(?i)\bEP?\s*(?P<n>\d+)(?:[-~](?P<end>\d+))?\b',
        r'\[(?P<n>\d{1,3})(?:[-~](?P<end>\d{1,3}))?(?:v\d+)?(?:\]|\s+END\])',
        r'(?:\s-\s|\s)(?P<n>\d{1,3})(?:[-~](?P<end>\d{1,3}))?(?:v\d+)?(?:\s+END)?\s*(?:\[|\(|$)',
        r'\.(?P<n>\d{2,3})\.',
    ]
    for pattern in patterns:
        m = re.search(pattern, Path(name).stem)
        if m:
            d = m.groupdict()
            return int(d['n']), int(d['end']) if d.get('end') else None, int(d['season']) if d.get('season') else None
    return None, None, None

def is_extra(p, group):
    parts = p.relative_to(group).parts[:-1]
    if any(s.casefold() in EXTRA_DIRS for s in parts):
        return True
    return bool(re.search(r'(?i)(?:^|[\[ _-])(?:NCOP|NCED|PV|CM|BDMenu|Menu|Trailer|Creditless)(?:\d*|v\d+)(?:$|[\] _.-])', p.stem))
