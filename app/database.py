from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class RequestLog:
    id: str
    created_at: str
    completed_at: str | None
    method: str
    path: str
    query: str | None
    upstream_url: str
    model: str | None
    stream: bool
    status_code: int | None
    latency_ms: float | None
    client_ip: str | None
    request_headers_json: str | None
    response_headers_json: str | None
    request_body: str | None
    response_body: str | None
    stream_chunks: str | None
    assembled_response: str | None
    usage_json: str | None
    error_json: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LogDB:
    def __init__(self, sqlite_path: str):
        self.sqlite_path = sqlite_path
        self._write_lock = asyncio.Lock()

    async def init(self) -> None:
        await asyncio.to_thread(self._init_sync)

    def _init_sync(self) -> None:
        path = Path(self.sqlite_path)
        if path.parent and str(path.parent) != ".":
            path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS request_logs (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    method TEXT NOT NULL,
                    path TEXT NOT NULL,
                    query TEXT,
                    upstream_url TEXT NOT NULL,
                    model TEXT,
                    stream INTEGER NOT NULL,
                    status_code INTEGER,
                    latency_ms REAL,
                    client_ip TEXT,
                    request_headers_json TEXT,
                    response_headers_json TEXT,
                    request_body TEXT,
                    response_body TEXT,
                    stream_chunks TEXT,
                    assembled_response TEXT,
                    usage_json TEXT,
                    error_json TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_request_logs_created_at ON request_logs(created_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_request_logs_status_code ON request_logs(status_code)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_request_logs_model ON request_logs(model)")
            conn.commit()

    async def insert(self, log: RequestLog) -> None:
        async with self._write_lock:
            await asyncio.to_thread(self._insert_sync, log)

    def _insert_sync(self, log: RequestLog) -> None:
        data = log.to_dict()
        data["stream"] = 1 if log.stream else 0
        columns = list(data.keys())
        placeholders = ",".join("?" for _ in columns)
        sql = f"INSERT OR REPLACE INTO request_logs ({','.join(columns)}) VALUES ({placeholders})"
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute(sql, [data[c] for c in columns])
            conn.commit()

    async def list_logs(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_logs_sync, limit, offset)

    def _list_logs_sync(self, limit: int, offset: int) -> list[dict[str, Any]]:
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, created_at, completed_at, method, path, model, stream,
                       status_code, latency_ms, client_ip, upstream_url, usage_json, error_json
                FROM request_logs
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
            return [dict(row) for row in rows]

    async def get_log(self, request_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_log_sync, request_id)

    def _get_log_sync(self, request_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM request_logs WHERE id = ?", (request_id,)).fetchone()
            if row is None:
                return None
            data = dict(row)
            data["stream"] = bool(data.get("stream"))
            for key in ("request_headers_json", "response_headers_json", "usage_json", "error_json"):
                if data.get(key):
                    try:
                        data[key] = json.loads(data[key])
                    except Exception:
                        pass
            return data


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
