"""LLM 层的 Pydantic 模型。

两部分：
1. 对话消息 —— 与各家供应商交互的 OpenAI 兼容结构；
2. 生成结果 —— 流水线要求模型输出的 JSON 契约（结构化解析的边界）。
这里只描述"长什么样"，不含任何 Key 或请求头信息。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    """一条对话消息。角色只允许 OpenAI 兼容的三种。"""

    role: Literal["system", "user", "assistant"] = "user"
    content: str

    @classmethod
    def system(cls, content: str) -> ChatMessage:
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> ChatMessage:
        return cls(role="user", content=content)


# ── 生成结果契约：流水线以 JSON mode 让模型产出以下结构 ──────────────────


class DiagnosisResult(BaseModel):
    """诊断步骤的输出：一段人话摘要 + 建议重点。"""

    summary: str = Field(description="对用户目标与薄弱点的诊断摘要（中文）")
    focus: list[str] = Field(default_factory=list, description="建议优先补的重点")

    def render_message(self) -> str:
        """拼成 SSE progress 事件的展示文本。"""
        head = f"诊断完成：{self.summary}"
        if self.focus:
            head += "；建议重点：" + "、".join(self.focus)
        return head


class PlanDay(BaseModel):
    """学习路径中的某一天（规划输出）。"""

    day_index: int = Field(ge=1, description="从 1 开始的第几天")
    topic: str = Field(min_length=1, description="当天主题")
    objective: str = Field(default="", description="当天目标")
    deliverable: str = Field(default="", description="当天要交出的东西")


class PlanResult(BaseModel):
    """规划步骤的输出：若干天的学习路径。"""

    days: list[PlanDay] = Field(default_factory=list)


class TermCard(BaseModel):
    """名词卡（讲解步骤里随正文一起产出）。"""

    term: str = Field(min_length=1)
    definition: str = Field(default="")
    analogy: str = Field(default="")
    pitfalls: str = Field(default="")
    topic: str = Field(default="")


class QuizItemOut(BaseModel):
    """练习题（出题步骤输出）。答案/评分要点随题一起存库。"""

    kind: Literal["choice", "blank", "short"] = "choice"
    question: str = Field(min_length=1)
    options: list[str] = Field(default_factory=list, description="选择题选项")
    answer: str = Field(default="")
    rubric: str = Field(default="", description="简答题评分要点")

    @property
    def has_obvious_correct_option(self) -> bool:
        return bool(self.options) and bool(self.answer)


class ExplainResult(BaseModel):
    """讲解步骤的输出：当天讲义正文 + 名词卡。"""

    lesson_title: str = Field(min_length=1)
    lesson_body_md: str = Field(default="")
    code_snippets: list[str] = Field(default_factory=list)
    terms: list[TermCard] = Field(default_factory=list)


class QuizResult(BaseModel):
    """出题步骤的输出：当天练习题列表。"""

    quizzes: list[QuizItemOut] = Field(default_factory=list)