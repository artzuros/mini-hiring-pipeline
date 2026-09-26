"""Application settings, loaded from the environment (and `.env` if present)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://postgres@127.0.0.1:5433/hiring"
    test_database_url: str = "postgresql+asyncpg://postgres@127.0.0.1:5433/hiring_test"

    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5"
    search_llm_fallback_enabled: bool = True
    search_llm_timeout_seconds: float = 8.0

    @property
    def llm_fallback_available(self) -> bool:
        """The fallback needs both a switch and a key to actually fire.

        When this is False the search service degrades cleanly to
        "rules-only, 422 on anything unparseable" rather than raising.
        """
        return self.search_llm_fallback_enabled and bool(self.anthropic_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
