from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.datastructures import Headers

from .config import Settings
from .database import LogDB, RequestLog, utc_now_iso
from .redaction import body_to_log_text, json_dumps, redact_headers, redact_json, truncate_text, try_load_json_bytes

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
}


def extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.strip().split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return authorization.strip()


def require_proxy_auth(request: Request, settings: Settings) -> None:
    if not settings.proxy_api_key:
        return
    expected = settings.proxy_api_key.get_secret_value()
    actual = extract_bearer_token(request.headers.get("Authorization"))
    if actual != expected:
        raise HTTPException(status_code=401, detail="Invalid proxy API key")


def require_admin_auth(request: Request, settings: Settings) -> None:
    if not settings.admin_api_key:
        raise HTTPException(status_code=403, detail="Admin log endpoints are disabled. Set ADMIN_API_KEY to enable them.")
    expected = settings.admin_api_key.get_secret_value()
    actual = extract_bearer_token(request.headers.get("Authorization"))
    if actual != expected:
        raise HTTPException(status_code=401, detail="Invalid admin API key")


def build_upstream_url(settings: Settings, path: str, query: str | None) -> str:
    url = f"{settings.upstream_base_url}/{path.lstrip('/')}" if path else settings.upstream_base_url
    if query:
        url = f"{url}?{query}"
    return url


