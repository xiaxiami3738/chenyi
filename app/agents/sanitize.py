"""文本清洗：剔除模型输出里的任何 URL。

设计来源：M1 里 strip_urls 定义在 services/links.py，M2 为了让六角色积木
不依赖 services 层（避免 agents ↔ services 循环导入），把这段纯文本规则
下沉到这里；services/links.py 再原样导出，对外导入路径 app.services.links.strip_urls 不变。
"""

from __future__ import annotations

import re

#: 模型输出里的任意 URL（含 markdown 链接语法 [文字](url)）
_URL_PATTERN = re.compile(r"\[([^\]]*)\]\(https?://[^)\s]+\)|https?://[^\s\"'<>）)】\]]+")


def _drop_url(match: re.Match) -> str:
    """markdown 链接保留文字、去掉 URL；裸 URL 整体删除。"""
    return match.group(1) if match.group(1) is not None else ""


def strip_urls(text: str) -> str:
    """从文本里剔除一切 URL —— 防模型编造链接的兜底。

    真正的链接只能来自人工策展表（``curated_links``），
    无论模型输出了什么 URL，落到库/事件里之前都必须在这里删掉。
    """
    return _URL_PATTERN.sub(_drop_url, text)