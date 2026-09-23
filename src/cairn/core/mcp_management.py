"""Shared managed-MCP configuration and read models."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ManagedMCPConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    port: int = Field(default=8081, ge=1024, le=65535, strict=True)
    auto_start: bool = True
    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
    )
    allowed_origins: list[str] = Field(default_factory=list)
    request_timeout_s: float = Field(default=15, gt=0, le=60, allow_inf_nan=False)
    max_request_bytes: int = Field(default=65536, ge=1024, le=1048576, strict=True)

    @field_validator("allowed_hosts", "allowed_origins")
    @classmethod
    def bounded_allowlist(cls, values: list[str]) -> list[str]:
        if len(values) > 32 or any(
            not value or len(value) > 255 or any(char.isspace() for char in value) or value == "*"
            for value in values
        ):
            raise ValueError(
                "Use at most32 explicit host/origin entries without whitespace or blanket wildcard"
            )
        return list(dict.fromkeys(values))


class MCPServiceView(BaseModel):
    config: ManagedMCPConfig
    generation: int
    observed_generation: int
    desired_state: Literal["running", "stopped"]
    state: Literal["running", "stopped", "starting", "stopping", "error", "unknown"]
    effective_port: int | None
    started_at: datetime | None
    heartbeat_at: datetime | None
    last_error: str | None
    allowed_ports: list[int]
    managed: bool


class MCPConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_generation: int = Field(ge=1, strict=True)
    config: ManagedMCPConfig


class MCPAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_generation: int = Field(ge=1, strict=True)
    action: Literal["start", "stop", "restart"]


class MCPLogView(BaseModel):
    id: int
    at: datetime
    level: str
    event: str
    detail: str
    request_id: str | None = None
    status: int | None = None
    duration_ms: int | None = None


class MCPLogs(BaseModel):
    items: list[MCPLogView]
