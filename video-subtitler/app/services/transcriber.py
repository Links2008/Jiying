"""本地 faster-whisper 与云端 ASR Provider。"""
from __future__ import annotations

import copy
import json
import logging
import math
import re
import threading
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path

from ..core.config import Config
from ..core.errors import AppError, Codes, dep_missing
from .speech_cuda import _add_nvidia_dll_dirs, _has_cuda
from .speech_text import (
    Segment, Transcript, _clip_audio_timing, _join_speech_tokens, _timed_audio_segments,
    _timed_word_segments, _wav_duration, normalize_asr_language, valid_language_code, write_json_atomic,
)
from .whisper_models import (
    WHISPER_FILES, _preparation_lock, download_whisper_model, local_whisper_model,
    whisper_model_complete, whisper_progress_tqdm,
)
from . import model_maintenance

__all__ = [
    "CloudAsrTranscriber", "FasterWhisperTranscriber", "Segment", "Transcript", "TranscriberProvider",
    "WHISPER_FILES", "build_transcriber", "download_whisper_model", "local_whisper_model",
    "normalize_asr_language", "valid_language_code", "whisper_model_complete", "whisper_progress_tqdm",
    "write_json_atomic", "_timed_word_segments",
]

log = logging.getLogger(__name__)


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
                release = self._retired and self._leases == 0
            if release:
                self._release_model()

    def close(self) -> None:
        """最后一个已开始的任务交还模型，切换配置无需等待推理。"""
        with self._lifecycle:
            if self._retired:
                return
            self._retired = True
            release = self._leases == 0
        model_maintenance.forget(self)
        if release:
            self._release_model()

    def _release_model(self) -> None:
        with self._lock:
            self._model = None
            self._loaded_key = None

    def release_idle_model(self) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        try:
            with self._lifecycle:
                if self._retired or self._model is None:
                    model_maintenance.forget(self)
                    return True
                if self._leases or time.monotonic() - self._last_used <= self._unload_after:
                    return False
                self._model = None
                self._loaded_key = None
                model_maintenance.forget(self)
                return True
        finally:
            self._lock.release()

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
        if self._unload_after > 0 and not self._retired:
            model_maintenance.track(self)
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
        from .whisper_audio import decode_audio

        try:
            model = self._load_model(device_override, on_progress=on_progress)
            def check_cancel():
                if on_progress:
                    on_progress(0.0, "读取语音识别音轨…")

            audio = decode_audio(audio_path, self._request_config, check_cancel)
            words: list[Segment] = []
            out, info = self._run_model(model, audio, source_lang,
                                        on_progress, vad=True, word_sink=words)
            if not out:
                # VAD 全删不一定真是没人声：低音量/配乐盖人声常被误杀
                #（实测 Big Buck Bunny 10 分钟语音被 VAD 全过滤导致烧录失败）。
                # 关 VAD 整段重试一次，仍无结果才判定为无语音内容。
                log.warning("VAD 过滤了全部音频（时长 %.1fs），关闭 VAD 重试",
                            float(info.duration or 0))
                words.clear()
                out, info = self._run_model(model, audio, source_lang,
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

    def _run_model(self, model, audio, source_lang: str | None,
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
            audio, language=source_lang or None,
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
        duration = float(info.duration or 0)
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
