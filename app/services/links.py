"""策展链接服务。

设计要点（对"模型编造链接"的防御）：
- 链接只来自 ``data/seed/curated_links.json`` 这份人工表；
- 落库前强制校验：必须 https、必须属于允许的代码托管域名；
- 模型输出里出现的任何 URL 都不会被渲染 —— 后续只允许按 topic 取这里的记录。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents.sanitize import strip_urls as strip_urls  # 再导出，对外路径不变
from ..models.tables import CuratedLink

#: 只信任代码托管站点，避免策展表被塞进钓鱼链接
ALLOWED_HOSTS = frozenset({"github.com", "www.github.com", "gitee.com", "www.gitee.com"})


def is_allowed_url(url: str) -> bool:
    """https + 允许的托管域名。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.netloc.lower() in ALLOWED_HOSTS


def load_seed_links(seed_dir: Path) -> list[dict]:
    """读取种子文件并过滤掉不合规条目。"""
    path = seed_dir / "curated_links.json"
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw.get("links", raw) if isinstance(raw, dict) else raw
    out: list[dict] = []
    for item in items:
        url = str(item.get("url", "")).strip()
        topic = str(item.get("topic", "")).strip()
        title = str(item.get("title", "")).strip()
        if not (url and topic and title) or not is_allowed_url(url):
            continue
        out.append(
            {
                "topic": topic,
                "title": title,
                "url": url,
                "why": str(item.get("why", "")).strip(),
                "tags": [str(t) for t in item.get("tags", [])],
                "stars": item.get("stars"),
            }
        )
    return out


def seed_curated_links(session: Session, seed_dir: Path) -> int:
    """把种子写入数据库（按 url 幂等 upsert）。返回本次新增条数。"""
    entries = load_seed_links(seed_dir)
    if not entries:
        return 0

    existing = {row.url: row for row in session.scalars(select(CuratedLink)).all()}
    added = 0
    now = datetime.now(UTC)
    for entry in entries:
        row = existing.get(entry["url"])
        if row is None:
            session.add(
                CuratedLink(
                    topic=entry["topic"],
                    title=entry["title"],
                    url=entry["url"],
                    why=entry["why"],
                    tags=entry["tags"],
                    stars=entry["stars"],
                    last_checked_at=now,
                )
            )
            added += 1
        else:
            row.topic = entry["topic"]
            row.title = entry["title"]
            row.why = entry["why"]
            row.tags = entry["tags"]
    session.commit()
    return added


def links_for_topic(session: Session, topic: str | None = None, limit: int = 12) -> list[CuratedLink]:
    """按主题取链接；主题为空时返回全部。"""
    stmt = select(CuratedLink)
    if topic:
        stmt = stmt.where(CuratedLink.topic == topic)
    stmt = stmt.order_by(CuratedLink.id).limit(limit)
    return list(session.scalars(stmt).all())


def topics_with_links(session: Session) -> list[str]:
    """返回有链接的主题列表（去重、保持稳定顺序）。"""
    stmt = select(CuratedLink.topic).distinct().order_by(CuratedLink.topic)
    return [row for row in session.scalars(stmt).all()]