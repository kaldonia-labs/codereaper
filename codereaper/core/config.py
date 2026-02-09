"""Application settings loaded from environment / .env via pydantic-settings."""

from __future__ import annotations

from pathlib import Path
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration for CodeReaper."""

    model_config = SettingsConfigDict(
        env_prefix="CODEREAPER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Server ──────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    cors_origins: list[str] = ["*"]

    # ── Storage ─────────────────────────────────────────────────────────
    data_dir: Path = Path("./data")
    db_path: Path = Path("./data/codereaper.db")

    # ── Index Agent ─────────────────────────────────────────────────────
    index_llm_provider: str = "gemini"
    index_llm_model: str = "gemini-3.0-flash"
    index_max_steps: int = 100
    index_viewport_width: int = 1920
    index_viewport_height: int = 1080

    # ── Coverage ────────────────────────────────────────────────────────
    default_passes: int = 3

    # ── Verification ────────────────────────────────────────────────────
    screenshot_diff_enabled: bool = False
    coverage_drop_threshold: float = 2.0  # percentage points


@lru_cache
def get_settings() -> Settings:
    """Return a cached singleton of application settings."""
    return Settings()
