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

#: 契约提示的开头。真实模型需要知道目标字段结构才能稳定输出；
#: 固定字段名散列便于测试断言与日志排查。
CONTRACT_HEADING = "输出 JSON 只能包含以下字段，字段名必须与列表完全一致："


def contract_hint(model: type[_T]) -> str:
    """把 Pydantic 契约转成给模型的字段说明（不包含任何 Key/敏感信息）。

    字段列表来自 ``model_json_schema()``，一次生成覆盖全部 Agent。
    文案刻意不用「目标：/主题：/天数：」这类带冒号的标签式短语，
    避免干扰 FakeLLM 的任务分发正则（fake.py 按拼接文本提取参数）。
    """
    schema = model.model_json_schema()
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    lines = []
    for name, info in props.items():
        field_type = info.get("type", "any")
        if field_type == "array":
            field_type = f"array[{info.get('items', {}).get('type', 'any')}]"
        req = "必填" if name in required else "可选"
        desc = (info.get("description") or "").strip()
        lines.append(f'- "{name}"（{field_type}，{req}）{desc}'.rstrip())
    return CONTRACT_HEADING + "\n" + "\n".join(lines)


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
    """调用 LLM 并解析；解析失败追加修正提示重试 1 次。

    每次调用都会附带``contract_hint(model)``字段结构说明：
    真实模型需要看到目标契约才能稳定输出（M14 实测修复：live 模式下
    deepseek 曾因不知道字段名而输出结构不符导致 invalid_output）；
    FakeLLM 按「任务标记」分发，对追加的提示文本无感。

    仍失败时抛 :class:`OutputInvalidError`（error_code="invalid_output"），
    由其调用的 run_agent 记录 failed 并中止整个会话。
    """
    hint = contract_hint(model)
    first_messages = [*messages, ChatMessage.user(hint)]
    first_text = await llm.chat(first_messages, json_mode=True)
    try:
        return parse_struct(first_text, model)
    except OutputInvalidError:
        retry_messages = [
            *first_messages,
            ChatMessage.user(RETRY_HINT),
            ChatMessage.user(hint),
        ]
        retry_text = await llm.chat(retry_messages, json_mode=True)
        return parse_struct(retry_text, model)