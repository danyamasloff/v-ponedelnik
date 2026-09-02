"""Application settings loaded from environment / .env.

Secrets live only in the environment. Never log `Settings.database_url`
directly — use :func:`masked_database_url` instead.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Runtime configuration for the channel factory."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: str = Field(
        description="SQLAlchemy async URL, e.g. postgresql+asyncpg://user:pw@localhost:5432/db",
    )
    test_database_url: str | None = Field(
        default=None,
        description="Database used by integration tests. Defaults to <database>_test.",
    )
    log_level: str = "INFO"
    db_echo: bool = False
    direct_columns_config: Path = PROJECT_ROOT / "config" / "direct_columns.yaml"
    niche_score_config: Path = PROJECT_ROOT / "config" / "niche_score.yaml"
    reports_dir: Path = PROJECT_ROOT / "reports"

    @property
    def effective_test_database_url(self) -> str:
        """Test database URL, derived from `database_url` when not set explicitly."""
        if self.test_database_url:
            return self.test_database_url
        url = make_url(self.database_url)
        return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()  # type: ignore[call-arg]  # values come from env/.env


def masked_database_url(url: str) -> str:
    """Render a database URL with the password replaced by ``***``."""
    return make_url(url).render_as_string(hide_password=True)
