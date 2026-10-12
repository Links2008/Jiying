"""配置系统：内置默认值 + config.yaml 深合并（点路径读取）。"""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

from .paths import default_config_path  # noqa: F401  (re-export 便于调用方)
from . import paths as _paths


def default_config_path() -> Path:
    return _paths.default_config_path()


# 默认配置 —— 与架构 v3 的 Provider 注册中心一一对应
DEFAULTS: dict = {
    "engines": {
        # 引擎降级链：先 yt-dlp，失败依次换 you-get / lux
        "order": ["ytdlp", "youget", "lux"],
    },
    "download": {
        "backend": "adaptive",           # native | aria2c | adaptive
        "fragments_concurrency": 16,     # yt-dlp -N，分片流并发数（YouTube/B站主力加速）
        "speed_floor_kbps": 500,         # 速度看门狗阈值
        "subprocess_timeout": 1800,      # 备用引擎（you-get/lux）单次下载超时（秒）
        "rate_limit": "",                # 全局限速，如 "5M"；空 = 不限
        "proxy_mode": "system",          # system 跟随系统 | direct 强制直连 | custom 自定义
        "custom_proxy": "",              # proxy_mode=custom 时的代理地址
        "aria2c": {
            # 打包版 bin/ 内置 aria2c：服务启动时自动拉起，不可达自动降级原生下载
            "enabled": True,
            "rpc_url": "http://127.0.0.1:6800/jsonrpc",
            "secret": "",                # aria2c --rpc-secret 对应值
            "poll_interval": 0.5,        # tellStatus 轮询间隔（秒）
            "max_conn_per_server": 16,
            "split": 16,
            "min_split_size": "1M",
        },
    },
    "transcriber": {
        "provider": "faster-whisper",    # faster-whisper（本地离线）| cloud（云 API）
        "model": "small",                # tiny/base/small/medium/large-v3
        "device": "auto",                # auto | cpu | cuda
        "compute_type": "auto",          # auto → cuda 用 int8_float16，cpu 用 int8
        # 模型仓库端点：huggingface.co 国内不可达，默认走公共镜像 hf-mirror.com；
        # config.yaml 写 hf_endpoint: "" 可覆盖回官方直连（服务启动时读一次）
        "hf_endpoint": "https://hf-mirror.com",
        "cloud": {                       # provider=cloud 时生效（镜像 translator.llm）
            "vendor": "siliconflow",     # deepseek | openai | siliconflow | zhipu | custom
            "base_url": "",              # 留空则按 vendor 取预设地址
            "api_key": "",
            "model": "",                 # 留空则按 vendor 取预设模型
            "timeout": 300,              # 单次请求超时（秒），长音频需放宽
            "language": "",              # 留空 = 自动检测（任务源语言优先）
        },
    },
    "translator": {
        "provider": "null",              # null（仅源语言）| llm（OpenAI 兼容接口）
        "llm": {
            "vendor": "deepseek",        # deepseek | openai | siliconflow | zhipu | custom
            "base_url": "",              # 留空则按 vendor 取预设地址
            "model": "",                 # 留空则按 vendor 取预设模型
            "api_key": "",
            "batch_size": 20,
            "timeout": 60,
        },
    },
    "render": {
        "default": "softmux",            # softmux 软封装 | burn 硬字幕烧录
        "font_size": 18,
        "font_name": "",                 # 硬字幕字体，如 Microsoft YaHei；空 = libass 默认
    },
    # 留空则自动探测；keep_source_video=false 时任务完成后连源片一并回收
    # （代价：无法再"按此字幕重新封装"，改字幕需重新下载）
    "paths": {"ffmpeg": "", "aria2c": "", "yt_dlp": "",
              "keep_source_video": True},
    "cookies": "",                       # Netscape 格式 cookies.txt，下载会员内容用
    "cookies_from_browser": "",          # 直读浏览器登录态（chrome/edge/firefox）；4K/会员清晰度用
    "proxy": "",                         # 如 http://127.0.0.1:7890
}


def deep_merge(base: dict, override: dict) -> dict:
    """递归合并 override 到 base（base 被就地修改），dict 深合并、其余直接覆盖。"""
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


class Config:
    """配置读取封装：config.get("download.aria2c.split") 点路径取值。"""

    def __init__(self, data: dict):
        self._data = data

    def get(self, dotted: str, default=None):
        node: object = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value) -> None:
        """运行时覆盖（CLI 参数优先于配置文件）。"""
        parts = dotted.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    @property
    def raw(self) -> dict:
        return self._data


def _migrate_legacy_llm(user: dict) -> None:
    """一次性迁移：旧版 config.yaml 的 translator.llm 没有 vendor 键。

    旧版把智谱地址/模型固化写进了文件，而现在的取值规则是「显式 base_url 优先于
    厂商预设」，残留值会盖过用户选的厂商。缺少 vendor 即判定为旧版，清掉固化值，
    交回厂商预设（默认 deepseek）接管。
    """
    translator = user.get("translator")
    if not isinstance(translator, dict):
        return
    llm = translator.get("llm")
    if not isinstance(llm, dict) or "vendor" in llm:
        return
    llm["vendor"] = "deepseek"
    llm["base_url"] = ""
    llm["model"] = ""


def load_config(path: str | Path | None = None) -> Config:
    """加载配置：默认值 ← config.yaml 深合并。文件不存在时用纯默认值。"""
    data = copy.deepcopy(DEFAULTS)
    p = Path(path) if path else default_config_path()
    if p.exists():
        with open(p, encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
        _migrate_legacy_llm(user)
        deep_merge(data, user)
    return Config(data)
