"""字幕生成：Segment 时间轴 → SRT / VTT 文件。"""
from __future__ import annotations

import math
import re
from pathlib import Path

from ..core.errors import AppError, Codes
from .transcriber import Segment


def fmt_ts(seconds: float, ms_sep: str = ",") -> str:
    """秒 → "HH:MM:SS,mmm"（SRT）或 "HH:MM:SS.mmm"（VTT）。全部换算到毫秒再进位。"""
    total_ms = max(int(round(seconds * 1000)), 0)
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1_000)
    return f"{h:02d}:{m:02d}:{s:02d}{ms_sep}{ms:03d}"


def to_srt(segments: list[Segment]) -> str:
    blocks = []
    for i, seg in enumerate(segments, start=1):
        blocks.append(f"{i}\n{fmt_ts(seg.start)} --> {fmt_ts(seg.end)}\n{seg.text}\n")
    return "\n".join(blocks)


def to_vtt(segments: list[Segment]) -> str:
    lines = ["WEBVTT", ""]
    for seg in segments:
        lines.append(f"{fmt_ts(seg.start, '.')} --> {fmt_ts(seg.end, '.')}")
        lines.append(seg.text)
        lines.append("")
    return "\n".join(lines)


# ---- 字幕排版：长句截断 + 折行 ----
# 显示宽度：CJK / 全角字符按 2 计，其余按 1 计。64 ≈ 32 个汉字，
# 对应默认 18 号字体在 640 宽画面中的安全区域；更大的字号由播放器按实际像素折行。
_MAX_LINE_W = 64
_MAX_LINES = 2
_SENTENCE_END = "。！？!?；;."
_CLAUSE_END = "，,、：:…"
_FULLW = re.compile(
    r"[\u1100-\u115f\u2e80-\ua4cf\ua960-\ua97f\uac00-\ud7ff"
    r"\uf900-\ufaff\ufe10-\ufe19\ufe30-\ufe6f\uff01-\uff60\uffe0-\uffe6]")


def _disp_width(text: str) -> int:
    """显示宽度：全角字符算 2、其余算 1。"""
    return sum(2 if _FULLW.match(ch) else 1 for ch in text)


def _cut_width(text: str, limit: int) -> tuple[str, str]:
    """按显示宽度切一刀，返回 (宽度不超过 limit 的前缀, 剩余)；至少切 1 个字符。"""
    width = 0
    for i, ch in enumerate(text):
        width += 2 if _FULLW.match(ch) else 1
        if width > limit:
            return (text[:1], text[1:]) if i == 0 else (text[:i], text[i:])
    return text, ""


def _wrap_piece(piece: str) -> list[str]:
    """只在超宽时折行，优先保留完整西文词，并均衡两行长度。"""
    rows: list[str] = []
    rest = piece.strip()
    while _disp_width(rest) > _MAX_LINE_W:
        width = _disp_width(rest)
        target = _MAX_LINE_W if width > 2 * _MAX_LINE_W else (width + 1) // 2
        head, _ = _cut_width(rest, target)
        max_head, _ = _cut_width(rest, _MAX_LINE_W)
        strong = [i + 1 for i, ch in enumerate(max_head)
                  if ch in _SENTENCE_END and
                  (ch != "." or i + 1 == len(rest) or rest[i + 1].isspace())]
        weak = [i + 1 for i, ch in enumerate(max_head) if ch in _CLAUSE_END]
        words = [i + 1 for i, ch in enumerate(max_head)
                 if ch.isspace() or ch in "）)】」』"]
        breaks: list[int] = []
        for candidates in (strong, weak, words):
            breaks = [i for i in candidates
                      if _disp_width(rest[:i].strip()) >= target * 0.4]
            if breaks:
                break
        cut = min(breaks, key=lambda i: abs(_disp_width(rest[:i].strip()) - target)) if breaks else len(head)
        rows.append(rest[:cut].strip())
        rest = rest[cut:].lstrip()
    if rest:
        rows.append(rest)
    return rows


def _normalize_text(text: str) -> str:
    parts = [re.sub(r"[ \t]+", " ", p).strip()
             for p in re.split(r"\r?\n", text or "")]
    joined = ""
    for part in filter(None, parts):
        if joined and not (_FULLW.match(joined[-1]) or _FULLW.match(part[0])):
            joined += " "
        joined += part
    return joined


def _segment_lines(text: str) -> list[str]:
    """一条字幕文本 → 折行后的行列表；短句和同一行内的标点不强制换行。"""
    return _wrap_piece(_normalize_text(text))


