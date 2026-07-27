"""Application settings.

Safety-relevant values are validated at startup and fail loudly. A misconfiguration
that could widen risk must stop the process, not degrade quietly.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from meridian_config import limits
from meridian_config.product import ENV_PREFIX


class Mode(StrEnum):
    RESEARCH = "research"
    BACKTEST = "backtest"
    PAPER = "paper"
    BROKER = "broker"  # not implemented; rejected at startup


class ApprovalMode(StrEnum):
    OBSERVE_ONLY = "OBSERVE_ONLY"
    MANUAL_APPROVAL = "MANUAL_APPROVAL"
    AUTO_PAPER_RESTRICTED = "AUTO_PAPER_RESTRICTED"
    AUTO_PAPER_FULL = "AUTO_PAPER_FULL"


class RiskProfileName(StrEnum):
    PRESERVATION = "PRESERVATION"
    CHALLENGE = "CHALLENGE"
    ASSERTIVE = "ASSERTIVE"
    EXPERIMENTAL = "EXPERIMENTAL"
    CUSTOM = "CUSTOM"


class ConfigurationError(RuntimeError):
    """Configuration that would be unsafe to run with."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Execution safety -------------------------------------------------
    mode: Mode = Mode.RESEARCH
    broker_execution_enabled: bool = False
    approval_mode: ApprovalMode = ApprovalMode.MANUAL_APPROVAL
    kill_switch: bool = False
    risk_profile: RiskProfileName = RiskProfileName.CHALLENGE
    max_risk_per_trade_pct: Decimal = limits.MAX_RISK_PER_TRADE_PCT

    # --- Storage ----------------------------------------------------------
    storage_backend: Literal["sqlite", "postgres"] = "sqlite"
    database_url: str = "sqlite+aiosqlite:///./var/meridian.db"
    event_bus: Literal["memory", "redis"] = "memory"
    redis_url: str | None = None

    # --- Market data ------------------------------------------------------
    market_data_provider: str = "synthetic"
    market_data_seed: int = 20260727
    max_data_age_seconds: int = 60

    # --- Model router -----------------------------------------------------
    ollama_enabled: bool = True
    ollama_base_url: str = "http://localhost:11434"
    ollama_worker_model: str = "llama3.2:3b"
    ollama_embedding_model: str = "nomic-embed-text"
    anthropic_enabled: bool = False
    anthropic_api_key: SecretStr | None = None
    openai_enabled: bool = False
    openai_api_key: SecretStr | None = None
    model_daily_cost_cap_usd: Decimal = Decimal("5.00")

    # --- Vault ------------------------------------------------------------
    vault_path: Path = Path("./obsidian-vault")
    vault_sync_enabled: bool = True

    # --- API / logging ----------------------------------------------------
    api_host: str = "127.0.0.1"
    api_port: int = 8787
    cors_origins: str = "http://localhost:3000"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    environment: str = "local"

    # ----------------------------------------------------------------------

    @field_validator("max_risk_per_trade_pct")
    @classmethod
    def _clamp_risk_ceiling(cls, v: Decimal) -> Decimal:
        """Risk ceiling may be lowered from the environment, never raised.

        Invariant I2 (monotone tightening) at the outermost tier. An env var that
        tried to raise the ceiling would be the single most dangerous
        misconfiguration available, so it is rejected rather than clamped —
        silently ignoring it would leave the operator believing it took effect.
        """
        if v > limits.MAX_RISK_PER_TRADE_PCT:
            raise ValueError(
                f"{ENV_PREFIX}MAX_RISK_PER_TRADE_PCT={v} exceeds the system hard limit "
                f"of {limits.MAX_RISK_PER_TRADE_PCT}%. Raising the ceiling requires a "
                f"code change to meridian_config.limits, not an environment variable."
            )
        if v < limits.MIN_RISK_PER_TRADE_PCT:
            raise ValueError(
                f"Risk per trade {v}% is below the {limits.MIN_RISK_PER_TRADE_PCT}% floor"
            )
        return v

    @field_validator("cors_origins")
    @classmethod
    def _reject_wildcard_cors(cls, v: str) -> str:
        if "*" in v:
            raise ValueError("Wildcard CORS is not permitted; list explicit origins")
        return v

    @model_validator(mode="after")
    def _validate_execution_safety(self) -> Settings:
        """Refuse to start in any configuration implying live execution."""
        if self.mode is Mode.BROKER:
            raise ConfigurationError(
                "MERIDIAN_MODE=broker is not supported. Live broker execution is not "
                "implemented in this build — there is no broker adapter to connect to. "
                "See docs/architecture.md §9."
            )
        if self.broker_execution_enabled:
            raise ConfigurationError(
                "MERIDIAN_BROKER_EXECUTION_ENABLED=true, but no broker adapter exists. "
                "This flag cannot enable anything; it is refused so its presence is "
                "never mistaken for a working live path. See docs/architecture.md §9."
            )
        if not limits.LIVE_EXECUTION_IMPLEMENTED and self.mode not in {
            Mode.RESEARCH,
            Mode.BACKTEST,
            Mode.PAPER,
        }:
            raise ConfigurationError(f"Mode {self.mode} is not available in this build")
        return self

    @model_validator(mode="after")
    def _validate_experimental_profile(self) -> Settings:
        """EXPERIMENTAL is confined to backtest and paper (risk-engine.md §7)."""
        if self.risk_profile is RiskProfileName.EXPERIMENTAL and self.mode not in {
            Mode.BACKTEST,
            Mode.PAPER,
            Mode.RESEARCH,
        }:
            raise ConfigurationError(
                "The EXPERIMENTAL risk profile is available only in research, backtest "
                "and paper modes."
            )
        return self

    @model_validator(mode="after")
    def _validate_provider_consistency(self) -> Settings:
        """An enabled provider without a key is a configuration error, not a warning.

        Silently disabling it would leave the operator believing cloud review is
        running when it is not.
        """
        if self.anthropic_enabled and self.anthropic_api_key is None:
            raise ConfigurationError("ANTHROPIC_ENABLED=true but ANTHROPIC_API_KEY is unset")
        if self.openai_enabled and self.openai_api_key is None:
            raise ConfigurationError("OPENAI_ENABLED=true but OPENAI_API_KEY is unset")
        if self.event_bus == "redis" and not self.redis_url:
            raise ConfigurationError("EVENT_BUS=redis but REDIS_URL is unset")
        if self.storage_backend == "postgres" and self.database_url.startswith("sqlite"):
            raise ConfigurationError("STORAGE_BACKEND=postgres but DATABASE_URL is a SQLite URL")
        return self

    @field_validator("max_data_age_seconds")
    @classmethod
    def _cap_data_age(cls, v: int) -> int:
        if v > limits.MAX_DATA_AGE_SECONDS_CEILING:
            raise ValueError(
                f"max_data_age_seconds={v} exceeds the {limits.MAX_DATA_AGE_SECONDS_CEILING}s "
                f"ceiling; stale data must block trading"
            )
        if v <= 0:
            raise ValueError("max_data_age_seconds must be positive")
        return v

    # --- Derived ----------------------------------------------------------

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def provider_status(self) -> dict[str, str]:
        """Provider configuration state — presence only, never values (security.md §2)."""
        return {
            "ollama": "enabled" if self.ollama_enabled else "disabled",
            "anthropic": "configured" if self.anthropic_api_key else "unconfigured",
            "openai": "configured" if self.openai_api_key else "unconfigured",
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Cached so validation runs exactly once."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the cache. Tests only."""
    get_settings.cache_clear()
