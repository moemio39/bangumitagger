import hashlib
import json
import os
import re
from pathlib import Path
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from .common import atomic_json, read_json

API = 'https://api.bgm.tv'
UA = 'moemio39/bangumitagger/1.0.0 (https://github.com/moemio39/bangumitagger)'

class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        if new.scheme != 'https':
            raise ValueError('拒绝跳转到非 HTTPS 地址')
        if req.has_header('Authorization') and (old.scheme, old.netloc) != (new.scheme, new.netloc):
            raise ValueError('拒绝跨站发送 Authorization')
        return super().redirect_request(req, fp, code, msg, headers, newurl)

class Client:
    def __init__(self, cache, proxy, token=None):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.token = token or os.environ.get('BGM_TOKEN', '')
        if self.token and not re.fullmatch(r'[A-Za-z0-9._~-]{1,512}', self.token):
            raise ValueError('Token 格式无效；不要包含空格、换行或 Bearer 前缀')
        proxy = proxy.strip()
        if proxy:
            p = urllib.parse.urlsplit(proxy)
            if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
                raise ValueError('代理请填无用户名密码的 HTTP 地址，例如 http://127.0.0.1:7890')
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({'http':proxy, 'https':proxy} if proxy else {}), SafeRedirect())
        self.lock = threading.Lock()
        self.last = 0.0

    def request(self, path, payload=None, ttl=7*86400):
        if not path.startswith('/v0/'):
            raise ValueError('只允许公共 Bangumi v0 元数据端点')
        name = hashlib.sha256((path + json.dumps(payload, ensure_ascii=False)).encode()).hexdigest()
        cached = self.cache / (name + '.json')
        value = read_json(cached)
        if value and time.time() - value['cached_at'] < ttl:
            return value['data']
        for attempt in range(4):
            with self.lock:
                time.sleep(max(0, .4 - (time.monotonic() - self.last)))
                self.last = time.monotonic()
            headers = {'User-Agent':UA, 'Accept':'application/json'}
            if self.token:
                headers['Authorization'] = 'Bearer ' + self.token
            body = None
            if payload is not None:
                headers['Content-Type'] = 'application/json'
                body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            try:
                req = urllib.request.Request(API + path, data=body, headers=headers)
                with self.opener.open(req, timeout=25) as response:
                    raw = response.read(16*1024*1024 + 1)
                    if len(raw) > 16*1024*1024:
                        raise ValueError('API 响应异常过大')
                    value = json.loads(raw)
                atomic_json(cached, {'cached_at':time.time(), 'data':value})
                return value
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504):
                    raise RuntimeError(f'Bangumi HTTP {e.code}；检查条目权限或 Token') from None
            except (OSError, TimeoutError):
                pass
            time.sleep(min(8, 2**attempt))
        raise RuntimeError('Bangumi 请求失败；检查配置的代理（不会自动绕过代理）')

    def search(self, keyword):
        keyword = keyword.strip()
        if not keyword:
            raise ValueError('搜索关键词为空；请填写作品名')
        return self.request('/v0/search/subjects?limit=12',
                            {'keyword':keyword, 'sort':'match', 'filter':{'type':[2]}}, ttl=86400)['data']

    def subject(self, sid):
        result = self.request(f'/v0/subjects/{int(sid)}')
        if result.get('type') != 2:
            raise ValueError('选择的 Bangumi 条目不是动画，请重新选择')
        return result

    def episodes(self, sid):
        result = []
        while True:
            page = self.request(f'/v0/episodes?subject_id={int(sid)}&limit=200&offset={len(result)}')
            rows = page.get('data', [])
            result.extend(rows)
            if not rows or len(result) >= page['total']:
                return result
            if len(result) > 20000:
                raise ValueError('章节分页数据异常')

    def bundle(self, sid):
        sid = int(sid)
        return {'subject':self.subject(sid), 'episodes':self.episodes(sid),
                'persons':self.request(f'/v0/subjects/{sid}/persons'),
                'characters':self.request(f'/v0/subjects/{sid}/characters')}

    def poster(self, subject):
        url = subject.get('images', {}).get('large')
        if not url:
            return None
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != 'https' or not parts.hostname or not (
                parts.hostname == 'bgm.tv' or parts.hostname.endswith('.bgm.tv') or
                parts.hostname == 'bangumi.tv' or parts.hostname.endswith('.bangumi.tv')):
            raise ValueError('封面地址不在 Bangumi HTTPS 域名内')
        # Separate unauthenticated request: never attach the API token to images.
        req = urllib.request.Request(url, headers={'User-Agent':UA})
        with self.opener.open(req, timeout=25) as response:
            data = response.read(12*1024*1024 + 1)
        if len(data) > 12*1024*1024 or len(data) < 32:
            raise ValueError('封面大小异常')
        if data.startswith(b'\xff\xd8\xff'):
            extension = '.jpg'
        elif data.startswith(b'\x89PNG\r\n\x1a\n'):
            extension = '.png'
        elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
            extension = '.webp'
        else:
            raise ValueError('封面不是 JPEG/PNG/WebP，拒绝保存 HTML 或其他数据')
        return data, extension
