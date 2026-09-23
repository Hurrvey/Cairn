"""Environment-only ingestion worker composition settings."""

from __future__ import annotations

import json
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class IngestionRuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CAIRN_INGESTION__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    tokenizers_json: str = "{}"
    max_source_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    max_artifact_bytes: int = Field(default=256 * 1024 * 1024, gt=0)
    ocr_enabled: bool = True
    ocr_languages: str = "eng+chi_sim+chi_tra"
    ocr_timeout_s: float = Field(default=45, gt=0, le=300, allow_inf_nan=False)
    ocr_max_concurrency: int = Field(default=1, ge=1, le=4)

    @field_validator("tokenizers_json")
    @classmethod
    def _valid_tokenizers(cls, value: str) -> str:
        parsed = json.loads(value)
        if not isinstance(parsed, dict) or any(
            not isinstance(name, str) or not isinstance(binding, str)
            for name, binding in parsed.items()
        ):
            raise ValueError("tokenizers_json must be a JSON object of string bindings")
        return value

    def tokenizer_bindings(self) -> dict[str, str]:
        return dict(json.loads(self.tokenizers_json))


@lru_cache(maxsize=1)
def get_ingestion_runtime_settings() -> IngestionRuntimeSettings:
    return IngestionRuntimeSettings()
