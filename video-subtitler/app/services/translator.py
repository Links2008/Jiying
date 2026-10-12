"""翻译 Provider：null（仅源语言）/ llm（OpenAI 兼容接口，适配 GLM、DeepSeek 等）。"""
from __future__ import annotations

import logging
import re
import threading
import time
from abc import ABC, abstractmethod
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.parse import urlsplit

import requests

from ..core.errors import AppError, Codes

log = logging.getLogger(__name__)

# 语言代码 → 展示名（用于翻译提示词与字幕轨道命名）
LANG_NAMES = {
    "zh": "中文", "en": "English", "ja": "日本語", "ko": "한국어",
    "es": "Español", "fr": "Français", "de": "Deutsch", "ru": "Русский",
    "pt": "Português", "ar": "العربية", "th": "ไทย", "vi": "Tiếng Việt",
}

LANG_CODES = {  # 语言代码 → ISO 639-2（ffmpeg 字幕轨道 language 元数据）
    "zh": "chi", "en": "eng", "ja": "jpn", "ko": "kor", "es": "spa",
    "fr": "fre", "de": "ger", "ru": "rus", "pt": "por", "ar": "ara",
}


class TranslatorProvider(ABC):
    name: str = "base"

    @abstractmethod
    def translate(self, texts: list[str], src_lang: str, tgt_lang: str,
                  on_progress=None) -> list[str]:
        """按句翻译，输入输出严格一一对应（时间轴不变的关键约束）。"""


class NullTranslator(TranslatorProvider):
    """不翻译：只出源语言字幕时使用。"""

    name = "null"

    def translate(self, texts: list[str], src_lang: str, tgt_lang: str,
                  on_progress=None) -> list[str]:
        return texts


# 翻译厂商预设：OpenAI 兼容的 /chat/completions 接口，选定厂商自动带出地址与模型。
# 前端会同步回填，此处兜底 —— config.yaml 手改或 base_url 缺失时仍可用。
LLM_VENDORS: dict[str, dict] = {
    "deepseek": {"base_url": "https://api.deepseek.com/v1",
                 "model": "deepseek-chat"},
    "openai": {"base_url": "https://api.openai.com/v1",
               "model": "gpt-4o-mini"},
    "siliconflow": {"base_url": "https://api.siliconflow.cn/v1",
                    "model": "deepseek-ai/DeepSeek-V3"},
    "zhipu": {"base_url": "https://open.bigmodel.cn/api/paas/v4",
              "model": "glm-4-flash"},
}


class LLMTranslator(TranslatorProvider):
    """OpenAI 兼容 chat/completions 接口的批量字幕翻译。"""

    name = "llm"

    def __init__(self, config):
        self._llm = config.get("translator.llm", {}) or {}
        self._stopped = threading.Event()

    def _base_url(self) -> str:
        vendor = str(self._llm.get("vendor", "") or "").strip().lower()
        preset = LLM_VENDORS.get(vendor, {})
        base = (str(self._llm.get("base_url", "") or "").strip()
                or preset.get("base_url", ""))
        return base.rstrip("/")

    def _model(self) -> str:
        vendor = str(self._llm.get("vendor", "") or "").strip().lower()
        return (str(self._llm.get("model", "") or "").strip()
                or LLM_VENDORS.get(vendor, {}).get("model", ""))

    # ---- 接口实现 ----
    def translate(self, texts: list[str], src_lang: str, tgt_lang: str,
                  on_progress=None) -> list[str]:
        if src_lang == tgt_lang:
            return texts
        batch = max(1, int(self._llm.get("batch_size", 20)))
        out = [""] * len(texts)
        self._stopped.clear()
        pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="subtitle-translate")
        pending = {}
        cursor = completed = 0
        try:
            while pending or cursor < len(texts):
                while cursor < len(texts) and len(pending) < 4:
                    pending[pool.submit(self._translate_chunk, texts[cursor:cursor + batch],
                                        src_lang, tgt_lang, offset=cursor)] = cursor
                    cursor += batch
                done, _ = wait(pending, timeout=.2, return_when=FIRST_COMPLETED)
                for future in sorted(done, key=pending.get):
                    start = pending.pop(future)
                    translated = future.result()
                    out[start:start + len(translated)] = translated
                    completed += len(translated)
                    if on_progress:
                        on_progress(completed / len(texts))
                if not done and on_progress:
                    on_progress(completed / len(texts))
            return out
        finally:
            self._stopped.set()
            pool.shutdown(wait=False, cancel_futures=True)

    # ---- 内部 ----
    def _translate_chunk(self, chunk: list[str], src: str, tgt: str,
                         offset: int) -> list[str]:
        prompt = (
            f"你是专业字幕翻译。把下面的编号台词从{LANG_NAMES.get(src, src)}"
            f"翻译成{LANG_NAMES.get(tgt, tgt)}。\n"
            "要求：\n"
            "1. 输出与输入完全相同的编号和行数，一行一条，禁止合并、增删行\n"
            "2. 台词可能是不完整的句子，结合上下文自然翻译\n"
            "3. 只输出译文行，不要任何解释\n"
            + "\n".join(f"{n}. {t}" for n, t in
                        enumerate(chunk, start=offset + 1))
        )
        for attempt in (1, 2, 3):
            if self._stopped.is_set():
                raise AppError(Codes.TASK_CANCELLED, "字幕翻译已停止")
            try:
                resp = self._call_api(prompt)
                parsed = self._parse_numbered(resp, len(chunk), offset)
                if parsed is not None:
                    return parsed
                log.warning("翻译返回编号或译文不完整（第 %d 次尝试）", attempt)
            except requests.RequestException as e:
                log.warning("翻译请求失败（第 %d 次）：%s", attempt, e)
            if attempt < 3:
                time.sleep(2 * attempt)
        raise AppError(
            Codes.TRANS_FAILED, "字幕翻译失败：连续三次未返回完整译文",
            suggestion="在设置中检查翻译服务、模型和网络，然后重试字幕处理",
        )

    def _call_api(self, prompt: str) -> str:
        base = self._base_url()
        if not base:
            raise AppError(Codes.CFG_INVALID, "翻译未配置接口地址",
                           suggestion="在「识别与翻译」里选择厂商，或填写自定义 URL")
        model = self._model()
        if not model:
            raise AppError(Codes.CFG_INVALID, "翻译未配置模型名",
                           suggestion="填写该厂商的翻译模型，如 deepseek-chat")
        payload = {
            "model": model, "temperature": 0.2,
            "messages": [{"role": "user", "content": prompt}],
        }
        if urlsplit(base).hostname == "api.deepseek.com":
            payload["thinking"] = {"type": "disabled"}
        resp = requests.post(
            base + "/chat/completions",
            headers={"Authorization": f"Bearer {self._llm.get('api_key', '')}"},
            json=payload,
            timeout=float(self._llm.get("timeout", 60)),
        )
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        if choice.get("finish_reason") in ("length", "content_filter"):
            raise requests.RequestException("翻译服务未完整返回译文")
        return choice["message"]["content"]

    @staticmethod
    def _parse_numbered(text: str, expect: int, offset: int) -> list[str] | None:
        """每个预期编号必须恰好出现一次，且译文非空；否则整批重试。"""
        if not isinstance(text, str):
            return None
        lines: dict[int, str] = {}
        matches = list(re.finditer(r"^[ \t]*(\d+)[ \t]*[.、)．][ \t]*", text, re.M))
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            content = [line.strip() for line in text[m.end():end].splitlines()
                       if line.strip() and not line.strip().startswith("```")]
            number, translated = int(m.group(1)), " ".join(content)
            if number in lines or not translated:
                return None
            lines[number] = translated
        expected = range(offset + 1, offset + expect + 1)
        if set(lines) != set(expected):
            return None
        return [lines[n] for n in expected]


def build_translator(config, src_lang: str, tgt_lang: str) -> TranslatorProvider:
    """按配置与语言对构建翻译器；同语言或 provider=null 时不翻译。"""
    if src_lang == tgt_lang or config.get("translator.provider", "null") == "null":
        return NullTranslator()
    return LLMTranslator(config)
