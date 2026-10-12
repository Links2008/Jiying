"""语音识别 Provider：默认 faster-whisper（本地离线），接口可扩展云 API。"""
from __future__ import annotations

import json
import fnmatch
import hashlib
import logging
import threading
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..core.errors import AppError, Codes, dep_missing
from ..core.paths import bin_dir, data_dir

log = logging.getLogger(__name__)

WHISPER_FILES = ["config.json", "preprocessor_config.json", "model.bin",
                 "tokenizer.json", "vocabulary.*"]
_whisper_download_lock = threading.Lock()
_cuda_dll_handles: dict[str, object] = {}
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
    """只读本地缓存，不连接模型仓库。"""
    try:
        from faster_whisper.utils import download_model
        path = Path(model) if Path(model).is_dir() else Path(
            download_model(model, local_files_only=True))
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
        part.replace(target)
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
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    bar = tqdm_class(total=size, initial=offset, unit="B", desc=f"ModelScope {name}") if tqdm_class else None
    try:
        with session.get(f"https://modelscope.cn/api/v1/models/{repo}/repo",
                         params={"Revision": info["Revision"], "FilePath": name},
                         headers=headers, stream=True, timeout=(10, 30)) as response:
            response.raise_for_status()
            if offset and response.status_code == 200:
                offset, digest = 0, hashlib.sha256()
                if bar:
                    bar.n = 0
            elif response.status_code == 206 and not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                raise OSError("模型续传范围不匹配")
            with part.open("ab" if offset else "wb", buffering=0) as file:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if check_cancel:
                        check_cancel()
                    if chunk:
                        file.write(chunk)
                        digest.update(chunk)
                        if bar:
                            bar.update(len(chunk))
            if check_cancel:
                check_cancel()
        if part.stat().st_size != size or digest.hexdigest() != expected:
            raise OSError(f"模型文件 {name} 大小或 SHA256 校验失败")
        part.replace(target)
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
        manifest.replace(path / "download.json")
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


@dataclass
class Segment:
    """一条带时间戳的字幕句。"""

    start: float
    end: float
    text: str


@dataclass
class Transcript:
    """一次转写的完整结果（含检测到的源语言）。"""

    language: str
    segments: list[Segment] = field(default_factory=list)

    def normalized(self) -> "Transcript":
        """语言归一化：中文输出统一为简体（Whisper 常输出繁体，如「天氣/識別」）。

        OpenCC 缺失时静默跳过（转换是增强项，不阻断主流程）。
        """
        if not self.language.startswith("zh"):
            return self
        try:
            from opencc import OpenCC
        except ImportError:
            log.info("检测到中文输出，但未安装 opencc，跳过繁简归一化"
                     "（pip install opencc-python-reimplemented）")
            return self
        cc = OpenCC("t2s")
        return self.__class__(
            language=self.language,
            segments=[Segment(s.start, s.end, cc.convert(s.text))
                      for s in self.segments])

    def save_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1),
                        encoding="utf-8")

    @classmethod
    def load_json(cls, path: Path) -> "Transcript":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(language=data["language"],
                   segments=[Segment(**s) for s in data["segments"]])


class TranscriberProvider(ABC):
    name: str = "base"

    @abstractmethod
    def transcribe(self, audio_path: Path, source_lang: str | None = None,
                   on_progress=None) -> Transcript:
        """转写音频 → Transcript。on_progress(fraction 0~1, note)。"""


