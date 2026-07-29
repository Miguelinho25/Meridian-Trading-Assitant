from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from nemonis_config.settings import ApprovalMode, Mode, RiskProfileName
from nemonis_marketdata.instruments import WATCHLIST, get_spec
from nemonis_marketdata.quality import QualityReport
from nemonis_risk.context import (
    AccountState,
    MarketState,
    PortfolioState,
    RiskContext,
    TradeProposal,
)
from nemonis_schemas.enums import DataQualityVerdict, Direction, Session

NOW = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)

RATES = {
    "EURUSD": Decimal("1.08500"),
    "GBPUSD": Decimal("1.26500"),
    "USDJPY": Decimal("157.200"),
    "USDCHF": Decimal("0.89500"),
    "USDCAD": Decimal("1.37500"),
    "EURGBP": Decimal("0.85500"),
    "GBPJPY": Decimal("199.000"),
    "AUDUSD": Decimal("0.65500"),
    "NZDUSD": Decimal("0.60500"),
    "EURJPY": Decimal("170.400"),
}


def good_quality(instrument: str = "EURUSD") -> QualityReport:
    return QualityReport(
        instrument=instrument,
        verdict=DataQualityVerdict.OK,
        score=Decimal(1),
        issues=(),
        bars_examined=500,
        assessed_at=NOW,
    )


def make_proposal(**kw) -> TradeProposal:
    base = {
        "proposal_id": "prp_test",
        "strategy_id": "ma-trend",
        "strategy_version": "0.1.0",
        "instrument": "EURUSD",
        "direction": Direction.LONG,
        "entry": Decimal("1.08500"),
        "stop": Decimal("1.08200"),
        "target": Decimal("1.09400"),  # 3:1 reward:risk
        "requested_risk_pct": Decimal("0.35"),
        "confidence": Decimal("0.75"),
        "decision_time": NOW,
    }
    return TradeProposal(**{**base, **kw})


def make_account(**kw) -> AccountState:
    base = {
        "account_id": "acc_test",
        "currency": "USD",
        "balance": Decimal("100000"),
        "equity": Decimal("100000"),
        "high_water_mark": Decimal("100000"),
        "drawdown_consumed": Decimal("0.00"),
        "daily_loss_used": Decimal("0"),
        "daily_loss_limit": Decimal("5000"),
        "total_loss_used": Decimal("0"),
        "total_loss_limit": Decimal("10000"),
    }
    return AccountState(**{**base, **kw})


def make_market(**kw) -> MarketState:
    instrument = kw.pop("instrument", "EURUSD")
    base = {
        "spec": get_spec(instrument),
        "bid": Decimal("1.08500"),
        "ask": Decimal("1.08508"),
        "quality": good_quality(instrument),
        "session": Session.LONDON,
        "is_weekend": False,
        "is_rollover": False,
        "minutes_to_news": 240,
        "atr": Decimal("0.00600"),
        "spread_multiple": Decimal("1.0"),
        "volatility_ratio": Decimal("1.0"),
    }
    return MarketState(**{**base, **kw})


def make_context(**kw) -> RiskContext:
    base = {
        "proposal": kw.pop("proposal", make_proposal()),
        "account": kw.pop("account", make_account()),
        "market": kw.pop("market", make_market()),
        "portfolio": kw.pop("portfolio", PortfolioState()),
        "mode": Mode.PAPER,
        "approval_mode": ApprovalMode.MANUAL_APPROVAL,
        "profile_name": RiskProfileName.CHALLENGE,
        "kill_switch_engaged": False,
        "rates": RATES,
        "specs": dict(WATCHLIST),
    }
    return RiskContext(**{**base, **kw})


@pytest.fixture
def ctx() -> RiskContext:
    """A context that should cleanly approve. Tests break one thing at a time."""
    return make_context()


@pytest.fixture
def now() -> datetime:
    return NOW
