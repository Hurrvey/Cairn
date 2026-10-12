"""Preview budgets shared by worker composition and authorized read paths."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PreviewSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CAIRN_INGESTION__", extra="ignore", env_file=".env"
    )
    preview_max_page_pixels: int = Field(default=4_000_000, gt=0)
    preview_max_image_bytes: int = Field(default=8 * 1024**2, gt=0)
    preview_max_document_bytes: int = Field(default=64 * 1024**2, gt=0)
    preview_max_pages: int = Field(default=100, gt=0, le=500)
    preview_timeout_s: float = Field(default=30, gt=0, allow_inf_nan=False)
    preview_max_concurrency: int = Field(default=1, ge=1, le=4)
    preview_response_max_bytes: int = Field(default=8 * 1024**2, gt=0)
