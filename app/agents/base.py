"""积木基座：共享上下文、统一执行包装与提示词工具。

M1 的 pipeline.step() 只存在于 _run_steps 内部，M2 把它提升为 run_agent：
任何一个"角色"（diagnostician/planner/…）只要实现 name + async run(ctx)，
就会被统一起 AgentRun（running → done/failed + error_code）、计时并落库。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import Settings
from ..llm.schemas import ChatMessage
from ..models.tables import AgentRun


class ChatLLM(Protocol):
    """任何 LLM 实现都满足的最小接口（FakeLLM 与 ChatClient 都是）。"""

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> str: ...


#: 拼进每一轮对话的系统约束
SYSTEM_PROMPT = (
    "你是一位耐心、严谨的 AI 开发入门导师。只输出合法 JSON，不要输出任何解释。"
    "回复里不允许出现任何 URL 链接。JSON 内容必须是中文。"
)


def build_messages(task: str, **fields: str | int) -> list[ChatMessage]:
    """构造一轮对话：系统约束 + 带任务标记与上下文参数的用户消息。

    - ``task`` 在消息里写成 ``任务标记：xxx``，让 FakeLLM 能确定性分发；
    - ``fields`` 拼成 ``标签：值`` 的多行参数区，发给真实模型时作为背景信息。
    """
    head = f"任务标记：{task}"
    body = "\n".join(f"{label}：{value}" for label, value in fields.items())
    return [
        ChatMessage.system(SYSTEM_PROMPT),
        ChatMessage.user(head if not body else f"{head}\n{body}"),
    ]


@dataclass
class AgentContext:
    """单个角色可触达的运行上下文（session_id / settings / llm / db / publish）。"""

    session_id: int
    settings: Settings
    llm: ChatLLM
    db: Session
    publish: Callable[[str, dict], Awaitable[int]]


class Agent(Protocol):
    """积木角色的对外契约：step 名 + 一次 run。"""

    name: str

    async def run(self, ctx: AgentContext) -> BaseModel | None: ...


def error_code_of(exc: Exception) -> str:
    """把任意异常映射成错误码：带 error_code 属性就用它，否则 internal。"""
    code = getattr(exc, "error_code", None)
    if isinstance(code, str) and code:
        return code
    return "internal"


async def run_agent(agent: Agent, ctx: AgentContext) -> BaseModel | None:
    """统一执行一个角色并记录 AgentRun（running → done / failed + error_code）。

    与 M1 step() 语义完全一致：先落 running，成功置 done 并记耗时；
    失败置 failed + error_code 后原样上抛，由调用方（run_generation）收尾会话。
    """
    run = AgentRun(session_id=ctx.session_id, step=agent.name, status="running")
    ctx.db.add(run)
    ctx.db.commit()
    start = time.perf_counter()
    try:
        result = await agent.run(ctx)
    except Exception as exc:  # noqa: BLE001 —— 统一收尾后上抛
        run.status = "failed"
        run.error_code = error_code_of(exc)
        run.duration_ms = int((time.perf_counter() - start) * 1000)
        ctx.db.commit()
        raise
    run.status = "done"
    run.duration_ms = int((time.perf_counter() - start) * 1000)
    ctx.db.commit()
    return result