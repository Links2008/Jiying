"""语音语言码、时间轴、字幕段落及转写文件读写。"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import unicodedata
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

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
