"""出题积木：单天主题 → 练习题，落 QuizItem 表并逐条发 quiz 事件。

quiz 事件需要携带入库后的 quiz_id，因此每个 item 先 add + flush 再 publish；
commit 放在整批之后，保证要么全部落库要么全部回滚。
"""

from __future__ import annotations

from ..llm.schemas import QuizResult
from ..models.tables import Plan, QuizItem
from .base import AgentContext, build_messages
from .sanitize import strip_urls
from .validator import ask_llm_with_retry


class Quizmaster:
    """出题角色：当天产出练习题（QuizItem），逐条发 quiz 事件。"""

    def __init__(self, plan: Plan) -> None:
        self.plan = plan
        self.name = f"quiz.{plan.day_index}"

    async def run(self, ctx: AgentContext) -> QuizResult:
        plan = self.plan
        messages = build_messages("quiz", 主题=plan.topic, 天次=plan.day_index)
        result = await ask_llm_with_retry(ctx.llm, messages, QuizResult)

        for item in result.quizzes:
            item_row = QuizItem(
                plan_id=plan.id,
                kind=item.kind,
                question=strip_urls(item.question),
                options=[strip_urls(option) for option in item.options],
                answer=strip_urls(item.answer),
                rubric=strip_urls(item.rubric),
            )
            ctx.db.add(item_row)
            ctx.db.flush()
            await ctx.publish(
                "quiz",
                {
                    "day": plan.day_index,
                    "quiz_id": item_row.id,
                    "kind": item_row.kind,
                    "question": item_row.question,
                    "options": item_row.options,
                    "answer": item_row.answer,
                    "rubric": item_row.rubric,
                },
            )
        ctx.db.commit()
        return result