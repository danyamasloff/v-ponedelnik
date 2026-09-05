"""Application settings loaded from environment / .env.

Secrets live only in the environment. Never log `Settings.database_url`
directly — use :func:`masked_database_url` instead.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
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

    # --- MAX publishing (https://dev.max.ru/docs-api) ---
    # SecretStr so the token cannot end up in a repr, a log line or a traceback.
    max_bot_token: SecretStr | None = Field(
        default=None, description="Bot token issued by MasterBot in the MAX app"
    )
    max_chat_id: int | None = Field(
        default=None, description="Default channel/chat id to publish into"
    )
    # Not the host from the docs (platform-api2) on purpose: that one is served
    # under the Russian Trusted Root CA, absent from certifi and from Windows.
    max_api_base_url: str = "https://platform-api.max.ru"
    max_request_timeout: float = 30.0
    max_ca_bundle: Path | None = Field(
        default=None,
        description="PEM bundle with an extra root CA, used only for MAX requests",
    )
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
