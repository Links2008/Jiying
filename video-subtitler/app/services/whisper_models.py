"""Whisper 模型发现、校验、下载和准备进度。"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import re
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

from ..core.errors import AppError, Codes
from ..core.paths import replace_with_retry

log = logging.getLogger(__name__)

WHISPER_FILES = ["config.json", "preprocessor_config.json", "model.bin",
                 "tokenizer.json", "vocabulary.*"]
_whisper_download_lock = threading.Lock()
_MODELSCOPE_WHISPER = ("tiny", "base", "small", "medium", "large-v3")


@contextmanager
def _preparation_lock(lock, check_cancel=None, on_wait=None):
    if check_cancel:
        check_cancel()
    while not lock.acquire(timeout=0.2):
        if check_cancel:
            check_cancel()
        if on_wait:
            on_wait()
    try:
        if check_cancel:
            check_cancel()
        yield
    finally:
        lock.release()


def whisper_progress_tqdm(callback, check_cancel=None):
    """共享模型字节进度；取消检查不得被普通 UI 回调容错吞掉。"""
    from tqdm import tqdm

    class _AggTqdm(tqdm):
        _bars: dict[int, tuple[int, int, bool]] = {}
        _lock = threading.Lock()
        _fixed_total = 0
        _next_bar = 0

        @classmethod
        def set_download_total(cls, total):
            with cls._lock:
                cls._bars.clear()
                cls._fixed_total = int(total)

        def __init__(self, *args, **kwargs):
            self._bytes = kwargs.get("unit") == "B" and not str(
                kwargs.get("desc", "")).startswith("Downloading")
            kwargs.update(disable=False, leave=False)
            super().__init__(*args, **kwargs)
            with _AggTqdm._lock:
                self._bar_id = _AggTqdm._next_bar
                _AggTqdm._next_bar += 1
                _AggTqdm._bars[self._bar_id] = (int(self.n), int(self.total or 0), self._bytes)
            if check_cancel:
                check_cancel()

        def display(self, *args, **kwargs):
            pass

        def update(self, n=1):
            if check_cancel:
                check_cancel()
            super().update(n)
            if not self._bytes:
                return
            with _AggTqdm._lock:
                _AggTqdm._bars[self._bar_id] = (int(self.n), int(self.total or 0), self._bytes)
                bars = [v for v in _AggTqdm._bars.values() if v[2]]
                done = sum(v for v, _, _ in bars)
                total = _AggTqdm._fixed_total or sum(x for _, x, _ in bars)
            if total > 0:
                try:
                    callback(min(done / total, 0.99))
                except AppError as exc:
                    if exc.code == Codes.TASK_CANCELLED:
                        raise
                except Exception:
                    pass

        def close(self):
            super().close()
            with _AggTqdm._lock:
                _AggTqdm._bars[self._bar_id] = (int(self.n), int(self.total or 0), self._bytes)

    return _AggTqdm


def whisper_model_complete(path: Path | str, model: str | None = None) -> bool:
    """缓存目录存在不代表下载完整；就绪还必须经过实际模型载入。"""
    path = Path(path)
    try:
        required = [path / name for name in ("model.bin", "config.json", "tokenizer.json")]
        vocabulary = list(path.glob("vocabulary.*"))
        if not vocabulary or any(not f.is_file() or f.stat().st_size == 0
                                 for f in required + vocabulary):
            return False
        for f in required[1:] + [v for v in vocabulary if v.suffix == ".json"]:
            json.loads(f.read_text(encoding="utf-8"))
        prep = path / "preprocessor_config.json"
        large_v3 = model in ("large-v3", "Systran/faster-whisper-large-v3")
        if large_v3 or prep.exists():
            settings = json.loads(prep.read_text(encoding="utf-8"))
            if not isinstance(settings, dict) or (large_v3 and settings.get("feature_size") != 128):
                return False
        return True
    except (OSError, ValueError, UnicodeError):
        return False


def local_whisper_model(model: str) -> Path | None:
    """只检查缓存文件，不提前导入 faster-whisper、CTranslate2 或音频运行库。"""
    try:
        from huggingface_hub import try_to_load_from_cache
        if Path(model).is_dir():
            path = Path(model)
        else:
            repo = model if '/' in model else {
                'large': 'Systran/faster-whisper-large-v3',
                'turbo': 'mobiuslabsgmbh/faster-whisper-large-v3-turbo',
                'large-v3-turbo': 'mobiuslabsgmbh/faster-whisper-large-v3-turbo',
                'distil-large-v3.5': 'distil-whisper/distil-large-v3.5-ct2',
            }.get(model, 'Systran/faster-' + (model.replace('distil-', 'distil-whisper-', 1)
                                           if model.startswith('distil-') else 'whisper-' + model))
            cached = try_to_load_from_cache(repo, 'config.json')
            if not isinstance(cached, str):
                raise FileNotFoundError('模型未缓存')
            path = Path(cached).parent
        if whisper_model_complete(path, model):
            return path
    except Exception:
        pass
    if model in _MODELSCOPE_WHISPER:
        path = _modelscope_model_path(model)
        try:
            sizes = json.loads((path / "download.json").read_text(encoding="utf-8"))
            if (isinstance(sizes, dict)
                    and {"model.bin", "config.json", "tokenizer.json"}.issubset(sizes)
                    and all("/" not in name and "\\" not in name
                            and (path / name).stat().st_size == size for name, size in sizes.items())
                    and whisper_model_complete(path, model)):
                return path
        except (OSError, ValueError, UnicodeError, TypeError):
            pass
    return None


def _whisper_proxy(config) -> str:
    from urllib.request import getproxies

    mode = config.get("download.proxy_mode", "system")
    proxy = ""
    if mode != "direct":
        if mode == "custom":
            proxy = str(config.get("download.custom_proxy", "") or "").strip()
        proxy = proxy or str(config.get("proxy", "") or "").strip()
        if not proxy:
            proxies = getproxies()
            proxy = proxies.get("https") or proxies.get("http") or ""
    return proxy


def _configure_whisper_http(config) -> None:
    import httpx
    import huggingface_hub
    from huggingface_hub.utils._http import hf_request_event_hook
    proxy = _whisper_proxy(config)
    huggingface_hub.set_client_factory(lambda: httpx.Client(
        proxy=proxy or None, trust_env=False, follow_redirects=True,
        timeout=httpx.Timeout(30.0, connect=10.0),
        event_hooks={"request": [hf_request_event_hook]}))


def _modelscope_model_path(model: str) -> Path:
    from huggingface_hub.constants import HF_HUB_CACHE
    return Path(HF_HUB_CACHE) / "jiying-modelscope" / model


def _model_file_hash(path: Path, check_cancel=None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            if check_cancel:
                check_cancel()
            digest.update(chunk)
    return digest.hexdigest()


def _download_modelscope_file(session, repo: str, path: Path, info: dict,
                              tqdm_class=None, check_cancel=None) -> None:
    if check_cancel:
        check_cancel()
    name, size, expected = info["Path"], info["Size"], info["Sha256"].lower()
    target = path / name
    part = path / f"{name}.{expected[:12]}.part"
    complete = target.is_file() and target.stat().st_size == size
    if complete and _model_file_hash(target, check_cancel) == expected:
        if tqdm_class:
            with tqdm_class(total=size, initial=size, unit="B", desc=f"ModelScope {name}") as bar:
                bar.update(0)
        return
    offset = part.stat().st_size if part.is_file() else 0
    if offset == size and _model_file_hash(part, check_cancel) == expected:
        replace_with_retry(part, target, before_retry=check_cancel)
        if tqdm_class:
            with tqdm_class(total=size, initial=size, unit="B", desc=f"ModelScope {name}") as bar:
                bar.update(0)
        return
    if offset >= size:
        offset = 0
    digest = hashlib.sha256()
    if offset:
        with part.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                if check_cancel:
                    check_cancel()
                digest.update(chunk)
    bar = tqdm_class(total=size, initial=offset, unit="B", desc=f"ModelScope {name}") if tqdm_class else None
    try:
        # 国内源可能拒绝 Range。只回退一次，收到有效的完整响应后才覆盖分片。
        for start in ([offset, 0] if offset else [0]):
            if check_cancel:
                check_cancel()
            headers = {"Range": f"bytes={start}-"} if start else {}
            with session.get(f"https://modelscope.cn/api/v1/models/{repo}/repo",
                             params={"Revision": info["Revision"], "FilePath": name},
                             headers=headers, stream=True, timeout=(10, 30)) as response:
                if start and response.status_code == 416:
                    continue
                response.raise_for_status()
                limit = size
                if response.status_code == 206:
                    span = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)",
                                        response.headers.get("Content-Range", ""), re.IGNORECASE)
                    if not span:
                        raise OSError("模型续传范围不匹配")
                    first, last, total = map(int, span.groups())
                    if first != start or not first <= last < size or total != size:
                        raise OSError("模型续传范围不匹配")
                    limit = last + 1
                elif response.status_code == 200:
                    start = 0
                else:
                    raise OSError(f"模型下载响应状态异常：{response.status_code}")
                if offset and not start:
                    digest = hashlib.sha256()
                    if bar:
                        bar.n = 0
                written = start
                # 等到首个有效数据块再打开文件，避免空响应截断已有分片。
                with ExitStack() as stack:
                    file = None
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if check_cancel:
                            check_cancel()
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > limit:
                            raise OSError("模型下载响应超出声明的文件范围")
                        if file is None:
                            file = stack.enter_context(part.open("ab" if start else "wb", buffering=0))
                        file.write(chunk)
                        digest.update(chunk)
                        if bar:
                            bar.update(len(chunk))
                if check_cancel:
                    check_cancel()
                if written == start:
                    raise OSError("模型下载响应没有数据")
                break
        if part.stat().st_size != size or digest.hexdigest() != expected:
            raise OSError(f"模型文件 {name} 大小或 SHA256 校验失败")
        replace_with_retry(part, target, before_retry=check_cancel)
    finally:
        if bar:
            bar.close()


def _download_modelscope(config, model: str, tqdm_class=None, on_status=None,
                         check_cancel=None) -> Path:
    """直接读取国内同名 CT2 仓库；保留分片、按仓库 SHA256 校验后原子提交。"""
    import requests
    repo = f"Systran/faster-whisper-{model}"
    proxy = _whisper_proxy(config)
    with requests.Session() as session:
        session.trust_env = False
        session.proxies = {"http": proxy, "https": proxy} if proxy else {}
        if check_cancel:
            check_cancel()
        with session.get(f"https://modelscope.cn/api/v1/models/{repo}/repo/files",
                         params={"Revision": "master", "Recursive": "true"},
                         timeout=(10, 30)) as response:
            response.raise_for_status()
            payload = response.json()
        files = [f for f in (payload.get("Data") or {}).get("Files", [])
                 if f.get("Type") == "blob" and "/" not in f.get("Path", "")
                 and "\\" not in f.get("Path", "")
                 and any(fnmatch.fnmatch(f.get("Path", ""), p) for p in WHISPER_FILES)]
        names = [f["Path"] for f in files]
        if (len(names) != len(set(names)) or not {"model.bin", "config.json", "tokenizer.json"}.issubset(names)
                or not any(n.startswith("vocabulary.") for n in names)):
            raise OSError("国内仓库缺少完整的 CTranslate2 模型文件")
        for info in files:
            sha = info.get("Sha256", "")
            if (not isinstance(info.get("Size"), int) or info["Size"] <= 0
                    or not info.get("Revision") or len(sha) != 64
                    or any(c not in "0123456789abcdef" for c in sha.lower())):
                raise OSError("国内仓库文件校验信息不完整")
        path = _modelscope_model_path(model)
        path.mkdir(parents=True, exist_ok=True)
        set_total = getattr(tqdm_class, "set_download_total", None)
        if callable(set_total):
            set_total(sum(f["Size"] for f in files))
        for info in files:
            if on_status:
                on_status(f"正在从国内 ModelScope 下载 Whisper {model} · {info['Path']}…")
            _download_modelscope_file(session, repo, path, info, tqdm_class, check_cancel)
        if not whisper_model_complete(path, model):
            raise OSError("国内模型文件不完整或 JSON 文件损坏")
        manifest = path / "download.json.part"
        manifest.write_text(json.dumps({f["Path"]: f["Size"] for f in files}), encoding="utf-8")
        replace_with_retry(manifest, path / "download.json", before_retry=check_cancel)
        return path


def download_whisper_model(config, model: str, tqdm_class=None, on_status=None,
                           check_cancel=None) -> Path:
    """国内直达优先，保留 Hub 镜像/官方的缓存续传回退，不修改进程环境。"""
    import huggingface_hub
    from faster_whisper import utils

    if check_cancel:
        check_cancel()
    cached = local_whisper_model(model)
    if cached is not None:
        return cached
    repo = utils._MODELS.get(model)
    if repo is None:
        if "/" not in model or Path(model).is_dir():
            raise AppError(Codes.CFG_INVALID, f"模型名称无效或本地模型不完整：{model}")
        repo = model
    endpoint = str(config.get("transcriber.hf_endpoint", "") or "").strip().rstrip("/")
    endpoints = list(dict.fromkeys([endpoint or "https://huggingface.co",
                                    "https://huggingface.co"]))
    last_error = None
    repair = False
    # ponytail: Hub 1.x 共享 HTTP 客户端，模型下载串行；需要并行时改为独立下载进程。
    wait_status = lambda: on_status("等待其他模型下载完成…") if on_status else None
    with _preparation_lock(_whisper_download_lock, check_cancel, wait_status):
        cached = local_whisper_model(model)
        if cached is not None:
            return cached
        if model in _MODELSCOPE_WHISPER:
            for attempt in range(2):
                if check_cancel:
                    check_cancel()
                if on_status:
                    on_status(f"正在连接国内 ModelScope 模型仓库（第 {attempt + 1}/2 次）…")
                try:
                    return _download_modelscope(config, model, tqdm_class, on_status, check_cancel)
                except Exception as e:
                    if isinstance(e, AppError) and e.code == Codes.TASK_CANCELLED:
                        raise
                    last_error = e
                    log.warning("Whisper %s 国内源下载尝试 %d 失败：%s", model, attempt + 1, e)
            if on_status:
                on_status("国内模型源暂不可用，正在切换备用仓库…")
        _configure_whisper_http(config)
        for endpoint in endpoints:
            for attempt in range(2):
                if check_cancel:
                    check_cancel()
                if on_status:
                    source = "官方" if endpoint == "https://huggingface.co" else "配置的镜像"
                    on_status(f"正在连接{source}模型仓库（第 {attempt + 1}/2 次）…")
                try:
                    options = dict(endpoint=endpoint, allow_patterns=list(WHISPER_FILES),
                        tqdm_class=tqdm_class, etag_timeout=10, token=False,
                        force_download=repair)
                    set_total = getattr(tqdm_class, "set_download_total", None)
                    if callable(set_total):
                        # Hub 的字节条 total 随文件线程启动增长；先取完整待下载量。
                        files = huggingface_hub.snapshot_download(repo, dry_run=True, **options)
                        if check_cancel:
                            check_cancel()
                        set_total(sum(f.file_size or 0 for f in files if f.will_download))
                    path = Path(huggingface_hub.snapshot_download(repo, **options))
                    if check_cancel:
                        check_cancel()
                    if not whisper_model_complete(path, model):
                        repair = True
                        raise OSError("模型文件不完整或 JSON 文件损坏")
                    return path
                except Exception as e:
                    if isinstance(e, AppError) and e.code == Codes.TASK_CANCELLED:
                        raise
                    last_error = e
                    log.warning("Whisper %s 下载尝试 %d 失败：%s", model, attempt + 1, e)
                    if attempt == 0:
                        for _ in range(10):
                            if check_cancel:
                                check_cancel()
                            time.sleep(0.1)
    raise AppError(Codes.ASR_FAILED, "Whisper 模型下载失败", detail=str(last_error),
                   retryable=True, suggestion="检查网络或代理后重试；未完成的下载会续传")
