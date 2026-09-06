"""Application settings loaded from environment / .env.

Secrets live only in the environment. Never log `Settings.database_url`
directly — use :func:`masked_database_url` instead.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
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
    research_sources_config: Path = PROJECT_ROOT / "config" / "research_sources.yaml"
    topic_score_config: Path = PROJECT_ROOT / "config" / "topic_score.yaml"
    topic_lexicon_config: Path = PROJECT_ROOT / "config" / "topic_lexicon.yaml"
    reports_dir: Path = PROJECT_ROOT / "reports"

    # The research engine needs its own API key: a Claude subscription does not
    # provide one, and without it the LLM components simply become unavailable.
    anthropic_api_key: str | None = None
    # Google's free tier needs no card, but its quota is per model and small:
    # measured on 2026-09-06, gemini-3.8-flash allows twenty requests a day.
    # The client rotates through models to stretch that.
    gemini_api_key: str | None = None
    gemini_daily_request_limit: int = 1000
    gemini_requests_per_minute: int = 10
    # Читается и как GH_TOKEN: префикс GITHUB_ зарезервирован GitHub Actions,
    # и workflow, который задаёт такую переменную, регистрируется не всегда.
    # Локально привычное имя продолжает работать.
    github_token: str | None = Field(
        default=None, validation_alias=AliasChoices("GITHUB_TOKEN", "GH_TOKEN")
    )

    # --- Publishing ---
    # Both default to "off": an autonomous publisher must be harmless until
    # someone deliberately turns it on.
    publish_mode: PublishMode = PublishMode.DRY_RUN
    auto_publish_enabled: bool = False
    # MAX credentials are declared once, above, as SecretStr + int chat id.
    telegram_bot_token: str | None = None

    # --- Content generation (PHASE 5) ---
    # A local model: free and offline. Used whenever it answers; the Gemini
    # free tier is the fallback, and without either the drafts stay skeletons.
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:7b-instruct"

    # Any OpenAI-compatible endpoint: Qwen through Alibaba Model Studio,
    # OpenRouter's ":free" models, or a self-hosted gateway. Used when Ollama
    # is not running and before falling back to Gemini.
    content_api_base_url: str | None = None
    content_api_key: SecretStr | None = None
    content_model: str | None = None

    # --- Posting schedule (PHASE 8) ---
    # Local times of the daily slots. Three a day is the cadence the channel
    # was planned around; a slot that has already been filled is never filled
    # twice, so running the command more often is harmless.
    publish_slots: str = "09:00,14:00,19:00"
    # How long after a slot opens it may still be filled. Wider than the gap
    # between runs, so a missed run does not silently skip a post.
    publish_slot_window_minutes: int = 180
    # Which slots of the day carry our own posts instead of news, as 0-based
    # indexes into PUBLISH_SLOTS. The middle slot by default: a channel that
    # only reacts to other people's releases never develops a voice.
    publish_own_slots: str = "1"
    # A topic older than this is history, not news: the scheduled track skips
    # it rather than telling readers about last week.
    publish_max_topic_age_hours: int = 48

    # --- Breaking news ---
    # Something big should not wait for the next slot. The bar is deliberately
    # high, because "breaking" that turns out to be routine costs more trust
    # than a late post: high score, very fresh, and confirmed by more than one
    # source or published by the vendor itself.
    breaking_enabled: bool = True
    breaking_min_score: float = 90.0
    breaking_max_age_minutes: int = 240
    breaking_min_sources: int = 2
    # Ceilings so a busy news day cannot turn the channel into a feed.
    breaking_max_per_day: int = 2
    breaking_min_gap_minutes: int = 90
    evergreen_topics_config: Path = PROJECT_ROOT / "config" / "evergreen_topics.yaml"
    brand_config: Path = PROJECT_ROOT / "config" / "brand.yaml"
    # Cards are rendered next to the reports, not into the repository root.
    cards_dir: Path = PROJECT_ROOT / "reports" / "cards"
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


def diagnose_database_url(url: str) -> list[str]:
    """Name the misconfigurations that produce unhelpful driver errors.

    Written after a real deployment: a connection string pasted from a hosting
    panel came through with the "?" percent-encoded, and asyncpg reported it as
    `unexpected keyword argument '?ssl'` — true, and useless to whoever set
    the secret. These checks turn each such case into a sentence naming the fix.
    """
    problems: list[str] = []
    if "%3F" in url.upper():
        problems.append(
            "в строке есть %3F — знак ? закодирован, поэтому параметры "
            "попадают в имя базы; вставьте обычный ?"
        )
    if not url.startswith("postgresql+asyncpg://"):
        problems.append("драйвер должен быть указан явно: postgresql+asyncpg://")
    if "sslmode=" in url:
        problems.append("asyncpg не понимает sslmode — замените на ssl=require")
    if "channel_binding=" in url:
        problems.append("asyncpg не понимает channel_binding — уберите этот параметр")
    if "neon.tech" in url and "ssl" not in url.split("?", 1)[-1]:
        problems.append("Neon отклоняет незашифрованные подключения — добавьте ?ssl=require")
    return problems


def masked_database_url(url: str) -> str:
    """Render a database URL with the password replaced by ``***``."""
    return make_url(url).render_as_string(hide_password=True)
