"""The safety guarantees this build claims must be enforced, not documented."""

from __future__ import annotations

from decimal import Decimal

import pytest
from meridian_config import (
    ApprovalMode,
    ConfigurationError,
    Mode,
    RiskProfileName,
    Settings,
    limits,
)


class TestDefaultsAreSafe:
    def test_defaults_to_research_mode(self) -> None:
        assert Settings().mode is Mode.RESEARCH

    def test_broker_execution_disabled_by_default(self) -> None:
        assert Settings().broker_execution_enabled is False

    def test_defaults_to_manual_approval(self) -> None:
        assert Settings().approval_mode is ApprovalMode.MANUAL_APPROVAL

    def test_defaults_to_challenge_profile(self) -> None:
        assert Settings().risk_profile is RiskProfileName.CHALLENGE

    def test_live_execution_is_not_implemented(self) -> None:
        assert limits.LIVE_EXECUTION_IMPLEMENTED is False


class TestLiveExecutionIsRefused:
    """architecture.md §9 — absent, not merely disabled."""

    def test_broker_mode_refused(self) -> None:
        with pytest.raises((ConfigurationError, ValueError), match="not supported"):
            Settings(mode=Mode.BROKER)

    def test_broker_execution_flag_refused(self) -> None:
        with pytest.raises((ConfigurationError, ValueError), match="no broker adapter exists"):
            Settings(broker_execution_enabled=True)


class TestRiskCeilingCannotBeRaised:
    """Invariant I2 at the outermost tier."""

    def test_env_cannot_raise_ceiling(self) -> None:
        over = limits.MAX_RISK_PER_TRADE_PCT + Decimal("0.5")
        with pytest.raises(ValueError, match="exceeds the system hard limit"):
            Settings(max_risk_per_trade_pct=over)

    def test_env_may_lower_ceiling(self) -> None:
        s = Settings(max_risk_per_trade_pct=Decimal("0.25"))
        assert s.max_risk_per_trade_pct == Decimal("0.25")

    def test_ceiling_at_exact_limit_allowed(self) -> None:
        s = Settings(max_risk_per_trade_pct=limits.MAX_RISK_PER_TRADE_PCT)
        assert s.max_risk_per_trade_pct == limits.MAX_RISK_PER_TRADE_PCT

    def test_absurdly_low_risk_rejected(self) -> None:
        with pytest.raises(ValueError, match="below the"):
            Settings(max_risk_per_trade_pct=Decimal("0.001"))


class TestFailClosedConfiguration:
    def test_enabled_provider_without_key_is_an_error(self) -> None:
        with pytest.raises((ConfigurationError, ValueError), match="OPENAI_API_KEY is unset"):
            Settings(openai_enabled=True)

    def test_redis_bus_without_url_is_an_error(self) -> None:
        with pytest.raises((ConfigurationError, ValueError), match="REDIS_URL is unset"):
            Settings(event_bus="redis")

    def test_postgres_backend_with_sqlite_url_is_an_error(self) -> None:
        with pytest.raises((ConfigurationError, ValueError), match="SQLite URL"):
            Settings(storage_backend="postgres", database_url="sqlite+aiosqlite:///./x.db")

    def test_wildcard_cors_refused(self) -> None:
        with pytest.raises(ValueError, match="Wildcard CORS"):
            Settings(cors_origins="*")

    def test_data_age_above_ceiling_refused(self) -> None:
        with pytest.raises(ValueError, match="stale data must block trading"):
            Settings(max_data_age_seconds=limits.MAX_DATA_AGE_SECONDS_CEILING + 1)


class TestSecretsNeverLeak:
    def test_provider_status_reports_presence_not_values(self) -> None:
        s = Settings(openai_enabled=True, openai_api_key="sk-secret-value-1234567890")
        status = s.provider_status()
        assert status["openai"] == "configured"
        assert "sk-secret" not in str(status)

    def test_secret_is_masked_in_repr(self) -> None:
        s = Settings(openai_enabled=True, openai_api_key="sk-secret-value-1234567890")
        assert "sk-secret-value" not in repr(s)
        assert "sk-secret-value" not in str(s.openai_api_key)
