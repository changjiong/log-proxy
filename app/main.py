from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request

from .config import Settings, get_settings
from .database import LogDB
from .proxy import proxy_request, require_admin_auth


def create_app(
    settings: Settings | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
    log_db: LogDB | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await app.state.log_db.init()
        try:
            yield
        finally:
            if app.state.log_tasks:
                await asyncio.gather(*app.state.log_tasks, return_exceptions=True)
            if not app.state.external_http_client:
                await app.state.http_client.aclose()

    app = FastAPI(
        title="OpenAI-compatible Log Proxy",
        version="0.1.0",
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.log_db = log_db or LogDB(settings.sqlite_path)
    app.state.external_http_client = http_client is not None
    app.state.http_client = http_client or httpx.AsyncClient(
        timeout=httpx.Timeout(settings.request_timeout_seconds),
        limits=httpx.Limits(max_keepalive_connections=100, max_connections=200),
    )
    app.state.log_tasks: set[asyncio.Task] = set()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": settings.service_name}

    @app.get("/logs")
    async def list_logs(request: Request, limit: int = 50, offset: int = 0):
        require_admin_auth(request, settings)
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        return {"data": await app.state.log_db.list_logs(limit=limit, offset=offset)}

    @app.get("/logs/{request_id}")
    async def get_log(request: Request, request_id: str):
        require_admin_auth(request, settings)
        log = await app.state.log_db.get_log(request_id)
        if log is None:
            raise HTTPException(status_code=404, detail="Log not found")
        return log

    @app.api_route("/v1", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def proxy_v1_root(request: Request):
        return await proxy_request(request, "")

    @app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def proxy_v1_path(request: Request, path: str):
        return await proxy_request(request, path)

    return app


app = create_app()
