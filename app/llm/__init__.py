"""LLM 接入层：供应商预设 + 统一客户端 + schema 契约 + demo 用 FakeLLM。"""

from .client import (
    ChatClient,
    LLMClientError,
    LLMConfig,
    config_from_settings,
    validate_base_url,
)
from .fake import FakeLLM
from .providers import PRESET_BY_ID, PRESETS, ProviderPreset, list_presets
from .schemas import (
    ChatMessage,
    DiagnosisResult,
    ExplainResult,
    GradeResult,
    PlanDay,
    PlanResult,
    QuizItemOut,
    QuizResult,
    ReviewResult,
    TermCard,
)

__all__ = [
    "PRESETS",
    "PRESET_BY_ID",
    "ProviderPreset",
    "list_presets",
    "ChatClient",
    "LLMConfig",
    "LLMClientError",
    "config_from_settings",
    "validate_base_url",
    "FakeLLM",
    "ChatMessage",
    "DiagnosisResult",
    "ExplainResult",
    "GradeResult",
    "PlanDay",
    "PlanResult",
    "QuizItemOut",
    "QuizResult",
    "ReviewResult",
    "TermCard",
]