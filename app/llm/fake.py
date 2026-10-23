"""FakeLLM：demo 模式的确定性本地"模型"。

特点：
- 无网络、零依赖：纯标准库，几毫秒返回；
- 确定性：同一输入永远得到同一输出（crc32 选池，不引入随机）；
- 中文内容：生成的 JSON 与真实供应商的形态一致，便于端到端演示与测试；
- 可调速：``delay_seconds`` 类属性，测试里 monkeypatch 成小延迟可观察并发。

流水线在 prompt 里带任务标记，FakeLLM 据此产出对应 JSON：:

    任务标记：diagnose|plan|explain|quiz
    天数：N          （plan 步骤）
    主题：xxx        （explain / quiz 步骤）
    目标：xxx        （diagnose 步骤）
"""

from __future__ import annotations

import asyncio
import json
import re
import zlib
from collections.abc import AsyncIterator

from .schemas import ChatMessage

_TASK_RE = re.compile(r"任务标记[:：]\s*(\S+)")
_DAYS_RE = re.compile(r"天数[:：]\s*(\d+)")
_TOPIC_RE = re.compile(r"主题[:：]\s*([^\n，,。]+)")
_GOAL_RE = re.compile(r"目标[:：]\s*([^\n]+)")

#: 规划步骤的主题池：两组不同风格的 7 天路径，按目标哈希取值
_DAY_TOPIC_SETS: tuple[tuple[str, ...], ...] = (
    (
        "大模型基础与工作原理",
        "提示工程入门",
        "上下文与 Token 机制",
        "RAG 检索增强概览",
        "Agent 智能体入门",
        "模型 API 接入实战",
        "综合项目演练与复盘",
    ),
    (
        "动手跑通第一个 AI 接口",
        "提示词设计进阶",
        "参数调优与温度控制",
        "向量与检索基础",
        "多步任务拆解",
        "工具调用与 Agent",
        "结业项目与答辩复盘",
    ),
)

#: 名词卡内容池（term/definition/analogy/pitfalls）
_TERM_POOL: tuple[dict[str, str], ...] = (
    {
        "term": "大模型",
        "definition": "参数量达到千亿级别、经过海量文本预训练的神经网络语言模型。",
        "analogy": "像一个读过海量书籍的图书馆员，能根据你的提问把相关段落组织成回答。",
        "pitfalls": "大模型不是数据库，回答可能过时或编造，需要核对来源。",
    },
    {
        "term": "Token",
        "definition": "模型处理文本的最小单位，约等于一个词或半个汉字。",
        "analogy": "像买菜的「按份计价」，模型按 Token 数量收费和限长。",
        "pitfalls": "中文一个汉字可能占 1~2 个 Token，凑字数会快速耗尽上下文。",
    },
    {
        "term": "提示工程",
        "definition": "通过设计输入提示词来引导模型输出符合预期的结果。",
        "analogy": "像给新同事写清晰的任务说明书，越具体越不容易跑偏。",
        "pitfalls": "不是玄学：先给角色与约束，再给示例，最后提问效果最好。",
    },
    {
        "term": "RAG",
        "definition": "检索增强生成：先从知识库检索相关内容，再让模型基于检索结果作答。",
        "analogy": "开卷考试：先翻资料（检索），再按资料答题（生成）。",
        "pitfalls": "检索质量决定回答质量，切分与召回做不好，答案会张冠李戴。",
    },
    {
        "term": "Agent",
        "definition": "能自主规划步骤、调用工具并逐步完成任务的智能体程序。",
        "analogy": "像带工具箱的员工：拆任务、选工具、干活、汇报。",
        "pitfalls": "自由度越高越不可控，需要限定步骤上限与权限边界。",
    },
)


def _pick(seed_text: str, pool: tuple | list) -> object:
    """用 seed_text 的 crc32 从池里确定性地取一个元素。"""
    if not pool:
        raise ValueError("内容池为空")
    return pool[zlib.crc32(seed_text.encode("utf-8")) % len(pool)]


def _topics_for(goal: str, days: int) -> list[str]:
    _set = _pick(goal or "默认", _DAY_TOPIC_SETS)
    return list(_set)[:days]


