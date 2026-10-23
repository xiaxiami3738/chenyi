"""讲解积木：单天主题 → 讲义 + 名词卡，落 Lesson / Term 表并发 lesson / term 事件。

每个 plan 各跑一次（step 名 explain.{day_index}），Term 的表 topic 缺省回落到
plan.topic，保证名词卡与当天主题一致。
"""

from __future__ import annotations

from ..llm.schemas import ExplainResult
from ..models.tables import Lesson, Plan, Term
from .base import AgentContext, build_messages
from .sanitize import strip_urls
from .validator import ask_llm_with_retry


class Explainer:
    """讲解角色：当天产出讲义（Lesson）+ N 张名词卡（Term）。"""

    def __init__(self, plan: Plan) -> None:
        self.plan = plan
        self.name = f"explain.{plan.day_index}"

    async def run(self, ctx: AgentContext) -> ExplainResult:
        plan = self.plan
        messages = build_messages("explain", 主题=plan.topic, 天次=plan.day_index)
        result = await ask_llm_with_retry(ctx.llm, messages, ExplainResult)

        lesson = Lesson(
            plan_id=plan.id,
            title=strip_urls(result.lesson_title),
            body_md=strip_urls(result.lesson_body_md),
            code_snippets=[strip_urls(snippet) for snippet in result.code_snippets],
            status="ready",
        )
        ctx.db.add(lesson)

        terms: list[Term] = []
        for card in result.terms:
            term = Term(
                plan_id=plan.id,
                term=strip_urls(card.term),
                definition=strip_urls(card.definition),
                analogy=strip_urls(card.analogy),
                pitfalls=strip_urls(card.pitfalls),
                topic=strip_urls(card.topic) or plan.topic,
            )
            ctx.db.add(term)
            terms.append(term)
        ctx.db.commit()

        await ctx.publish(
            "lesson",
            {
                "day": plan.day_index,
                "topic": plan.topic,
                "title": lesson.title,
                "body_md": lesson.body_md,
                "code_snippets": lesson.code_snippets,
            },
        )
        for term in terms:
            await ctx.publish(
                "term",
                {
                    "day": plan.day_index,
                    "term": term.term,
                    "definition": term.definition,
                    "analogy": term.analogy,
                    "pitfalls": term.pitfalls,
                    "topic": term.topic,
                },
            )
        return result