def filter_inbound_headers(headers: Headers, settings: Settings, request_id: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers.items():
        key_l = key.lower()
        if key_l in HOP_BY_HOP_HEADERS or key_l == "host":
            continue
        if settings.upstream_api_key and key_l == "authorization":
            # Replace the caller/new-api key with the dedicated upstream/CPA key.
            continue
        # Avoid gzip ambiguity in raw body logs. The upstream may still send SSE uncompressed.
        if key_l == "accept-encoding":
            continue
        out[key] = value
    if settings.upstream_api_key:
        out["Authorization"] = f"Bearer {settings.upstream_api_key.get_secret_value()}"
    out["X-Log-Proxy-Request-ID"] = request_id
    return out


def filter_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in HOP_BY_HOP_HEADERS:
            continue
        out[key] = value
    return out


def get_client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    if request.client:
        return request.client.host
    return None


def parse_request_metadata(body: bytes) -> tuple[str | None, bool]:
    parsed = try_load_json_bytes(body)
    if isinstance(parsed, dict):
        model = parsed.get("model")
        stream = bool(parsed.get("stream", False))
        return str(model) if model is not None else None, stream
    return None, False


def extract_usage_from_json_bytes(body: bytes) -> str | None:
    parsed = try_load_json_bytes(body)
    if isinstance(parsed, dict) and "usage" in parsed:
        return json_dumps(parsed.get("usage"))
    return None


def extract_error_from_response(body: bytes, status_code: int) -> str | None:
    if status_code < 400:
        return None
    parsed = try_load_json_bytes(body)
    if parsed is not None:
        return json_dumps(parsed)
    if body:
        return json_dumps({"status_code": status_code, "body": body.decode("utf-8", errors="replace")})
    return json_dumps({"status_code": status_code})


class SSEAssembler:
    """Collects raw SSE bytes and extracts readable text where possible."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self._raw_parts: list[str] = []
        self._raw_bytes = 0
        self._buffer = ""
        self._content_parts: list[str] = []
        self.usage: Any | None = None
        self.finish_reason: str | None = None

    def add_chunk(self, chunk: bytes, *, keep_raw: bool) -> None:
        text = chunk.decode("utf-8", errors="replace")
        if keep_raw and self._raw_bytes < self.max_bytes:
            remaining = self.max_bytes - self._raw_bytes
            encoded = text.encode("utf-8")
            if len(encoded) > remaining:
                text_for_raw = encoded[:remaining].decode("utf-8", errors="ignore") + f"\n...[truncated at {self.max_bytes} bytes]"
                self._raw_parts.append(text_for_raw)
                self._raw_bytes = self.max_bytes
            else:
                self._raw_parts.append(text)
                self._raw_bytes += len(encoded)
        self._buffer += text
        self._parse_lines()

    def _parse_lines(self) -> None:
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.strip("\r")
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                payload = json.loads(data)
            except Exception:
                continue
            if isinstance(payload, dict):
                if payload.get("usage") is not None:
                    self.usage = payload.get("usage")
                choices = payload.get("choices")
                if isinstance(choices, list):
                    for choice in choices:
                        if not isinstance(choice, dict):
                            continue
                        if choice.get("finish_reason"):
                            self.finish_reason = str(choice.get("finish_reason"))
                        delta = choice.get("delta")
                        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                            self._content_parts.append(delta["content"])
                        message = choice.get("message")
                        if isinstance(message, dict) and isinstance(message.get("content"), str):
                            self._content_parts.append(message["content"])
                # Responses API streaming sometimes uses output text delta events.
                if isinstance(payload.get("delta"), str):
                    self._content_parts.append(payload["delta"])

    @property
    def raw_text(self) -> str | None:
        return "".join(self._raw_parts) if self._raw_parts else None

    @property
    def assembled_text(self) -> str | None:
        text = "".join(self._content_parts)
        return text or None

    @property
    def usage_json(self) -> str | None:
        return json_dumps(self.usage) if self.usage is not None else None


def spawn_log_task(app: FastAPI, coro: Any) -> None:
    task = asyncio.create_task(coro)
    app.state.log_tasks.add(task)
    task.add_done_callback(app.state.log_tasks.discard)


async def write_log_safely(db: LogDB, log: RequestLog) -> None:
    try:
        await db.insert(log)
    except Exception:
        # Logging must never break the proxy path.
        pass


def create_log_base(
    *,
    request_id: str,
    created_at: str,
    method: str,
    path: str,
    query: str | None,
    upstream_url: str,
    model: str | None,
    stream: bool,
    client_ip: str | None,
    request_headers_json: str | None,
    request_body: str | None,
) -> dict[str, Any]:
    return {
        "id": request_id,
        "created_at": created_at,
        "completed_at": None,
        "method": method,
        "path": path,
        "query": query,
        "upstream_url": upstream_url,
        "model": model,
        "stream": stream,
        "status_code": None,
        "latency_ms": None,
        "client_ip": client_ip,
        "request_headers_json": request_headers_json,
        "response_headers_json": None,
        "request_body": request_body,
        "response_body": None,
        "stream_chunks": None,
        "assembled_response": None,
        "usage_json": None,
        "error_json": None,
    }


def serialize_headers_for_log(headers: Mapping[str, Any], settings: Settings) -> str:
    return json_dumps(redact_headers(headers, settings.redact_header_names))


async def proxy_request(request: Request, path: str) -> Response:
    app: FastAPI = request.app
    settings: Settings = app.state.settings
    db: LogDB = app.state.log_db
    client: httpx.AsyncClient = app.state.http_client

    require_proxy_auth(request, settings)

    request_id = request.headers.get("x-request-id") or f"lp_{uuid.uuid4().hex}"
    created_at = utc_now_iso()
    start = time.perf_counter()
    body = await request.body()
    model, requested_stream = parse_request_metadata(body)
    upstream_url = build_upstream_url(settings, path, request.url.query)
    upstream_headers = filter_inbound_headers(request.headers, settings, request_id)

    request_headers_json = (
        serialize_headers_for_log(request.headers, settings) if settings.log_request_headers else None
    )
    request_body = (
        body_to_log_text(body, max_bytes=settings.max_log_body_bytes, sensitive_json_keys=settings.redact_json_key_names)
        if settings.log_request_body
        else None
    )
    log_base = create_log_base(
        request_id=request_id,
        created_at=created_at,
        method=request.method,
        path=f"/v1/{path}" if path else "/v1",
        query=request.url.query or None,
        upstream_url=upstream_url,
        model=model,
        stream=requested_stream,
        client_ip=get_client_ip(request),
        request_headers_json=request_headers_json,
        request_body=request_body,
    )

    try:
        if requested_stream:
            upstream_request = client.build_request(
                request.method,
                upstream_url,
                headers=upstream_headers,
                content=body,
            )
            upstream_response = await client.send(upstream_request, stream=True)
            response_headers = filter_response_headers(upstream_response.headers)
            assembler = SSEAssembler(max_bytes=settings.max_log_body_bytes)

            async def stream_generator() -> AsyncIterator[bytes]:
                error_json: str | None = None
                try:
                    async for chunk in upstream_response.aiter_raw():
                        if chunk:
                            assembler.add_chunk(chunk, keep_raw=settings.log_stream_chunks)
                            yield chunk
                except Exception as exc:
                    error_json = json_dumps({"stream_error": str(exc)})
                    raise
                finally:
                    await upstream_response.aclose()
                    completed_at = utc_now_iso()
                    latency_ms = round((time.perf_counter() - start) * 1000, 3)
                    log = RequestLog(
                        **{
                            **log_base,
                            "completed_at": completed_at,
                            "status_code": upstream_response.status_code,
                            "latency_ms": latency_ms,
                            "response_headers_json": serialize_headers_for_log(upstream_response.headers, settings)
                            if settings.log_response_headers
                            else None,
                            "stream_chunks": assembler.raw_text if settings.log_response_body else None,
                            "assembled_response": truncate_text(assembler.assembled_text, settings.max_log_body_bytes)
                            if assembler.assembled_text
                            else None,
                            "usage_json": assembler.usage_json,
                            "error_json": error_json
                            or (json_dumps({"status_code": upstream_response.status_code}) if upstream_response.status_code >= 400 else None),
                        }
                    )
                    spawn_log_task(app, write_log_safely(db, log))

            return StreamingResponse(
                stream_generator(),
                status_code=upstream_response.status_code,
                headers=response_headers,
                media_type=upstream_response.headers.get("content-type"),
            )

        upstream_response = await client.request(
            request.method,
            upstream_url,
            headers=upstream_headers,
            content=body,
        )
        completed_at = utc_now_iso()
        latency_ms = round((time.perf_counter() - start) * 1000, 3)
        response_body = (
            body_to_log_text(
                upstream_response.content,
                max_bytes=settings.max_log_body_bytes,
                sensitive_json_keys=settings.redact_json_key_names,
            )
            if settings.log_response_body
            else None
        )
        log = RequestLog(
            **{
                **log_base,
                "completed_at": completed_at,
                "status_code": upstream_response.status_code,
                "latency_ms": latency_ms,
                "response_headers_json": serialize_headers_for_log(upstream_response.headers, settings)
                if settings.log_response_headers
                else None,
                "response_body": response_body,
                "usage_json": extract_usage_from_json_bytes(upstream_response.content),
                "error_json": extract_error_from_response(upstream_response.content, upstream_response.status_code),
            }
        )
        spawn_log_task(app, write_log_safely(db, log))
        return Response(
            content=upstream_response.content,
            status_code=upstream_response.status_code,
            headers=filter_response_headers(upstream_response.headers),
            media_type=upstream_response.headers.get("content-type"),
        )
    except HTTPException:
        raise
    except Exception as exc:
        completed_at = utc_now_iso()
        latency_ms = round((time.perf_counter() - start) * 1000, 3)
        log = RequestLog(
            **{
                **log_base,
                "completed_at": completed_at,
                "status_code": 502,
                "latency_ms": latency_ms,
                "error_json": json_dumps({"proxy_error": str(exc)}),
            }
        )
        spawn_log_task(app, write_log_safely(db, log))
        return JSONResponse(
            status_code=502,
            content={"error": {"message": "Upstream proxy error", "type": "proxy_error", "request_id": request_id}},
        )
