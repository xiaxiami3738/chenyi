"""Agent 积木层：六个可独立复用、可单测的角色 + 统一执行包装。

M1 把编排逻辑都写在 services/pipeline.py 的 _run_steps 里，M2 拆分为
diagnostician/planner/explainer/quizmaster/grader/reviewer 六块积木。
错误码与文本清洗等公共能力也沉到此层，services.pipeline 再原样导出，
保持 app.services.* 的对外导入路径不变。
"""

from .base import Agent, AgentContext, build_messages, error_code_of, run_agent
from .diagnostician import Diagnostician
from .errors import OutputInvalidError, PipelineError
from .explainer import Explainer
from .grader import Grader
from .planner import Planner, plan_days
from .quizmaster import Quizmaster
from .reviewer import Reviewer
from .sanitize import strip_urls
from .validator import ask_llm_with_retry, parse_struct

__all__ = [
    "Agent",
    "AgentContext",
    "Diagnostician",
    "Explainer",
    "Grader",
    "OutputInvalidError",
    "PipelineError",
    "Planner",
    "Quizmaster",
    "Reviewer",
    "ask_llm_with_retry",
    "build_messages",
    "error_code_of",
    "plan_days",
    "parse_struct",
    "run_agent",
    "strip_urls",
]