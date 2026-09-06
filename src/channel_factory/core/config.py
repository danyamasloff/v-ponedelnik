"""Application settings loaded from environment / .env.

Secrets live only in the environment. Never log `Settings.database_url`
directly — use :func:`masked_database_url` instead.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from channel_factory.core.enums import PublishMode

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
    research_sources_config: Path = PROJECT_ROOT / "config" / "research_sources.yaml"
    topic_score_config: Path = PROJECT_ROOT / "config" / "topic_score.yaml"
    topic_lexicon_config: Path = PROJECT_ROOT / "config" / "topic_lexicon.yaml"
    reports_dir: Path = PROJECT_ROOT / "reports"

    # The research engine needs its own API key: a Claude subscription does not
    # provide one, and without it the LLM components simply become unavailable.
    anthropic_api_key: str | None = None
    # Google's free tier needs no card and covers our volumes with room to
    # spare. Preferred automatically when present, because it costs nothing.
    gemini_api_key: str | None = None
    gemini_daily_request_limit: int = 1000
    gemini_requests_per_minute: int = 10
    github_token: str | None = None

    # --- Publishing ---
    # Both default to "off": an autonomous publisher must be harmless until
    # someone deliberately turns it on.
    publish_mode: PublishMode = PublishMode.DRY_RUN
    auto_publish_enabled: bool = False
    max_bot_token: str | None = None
    max_channel_id: str | None = None
    telegram_bot_token: str | None = None
    telegram_channel_id: str | None = None

    # Hard spend ceilings. Enforced against the sum of ai_generations, so a bug
    # cannot quietly drain the API balance: calls stop, research continues.
    daily_cost_limit_usd: Decimal = Decimal("2.00")
    monthly_cost_limit_usd: Decimal = Decimal("30.00")

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
