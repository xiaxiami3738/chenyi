"""复核积木（M2 样板）：对结果做 schema 校验 + 失败重试 1 次。

与 grader 的思路一致：run() 走统一的 ask_llm_with_retry 解析成
:class:`ReviewResult`；真实复核流程（人工复核单、多版本比对等）留到
M5/M6 扩展，见 :meth:`review`。
"""

from __future__ import annotations

from ..llm.schemas import ReviewResult
from .base import AgentContext, build_messages
from .validator import ask_llm_with_retry


class Reviewer:
    """复核角色：对已产出内容做抽查复核（输出契约 ReviewResult）。"""

    name = "reviewer"

    def __init__(self, *, content: str = "", subject: str = "") -> None:
        self.content = content
        self.subject = subject

    async def run(self, ctx: AgentContext) -> ReviewResult:
        messages = build_messages("reviewer", 主题=self.subject, 内容=self.content)
        return await ask_llm_with_retry(ctx.llm, messages, ReviewResult)

    async def review(self, ctx: AgentContext) -> dict:
        """预留复核入口（M5/M6 扩展：接入人工复核单与版本比对）。"""
        # TODO(M5/M6)：实现真实复核流程
        return {"verdict": "", "comment": ""}