class FasterWhisperTranscriber(TranscriberProvider):
    """faster-whisper：CTranslate2 加速的 Whisper，99 语种、自动检测语言。"""

    name = "faster-whisper"

    def __init__(self, config):
        self._cfg = config
        self._model = None       # 模型懒加载（大模型加载慢，按任务首用时初始化）
        self._loaded_key = None  # 当前已加载模型的 (尺寸, 设备, 精度)，配置变更时自动重载
        # 模型实例全局共享且会热重载（CUDA 降级 CPU / 切换模型），并发推理 +
        # 并发重载会在 CTranslate2 内部队列上死锁（实测两个任务线程同时卡死
        # 在 encode），因此加载与推理全程串行 —— 与架构 v3 "Whisper 串行"一致
        self._lock = threading.Lock()
        # CUDA 运行库缺失（cublas/cudnn）时首次调用可能快速报错，但同进程内
        # 再次初始化 CUDA 会把驱动调用永久挂死（实测 encode 无输出无异常），
        # 因此失败一次后本进程内不再尝试 CUDA
        self._cuda_broken = False
        # 空闲自动卸载：模型常驻要占数百 MB 内存，对常驻桌面应用是实打实的
        # 负担；空闲 N 分钟后释放，下次任务重新加载（CPU 加载实测仅数秒）
        self._last_used = time.monotonic()
        self._unload_after = float(self._cfg.get("transcriber.idle_unload_minutes", 10) or 0) * 60
        if self._unload_after > 0:
            t = threading.Thread(target=self._idle_unloader, daemon=True)
            t.start()

    def _idle_unloader(self) -> None:
        while True:
            time.sleep(60)
            with self._lock:
                if (self._model is not None
                        and time.monotonic() - self._last_used > self._unload_after):
                    log.info("模型空闲 %.0f 分钟，自动卸载释放内存",
                             (time.monotonic() - self._last_used) / 60)
                    self._model = None
                    self._loaded_key = None

    def _load_model(self, device_override: str | None = None,
                    model_override: str | None = None, on_progress=None):
        size = model_override or self._cfg.get("transcriber.model", "small")
        preparing_note = [f"正在检查 Whisper {size} 模型…"]
        def report(note):
            preparing_note[0] = note
            if on_progress:
                on_progress(0.0, note)
        def check_cancel():
            if on_progress:
                on_progress(0.0, preparing_note[0])
        check_cancel()
        device = "cpu" if self._cuda_broken else (
            device_override or self._cfg.get("transcriber.device", "auto"))
        compute = self._cfg.get("transcriber.compute_type", "auto")
        if self._cuda_broken or device_override == "cpu":
            compute = "int8"  # CUDA 精度不能沿用到 CPU 降级路径
        if device == "auto":
            device = "cuda" if _has_cuda() else "cpu"
        if compute == "auto":
            compute = "int8_float16" if device == "cuda" else "int8"
        # 配置变更（如 UI 里切换模型）→ 键变化 → 自动重载
        key = (size, device, compute)
        if self._model is not None and self._loaded_key == key:
            return self._model
        try:
            from faster_whisper import WhisperModel  # 延迟导入 + 友好缺失提示
        except ImportError as e:
            raise dep_missing("faster-whisper", "pip install faster-whisper",
                              "语音识别") from e
        if device == "cuda":
            try:
                _add_nvidia_dll_dirs(check_cancel, report)
            except (RuntimeError, ValueError, OSError) as exc:
                log.warning("GPU 组件不可用，使用 CPU：%s", exc)
                report("GPU 组件未就绪，继续使用 CPU 识别；可在设置中下载组件")
                device, compute = "cpu", "int8"
                key = (size, device, compute)
                if self._model is not None and self._loaded_key == key:
                    return self._model
        progress = whisper_progress_tqdm(
            lambda f: report(f"正在下载 Whisper {size} 模型 {f:.0%}…"),
            check_cancel=check_cancel) if on_progress else None
        path = download_whisper_model(self._cfg, size, tqdm_class=progress,
                                      on_status=report, check_cancel=check_cancel)
        report(f"正在载入 Whisper {size} 模型…")
        log.info("加载 Whisper 模型 %s（device=%s, compute=%s）", size, device, compute)
        try:
            self._model = WhisperModel(str(path), device=device, compute_type=compute)
        except Exception as e:
            if device != "cuda" or not any(k in str(e).lower()
                                           for k in ("cuda", "cublas", "cudnn", "compute type")):
                raise
            log.warning("CUDA 模型载入失败，改用 CPU/int8：%s", e)
            self._cuda_broken = True
            self._model = None
            self._loaded_key = None
            self._model = WhisperModel(str(path), device="cpu", compute_type="int8")
            key = (size, "cpu", "int8")
        self._loaded_key = key
        report(f"Whisper {size} 模型已就绪，准备识别…")
        return self._model

    def preload(self, model: str) -> str:
        """立即把指定模型载入内存（软件内下载完成后调用，首个任务无需再等加载）。

        与懒加载共用 _load_model：同 (尺寸, 设备, 精度) 命中即安全 no-op，换模型则
        替换实例。全程持锁，避免与转写并发触发 CTranslate2 初始化死锁。
        """
        with self._lock:
            self._last_used = time.monotonic()
            # CUDA 运行库曾经失败过就不再重试（同进程二次初始化会永久挂死）
            self._load_model("cpu" if self._cuda_broken else None,
                             model_override=model)
        log.info("Whisper 模型 %s 已载入，后续任务可直接使用", model)
        return model

    def transcribe(self, audio_path: Path, source_lang: str | None = None,
                   on_progress=None) -> Transcript:
        # GPU 侧常见问题：有驱动但缺 cuBLAS/cuDNN 运行库 → 自动降级 CPU 重试。
        # 全程持锁：模型加载、CUDA 降级与推理不允许与其他任务交错
        waiting = lambda: on_progress(0.0, "等待其他语音识别或模型载入完成…") if on_progress else None
        with _preparation_lock(self._lock, waiting):
            self._last_used = time.monotonic()
            override = "cpu" if self._cuda_broken else None
            try:
                return self._transcribe_inner(audio_path, source_lang, on_progress,
                                              device_override=override)
            except AppError as e:
                detail = (f"{e.detail}{e.message}").lower()
                if not any(k in detail for k in ("cublas", "cudnn", "cuda")):
                    raise
                log.warning("CUDA 运行库不可用，自动降级 CPU 重试：%s", e)
                self._cuda_broken = True
                self._model = None
                self._loaded_key = None
                return self._transcribe_inner(audio_path, source_lang, on_progress,
                                              device_override="cpu")

    def _transcribe_inner(self, audio_path: Path, source_lang: str | None,
                          on_progress, device_override: str | None) -> Transcript:
        try:
            model = self._load_model(device_override, on_progress=on_progress)
            out, info = self._run_model(model, audio_path, source_lang,
                                        on_progress, vad=True)
            if not out:
                # VAD 全删不一定真是没人声：低音量/配乐盖人声常被误杀
                #（实测 Big Buck Bunny 10 分钟语音被 VAD 全过滤导致烧录失败）。
                # 关 VAD 整段重试一次，仍无结果才判定为无语音内容。
                log.warning("VAD 过滤了全部音频（时长 %.1fs），关闭 VAD 重试",
                            float(info.duration or 0))
                out, info = self._run_model(model, audio_path, source_lang,
                                            on_progress, vad=False)
        except AppError:
            raise
        except Exception as e:
            raise AppError(Codes.ASR_FAILED, f"语音识别失败：{e}",
                           detail=str(e), retryable=True) from e
        if not out:  # 两种模式都无输出 = 确实没检测到人声，明确告知而非产出空字幕
            raise AppError(
                Codes.ASR_FAILED, "未在音频中检测到语音内容",
                detail="整段音频（时长 %.1fs）未识别出任何语句" % (info.duration or 0),
                suggestion="确认视频包含清晰人声；纯音乐/纯静音无法生成字幕")
        if on_progress:
            on_progress(1.0, "转写完成")
        log.info("转写完成：%d 句，语言=%s", len(out), info.language)
        transcript = Transcript(language=str(info.language or "unknown"), segments=out)
        return transcript.normalized()

    def _run_model(self, model, audio_path: Path, source_lang: str | None,
                   on_progress, vad: bool) -> tuple[list[Segment], object]:
        """跑一遍 whisper 生成器；vad=False 用于 VAD 误杀全音频时的兜底。"""
        # beam_size 默认 5：贪心解码（1）CPU 上快约 2 倍，但实测同一段音频
        # 多次转写结果不稳定（断句 13~15 句、数字「二零二六/2026」来回漂移），
        # 且更易出现同音字错（逝世→试试）。改 5 换稳定与准确，追求速度可在
        # config 设 transcriber.beam_size: 1
        beam = int(self._cfg.get("transcriber.beam_size", 5) or 5)
        segments_iter, info = model.transcribe(
            str(audio_path), language=source_lang or None,
            vad_filter=vad,           # 过滤静音段：长视频更快更准
            beam_size=max(1, beam),
            # 词级时间戳：Whisper 句级时间戳天然首尾相接（上一句 end=下一句
            # start，句间静音被吞进上一句），字幕会一直挂到下一句顶掉才换。
            # 开启后句子的 start/end 取首/末词的真实时刻，停顿期间字幕正常
            # 消失。代价：转写稍慢（CPU 上约一~三成），换时间轴准确。
            word_timestamps=True,
        )
        duration = float(info.duration or 0)
        out: list[Segment] = []
        last_report = time.monotonic()
        for seg in segments_iter:  # 生成器惰性执行，边转边收
            words = getattr(seg, "words", None)
            if words and words[-1].end > words[0].start:
                # 词级时间戳可用：取首/末词真实时刻，句末静音不拖尾
                start, end = float(words[0].start), float(words[-1].end)
            else:
                start, end = float(seg.start), float(seg.end)
            out.append(Segment(start=start, end=end, text=seg.text.strip()))
            if on_progress and duration and time.monotonic() - last_report > 1.0:
                on_progress(min(end / duration, 1.0), f"转写至 {end:.0f}s")
                last_report = time.monotonic()
        return out, info