class FakeLLM:
    """确定性本地生成器，接口形状与 ChatClient 对齐（chat/stream）。"""

    #: 每次调用的模拟延迟（秒）。测试用 monkeypatch 可控。
    delay_seconds: float = 0.0

    def __init__(self) -> None:
        self.name = "fake"

    # ── 公共接口 ───────────────────────────────────────────────

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> str:
        """返回一段确定性 JSON 文本（纯本地生成，忽略 json_mode/max_tokens）。"""
        del json_mode, max_tokens
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        text = "\n".join(message.content for message in messages if message.content)
        payload = self._dispatch(text)
        return json.dumps(payload, ensure_ascii=False)

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """把 chat 的 JSON 结果切块吐出，模拟逐 token 流式。"""
        del json_mode, max_tokens
        text = "\n".join(message.content for message in messages if message.content)
        payload = self._dispatch(text)
        blob = json.dumps(payload, ensure_ascii=False)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        chunk_size = max(1, len(blob) // 10)
        for start in range(0, len(blob), chunk_size):
            yield blob[start : start + chunk_size]

    # ── 内部：按任务标记分派 ───────────────────────────────────

    def _dispatch(self, text: str) -> dict:
        match = _TASK_RE.search(text)
        task = match.group(1) if match else "diagnose"
        try:
            if task == "plan":
                return self._plan(text)
            if task == "explain":
                return self._explain(text)
            if task == "quiz":
                return self._quiz(text)
            return self._diagnose(text)
        except (ValueError, IndexError) as exc:  # 缺参时兜底成诊断
            return self._diagnose(f"{text} 目标：{exc}")

    def _diagnose(self, text: str) -> dict:
        goal_match = _GOAL_RE.search(text)
        goal = goal_match.group(1).strip() if goal_match else "学习 AI 开发"
        focus_pool = (
            "大模型工作原理",
            "提示工程",
            "RAG 检索增强",
            "Agent 智能体",
            "模型调用与 API",
        )
        focus = [_pick(goal, focus_pool)] if goal else []
        return {
            "summary": (
                f"根据你的目标「{goal}」，建议从基础概念与动手实践结合的方式推进。"
            ),
            "focus": focus,
        }

    def _plan(self, text: str) -> dict:
        days_match = _DAYS_RE.search(text)
        days = int(days_match.group(1)) if days_match else 7
        days = max(3, min(7, days))
        goal_match = _GOAL_RE.search(text)
        goal = goal_match.group(1).strip() if goal_match else "学习 AI 开发"
        topics = _topics_for(goal, days)
        return {
            "days": [
                {
                    "day_index": index,
                    "topic": topic,
                    "objective": f"掌握「{topic}」的核心概念并能动手实践",
                    "deliverable": f"完成「{topic}」相关的练习或小结",
                }
                for index, topic in enumerate(topics, start=1)
            ]
        }

    def _explain(self, text: str) -> dict:
        topic_match = _TOPIC_RE.search(text)
        topic = topic_match.group(1).strip() if topic_match else "大模型基础"
        terms = [_pick(f"{topic}#{i}", _TERM_POOL) for i in range(2)]
        return {
            "lesson_title": f"{topic}：从概念到实践",
            "lesson_body_md": (
                f"# {topic}\n\n"
                f"本课围绕「{topic}」展开：先建立整体认知，再通过示例上手。\n\n"
                "## 核心要点\n"
                f"- 理解「{topic}」的定位与适用场景；\n"
                "- 掌握最小可用示例的写法；\n"
                "- 知道常见的坑与排查思路。\n\n"
                "## 动手任务\n"
                "把示例代码跑通，并试着改一个参数观察输出变化。"
            ),
            "code_snippets": [
                "import openai  # 示例：调用模型\n"
                'resp = openai.chat.completions.create(model="demo", messages=[{"role": "user", "content": "你好"}])'
            ],
            "terms": terms,
        }

    def _quiz(self, text: str) -> dict:
        topic_match = _TOPIC_RE.search(text)
        topic = topic_match.group(1).strip() if topic_match else "大模型基础"
        seed = zlib.crc32(topic.encode("utf-8"))
        options = ["结论确定、可以当事实库使用", "按统计规律生成文本，可能编造", "只能处理英文", "不会犯错"]
        answer = options[seed % 2]  # 确定性选一个合理答案
        quizzes = [
            {
                "kind": "choice",
                "question": f"关于「{topic}」，以下哪个说法更准确？",
                "options": options,
                "answer": answer,
                "rubric": "理解大模型输出的概率性与幻觉风险。",
            },
            {
                "kind": "short",
                "question": f"用一句话说明「{topic}」在实际项目中的作用。",
                "options": [],
                "answer": "示例回答：用来把非结构化的输入整理成结构化、可复用的输出。",
                "rubric": "能说出输入输出转换这一核心即可得分。",
            },
        ]
        return {"quizzes": quizzes}