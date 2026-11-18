"""复用下载会话，限量读取 MP4 索引确认原始流画质。"""
from __future__ import annotations

import json
import subprocess
import time
from fractions import Fraction

from ..media_info import find_ffprobe


def probe_mp4(ydl, url, headers):
    """只读取顶层原子头及 moov；超时或不支持 Range 时保留未知画质。"""
    from yt_dlp.networking import Request
    executable = find_ffprobe()
    if not executable:
        return {}
    deadline = time.monotonic() + 8
    budget = 2 * 1024 * 1024
    total_size = 0
    def read_range(offset, size):
        nonlocal budget, total_size
        remaining = deadline - time.monotonic()
        if remaining <= 0 or size > budget:
            raise ValueError('媒体信息读取超过预算')
        request = Request(url, headers=headers | {'Range': f'bytes={offset}-{offset+size-1}',
            'Accept-Encoding': 'identity'}, extensions={'timeout': min(3, remaining)})
        with ydl.urlopen(request) as response:
            if offset and (response.status != 206 or not response.headers.get(
                    'Content-Range', '').startswith(f'bytes {offset}-')):
                raise ValueError('服务器未返回指定分段')
            total = response.headers.get('Content-Range', '').rpartition('/')[-1]
            if total.isdigit():
                total_size = int(total)
            payload = response.read(size)
        budget -= len(payload)
        if len(payload) != size:
            raise ValueError('媒体索引不完整')
        return payload
    try:
        offset, ftyp, moov = 0, b'', b''
        for _ in range(12):
            header = read_range(offset, 16)
            size, kind = int.from_bytes(header[:4], 'big'), header[4:8]
            header_size = 16 if size == 1 else 8
            if size == 1:
                size = int.from_bytes(header[8:16], 'big')
            if size < header_size:
                return {}
            if kind in (b'ftyp', b'moov'):
                atom = read_range(offset, size)
                if kind == b'ftyp':
                    ftyp = atom
                else:
                    moov = atom
                    break
            offset += size
        if not moov:
            return {}
        result = subprocess.run([executable, '-v', 'error', '-show_streams', '-of', 'json',
            '-i', 'pipe:0'], input=ftyp + moov, capture_output=True,
            timeout=max(.1, deadline-time.monotonic()),
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        streams = json.loads(result.stdout).get('streams', [])
        video = next((s for s in streams if s.get('codec_type') == 'video'), {})
        audio = next((s for s in streams if s.get('codec_type') == 'audio'), {})
        if not video.get('width') or not video.get('height'):
            return {}
        metadata = {key: video[key] for key in ('width', 'height')}
        metadata.update(vcodec=video.get('codec_name'), acodec=audio.get('codec_name') or 'none')
        if total_size:
            metadata['filesize'] = total_size
        if video.get('duration'):
            metadata['duration'] = float(video['duration'])
        rate = video.get('avg_frame_rate') or video.get('r_frame_rate')
        try:
            metadata['fps'] = float(Fraction(rate)) if rate else 0
        except (ValueError, ZeroDivisionError):
            metadata['fps'] = 0
        return metadata
    except Exception:
        return {}  # 额外探测失败不能丢弃可下载的原始流，也不输出签名地址或凭据。
