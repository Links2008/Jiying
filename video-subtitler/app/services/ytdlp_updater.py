"""跟随 yt-dlp 官方 GitHub 发布；校验后在解析、下载全部空闲时换入新版。"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import re
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from ..core.paths import data_dir

log = logging.getLogger(__name__)
GITHUB_API = 'https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest'
RELEASE_ROOT = 'https://github.com/yt-dlp/yt-dlp/releases/download/'
CHECK_INTERVAL = 12 * 3600
HTTP_TIMEOUT = 10
_runtime_lock = threading.RLock()
_check_lock = threading.Lock()
_active_users = 0
_ADAPTERS = {'app.services.downloader.native_ranges', 'app.services.engines.douyin_public',
             'app.services.engines.xiaohongshu_public'}


def _vendor_dir() -> Path:
    return data_dir() / 'vendor' / 'ytdlp'


def _state_path() -> Path:
    return _vendor_dir() / 'state.json'


def _discard(folder: Path) -> None:
    root, target = _vendor_dir().resolve(), folder.resolve()
    if target == root or not target.is_relative_to(root):
        raise RuntimeError('更新临时目录越界，拒绝清理')
    if folder.exists():
        shutil.rmtree(folder)


def _parse_ver(version: str) -> tuple:
    return tuple(int(p) for p in str(version).split('.') if p.isdecimal())


def bundled_version() -> str:
    """查询版本不加载整套站点解析器，避免设置读取增加启动内存。"""
    loaded = sys.modules.get('yt_dlp.version')
    if loaded is not None:
        return str(loaded.__version__)
    try:
        return importlib.metadata.version('yt-dlp')
    except importlib.metadata.PackageNotFoundError:
        return '0'


def _package_version(folder: Path) -> str:
    try:
        text = (folder / 'version.py').read_text(encoding='utf-8')
        match = re.search(r"__version__\s*=\s*['\"]([\d.]+)['\"]", text)
        return match[1] if match else ''
    except (OSError, UnicodeError):
        return ''


def vendored_version() -> str:
    return _package_version(_vendor_dir() / 'yt_dlp')


def pending_version() -> str:
    return _package_version(_vendor_dir() / 'pending' / 'yt_dlp')


def _read_state() -> dict:
    try:
        value = json.loads(_state_path().read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def _write_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix='.tmp', delete=False) as file:
        temp = Path(file.name)
        file.write(json.dumps(state, ensure_ascii=False).encode('utf-8'))
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _latest_release() -> tuple[str, str, str]:
    import requests
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'Jiying-Engine-Updater'}
    response = requests.get(GITHUB_API, timeout=HTTP_TIMEOUT, headers=headers)
    response.raise_for_status()
    release = response.json()
    version = str(release.get('tag_name', ''))
    if release.get('draft') or release.get('prerelease') or not re.fullmatch(r'\d{4}\.\d{1,2}\.\d{1,2}', version):
        raise RuntimeError('官方发布版本无效，保留当前解析引擎')
    asset = next((a for a in release.get('assets', []) if a.get('name') == 'yt-dlp'), {})
    url = RELEASE_ROOT + version + '/yt-dlp'
    if asset.get('browser_download_url') != url:
        raise RuntimeError('未找到官方通用解析包，保留当前引擎')
    digest = str(asset.get('digest') or '').removeprefix('sha256:')
    if not re.fullmatch('[0-9a-fA-F]{64}', digest):
        checksum = requests.get(RELEASE_ROOT + version + '/SHA2-256SUMS', timeout=HTTP_TIMEOUT, headers=headers)
        checksum.raise_for_status()
        match = re.search(r'^([0-9a-fA-F]{64})\s+\*?yt-dlp\s*$', checksum.text, re.M)
        if not match:
            raise RuntimeError('缺少官方 SHA-256 校验信息，拒绝安装')
        digest = match[1]
    return version, url, digest.lower()


def _install_release(url: str, sha256: str, version: str) -> None:
    """只写入独立候选目录；下载或校验失败不修改已安装包。"""
    import requests
    if not re.fullmatch('[0-9a-fA-F]{64}', sha256 or ''):
        raise RuntimeError('缺少完整 SHA-256 校验，拒绝安装')
    if url != RELEASE_ROOT + version + '/yt-dlp':
        raise RuntimeError('更新下载地址不是所选官方发布')
    vendor = _vendor_dir()
    vendor.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='update-', dir=vendor) as directory:
        archive_path = Path(directory) / 'release.zip'
        digest = hashlib.sha256()
        downloaded = 0
        with requests.get(url, timeout=(10, 60), stream=True) as response:
            response.raise_for_status()
            with archive_path.open('wb') as file:
                for chunk in response.iter_content(1 << 16):
                    downloaded += len(chunk)
                    if downloaded > 32 * 1024 * 1024:
                        raise RuntimeError('解析更新包超过允许大小')
                    file.write(chunk)
                    digest.update(chunk)
        if digest.hexdigest() != sha256.lower():
            raise RuntimeError('解析更新包 SHA-256 不符，已丢弃')
        try:
            with zipfile.ZipFile(archive_path) as archive:
                entries = archive.infolist()
                if len(entries) > 10000 or sum(e.file_size for e in entries) > 80 * 1024 * 1024:
                    raise RuntimeError('解析更新包解压大小异常')
                for entry in entries:
                    parts = PurePosixPath(entry.filename).parts
                    if ('\\' in entry.filename or ':' in entry.filename or entry.filename.startswith('/')
                            or '..' in parts or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                        raise RuntimeError('解析更新包包含不安全路径')
                if archive.testzip():
                    raise RuntimeError('解析更新包损坏')
                archive.extractall(directory, members=[e.filename for e in entries
                                   if e.filename.startswith('yt_dlp/') and not e.is_dir()])
        except zipfile.BadZipFile as error:
            raise RuntimeError('解析更新包格式错误') from error
        source = Path(directory) / 'yt_dlp'
        if _parse_ver(_package_version(source)) != _parse_ver(version) or not (source / '__init__.py').is_file():
            raise RuntimeError('解析更新包版本不符')
        for file in source.rglob('*.py'):
            compile(file.read_bytes(), str(file), 'exec')
        with _runtime_lock:
            pending = vendor / 'pending'
            pending.mkdir(exist_ok=True)
            target, previous = pending / 'yt_dlp', pending / 'previous'
            _discard(previous)
            if target.exists():
                target.replace(previous)
            try:
                source.replace(target)
            except BaseException:
                if previous.exists():
                    previous.replace(target)
                raise
            finally:
                if target.exists():
                    _discard(previous)


def _evict_ytdlp() -> None:
    for name in list(sys.modules):
        if name == 'yt_dlp' or name.startswith('yt_dlp.') or name in _ADAPTERS:
            sys.modules.pop(name, None)


def _smoke_test(expected: str) -> bool:
    try:
        _evict_ytdlp()
        import yt_dlp
        if _parse_ver(yt_dlp.version.__version__) != _parse_ver(expected):
            return False
        yt_dlp.YoutubeDL({'quiet': True}).close()
        return True
    except Exception as error:
        log.warning('解析引擎自检失败：%s', error)
        return False


def _activate_pending() -> str:
    with _runtime_lock:
        version = pending_version()
        if _active_users or not version:
            return ''
        vendor = _vendor_dir()
        current, previous = vendor / 'yt_dlp', vendor / 'yt_dlp.old'
        modules = {name: module for name, module in sys.modules.copy().items()
                   if name == 'yt_dlp' or name.startswith('yt_dlp.') or name in _ADAPTERS}
        root = str(vendor)
        added = root not in sys.path
        if added:
            sys.path.insert(0, root)
        swapped = False
        try:
            if previous.exists():
                if not current.exists():
                    previous.replace(current)
                else:
                    _discard(previous)
            if current.exists():
                current.replace(previous)
            (vendor / 'pending' / 'yt_dlp').replace(current)
            swapped = True
            if not _smoke_test(version):
                raise RuntimeError('新版解析引擎自检失败，已恢复原版本')
        except Exception as error:
            if swapped:
                _discard(current)
            if previous.exists() and not current.exists():
                previous.replace(current)
            _evict_ytdlp()
            sys.modules.update(modules)
            if added:
                sys.path.remove(root)
            state = _read_state()
            state['error'] = str(error)
            _write_state(state)
            log.warning('%s', error)
            return ''
        _discard(previous)
        state = _read_state()
        state.update(installed=version, error='')
        _write_state(state)
        log.info('yt-dlp 官方发布 %s 已生效', version)
        return version


def activate_vendored() -> str:
    """启动时只检查本地已验证包，不访问网络。"""
    with _runtime_lock:
        if pending_version():
            _activate_pending()
        version = vendored_version()
        if not version or _parse_ver(version) <= _parse_ver(bundled_version()):
            return ''
        root = str(_vendor_dir())
        if root not in sys.path:
            sys.path.insert(0, root)
        if _smoke_test(version):
            return version
        sys.path.remove(root)
        _evict_ytdlp()
        return ''


@contextmanager
def runtime_session():
    """并行任务只计数，不互相等待；最后一个任务结束后再切换模块。"""
    global _active_users
    with _runtime_lock:
        if not _active_users:
            try:
                _activate_pending()
            except Exception as error:
                log.warning('更新暂未生效，继续使用当前引擎：%s', error)
        _active_users += 1
    try:
        yield
    finally:
        with _runtime_lock:
            _active_users -= 1
            if not _active_users:
                try:
                    _activate_pending()
                except Exception as error:
                    log.warning('更新切换失败，保留下载结果：%s', error)


def _check_once(force: bool = False) -> None:
    with _check_lock:
        state = _read_state()
        if not force and time.time() - float(state.get('last_check', 0)) < CHECK_INTERVAL:
            return
        latest, url, digest = _latest_release()
        state.update(last_check=time.time(), latest_seen=latest, error='')
        _write_state(state)
        current = pending_version() or vendored_version() or bundled_version()
        if _parse_ver(latest) > _parse_ver(current):
            _install_release(url, digest, latest)
        _activate_pending()


def auto_update(config=None) -> None:
    if config is not None and not bool(config.get('update.yt_dlp_auto', True)):
        return
    time.sleep(5)  # 窗口与本地 API 先就绪，再检查远端发布。
    while True:
        try:
            _check_once()
        except Exception as error:
            log.info('yt-dlp 更新未完成，继续使用原版本：%s', error)
        time.sleep(CHECK_INTERVAL)


def check_now() -> dict:
    try:
        _check_once(force=True)
    except Exception as error:
        return {'ok': False, 'error': str(error)}
    state = _read_state()
    return {'ok': not bool(state.get('error')), 'error': state.get('error', ''),
            'installed': state.get('installed', ''), 'pending': pending_version(),
            'latest_seen': state.get('latest_seen', '')}


def status() -> dict:
    with _runtime_lock:
        state = _read_state()
        return {'active': bundled_version(), 'vendored': vendored_version(), 'pending': pending_version(),
                'bundled': '', 'latest_seen': state.get('latest_seen', ''), 'last_check': state.get('last_check', 0),
                'source': 'https://github.com/yt-dlp/yt-dlp/releases'}