def _split_near(text: str, share: float) -> tuple[str, str]:
    """按时长占比在句末、逗号或词边界附近切分跨时间窗的译文。"""
    target = _disp_width(text) * share
    points = [i + 1 for i, ch in enumerate(text[:-1])
              if ch.isspace() or ch in _CLAUSE_END or
              (ch in _SENTENCE_END and (ch != "." or text[i + 1].isspace()))]
    points = [i for i in points if text[:i].strip() and text[i:].strip()]
    if points:
        cut = min(points, key=lambda i: abs(_disp_width(text[:i]) - target)
                  - (8 if text[i - 1] in _SENTENCE_END else 0))
    else:
        cut = len(_cut_width(text, max(1, round(target)))[0])
    return text[:cut].strip(), text[cut:].strip()


def layout_segments(segments: list[Segment]) -> list[Segment]:
    """把整段直出的长句「截断 + 折行」成便于观看的字幕条。

    转写/翻译只按整句给时间戳、文本一口气铺满整行，长时间挂着又长又难读。这里：

    - 折行：每行显示宽度不超过 _MAX_LINE_W（≈32 个汉字 / 64 个西文字符），
      短句不折行，超宽时优先在词或标点边界折行；
    - 截断：优先在句末、分句标点处分条，每条最多 _MAX_LINES 行；超出部分
      按各行字数占比重新分配时间轴（Whisper 只提供整句时间戳）；
    - 时间轴只在同一条 Segment 内部再切分，绝不跨句借时间，也绝不改动原句起止；
    - 文本零丢失：把所有折行拼回去，与原句逐字符一致（只有空白会被归一）。
    """
    out: list[Segment] = []
    for seg in segments:
        rows = _segment_lines(seg.text)
        start0 = max(seg.start, 0.0)
        end0 = max(seg.end, start0)
        if not rows:
            out.append(Segment(start0, end0, seg.text))
            continue
        groups: list[list[str]] = []
        group: list[str] = []
        for row in rows:
            group.append(row)
            if len(group) == _MAX_LINES or row[-1] in _SENTENCE_END + _CLAUSE_END:
                groups.append(group)
                group = []
        if group:
            groups.append(group)
        if len(groups) == 1:
            out.append(Segment(start0, end0, "\n".join(groups[0])))
            continue
        span = end0 - start0
        total = sum(_disp_width(row) for row in rows) or 1
        acc = 0
        for i, group in enumerate(groups):
            start = start0 + span * acc / total
            acc += sum(_disp_width(row) for row in group)
            end = end0 if i == len(groups) - 1 else start0 + span * acc / total
            out.append(Segment(round(start, 3), round(max(end, start), 3),
                               "\n".join(group)))
    return out


