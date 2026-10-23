"""诊断积木：目标 / 水平 / 薄弱点 → 诊断意见。

输入来自 LearnSession 上用户填的 goal/level/weak_points；
产出 :class:`DiagnosisResult`，正文只经 publish 走 SSE progress 事件，不入库
（会话表没有诊断正文列，且诊断是一次性过程信息）。
"""

from __future__ import annotations

from ..llm.schemas import DiagnosisResult
from .base import AgentContext, build_messages
from .validator import ask_llm_with_retry


class Diagnostician:
    """诊断角色：产出诊断意见并发 progress 事件。"""

    name = "diagnose"

    def __init__(self, *, goal: str = "", level: str = "", weak_points: str = "") -> None:
        self.goal = goal
        self.level = level
        self.weak_points = weak_points

    async def run(self, ctx: AgentContext) -> DiagnosisResult:
        messages = build_messages(
            "diagnose", 目标=self.goal, 水平=self.level, 薄弱点=self.weak_points
        )
        result = await ask_llm_with_retry(ctx.llm, messages, DiagnosisResult)
        await ctx.publish(
            "progress",
            {"step": "diagnose", "message": result.render_message()},
        )
        return result