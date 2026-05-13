from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

SENSITIVE_VALUE = "***REDACTED***"


def mask_secret(value: str) -> str:
    """Return a safe representation of a credential-like string."""
    if not value:
        return SENSITIVE_VALUE
    stripped = value.strip()
    if stripped.lower().startswith("bearer "):
        token = stripped[7:].strip()
        suffix = token[-4:] if len(token) >= 4 else ""
        return f"Bearer ***{suffix}" if suffix else "Bearer ***"
    if len(stripped) <= 8:
        return "***"
    return f"***{stripped[-4:]}"


def redact_headers(headers: Mapping[str, Any], sensitive_names: set[str]) -> dict[str, str]:
    redacted: dict[str, str] = {}
    for key, value in headers.items():
        key_s = str(key)
        value_s = str(value)
        if key_s.lower() in sensitive_names:
            redacted[key_s] = mask_secret(value_s)
        else:
            redacted[key_s] = value_s
    return redacted


def should_redact_json_key(key: str, sensitive_names: set[str]) -> bool:
    key_l = key.lower()
    if key_l in sensitive_names:
        return True
    return any(fragment in key_l for fragment in ("secret", "token", "password", "credential"))


def redact_json(value: Any, sensitive_names: set[str]) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, child in value.items():
            key_s = str(key)
            if should_redact_json_key(key_s, sensitive_names):
                out[key_s] = mask_secret(str(child)) if isinstance(child, str) else SENSITIVE_VALUE
            else:
                out[key_s] = redact_json(child, sensitive_names)
        return out
    if isinstance(value, list):
        return [redact_json(item, sensitive_names) for item in value]
    return value


def try_load_json_bytes(body: bytes) -> Any | None:
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return None


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def truncate_text(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return f"{truncated}\n...[truncated at {max_bytes} bytes]"


def body_to_log_text(body: bytes, *, max_bytes: int, sensitive_json_keys: set[str]) -> str | None:
    if not body:
        return None
    parsed = try_load_json_bytes(body)
    if parsed is not None:
        safe = redact_json(parsed, sensitive_json_keys)
        return truncate_text(json_dumps(safe), max_bytes)
    return truncate_text(body.decode("utf-8", errors="replace"), max_bytes)
