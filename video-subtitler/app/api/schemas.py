"""请求模型。字段与默认值和桌面协议保持一致。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class TaskIn(BaseModel):
    url: str
    resolution: str = "best"
    langs: list[str] = []
    recognize: bool = False
    translate: bool = False
    burn: bool = False
    output_dir: str = ""      # 自定义输出目录（空 = 默认数据目录）
    font_name: str = ""       # 硬字幕字体（空 = 配置默认）
    font_size: int = 0        # 硬字幕字号（0 = 配置默认）
    position: str = "bottom"  # 字幕位置 bottom/middle/top
    source_lang: str = ""     # 源语言（空 = 自动检测）
    outline: int = 1          # 硬字幕黑色描边粗细（0-8）
    bilingual: bool = False   # 双语上下堆叠烧录（中文在上、英文在下）
    person_notes: bool = False  # 根据对白中的英文人名生成右上角注释
    audio_subtitles: bool | None = None  # 未提供时沿用旧客户端的提交语义
    color: str = "#FFFFFF"
    vertical_offset: int = 0
    horizontal_offset: int = 0
    enhance: bool = False     # 仅用于拒绝旧客户端的超分请求


class ProbeIn(BaseModel):
    url: str
    force_refresh: bool = False


class BilibiliLoginPollIn(BaseModel):
    session_id: str


class ClearDoneIn(BaseModel):
    with_files: bool = False   # true = 连带删除登记产物（同一套安全边界）
    kind: str = ""              # subtitle/video = 只删除该类文件，保留记录
    task_ids: list[str] | None = None  # 显式列表仅删除确认快照；空列表不删除任何任务


class SubtitleIn(BaseModel):
    text: str


class DownloadConfigIn(BaseModel):
    proxy_mode: str | None = None        # system | direct | custom
    custom_proxy: str | None = None
    fragments_concurrency: int | None = None
    aria2c_enabled: bool | None = None
    cookies: str | None = None            # Netscape cookies.txt 路径（会员/4K 用）
    cookies_from_browser: str | None = None  # 直读浏览器登录态：chrome/edge/firefox
    max_concurrent: int | None = None    # 同时下载数（任务并发上限）
    model: str | None = None             # whisper: tiny/base/small/medium/large-v3
    transcriber_provider: str | None = None  # faster-whisper | cloud
    asr_vendor: str | None = None        # openai/siliconflow/zhipu/custom
    asr_base_url: str | None = None      # OpenAI 兼容接口根地址
    asr_api_key: str | None = None
    asr_model: str | None = None
    asr_hotwords: str | None = Field(default=None, max_length=2000)
    translator_provider: str | None = None  # null | llm
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_vendor: str | None = None        # deepseek/openai/siliconflow/zhipu/custom
    llm_base_url: str | None = None
    llm_domain_context: str | None = Field(default=None, max_length=2000)
    llm_terminology_notes: str | None = Field(default=None, max_length=2000)
