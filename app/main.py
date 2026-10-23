"""应用入口：``uvicorn app.main:app``。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .api import meta_router, sessions_router
from .api.ratelimit import RateLimitMiddleware
from .config import BASE_DIR, Settings, get_settings
from .llm.redact import install_log_redaction, mask_secrets
from .models import Base, ensure_sqlite_parent, make_engine, make_session_factory
from .services import seed_curated_links

logger = logging.getLogger("chenyi")

TEMPLATES_DIR = BASE_DIR / "app" / "templates"
STATIC_DIR = BASE_DIR / "app" / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    install_log_redaction()
    settings: Settings = app.state.settings

    ensure_sqlite_parent(settings.database_url)
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    session = factory()
    try:
        added = seed_curated_links(session, settings.seed_dir)
    finally:
        session.close()

    app.state.engine = engine
    app.state.session_factory = factory
    logger.info(
        "chenyi 启动完成 mode=%s db=%s 新增策展链接=%d",
        settings.app_mode,
        settings.db_path,
        added,
    )
    try:
        yield
    finally:
        engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="AI 开发入门向导",
        description="让普通人快速接手 AI 开发：诊断 → 路径 → 讲解 → 练习 → 复盘，并附人工策展的 GitHub 链接。",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings
    #: 应用层并发信号量：同时进行的生成任务不超过 max_concurrency，超限 429
    app.state.semaphore = asyncio.Semaphore(settings.max_concurrency)
    #: 进程内 SSE 事件总线：session_id -> SessionEventBus（生成结束后即移除）
    app.state.live: dict[int, object] = {}
    #: 应用层速率限制：POST/PUT/DELETE 按 IP 滑动窗口（默认 10 次/分钟）
    app.add_middleware(
        RateLimitMiddleware,
        limit=settings.rate_limit_per_minute,
        trust_proxy=settings.trust_proxy_headers,
    )
    app.include_router(meta_router)
    app.include_router(sessions_router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals["version"] = __version__

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "demo": settings.demo,
                "app_mode": settings.app_mode,
                "max_input_chars": settings.max_input_chars,
                "rate_limit_per_minute": settings.rate_limit_per_minute,
            },
        )

    @app.get("/history", response_class=HTMLResponse, include_in_schema=False)
    def history(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "history.html",
            {
                "demo": settings.demo,
                "app_mode": settings.app_mode,
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常：%s", mask_secrets(str(exc)))
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal", "message": "服务内部错误，请稍后重试"}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        """Pydantic 校验失败统一为错误信封，前端好提示。"""
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "请求参数不合法",
                    "detail": mask_secrets(str(exc.errors())),
                }
            },
        )

    return app


app = create_app()