# 云端识别厂商预设：选定厂商时自动带出接口地址与默认模型。
# 前端会同步回填，此处兜底 —— config.yaml 手改或 base_url 缺失时仍可用。
ASR_VENDORS: dict[str, dict] = {
    # deepseek 没有语音识别接口，不属于识别厂商（其 OpenAI 兼容接口仅用于翻译）
    "openai": {"base_url": "https://api.openai.com/v1", "model": "whisper-1"},
    "siliconflow": {"base_url": "https://api.siliconflow.cn/v1",
                    "model": "FunAudioLLM/SenseVoiceSmall"},
    "zhipu": {"base_url": "https://open.bigmodel.cn/api/paas/v4",
              "model": "glm-asr"},
}


class CloudAsrTranscriber(TranscriberProvider):
    """云端语音识别：任意 OpenAI 兼容的 /audio/transcriptions 接口。

    与本地 faster-whisper 二选一（transcriber.provider: cloud）。厂商（vendor）
    决定默认接口地址与模型，也可用 base_url 直接指定自建/第三方服务。返回
    verbose_json 时按 segments 还原时间轴；仅有整段文本时退化为单条字幕。
    """

    name = "cloud"

    def __init__(self, config):
        self._cfg = config
        self._cloud = config.get("transcriber.cloud", {}) or {}

    def _base_url(self) -> str:
        vendor = str(self._cloud.get("vendor", "") or "").strip().lower()
        preset = ASR_VENDORS.get(vendor, {})
        base = (str(self._cloud.get("base_url", "") or "").strip()
                or preset.get("base_url", ""))
        return base.rstrip("/")

    def transcribe(self, audio_path: Path, source_lang: str | None = None,
                   on_progress=None) -> Transcript:
        import requests
        base = self._base_url()
        if not base:
            raise AppError(Codes.CFG_INVALID, "云端识别未配置接口地址",
                           suggestion="在「识别与翻译」里选择厂商，或填写自定义 URL")
        vendor = str(self._cloud.get("vendor", "") or "").strip().lower()
        model = (str(self._cloud.get("model", "") or "").strip()
                 or ASR_VENDORS.get(vendor, {}).get("model", ""))
        if not model:
            raise AppError(Codes.CFG_INVALID, "云端识别未配置模型名",
                           suggestion="填写该厂商的识别模型，如 whisper-1")
        if not audio_path.exists():
            raise AppError(Codes.ASR_FAILED, f"音频文件不存在：{audio_path}")
        api_key = str(self._cloud.get("api_key", "") or "").strip()
        lang = (source_lang or str(self._cloud.get("language", "") or "")).strip()
        if on_progress:
            on_progress(0.05, "上传音频至云端识别…")
        data = {"model": model, "response_format": "verbose_json"}
        if lang:
            data["language"] = lang
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            with open(audio_path, "rb") as fh:
                resp = requests.post(
                    base + "/audio/transcriptions", headers=headers, data=data,
                    files={"file": (audio_path.name, fh, "audio/wav")},
                    timeout=float(self._cloud.get("timeout", 300) or 300))
        except requests.RequestException as e:
            raise AppError(Codes.ASR_FAILED, f"云端识别请求失败：{e}",
                           detail=str(e), retryable=True) from e
        if resp.status_code >= 400:
            raise AppError(Codes.ASR_FAILED,
                           f"云端识别返回错误（HTTP {resp.status_code}）",
                           detail=(resp.text or "")[:500],
                           suggestion="核对接口地址、API Key、模型名与厂商是否匹配")
        try:
            payload = resp.json()
        except ValueError as e:
            raise AppError(Codes.ASR_FAILED, "云端识别返回了非 JSON 结果",
                           detail=(resp.text or "")[:500]) from e
        segments, language = self._parse(payload, audio_path)
        if not segments:
            raise AppError(Codes.ASR_FAILED, "云端识别未返回任何语句",
                           detail=json.dumps(payload, ensure_ascii=False)[:400],
                           suggestion="确认该模型支持音频转写，且音频包含清晰人声")
        if on_progress:
            on_progress(1.0, "转写完成")
        log.info("云端转写完成：%d 句，语言=%s", len(segments), language)
        return Transcript(language=language, segments=segments).normalized()

    def _parse(self, payload: dict, audio_path: Path
               ) -> tuple[list[Segment], str]:
        """归一化各厂商返回：优先带时间轴的 segments，否则退化为单条整段字幕。"""
        language = str(payload.get("language") or "unknown")
        out: list[Segment] = []
        for s in (payload.get("segments") or []):
            if not isinstance(s, dict):
                continue
            text = str(s.get("text", "")).strip()
            if not text:
                continue
            try:
                out.append(Segment(start=float(s.get("start", 0) or 0),
                                   end=float(s.get("end", 0) or 0), text=text))
            except (TypeError, ValueError):
                continue
        if out:
            return out, language
        text = str(payload.get("text") or "").strip()
        if not text:
            return [], language
        try:
            duration = float(payload.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        if duration <= 0:  # 部分厂商不回时长，本地探测兜底（wav 已存在）
            from .renderer import probe_duration
            try:
                duration = probe_duration(self._cfg, audio_path)
            except Exception:
                duration = 0.0
        if duration <= 0:
            log.warning("云端未返回时间轴与时长，退化为单条整段字幕")
            duration = 1.0
        return [Segment(start=0.0, end=duration, text=text)], language


def _has_cuda() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def _add_nvidia_dll_dirs(check_cancel=None, on_status=None) -> None:
    """注册随包及 pip CUDA 运行库；原生 LoadLibrary 还需要进程 PATH。"""
    import os
    import sys
    if os.name != "nt":
        return
    from .cuda_runtime import ensure_cuda_runtime
    bundled = bin_dir() / "cuda"
    if (bundled / "runtime.json").is_file():
        bundled = ensure_cuda_runtime(bundled, data_dir() / "cache" / "cuda",
                                      check_cancel, on_status)
    candidates = [bundled] + [
        Path(base) / "nvidia" / pkg / "bin"
        for pkg in ("cublas", "cudnn") for base in list(sys.path)]
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        path = str(candidate.resolve())
        if path in _cuda_dll_handles:
            continue
        try:
            _cuda_dll_handles[path] = os.add_dll_directory(path)
            if path not in os.environ.get("PATH", "").split(os.pathsep):
                os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
            log.info("已注册 CUDA 运行库目录：%s", path)
        except OSError:
            pass


def build_transcriber(config) -> TranscriberProvider:
    """Provider 注册中心：按配置切换识别后端。"""
    provider = config.get("transcriber.provider", "faster-whisper")
    registry = {"faster-whisper": FasterWhisperTranscriber,
                "cloud": CloudAsrTranscriber}
    cls = registry.get(provider)
    if cls is None:
        raise AppError(Codes.CFG_INVALID, f"未知的识别 Provider：{provider}",
                       suggestion=f"可选值：{', '.join(registry)}")
    return cls(config)
