"""User-confirmed website sessions; each platform has its own Netscape file."""
from __future__ import annotations

import os
import re
import tempfile
from http.cookiejar import Cookie, MozillaCookieJar
from urllib.parse import urlsplit

from ..core.errors import AppError, Codes
from ..core.paths import data_dir

DOMAINS = {'douyin': ('douyin.com', 'iesdouyin.com'),
           'xiaohongshu': ('xiaohongshu.com', 'xhslink.com')}


def allows_domain(platform, domain):
    host = domain.lstrip('.').lower() if isinstance(domain, str) else ''
    return any(host == base or host.endswith('.' + base) for base in DOMAINS.get(platform, ()))


def platform_for_url(url):
    host = urlsplit(url).hostname or ''
    return next((key for key in DOMAINS if allows_domain(key, host)), None)


def session_path(platform):
    return data_dir() / 'platform-sessions' / (platform + '.txt')


def save_session(platform, cookies):
    invalid = AppError(Codes.AUTH_NEED_COOKIES, '官网会话无效，请重新打开连接窗口')
    if not isinstance(platform, str) or platform not in DOMAINS or not isinstance(cookies, list):
        raise invalid
    if not 0 < len(cookies) <= 300:
        raise invalid
    jar = MozillaCookieJar()
    for item in cookies:
        if not isinstance(item, dict):
            raise invalid
        domain, name, value, path = (item.get(k, '') for k in ('domain', 'name', 'value', 'path'))
        expires = item.get('expires', 0)
        if (not all(isinstance(v, str) and len(v) <= 16384 and not re.search(r'[\x00-\x1f\x7f]', v)
                    for v in (domain, name, value, path))
                or not re.fullmatch(r'\.?[a-zA-Z0-9.-]+', domain) or not allows_domain(platform, domain)
                or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-zA-Z-]+", name) or not path.startswith('/')
                or type(expires) is not int or not 0 <= expires <= 253402300799):
            raise invalid
        jar.set_cookie(Cookie(version=0, name=name, value=value, port=None, port_specified=False,
            domain=domain, domain_specified=domain.startswith('.'), domain_initial_dot=domain.startswith('.'),
            path=path, path_specified=True, secure=item.get('secure') is True, expires=expires or None,
            discard=expires == 0, comment=None, comment_url=None,
            rest={'HTTPOnly': None} if item.get('http_only') is True else {}))
    target, temporary = session_path(platform), None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, suffix='.tmp', delete=False) as file:
            temporary = file.name
        jar.save(temporary, ignore_discard=True, ignore_expires=True)
        os.replace(temporary, target)
    except OSError:
        raise AppError(Codes.DISK_FAILED, '官网会话保存失败，请重试', retryable=True) from None
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def load_session(ydl, url):
    platform = platform_for_url(url)
    if not platform:
        return False
    target = session_path(platform)
    if not target.is_file():
        return False
    jar = MozillaCookieJar(str(target))
    try:
        jar.load(ignore_discard=True)
    except OSError:
        return False
    loaded = False
    for cookie in jar:
        if allows_domain(platform, cookie.domain) and not cookie.is_expired():
            ydl.cookiejar.set_cookie(cookie)
            loaded = True
    if loaded:
        ydl.params['_jiying_platform_session'] = True
    return loaded