def save_srt(segments: list[Segment], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先排版再落盘：长句截断成多条、每条折行 ≤ _MAX_LINES 行，避免字幕一股脑铺满
    path.write_text(to_srt(layout_segments(segments)), encoding="utf-8-sig")  # BOM 兼容部分播放器
    return path


# ---- 双语上下堆叠 ----
_TS_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")


def _ts_to_sec(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def parse_srt(text: str) -> list[Segment]:
    """解析 SRT → Segment 列表（只服务本项目自产字幕，宽松解析）。

    to_srt() 的输出格式固定：空行分块、第二行时间轴、其余行文本。解析不出
    的块直接跳过 —— 补语言/双语合并绝不能把整条流水线拖垮。
    """
    segs: list[Segment] = []
    body = (text or "").lstrip("\ufeff")
    for block in re.split(r"\r?\n\s*\r?\n", body):
        lines = block.splitlines()
        if len(lines) < 3:
            continue
        m = _TS_RE.search(lines[1])
        if not m:
            continue
        g = m.groups()
        try:
            start = _ts_to_sec(*g[:4])
            end = _ts_to_sec(*g[4:])
        except (TypeError, ValueError):
            continue
        segs.append(Segment(start, end, "\n".join(lines[2:]).strip()))
    return segs


def validate_srt(text: str) -> None:
    """编辑保存采用严格校验，任何错误块都不能被静默跳过。"""
    body = (text or "").lstrip("\ufeff").strip()
    if not body:
        raise ValueError("字幕不能为空；请保留序号、时间轴和字幕文字")
    for number, block in enumerate(re.split(r"\r?\n\s*\r?\n", body), 1):
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].strip().isdigit():
            raise ValueError(f"第 {number} 条字幕缺少有效序号或文字")
        match = _TS_RE.fullmatch(lines[1].strip())
        if not match:
            raise ValueError(f"第 {number} 条字幕时间轴无效，请保留 00:00:00,000 --> 00:00:01,000 格式")
        groups = match.groups()
        if any(int(groups[i]) >= 60 for i in (1, 2, 5, 6)):
            raise ValueError(f"第 {number} 条字幕的分钟和秒必须小于 60")
        if _ts_to_sec(*groups[4:]) <= _ts_to_sec(*groups[:4]):
            raise ValueError(f"第 {number} 条字幕的结束时间必须晚于开始时间")
        if not "\n".join(lines[2:]).strip():
            raise ValueError(f"第 {number} 条字幕文字不能为空")


def segments_cover(cover: list[Segment], expected: list[Segment]) -> bool:
    """有效文本必须连续覆盖源句时间窗；容忍 SRT 毫秒舍入，不要求条数相同。"""
    windows: list[tuple[float, float]] = []
    for seg in sorted(cover, key=lambda s: s.start):
        if not seg.text.strip() or not math.isfinite(seg.start) or not math.isfinite(seg.end):
            return False
        if seg.end < seg.start:
            return False
        if windows and seg.start <= windows[-1][1] + .002:
            windows[-1] = (windows[-1][0], max(windows[-1][1], seg.end))
        else:
            windows.append((seg.start, seg.end))
    cursor = 0
    for seg in sorted(expected, key=lambda s: s.start):
        if not seg.text.strip():
            continue
        start, end = max(0.0, seg.start), max(0.0, seg.end, seg.start)
        while cursor < len(windows) and windows[cursor][1] < start - .002:
            cursor += 1
        if cursor == len(windows) or windows[cursor][0] > start + .002 or windows[cursor][1] < end - .002:
            return False
    return True


def save_srt_bilingual(top_srt: Path, bottom_srt: Path, out: Path) -> Path:
    """把「上层字幕」与「下层字幕」按时间合并成单条双语 SRT。

    双语堆叠使用标准 SRT 换行：文本写成「中文\nEnglish」。
    两层必须覆盖相同时间窗，按时间重叠配对，每条最多两行。
    较短的一层持续显示，避免另一层折行后出现单语字幕。
    输出沿用 utf-8-sig（BOM），与 save_srt 一致以兼容部分播放器。
    """
    top = sorted(parse_srt(top_srt.read_text(encoding="utf-8-sig", errors="replace")),
                 key=lambda seg: seg.start)
    bottom = sorted(parse_srt(bottom_srt.read_text(encoding="utf-8-sig", errors="replace")),
                    key=lambda seg: seg.start)
    if not top or not bottom or not segments_cover(top, bottom) or not segments_cover(bottom, top):
        raise AppError(Codes.TRANS_FAILED, "双语字幕存在缺失的语言或时间段",
                       suggestion="重试字幕翻译；如编辑过字幕，请补齐对应时间段后重新生成视频")
    rows: list[tuple[Segment | None, list[Segment]]] = []
    related_by_top: list[list[Segment]] = [[] for _ in top]
    unmatched: list[Segment] = []
    cursor = 0
    for b in bottom:
        while cursor < len(top) and top[cursor].end <= b.start:
            cursor += 1
        overlaps: list[tuple[int, float]] = []
        index = cursor
        while index < len(top) and top[index].start < b.end:
            overlap = min(top[index].end, b.end) - max(top[index].start, b.start)
            if overlap > 0:
                overlaps.append((index, overlap))
            index += 1
        if not overlaps:
            unmatched.append(b)
        elif len(overlaps) == 1:
            related_by_top[overlaps[0][0]].append(b)
        elif _disp_width(_normalize_text(b.text)) <= _MAX_LINE_W:
            for target, _ in overlaps:
                a = top[target]
                related_by_top[target].append(Segment(max(a.start, b.start), min(a.end, b.end), b.text))
        else:
            rest = _normalize_text(b.text)
            remaining = sum(duration for _, duration in overlaps)
            for i, (target, duration) in enumerate(overlaps):
                if i == len(overlaps) - 1:
                    part = rest
                else:
                    part, rest = _split_near(rest, duration / remaining)
                if not part:
                    part = _normalize_text(b.text)
                if part:
                    a = top[target]
                    related_by_top[target].append(Segment(
                        max(a.start, b.start), min(a.end, b.end), part))
                remaining -= duration
    rows.extend((a, related_by_top[i]) for i, a in enumerate(top))
    rows.extend((None, [b]) for b in unmatched)
    rows.sort(key=lambda row: (row[0] or row[1][0]).start)
    blocks: list[str] = []
    last_end = 0.0
    for a, related in rows:
        up = _segment_lines(a.text if a else "")
        down = _segment_lines("\n".join(b.text for b in related))
        count = max(len(up), len(down))
        if not count:
            continue
        seg = a or related[0]
        start = max(last_end, seg.start, 0.0)
        end = max(start, seg.end)
        for index in range(count):
            line = "\n".join(part for part in
                             (up[min(index * len(up) // count, len(up) - 1)] if up else "",
                              down[min(index * len(down) // count, len(down) - 1)] if down else "") if part)
            cue_start = start + (end - start) * index / count
            cue_end = start + (end - start) * (index + 1) / count
            blocks.append(f"{len(blocks) + 1}\n{fmt_ts(cue_start)} --> {fmt_ts(cue_end)}\n{line}\n")
        last_end = end
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(blocks), encoding="utf-8-sig")
    return out
