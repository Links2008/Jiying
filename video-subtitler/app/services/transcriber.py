"""语音识别 Provider：默认 faster-whisper（本地离线），接口可扩展云 API。"""
from __future__ import annotations

import json
import copy
import fnmatch
import hashlib
import logging
import math
import os
import re
import tempfile
import threading
import time
import unicodedata
import wave
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..core.config import Config
from ..core.errors import AppError, Codes, dep_missing
from ..core.paths import bin_dir, data_dir

log = logging.getLogger(__name__)

WHISPER_FILES = ["config.json", "preprocessor_config.json", "model.bin",
                 "tokenizer.json", "vocabulary.*"]
_whisper_download_lock = threading.Lock()
_cuda_dll_handles: dict[str, object] = {}
_MODELSCOPE_WHISPER = ("tiny", "base", "small", "medium", "large-v3")


def normalize_asr_language(value) -> str:
    """云端可能返回语言全名或地区码，统一为安全的基础语言代码。"""
    if not isinstance(value, str):
        return 'unknown'
    name = value.strip().casefold().replace('_', '-')
    aliases = {
        'english': 'en', 'eng': 'en', 'chinese': 'zh', 'mandarin': 'zh', '中文': 'zh',
        'chi': 'zh', 'zho': 'zh', 'cmn': 'zh', 'cantonese': 'yue',
        'japanese': 'ja', '日本語': 'ja', 'jpn': 'ja', 'korean': 'ko', '한국어': 'ko', 'kor': 'ko',
        'spanish': 'es', 'español': 'es', 'spa': 'es', 'french': 'fr', 'français': 'fr', 'fra': 'fr', 'fre': 'fr',
        'german': 'de', 'deutsch': 'de', 'deu': 'de', 'ger': 'de', 'russian': 'ru', 'rus': 'ru',
        'portuguese': 'pt', 'por': 'pt', 'arabic': 'ar', 'ara': 'ar', 'thai': 'th', 'tha': 'th',
        'vietnamese': 'vi', 'vie': 'vi', 'italian': 'it', 'ita': 'it', 'dutch': 'nl', 'nld': 'nl', 'dut': 'nl',
        'hindi': 'hi', 'hin': 'hi', 'indonesian': 'id', 'ind': 'id', 'turkish': 'tr', 'tur': 'tr',
        'hebrew': 'he', 'heb': 'he', 'malay': 'ms', 'polish': 'pl', 'ukrainian': 'uk',
        'swedish': 'sv', 'danish': 'da', 'norwegian': 'no', 'finnish': 'fi', 'greek': 'el',
        'czech': 'cs', 'romanian': 'ro', 'hungarian': 'hu', 'persian': 'fa', 'farsi': 'fa',
        'bengali': 'bn', 'tamil': 'ta', 'telugu': 'te', 'urdu': 'ur', 'swahili': 'sw',
    }
    if name in aliases:
        return aliases[name]
    if re.fullmatch(r'[a-z]{2,3}(?:-[a-z0-9]{2,8})*', name):
        return name.split('-')[0]
    return 'unknown'


def valid_language_code(value, allow_unknown=False) -> bool:
    """语言字段会用于字幕文件名，仅允许语言代码和可选的未知语言标记。"""
    return (isinstance(value, str) and len(value) <= 35
            and (allow_unknown and value == 'unknown'
                 or bool(re.fullmatch(r'[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*', value))))


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


def _sentence_finished(text: str) -> bool:
    """句末引号不妨碍断句；小数、常见称谓和字母缩写不作为句末。"""
    text = text.rstrip().rstrip('"\'”’」』）)]}')
    if text.endswith(tuple("。！？!?；;")):
        return True
    if not text.endswith("."):
        return False
    last = text.split()[-1].lower()
    return last not in {"mr.", "mrs.", "ms.", "dr.", "prof.", "sr.", "jr.",
                        "e.g.", "i.e.", "vs.", "etc."} and not re.fullmatch(r"(?:[a-z]\.)+", last)


