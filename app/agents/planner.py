"""规划积木：目标 / 薄弱点 / 每日分钟数 → N 天学习路径并落 Plan 表。

天数走确定性公式 :func:`plan_days`（不交给模型决定规模），主题和目标
由模型产出（Planner 之后模型仍可能编造 URL，落库前全部 strip_urls）。
"""

from __future__ import annotations

from ..llm.schemas import PlanResult
from ..models.tables import Plan
from .base import AgentContext, build_messages
from .sanitize import strip_urls
from .validator import ask_llm_with_retry


def plan_days(daily_minutes: int) -> int:
    """天数确定性公式：时间越紧天数越少（上限 7，下限 3）。"""
    return max(3, min(7, 600 // daily_minutes))


class Planner:
    """规划角色：把 N 天路径写入 Plan 表并发 progress 事件。"""

    name = "plan"

    def __init__(
        self,
        *,
        goal: str = "",
        weak_points: str = "",
        daily_minutes: int = 30,
    ) -> None:
        self.goal = goal
        self.weak_points = weak_points
        self.daily_minutes = daily_minutes

    async def run(self, ctx: AgentContext) -> PlanResult:
        days = plan_days(self.daily_minutes)
        messages = build_messages(
            "plan", 天数=days, 目标=self.goal, 薄弱点=self.weak_points
        )
        result = await ask_llm_with_retry(ctx.llm, messages, PlanResult)
        for day in result.days:
            ctx.db.add(
                Plan(
                    session_id=ctx.session_id,
                    day_index=day.day_index,
                    topic=strip_urls(day.topic),
                    objective=strip_urls(day.objective),
                    deliverable=strip_urls(day.deliverable),
                )
            )
        ctx.db.commit()
        await ctx.publish(
            "progress",
            {
                "step": "plan",
                "message": f"已为你规划 {len(result.days)} 天的学习路径（每天约 {self.daily_minutes} 分钟）。",
            },
        )
        return result