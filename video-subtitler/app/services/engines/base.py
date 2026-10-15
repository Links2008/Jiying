"""站点引擎抽象：探测（列出可用分辨率）+ 下载。

引擎只关心「这个站点怎么下」，协议级多线程（分段/分片）由下载内核处理。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FormatInfo:
    """一条可下载的视频格式；不同帧率和动态范围保留独立选项。"""

    format_id: str
    label: str            # 展示用，如 "1080p"
    height: int = 0
    fps: float = 0.0
    ext: str = ""
    filesize: int = 0     # 字节；未知为 0
    vcodec: str = ""
    acodec: str = ""
    protocol: str = "https"
    dynamic_range: str = ""
    size_is_estimate: bool = False  # filesize > 0 时标记是否含估算流体积

    @property
    def protocol_class(self) -> str:
        """协议归类：直链（可 aria2c 分段）vs 分片流（yt-dlp -N 并发）。"""
        return "fragments" if self.protocol.startswith(("m3u8", "dash")) else "direct"


@dataclass
class ProbeResult:
    """探测结果：站点元信息 + 分辨率列表（从高到低）。"""

    video_id: str
    title: str
    duration: float = 0.0
    webpage_url: str = ""
    thumbnail: str = ""
    formats: list[FormatInfo] = field(default_factory=list)
    quality_notice: str = ""
    auth_status: str = "none"  # none / loaded（已读取 SESSDATA）/ unknown

    def best_format(self) -> FormatInfo | None:
        return self.formats[0] if self.formats else None


@dataclass
class DirectTarget:
    """单一直达流（音视频合一）：aria2c 分段加速的输入。"""

    url: str
    filename: str
    headers: dict = field(default_factory=dict)
    kind: str = "merged"     # merged | video | audio（拆流下载时使用）
    duration: float = 0.0    # 来源声明的完整时长，直链完成后用于校验


@dataclass
class DownloadProgress:
    """统一进度事件（yt-dlp hook / aria2c tellStatus 都适配到它）。"""

    fraction: float = 0.0        # 0~1
    speed_bps: float = 0.0
    eta: float | None = None
    backend: str = "native"
    note: str = ""               # 阶段说明，如 "下载视频流"


def has_download_bytes(dest_dir: Path) -> bool:
    """进度尚未上报时，也不能因已有下载残片而从头换引擎。"""
    try:
        return dest_dir.exists() and any(
            p.is_file() and p.suffix not in (".aria2", ".ytdl") and p.stat().st_size > 0
            for p in dest_dir.rglob("*"))
    except OSError:
        return True  # 无法确认残片状态时保守地不重新下载


class EngineProvider(ABC):
    """引擎接口。所有实现必须无状态、可被路由器顺序复用。"""

    name: str = "base"

    def available(self) -> bool:
        """引擎依赖的外部程序是否存在；缺失时路由器直接跳过。"""
        return True

    @abstractmethod
    def probe(self, url: str) -> ProbeResult | None:
        """解析链接。返回 None 表示「本引擎不支持该站点」（交给下一个引擎）；
        站点/网络错误则抛 AppError。"""

    @abstractmethod
    def download(self, url: str, resolution: str, dest_dir: Path,
                 on_progress=None) -> list[Path]:
        """下载并返回产物文件（合并后的视频，或 [视频, 音频] 两个文件）。

        resolution: "best" | "1080p" | 探测结果中的 format_id
        on_progress: callable(DownloadProgress)
        """
