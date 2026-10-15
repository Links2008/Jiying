"""对真实识别结果和人工逐字稿计算 CER/WER；不调用识别服务或下载模型。"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.subtitler import parse_srt  # noqa: E402


def edit_distance(reference, hypothesis) -> int:
    """精确的位并行 Levenshtein 距离，避免长逐字稿的二维矩阵内存。"""
    size = len(reference)
    if not size:
        return len(hypothesis)
    matches = {}
    for index, token in enumerate(reference):
        matches[token] = matches.get(token, 0) | (1 << index)
    mask, top = (1 << size) - 1, 1 << (size - 1)
    positive, negative, distance = mask, 0, size
    for token in hypothesis:
        equal = matches.get(token, 0)
        vertical = equal | negative
        horizontal = (((equal & positive) + positive) ^ positive) | equal
        plus = negative | ~(horizontal | positive)
        minus = positive & horizontal
        if plus & top:
            distance += 1
        elif minus & top:
            distance -= 1
        plus, minus = ((plus << 1) | 1) & mask, (minus << 1) & mask
        positive = (minus | ~(vertical | plus)) & mask
        negative = plus & vertical
    return distance


def normalized(text: str, language: str) -> str:
    text = unicodedata.normalize('NFKC', text).casefold()
    if language.startswith('zh'):
        from opencc import OpenCC
        text = OpenCC('t2s').convert(text)
    text = ''.join(ch for ch in text if not unicodedata.category(ch).startswith(('P', 'S')))
    return re.sub(r'\s+', ' ', text).strip()


def evaluate(reference: str, hypothesis: str, language: str) -> dict:
    reference, hypothesis = normalized(reference, language), normalized(hypothesis, language)
    chars, recognized = reference.replace(' ', ''), hypothesis.replace(' ', '')
    if not chars:
        raise ValueError('人工逐字稿不能为空')
    character_errors = edit_distance(chars, recognized)
    result = {'reference_characters': len(chars), 'recognized_characters': len(recognized),
              'character_errors': character_errors, 'cer': character_errors / len(chars),
              'wer': None, 'normalization': '全半角、大小写、标点符号和空白；中文统一简体'}
    if not language.startswith(('zh', 'ja', 'ko')):
        words, recognized_words = reference.split(), hypothesis.split()
        word_errors = edit_distance(words, recognized_words)
        result.update(reference_words=len(words), word_errors=word_errors, wer=word_errors / len(words))
    return result


def read_text(path: Path) -> str:
    text = path.read_text(encoding='utf-8-sig')
    if path.suffix.lower() == '.srt':
        cues = parse_srt(text)
        if not cues:
            raise ValueError('字幕文件没有可读取的时间轴和文字')
        return ' '.join(cue.text for cue in cues)
    if path.suffix.lower() == '.json':
        return ' '.join(segment['text'] for segment in json.loads(text)['segments'])
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description='对照人工逐字稿计算识别错误率；CER/WER 越低越好')
    parser.add_argument('--reference', type=Path, required=True, help='UTF-8 人工逐字稿或标准 SRT')
    parser.add_argument('--hypothesis', type=Path, required=True, help='实际生成的 SRT、转写 JSON 或文本')
    parser.add_argument('--language', default='zh', help='源语言，中文默认使用 CER')
    args = parser.parse_args()
    print(json.dumps(evaluate(read_text(args.reference), read_text(args.hypothesis), args.language),
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