def _join_speech_tokens(tokens) -> str:
    """无原文时保留已有空格，并在分离的西文词之间补空格。"""
    text = ""
    for token in tokens:
        if not token:
            continue
        if (text and (text[-1].isalnum() or text[-1] in ",.!?;:") and token[0].isalnum()
                and unicodedata.east_asian_width(text[-1]) not in "WF"
                and unicodedata.east_asian_width(token[0]) not in "WF"):
            text += " "
        text += token
    return text.strip()


def _timed_word_segments(text: str, words, word_sink: list[Segment] | None = None) -> list[Segment] | None:
    """按真实词时间、句末和停顿分段；无法完整对齐时保留调用方的整段结果。

    云端词表可能省略标点，因此把词逐一对齐到原文后再切片，不能用词表
    覆盖原文。无标点长句最多约 6 秒或 32 个汉字，并始终在词边界切分。
    宽度控制在字幕单行内，避免后续排版再次按字数估算字幕起止时间。
    此处不判断说话人身份；没有停顿或标点的换人需要专门的说话人识别。
    """
    if not isinstance(words, (list, tuple)) or not words:
        return None
    timed: list[tuple[str, float, float]] = []
    for item in words:
        get = (item.get if isinstance(item, dict)
               else lambda key, default=None: getattr(item, key, default))
        token = get("word", get("text", ""))
        if not isinstance(token, str):
            return None
        if not token.strip():
            continue
        try:
            start, end = float(get("start")), float(get("end"))
        except (TypeError, ValueError):
            return None
        if (not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start
                or (timed and (start < timed[-1][1] or end < timed[-1][2]))):
            return None
        timed.append((token, start, end))
    if not timed:
        return None
    text = text.strip()
    if not text:
        text = _join_speech_tokens(token for token, _, _ in timed)
    boundaries = [0]
    cursor = 0
    for i, (token, _, _) in enumerate(timed):
        token = token.strip()
        pos = text.find(token, cursor)
        if pos < 0 or any(not ch.isspace() and not unicodedata.category(ch).startswith("P")
                          for ch in text[cursor:pos]):
            return None
        if i:
            cut = pos
            # 原文额外提供的前置引号归给下一句，句末标点仍归给上一句。
            while cut > cursor:
                ch = text[cut - 1]
                opening = ch in "“‘「『（([{"
                opening = opening or ch == '"' and text[:cut].count('"') % 2 == 1
                if not ch.isspace() and not opening:
                    break
                cut -= 1
            boundaries.append(cut)
        cursor = pos + len(token)
    if any(not ch.isspace() and not unicodedata.category(ch).startswith("P") for ch in text[cursor:]):
        return None
    boundaries.append(len(text))
    out: list[Segment] = []
    first = 0

    def emit(last: int) -> None:
        out.append(Segment(timed[first][1], timed[last][2],
                           text[boundaries[first]:boundaries[last + 1]].strip()))

    for i in range(len(timed)):
        if i > first:
            candidate = text[boundaries[first]:boundaries[i + 1]].strip()
            width = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in candidate)
            if (timed[i][1] - timed[i - 1][2] >= .6 - 1e-6
                    or timed[i][2] - timed[first][1] > 6 or width > 64):
                if timed[i - 1][2] > timed[first][1] and timed[i][1] >= timed[i - 1][2]:
                    emit(i - 1)
                    first = i
        piece = text[boundaries[first]:boundaries[i + 1]]
        next_clear = i == len(timed) - 1 or timed[i + 1][1] >= timed[i][2]
        if _sentence_finished(piece) and timed[i][2] > timed[first][1] and next_clear:
            emit(i)
            first = i + 1
    if first < len(timed):
        if timed[-1][2] > timed[first][1]:
            emit(len(timed) - 1)
        else:
            # 末尾仅剩零时长词时不能丢文本，也不能生成零时长字幕。
            return None
    if out and word_sink is not None:
        word_sink.extend(Segment(start, end, text[boundaries[i]:boundaries[i + 1]])
                         for i, (_, start, end) in enumerate(timed))
    return out or None


