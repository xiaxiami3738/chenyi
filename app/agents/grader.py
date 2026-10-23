"""评估积木（M2 样板）：对作答做 schema 校验 + 失败重试 1 次。

M2 只搭「校验 + 重试」的样板：run() 走统一的 ask_llm_with_retry 解析成
:class:`GradeResult`。真正的评估流程（Attempt / Review 落库、多轮批改、
参考 rubrics 判分等）留到 M5/M6 扩展，见 :meth:`evaluate`。
"""

from __future__ import annotations

from ..llm.schemas import GradeResult
from .base import AgentContext, build_messages
from .validator import ask_llm_with_retry


class Grader:
    """评估角色：把一次作答交给 LLM 判分（输出契约 GradeResult）。"""

    name = "grader"

    def __init__(self, *, question: str = "", answer: str = "", rubric: str = "") -> None:
        self.question = question
        self.answer = answer
        self.rubric = rubric

    async def run(self, ctx: AgentContext) -> GradeResult:
        messages = build_messages(
            "grader", 题目=self.question, 作答=self.answer, 评分要点=self.rubric
        )
        return await ask_llm_with_retry(ctx.llm, messages, GradeResult)

    async def evaluate(self, ctx: AgentContext) -> dict:
        """预留评估入口（M5/M6 扩展：接入 Attempt / Review 落库与多轮批改）。"""
        # TODO(M5/M6)：实现真实评估流程
        return {"score": None, "comment": "", "suggestion": ""}