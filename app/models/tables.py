"""数据模型。

注意：这里**没有**任何存放 LLM Key（明文）的表或字段，这是设计约束而不是疏漏。

唯一的例外是 ``vault_entries``（M7.3 起）：访客**显式选择**服务端加密代存后，
用服务器主密钥（Fernet）加密成密文落库，且列名刻意避开 key/secret/credential
一类字面命名——详见 tests/unit/test_no_key_storage.py 的显式开白。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class LearnSession(Base):
    """一次学习会话（匿名 client_id 归属，不做登录）。"""

    __tablename__ = "learn_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    goal: Mapped[str] = mapped_column(Text)
    level: Mapped[str] = mapped_column(String(32), default="beginner")
    weak_points: Mapped[str] = mapped_column(Text, default="")
    daily_minutes: Mapped[int] = mapped_column(Integer, default=30)
    status: Mapped[str] = mapped_column(String(24), default="created", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    plans: Mapped[list[Plan]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="Plan.day_index"
    )
    links: Mapped[list[SessionLink]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    runs: Mapped[list[AgentRun]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class Plan(Base):
    """学习路径中的某一天。"""

    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("learn_sessions.id", ondelete="CASCADE"))
    day_index: Mapped[int] = mapped_column(Integer)
    topic: Mapped[str] = mapped_column(String(200))
    objective: Mapped[str] = mapped_column(Text, default="")
    deliverable: Mapped[str] = mapped_column(Text, default="")

    session: Mapped[LearnSession] = relationship(back_populates="plans")
    lessons: Mapped[list[Lesson]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )
    terms: Mapped[list[Term]] = relationship(back_populates="plan", cascade="all, delete-orphan")
    quiz_items: Mapped[list[QuizItem]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )


class Lesson(Base):
    """一天的讲解正文。"""

    __tablename__ = "lessons"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))
    body_md: Mapped[str] = mapped_column(Text, default="")
    code_snippets: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(24), default="pending")

    plan: Mapped[Plan] = relationship(back_populates="lessons")


class Term(Base):
    """名词卡：AI 开发术语的人话解释。"""

    __tablename__ = "terms"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"))
    term: Mapped[str] = mapped_column(String(120), index=True)
    definition: Mapped[str] = mapped_column(Text)
    analogy: Mapped[str] = mapped_column(Text, default="")
    pitfalls: Mapped[str] = mapped_column(Text, default="")
    topic: Mapped[str] = mapped_column(String(120), default="")

    plan: Mapped[Plan] = relationship(back_populates="terms")


class QuizItem(Base):
    """练习题。"""

    __tablename__ = "quiz_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16), default="choice")  # choice|blank|short
    question: Mapped[str] = mapped_column(Text)
    options: Mapped[list] = mapped_column(JSON, default=list)
    answer: Mapped[str] = mapped_column(Text, default="")
    rubric: Mapped[str] = mapped_column(Text, default="")

    plan: Mapped[Plan] = relationship(back_populates="quiz_items")
    attempts: Mapped[list[Attempt]] = relationship(
        back_populates="quiz_item", cascade="all, delete-orphan"
    )


class Attempt(Base):
    """一次作答与判分结果。"""

    __tablename__ = "attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    quiz_item_id: Mapped[int] = mapped_column(ForeignKey("quiz_items.id", ondelete="CASCADE"))
    user_answer: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    feedback: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    quiz_item: Mapped[QuizItem] = relationship(back_populates="attempts")


class Review(Base):
    """复盘报告。"""

    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("learn_sessions.id", ondelete="CASCADE"))
    completion_rate: Mapped[float] = mapped_column(Float, default=0.0)
    wrong_items: Mapped[list] = mapped_column(JSON, default=list)
    suggestions_md: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CuratedLink(Base):
    """人工策展的 GitHub 链接。

    防幻觉的关键：模型永远不产出 URL，只能按 topic 从这里取。
    """

    __tablename__ = "curated_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    topic: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(500), unique=True)
    why: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    stars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SessionLink(Base):
    """记录某次会话推荐过哪些链接。"""

    __tablename__ = "session_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("learn_sessions.id", ondelete="CASCADE"))
    curated_link_id: Mapped[int] = mapped_column(ForeignKey("curated_links.id", ondelete="CASCADE"))

    session: Mapped[LearnSession] = relationship(back_populates="links")


class AgentRun(Base):
    """多 Agent 编排的可观测性：每步耗时/状态/错误码（不记 prompt 原文，也不记 Key）。"""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("learn_sessions.id", ondelete="CASCADE"))
    step: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[LearnSession] = relationship(back_populates="runs")


class VaultEntry(Base):
    """服务端加密代存的 Key（M7.3，可选功能）。

    设计约束（与 test_no_key_storage.py 的显式开白一一对应）：
    - 这是全库唯一允许携带「Key 相关内容」的表；
    - 只存 ``cipher_blob`` 密文（Fernet token），绝不存明文；
    - 列名刻意避开 api_key/secret/password/credential/bearer 等字面命名；
    - 同一位访客（client_id）同一 scope（供应商 id 或 base_url）只保留一份。
    """

    __tablename__ = "vault_entries"
    __table_args__ = (UniqueConstraint("client_id", "scope", name="uq_vault_client_scope"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    scope: Mapped[str] = mapped_column(String(200), index=True)
    cipher_blob: Mapped[str] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )