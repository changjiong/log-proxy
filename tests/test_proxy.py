from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.config import Settings
from app.main import create_app


@pytest.fixture
def upstream_app() -> FastAPI:
    app = FastAPI()

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [{"id": "gpt-5.4", "object": "model"}]}

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        payload = await request.json()
        auth = request.headers.get("Authorization")
        if auth != "Bearer upstream-secret":
            return JSONResponse(status_code=401, content={"error": {"message": "bad upstream key"}})
        if payload.get("stream"):

            async def events():
                yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
                yield b'data: {"choices":[{"delta":{"content":" world"}}],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n\n'
                yield b"data: [DONE]\n\n"

            return StreamingResponse(events(), media_type="text/event-stream")
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": payload.get("model"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        }

    return app


@pytest.fixture
def proxy_client(tmp_path: Path, upstream_app: FastAPI):
    async def setup():
        upstream_transport = httpx.ASGITransport(app=upstream_app)
        upstream_client = httpx.AsyncClient(transport=upstream_transport, base_url="http://upstream")
        settings = Settings(
            upstream_base_url="http://upstream/v1",
            upstream_api_key="upstream-secret",
            proxy_api_key="proxy-secret",
            admin_api_key="admin-secret",
            sqlite_path=str(tmp_path / "logs.sqlite3"),
            max_log_body_bytes=200_000,
        )
        app = create_app(settings, http_client=upstream_client)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        transport = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(transport=transport, base_url="http://proxy")
        return client, app, upstream_client, lifespan

    client, app, upstream_client, lifespan = asyncio.run(setup())
    try:
        yield client, app
    finally:
        async def teardown():
            await client.aclose()
            await lifespan.__aexit__(None, None, None)
            await upstream_client.aclose()

        asyncio.run(teardown())

def wait_for_log_tasks(app: FastAPI) -> None:
    async def _wait() -> None:
        for _ in range(20):
            tasks = list(app.state.log_tasks)
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0.01)

def wait_for_log_tasks(app: FastAPI) -> None:
    async def _wait() -> None:
        for _ in range(20):
            tasks = list(app.state.log_tasks)
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0.01)

    asyncio.run(_wait())


def test_models_proxy_and_auth(proxy_client):
    client, app = proxy_client
    res = asyncio.run(client.get("/v1/models", headers={"Authorization": "Bearer proxy-secret"}))
    assert res.status_code == 200
    assert res.json()["data"][0]["id"] == "gpt-5.4"
    wait_for_log_tasks(app)
    logs = asyncio.run(client.get("/logs", headers={"Authorization": "Bearer admin-secret"}))
    assert logs.status_code == 200
    assert logs.json()["data"][0]["path"] == "/v1/models"


def test_non_stream_chat_logs_request_and_response(proxy_client):
    client, app = proxy_client
    payload = {"model": "gpt-5.4", "messages": [{"role": "user", "content": "hello"}], "api_key": "should-redact"}
    res = asyncio.run(client.post("/v1/chat/completions", headers={"Authorization": "Bearer proxy-secret"}, json=payload))
    assert res.status_code == 200
    assert res.json()["choices"][0]["message"]["content"] == "hello"
    wait_for_log_tasks(app)

    logs = asyncio.run(client.get("/logs", headers={"Authorization": "Bearer admin-secret"})).json()["data"]
    detail = asyncio.run(client.get(f"/logs/{logs[0]['id']}", headers={"Authorization": "Bearer admin-secret"})).json()
    assert detail["model"] == "gpt-5.4"
    assert detail["status_code"] == 200
    assert "should-redact" not in detail["request_body"]
    assert "chatcmpl-test" in detail["response_body"]
    assert detail["usage_json"]["total_tokens"] == 6
    auth = detail["request_headers_json"].get("authorization") or detail["request_headers_json"].get("Authorization")
    assert auth is not None and auth.startswith("Bearer ***")


def test_stream_chat_is_forwarded_and_logged(proxy_client):
    client, app = proxy_client
    payload = {"model": "gpt-5.4", "messages": [{"role": "user", "content": "hello"}], "stream": True}

    async def stream_request():
        chunks = []
        async with client.stream("POST", "/v1/chat/completions", headers={"Authorization": "Bearer proxy-secret"}, json=payload) as res:
            assert res.status_code == 200
            async for chunk in res.aiter_bytes():
                chunks.append(chunk)
        return b"".join(chunks).decode()

    body = asyncio.run(stream_request())
    assert "hello" in body
    assert "[DONE]" in body
    wait_for_log_tasks(app)

    logs = asyncio.run(client.get("/logs", headers={"Authorization": "Bearer admin-secret"})).json()["data"]
    detail = asyncio.run(client.get(f"/logs/{logs[0]['id']}", headers={"Authorization": "Bearer admin-secret"})).json()
    assert detail["stream"] is True
    assert detail["assembled_response"] == "hello world"
    assert "data:" in detail["stream_chunks"]
    assert detail["usage_json"]["total_tokens"] == 7


def test_rejects_bad_proxy_key(proxy_client):
    client, _ = proxy_client
    res = asyncio.run(client.get("/v1/models", headers={"Authorization": "Bearer wrong"}))
    assert res.status_code == 401
