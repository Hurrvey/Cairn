"""Configuration.

Every setting arrives from the environment with the ``CAIRN_`` prefix; nested
settings use ``__`` (``CAIRN_DB__POOL_SIZE``).

The process MUST refuse to start on invalid configuration rather than failing at
first request — a deployment that boots and then 500s on every call is far
harder to diagnose than one that exits with a message naming the bad setting.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Role", "Settings", "get_settings"]

Role = Literal["control", "data", "worker", "all"]


class DatabaseSettings(BaseModel):
    pool_size: int = 20
    max_overflow: int = 10
    pool_pre_ping: bool = True
    statement_timeout_ms: int = 30_000
    echo: bool = False


class AuthSettings(BaseModel):
    session_ttl_minutes: int = 480
    change_token_ttl_minutes: int = 10
    cookie_name: str = "cairn_session"
    csrf_cookie_name: str = "cairn_csrf"
    cookie_secure: bool = True
    cookie_domain: str | None = None

    # Argon2id — FR-A-08 requires m >= 64 MiB, t >= 3, p = 4.
    argon2_memory_kib: int = Field(default=65_536, ge=65_536)
    argon2_time_cost: int = Field(default=3, ge=3)
    argon2_parallelism: int = Field(default=4, ge=1)

    # Password policy — FR-A-09.
    password_min_length: int = Field(default=12, ge=12)
    password_max_length: int = 256
    password_require_classes: int = Field(default=3, ge=1, le=4)

    # Lockout — FR-A-11.
    lockout_thresholds: dict[int, int] = Field(
        default_factory=lambda: {5: 60, 7: 300, 10: 1_800, 15: 86_400}
    )
    login_ip_attempts_per_minute: int = 20


class HttpSettings(BaseModel):
    """Outbound HTTP. Defaults are the SSRF guard's limits (NFR-SEC-09)."""

    timeout_s: float = 30.0
    connect_timeout_s: float = 10.0
    max_response_bytes: int = 52_428_800  # 50 MiB
    max_redirects: int = 5
    user_agent: str = "Cairn/0.1 (+https://docs.cairn.io)"
    allow_private_addresses: bool = False  # dev/test escape hatch ONLY


class ObjectStoreSettings(BaseModel):
    local_path: str = "/var/lib/cairn/objects"
    presign_ttl_s: int = 900
    max_inline_bytes: int = 8_388_608  # what `get_bytes` will materialise


class TaskSettings(BaseModel):
    """Worker runtime. `queue` is required when CAIRN_ROLE=worker."""

    queue: str | None = None
    concurrency: int | None = None
    poll_interval_s: float = 1.0
    reaper_interval_s: int = 30
    graceful_shutdown_s: int = 30
    retain_done_days: int = 7
    default_workspace_concurrency: int = 16
    runtime_refresh_interval_s: float = Field(default=60, gt=0, le=3600)
    runtime_refresh_batch_size: int = Field(default=100, ge=1, le=1000)


class EmbeddingSettings(BaseModel):
    cache_ttl_s: int = Field(default=604800, ge=1)
    cache_enabled: bool = True
    query_timeout_s: float = Field(default=2, gt=0)
    document_timeout_s: float = Field(default=30, gt=0)
    max_concurrency: int = Field(default=8, ge=1)
    max_retries: int = Field(default=3, ge=0, le=10)
    circuit_failure_threshold: int = Field(default=5, ge=1)
    circuit_window_s: float = Field(default=60, gt=0)
    circuit_reset_s: float = Field(default=30, gt=0)


class RetrievalEmbeddingEndpointSettings(BaseModel):
    dialect: Literal["tei", "infinity"]
    base_url: str
    allow_private: bool = False
    namespace: str
    max_batch_size: int = Field(default=16, ge=1, le=1024)
    api_key: SecretStr | None = None


class RerankEndpointSettings(BaseModel):
    """Operator-owned endpoints; requests select a name, never a URL or credential."""

    base_url: str
    dialect: Literal["tei", "infinity"] = "tei"
    model_name: str | None = None
    workspace_ids: list[UUID] = Field(min_length=1)
    allow_private: bool = False
    api_key: SecretStr | None = None
    max_candidates: int = Field(default=100, ge=1, le=1000)
    max_request_bytes: int = Field(default=1024 * 1024, ge=1024, le=8 * 1024 * 1024)


class RetrievalSettings(BaseModel):
    embedding_endpoints: dict[UUID, RetrievalEmbeddingEndpointSettings] = Field(
        default_factory=dict
    )
    tokenizer_files: dict[str, str] = Field(default_factory=dict)
    request_timeout_s: float = Field(default=5, gt=0, le=30)
    runtime_timeout_s: float = Field(default=0.25, gt=0, le=5)
    search_timeout_s: float = Field(default=2, gt=0, le=10)
    rerank_endpoints: dict[str, RerankEndpointSettings] = Field(default_factory=dict)


class MCPSettings(BaseModel):
    managed: bool = False
    port_min: int = Field(default=8081, ge=1024, le=65535)
    port_max: int = Field(default=8090, ge=1024, le=65535)
    gateway_port: int = Field(default=8099, ge=1024, le=65535)

    @model_validator(mode="after")
    def managed_ports(self) -> MCPSettings:
        if not 0 <= self.port_max - self.port_min < 32:
            raise ValueError("MCP published port range must contain1..32 ports")
        if self.port_min <= self.gateway_port <= self.port_max:
            raise ValueError("MCP gateway port must be outside the published range")
        return self

    allowed_hosts: list[str] = Field(
        default_factory=lambda: [
            "localhost",
            "localhost:*",
            "127.0.0.1",
            "127.0.0.1:*",
            "[::1]",
            "[::1]:*",
        ]
    )
    allowed_origins: list[str] = Field(default_factory=list)
    max_request_bytes: int = Field(default=65536, ge=1024, le=1048576)
    request_timeout_s: float = Field(default=15, gt=0, le=60)


class TelemetrySettings(BaseModel):
    otlp_endpoint: str | None = None
    metrics_enabled: bool = True
    service_name: str = "cairn"
    trace_sample_ratio: float = 1.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CAIRN_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    role: Role = "all"
    environment: Literal["dev", "staging", "prod"] = "dev"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    database_url: str
    redis_url: str
    master_key: SecretStr

    initial_admin_password: SecretStr | None = None

    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    http: HttpSettings = Field(default_factory=HttpSettings)
    tasks: TaskSettings = Field(default_factory=TaskSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    mcp: MCPSettings = Field(default_factory=MCPSettings)
    objectstore: ObjectStoreSettings = Field(default_factory=ObjectStoreSettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)

    @property
    def is_prod(self) -> bool:
        return self.environment == "prod"

    @property
    def serves_control_plane(self) -> bool:
        return self.role in ("control", "all")

    @property
    def serves_data_plane(self) -> bool:
        return self.role in ("data", "all")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and validate settings once. Exits the process on invalid config."""
    try:
        return Settings()
    except ValidationError as exc:  # pragma: no cover — exercised by TC-M00-02
        print("Cairn cannot start: invalid configuration.\n", file=sys.stderr)
        for error in exc.errors():
            location = "CAIRN_" + "__".join(str(p) for p in error["loc"]).upper()
            print(f"  {location}: {error['msg']}", file=sys.stderr)
        print("\nSee docs/06-ops/01-deployment.md §6.", file=sys.stderr)
        raise SystemExit(78) from exc  # EX_CONFIG
