"""统一错误体系（v3 错误码规范）：分类码 + 用户可读信息 + 重试/建议。"""
from __future__ import annotations

from dataclasses import dataclass, field


class Codes:
    # 分类前缀：E-NET 网络 / E-ENG 引擎站点 / E-AUTH 凭据 / E-DISK 磁盘
    #          E-ASR 识别 / E-TRANS 翻译 / E-RENDER 渲染 / E-CFG 配置 / E-DEP 依赖
    NET_TIMEOUT = "E-NET-001"
    NET_FAILED = "E-NET-002"
    ENG_UNSUPPORTED = "E-ENG-201"
    ENG_FAILED = "E-ENG-202"
    AUTH_NEED_COOKIES = "E-AUTH-301"
    TASK_CANCELLED = "E-TASK-402"
    DISK_FAILED = "E-DISK-401"
    ASR_FAILED = "E-ASR-502"
    ASR_NO_SPEECH = "E-ASR-503"
    TRANS_FAILED = "E-TRANS-601"
    RENDER_FAILED = "E-RENDER-701"
    CFG_INVALID = "E-CFG-801"
    DEP_MISSING = "E-DEP-901"


@dataclass
class AppError(Exception):
    """业务异常基类：所有可预期错误都用它抛出，前端直接消费 to_dict()。"""

    code: str
    message: str
    detail: str = ""
    retryable: bool = False
    suggestion: str = ""

    def __str__(self) -> str:  # 让日志里直接可读
        return f"[{self.code}] {self.message}" + (f"（{self.detail}）" if self.detail else "")

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
            "retryable": self.retryable,
            "suggestion": self.suggestion,
        }


def dep_missing(module: str, install: str, feature: str) -> AppError:
    """可选依赖缺失的友好错误。"""
    return AppError(
        code=Codes.DEP_MISSING,
        message=f"功能「{feature}」需要依赖 {module}，当前环境未安装",
        detail=f"ModuleNotFoundError: {module}",
        suggestion=f"执行 {install} 后重试",
    )
