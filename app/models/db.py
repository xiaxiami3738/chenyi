"""数据库引擎与会话工厂（SQLAlchemy 2.0，SQLite 起步）。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def make_engine(database_url: str) -> Engine:
    """创建引擎。SQLite 需要放开 check_same_thread（FastAPI 线程池）。"""
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args, future=True)

    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _record):  # pragma: no cover - 驱动回调
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def ensure_sqlite_parent(database_url: str) -> None:
    """确保 SQLite 文件所在目录存在。"""
    if not database_url.startswith("sqlite:///"):
        return
    raw = database_url.removeprefix("sqlite:///")
    Path(raw).parent.mkdir(parents=True, exist_ok=True)


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """在 FastAPI 依赖中使用的会话上下文。"""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()