from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_REDACT_HEADERS = "authorization,cookie,set-cookie,x-api-key,api-key,proxy-authorization"
DEFAULT_REDACT_JSON_KEYS = "authorization,api_key,apikey,apiKey,password,passwd,secret,token,access_token,refresh_token,cookie"


class Settings(BaseSettings):
    """Runtime configuration for the OpenAI-compatible log proxy."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    upstream_base_url: Annotated[
        str,
        Field(
            description="Base URL for the upstream OpenAI-compatible service, e.g. http://relay-cli-proxy:8317/v1",
        ),
    ] = "http://relay-cli-proxy:8317/v1"
    upstream_api_key: SecretStr | None = Field(
        default=None,
        description="API key used when forwarding requests to upstream. If omitted, inbound Authorization is passed through.",
    )

    proxy_api_key: SecretStr | None = Field(
        default=None,
        description="Optional ingress API key. If set, incoming requests must use this Bearer token.",
    )
    admin_api_key: SecretStr | None = Field(
        default=None,
        description="Optional admin token for /logs endpoints. If omitted, /logs endpoints are disabled.",
    )

    sqlite_path: str = Field(default="/data/log-proxy.sqlite3")
    request_timeout_seconds: float = Field(default=600.0, ge=1.0)
    max_log_body_bytes: int = Field(default=2_000_000, ge=1024)

    log_request_body: bool = True
    log_response_body: bool = True
    log_stream_chunks: bool = True
    log_request_headers: bool = True
    log_response_headers: bool = True

    redact_headers: str = DEFAULT_REDACT_HEADERS
    redact_json_keys: str = DEFAULT_REDACT_JSON_KEYS
    service_name: str = "log-proxy"

    @field_validator("upstream_base_url")
    @classmethod
    def normalize_upstream_base_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("upstream_base_url cannot be empty")
        return value.rstrip("/")

    @property
    def redact_header_names(self) -> set[str]:
        return {x.strip().lower() for x in self.redact_headers.split(",") if x.strip()}

    @property
    def redact_json_key_names(self) -> set[str]:
        return {x.strip().lower() for x in self.redact_json_keys.split(",") if x.strip()}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
