"""LLM 输出校验与重试。

M2 约定：模型输出必须能解析成约定的 Pydantic 结构（"契约"，见 llm/schemas.py）。
解析失败（JSON 解析 / 字段校验）先原样重试 1 次，重试仍失败则抛
:class:`OutputInvalidError`（error_code="invalid_output"），由 run_agent 记 failed
并中止会话。FakeLLM 与 ChatClient 都走这一层，live / demo 行为一致。
"""

from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from ..llm.schemas import ChatMessage
from .base import ChatLLM
from .errors import OutputInvalidError

_T = TypeVar("_T", bound=BaseModel)

#: 去掉模型常见的 ```json ``` 代码围栏
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

#: 重试时追加的修正提示（不含"任务标记"，FakeLLM 仍按原消息分发）
RETRY_HINT = (
    "你上一步的输出不是合法 JSON 或不符合约定的结构。"
    "请重新输出：只要 JSON 本身，不要代码围栏、不要多余解释、不要 URL。"
)


def parse_struct(text: str, model: type[_T]) -> _T:
    """把 LLM 输出解析成目标结构；解析失败抛 OutputInvalidError。

    容忍 ```json``` 围栏与首尾空白；出错信息保留给日志/事件，不带 Key。
    """
    cleaned = _FENCE_RE.sub("", text.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise OutputInvalidError("模型输出的 JSON 无法解析") from exc
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise OutputInvalidError(f"模型输出不符合约定结构：{exc.errors()[:2]}") from exc


async def ask_llm_with_retry(
    llm: ChatLLM,
    messages: list[ChatMessage],
    model: type[_T],
) -> _T:
    """调用 LLM 并解析；解析失败追加一行修正提示重试 1 次。

    仍失败时抛 :class:`OutputInvalidError`（error_code="invalid_output"），
    由其调用的 run_agent 记录 failed 并中止整个会话。
    """
    first_text = await llm.chat(messages, json_mode=True)
    try:
        return parse_struct(first_text, model)
    except OutputInvalidError:
        retry_messages = [*messages, ChatMessage.user(RETRY_HINT)]
        retry_text = await llm.chat(retry_messages, json_mode=True)
        return parse_struct(retry_text, model)