def _wav_duration(audio_path: Path | None) -> float:
    if audio_path is None:
        return 0.0
    try:
        with wave.open(str(audio_path), 'rb') as audio:
            return audio.getnframes() / audio.getframerate()
    except (OSError, wave.Error, EOFError, ZeroDivisionError):
        return 0.0


def _clip_audio_timing(items: list[Segment], duration: float, allow_zero=False) -> list[Segment]:
    """轻微编码尾差可截断，超出实际音频或无法显示的字幕时间必须回退或报错。"""
    out = []
    for item in items:
        start, end = item.start, item.end
        if (not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start
                or (duration > 0 and (start > duration or end > duration + .1))):
            raise ValueError('字幕时间超出有效音频范围')
        if duration > 0:
            end = min(end, duration)
        if not allow_zero and round(end * 1000) <= round(start * 1000):
            raise ValueError('字幕时长不足一毫秒')
        out.append(Segment(start, end, item.text))
    return out


def _timed_audio_segments(text: str, words, duration: float,
                          word_sink: list[Segment] | None = None) -> list[Segment] | None:
    aligned = []
    cues = _timed_word_segments(text, words, aligned)
    if not cues:
        return None
    try:
        cues = _clip_audio_timing(cues, duration)
        aligned = _clip_audio_timing(aligned, duration, allow_zero=True)
    except ValueError:
        return None
    if word_sink is not None:
        word_sink.extend(aligned)
    return cues


def write_json_atomic(path: Path, value: dict) -> None:
    """先写临时文件再替换，进程中断不能损坏已有识别检查点。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=1, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@dataclass
class Transcript:
    """一次转写的完整结果（含检测到的源语言）。"""

    language: str
    segments: list[Segment] = field(default_factory=list)
    words: list[Segment] = field(default_factory=list)

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
                      for s in self.segments],
            words=[Segment(s.start, s.end, cc.convert(s.text)) for s in self.words])

    def save_json(self, path: Path) -> None:
        self.validate()
        write_json_atomic(path, asdict(self))

    @classmethod
    def load_json(cls, path: Path) -> "Transcript":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def from_dict(cls, data: dict) -> "Transcript":
        if (not isinstance(data, dict) or not isinstance(data.get('language'), str)
                or not data['language'].strip() or not isinstance(data.get('segments'), list)
                or not isinstance(data.get('words', []), list)):
            raise ValueError('识别检查点的语言或句子结构无效')
        try:
            result = cls(language=normalize_asr_language(data['language']),
                         segments=[Segment(**s) for s in data['segments']],
                         words=[Segment(**s) for s in data.get('words', [])])
        except TypeError as exc:
            raise ValueError('识别检查点的时间轴结构无效') from exc
        result.validate()
        return result

    def validate(self) -> None:
        """缓存中的文本与时间轴都须有效；词级标点可以零时长，字幕句不可以。"""
        if (not isinstance(self.language, str) or not self.language.strip()
                or not isinstance(self.segments, list) or not isinstance(self.words, list)):
            raise ValueError('识别结果的语言或句子结构无效')
        for items, is_word in ((self.segments, False), (self.words, True)):
            for item in items:
                if (not isinstance(item, Segment) or not isinstance(item.text, str) or not item.text.strip()
                        or type(item.start) not in (int, float) or type(item.end) not in (int, float)
                        or not math.isfinite(item.start) or not math.isfinite(item.end)
                        or item.start < 0 or item.end < item.start
                        or (not is_word and round(item.end * 1000) <= round(item.start * 1000))):
                    raise ValueError('识别结果含空文本或无效时间轴')


class TranscriberProvider(ABC):
    name: str = "base"

    @abstractmethod
    def transcribe(self, audio_path: Path, source_lang: str | None = None,
                   on_progress=None) -> Transcript:
        """转写音频 → Transcript。on_progress(fraction 0~1, note)。"""


class FasterWhisperTranscriber(TranscriberProvider):
    """faster-whisper：CTranslate2 加速的 Whisper，99 语种、自动检测语言。"""

    name = "faster-whisper"
    supports_word_timestamps = True

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
        self._lifecycle = threading.Condition()
        self._leases = 0
        self._lease_context = threading.local()
        self._retired = False
        self._retire_requested = threading.Event()
        self._maintenance_thread = None
        if self._unload_after > 0:
            self._maintenance_thread = threading.Thread(target=self._idle_unloader, daemon=True,
                                                        name='whisper-maintenance')
            self._maintenance_thread.start()

    @property
    def _request_config(self):
        return getattr(self._lease_context, 'config_snapshot', None) or self._cfg

    @contextmanager
    def _settings_snapshot(self):
        """载入、GPU 降级与 VAD 重试使用同一份参数；下一次调用读取新设置。"""
        previous = getattr(self._lease_context, 'config_snapshot', None)
        if previous is None:
            self._lease_context.config_snapshot = Config(copy.deepcopy(self._cfg.raw))
        try:
            yield
        finally:
            self._lease_context.config_snapshot = previous

    @contextmanager
    def lease(self):
        """已开始的任务可续用旧实例；切换后不再让新任务占用旧模型。"""
        depth = getattr(self._lease_context, 'depth', 0)
        with self._lifecycle:
            if self._retired and depth == 0:
                raise AppError(Codes.CFG_INVALID, '识别后端已切换，请使用当前设置重试')
            self._leases += 1
        self._lease_context.depth = depth + 1
        try:
            yield self
        finally:
            self._lease_context.depth = depth
            with self._lifecycle:
                self._leases -= 1
                self._last_used = time.monotonic()
                self._lifecycle.notify_all()

    def close(self) -> None:
        """非阻塞退休：唤醒维护线程，最后一个任务退出后才释放旧模型。"""
        with self._lifecycle:
            if self._retired:
                return
            self._retired = True
            self._retire_requested.set()
            if self._maintenance_thread is None:
                self._maintenance_thread = threading.Thread(target=self._release_retired, daemon=True,
                                                            name='whisper-retirement')
                self._maintenance_thread.start()

    def _release_retired(self) -> None:
        with self._lifecycle:
            while self._leases:
                self._lifecycle.wait()
        with self._lock:
            self._model = None
            self._loaded_key = None

    def _idle_unloader(self) -> None:
        while not self._retire_requested.wait(60):
            with self._lock:
                with self._lifecycle:
                    idle = not self._leases and not self._retired
                if (idle and self._model is not None
                        and time.monotonic() - self._last_used > self._unload_after):
                    log.info("模型空闲 %.0f 分钟，自动卸载释放内存",
                             (time.monotonic() - self._last_used) / 60)
                    self._model = None
                    self._loaded_key = None
        self._release_retired()

    def _load_model(self, device_override: str | None = None,
                    model_override: str | None = None, on_progress=None):
        config = self._request_config
        size = model_override or config.get("transcriber.model", "small")
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
            device_override or config.get("transcriber.device", "auto"))
        compute = config.get("transcriber.compute_type", "auto")
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
        path = download_whisper_model(config, size, tqdm_class=progress,
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
        with self.lease(), self._settings_snapshot(), self._lock:
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
        with self.lease(), self._settings_snapshot(), _preparation_lock(self._lock, waiting):
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
            words: list[Segment] = []
            out, info = self._run_model(model, audio_path, source_lang,
                                        on_progress, vad=True, word_sink=words)
            if not out:
                # VAD 全删不一定真是没人声：低音量/配乐盖人声常被误杀
                #（实测 Big Buck Bunny 10 分钟语音被 VAD 全过滤导致烧录失败）。
                # 关 VAD 整段重试一次，仍无结果才判定为无语音内容。
                log.warning("VAD 过滤了全部音频（时长 %.1fs），关闭 VAD 重试",
                            float(info.duration or 0))
                words.clear()
                out, info = self._run_model(model, audio_path, source_lang,
                                            on_progress, vad=False, word_sink=words)
        except AppError:
            raise
        except Exception as e:
            raise AppError(Codes.ASR_FAILED, f"语音识别失败：{e}",
                           detail=str(e), retryable=True) from e
        if not out:  # 两种模式都无输出 = 确实没检测到人声，明确告知而非产出空字幕
            raise AppError(
                Codes.ASR_NO_SPEECH, "未在音频中检测到语音内容",
                detail="整段音频（时长 %.1fs）未识别出任何语句" % (info.duration or 0),
                suggestion="确认视频包含清晰人声；纯音乐/纯静音无法生成字幕")
        if on_progress:
            on_progress(1.0, "转写完成")
        log.info("转写完成：%d 句，语言=%s", len(out), info.language)
        transcript = Transcript(language=str(info.language or "unknown"), segments=out, words=words)
        return transcript.normalized()

    def _run_model(self, model, audio_path: Path, source_lang: str | None,
                   on_progress, vad: bool, word_sink: list[Segment] | None = None
                   ) -> tuple[list[Segment], object]:
        """跑一遍 whisper 生成器；vad=False 用于 VAD 误杀全音频时的兜底。"""
        # beam_size 默认 5：贪心解码（1）CPU 上快约 2 倍，但实测同一段音频
        # 多次转写结果不稳定（断句 13~15 句、数字「二零二六/2026」来回漂移），
        # 且更易出现同音字错（逝世→试试）。改 5 换稳定与准确，追求速度可在
        # config 设 transcriber.beam_size: 1
        config = self._request_config
        beam = int(config.get("transcriber.beam_size", 5) or 5)
        segments_iter, info = model.transcribe(
            str(audio_path), language=source_lang or None,
            vad_filter=vad,           # 过滤静音段：长视频更快更准
            beam_size=max(1, beam),
            # 词级时间戳：Whisper 句级时间戳天然首尾相接（上一句 end=下一句
            # start，句间静音被吞进上一句），字幕会一直挂到下一句顶掉才换。
            # 开启后按词的真实时刻和句末/停顿分段，避免对话粘连和静音拖尾。
            # 代价：转写稍慢（CPU 上约一~三成），换时间轴准确。
            word_timestamps=True,
            # 长音频减少前一窗口错误滚动传播；可显式恢复原有上下文解码。
            condition_on_previous_text=bool(config.get("transcriber.condition_on_previous_text", False)),
            initial_prompt=str(config.get("transcriber.initial_prompt", "") or "") or None,
            hotwords=str(config.get("transcriber.hotwords", "") or "") or None,
        )
        duration = _wav_duration(audio_path) or float(info.duration or 0)
        if not math.isfinite(duration) or duration <= 0:
            duration = 0.0
        out: list[Segment] = []
        all_timed = True
        last_report = time.monotonic()
        for seg in segments_iter:  # 生成器惰性执行，边转边收
            text = seg.text.strip()
            if not text:
                continue
            words = getattr(seg, "words", None)
            cues = _timed_audio_segments(text, words, duration, word_sink)
            if cues:
                out.extend(cues)
                end = cues[-1].end
            else:
                all_timed = False
                # 词表缺失、损坏或文本无法对齐时，保留原句和其时间轴，避免丢字。
                start, end = float(seg.start), float(seg.end)
                fallback = _clip_audio_timing([Segment(start=start, end=end, text=text)], duration)
                out.extend(fallback)
                end = fallback[-1].end
            if on_progress and duration and time.monotonic() - last_report > 1.0:
                on_progress(min(end / duration, 1.0), f"转写至 {end:.0f}s")
                last_report = time.monotonic()
        if not all_timed and word_sink is not None:
            word_sink.clear()
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
    verbose_json 时优先按 words 还原时间轴，其次使用 segments；
    仅有整段文本时退化为单条字幕，不伪造精确的词级对齐。
    """

    name = "cloud"

    def __init__(self, config):
        self._cfg = config

    @property
    def _cloud(self) -> dict:
        cloud = self._cfg.get('transcriber.cloud', {}) or {}
        if not isinstance(cloud, dict):
            raise AppError(Codes.CFG_INVALID, '云端识别设置格式无效')
        return cloud

    def _base_url(self, cloud=None) -> str:
        cloud = self._cloud if cloud is None else cloud
        vendor = str(cloud.get("vendor", "") or "").strip().lower()
        preset = ASR_VENDORS.get(vendor, {})
        base = (str(cloud.get("base_url", "") or "").strip()
                or preset.get("base_url", ""))
        return base.rstrip("/")

    @property
    def supports_word_timestamps(self) -> bool:
        cloud = self._cloud
        return (str(cloud.get('vendor') or '').strip().lower() == 'openai'
                and (str(cloud.get('model') or '').strip() or 'whisper-1') == 'whisper-1')

    def transcribe(self, audio_path: Path, source_lang: str | None = None,
                   on_progress=None) -> Transcript:
        import requests
        # 一次请求使用同一份快照，设置换页不能混用旧地址与新密钥或模型。
        cloud = copy.deepcopy(self._cloud)
        base = self._base_url(cloud)
        if not base:
            raise AppError(Codes.CFG_INVALID, "云端识别未配置接口地址",
                           suggestion="在「识别与翻译」里选择厂商，或填写自定义 URL")
        vendor = str(cloud.get("vendor", "") or "").strip().lower()
        model = (str(cloud.get("model", "") or "").strip()
                 or ASR_VENDORS.get(vendor, {}).get("model", ""))
        if not model:
            raise AppError(Codes.CFG_INVALID, "云端识别未配置模型名",
                           suggestion="填写该厂商的识别模型，如 whisper-1")
        if not audio_path.exists():
            raise AppError(Codes.ASR_FAILED, f"音频文件不存在：{audio_path}")
        api_key = str(cloud.get("api_key", "") or "").strip()
        lang = (source_lang or str(cloud.get("language", "") or "")).strip()
        if on_progress:
            on_progress(0.05, "上传音频至云端识别…")
        data = {"model": model, "response_format": "verbose_json"}
        if vendor == "openai" and model == "whisper-1":
            # 官方 Whisper 支持词级时间；其他兼容厂商不强加可选参数。
            data["timestamp_granularities[]"] = ["word", "segment"]
        if lang:
            data["language"] = lang
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            with open(audio_path, "rb") as fh:
                resp = requests.post(
                    base + "/audio/transcriptions", headers=headers, data=data,
                    files={"file": (audio_path.name, fh, "audio/wav")},
                    timeout=float(cloud.get("timeout", 300) or 300))
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
        if not isinstance(payload, dict):
            raise AppError(Codes.ASR_FAILED, '云端识别返回了无效的结果结构', retryable=True)
        words: list[Segment] = []
        segments, language = self._parse(payload, audio_path, words)
        if language == 'unknown':
            language = normalize_asr_language(lang)
        if not segments:
            raise AppError(Codes.ASR_NO_SPEECH, "云端识别未返回任何语句",
                           detail=json.dumps(payload, ensure_ascii=False)[:400],
                           suggestion="确认该模型支持音频转写，且音频包含清晰人声")
        if on_progress:
            on_progress(1.0, "转写完成")
        log.info("云端转写完成：%d 句，语言=%s", len(segments), language)
        return Transcript(language=language, segments=segments, words=words).normalized()

    def _parse(self, payload: dict, audio_path: Path, word_sink: list[Segment] | None = None
               ) -> tuple[list[Segment], str]:
        """优先使用完整对齐的词级时间轴；缺失时保留厂商段落或整段文本。"""
        if not isinstance(payload, dict):
            raise AppError(Codes.ASR_FAILED, '云端识别返回了无效的结果结构', retryable=True)
        if payload.get('error') or not any((isinstance(payload.get('text'), str),
                                          isinstance(payload.get('segments'), list),
                                          isinstance(payload.get('words'), list))):
            # HTTP 200 不代表识别成功，错误对象不能作为无语音分段写入检查点。
            raise AppError(Codes.ASR_FAILED, '云端识别未返回有效转写结果', retryable=True)
        language = normalize_asr_language(payload.get('language'))
        duration = _wav_duration(audio_path)
        raw_text = payload.get('text')
        if raw_text is not None and not isinstance(raw_text, str):
            raise AppError(Codes.ASR_FAILED, '云端识别返回了无效的文本结构', retryable=True)
        source = (raw_text or '').strip()
        raw_segments = payload.get('segments') or []
        malformed = not isinstance(raw_segments, list)
        raw_segments = raw_segments if isinstance(raw_segments, list) else []
        if not source:
            # 若同时给了段落，用段落全文校验词表是否完整，不能丢掉未对齐的尾句。
            source = _join_speech_tokens(s['text'].strip() for s in raw_segments
                                         if isinstance(s, dict) and isinstance(s.get('text'), str))
        if not source and isinstance(payload.get('words'), list):
            tokens = []
            for item in payload['words']:
                token = item.get('word', item.get('text')) if isinstance(item, dict) else None
                if not isinstance(token, str):
                    raise AppError(Codes.ASR_FAILED, '云端识别词表损坏且没有完整文本可恢复', retryable=True)
                tokens.append(token)
            # 仅词表的接口也可能返回坏时间；先保留全文再尝试精确对齐。
            source = _join_speech_tokens(tokens)
        timed = _timed_audio_segments(source, payload.get("words"), duration, word_sink)
        if timed:
            return timed, language
        out: list[Segment] = []
        aligned: list[Segment] = []
        all_timed = True
        for s in raw_segments:
            if not isinstance(s, dict) or not isinstance(s.get('text'), str):
                malformed = True
                continue
            text = s['text'].strip()
            if not text:
                continue
            timed = _timed_audio_segments(text, s.get("words"), duration, aligned)
            if timed:
                out.extend(timed)
                continue
            try:
                all_timed = False
                start, end = float(s.get('start')), float(s.get('end'))
                if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
                    raise ValueError('无效的段落时间轴')
                out.extend(_clip_audio_timing([Segment(start=start, end=end, text=text)], duration))
            except (TypeError, ValueError):
                malformed = True
        # 任一段损坏或整段文字比段落列表更完整时，不能以有效前半段冒充全部结果。
        complete = re.sub(r'\s+', '', ''.join(s.text for s in out)).casefold() == re.sub(r'\s+', '', source).casefold()
        if out and not malformed and complete:
            if all_timed and word_sink is not None:
                word_sink.extend(aligned)
            return out, language
        if word_sink is not None:
            word_sink.clear()
        if not source:
            if malformed:
                raise AppError(Codes.ASR_FAILED, '云端识别段落损坏且没有完整文本可恢复', retryable=True)
            return [], language
        # 丢失精确时间轴时保留全文，在实际音频范围内显示；绝不编造一秒时长。
        if not math.isfinite(duration) or duration <= 0:
            try:
                duration = float(payload.get('duration') or 0)
            except (TypeError, ValueError):
                duration = 0.0
        if not math.isfinite(duration) or duration <= 0:
            from .renderer import probe_duration
            try:
                duration = probe_duration(self._cfg, audio_path)
            except Exception:
                duration = 0.0
        if not math.isfinite(duration) or duration <= 0:
            raise AppError(Codes.ASR_FAILED, '缺少有效字幕时间轴，且无法确定实际音频时长', retryable=True,
                           suggestion='确认音频文件完整，或选择返回时间戳的识别模型后重试')
        return [Segment(start=0.0, end=duration, text=source)], language


def _has_cuda() -> bool:
    try:
        if os.name == 'nt':
            import ctypes
            driver = ctypes.WinDLL('nvcuda.dll', winmode=0x800)
            driver.cuInit.argtypes, driver.cuInit.restype = [ctypes.c_uint], ctypes.c_int
            driver.cuDeviceGetCount.argtypes = [ctypes.POINTER(ctypes.c_int)]
            driver.cuDeviceGetCount.restype = ctypes.c_int
            count = ctypes.c_int()
            return driver.cuInit(0) == 0 and driver.cuDeviceGetCount(ctypes.byref(count)) == 0 and count.value > 0
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
