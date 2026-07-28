"""Risk engine — the only path to an order.

Pure and deterministic: no clock, no I/O, no network, no float. The import-linter
contracts in pyproject.toml enforce that it can never reach the model router or
the database.
"""

from __future__ import annotations

from meridian_risk.context import (
    AccountState,
    MarketState,
    OpenPosition,
    PortfolioState,
    RiskContext,
    TradeProposal,
)
from meridian_risk.decision import (
    DecisionForgeryError,
    RiskDecision,
    RuleOutcome,
)
from meridian_risk.engine import RiskEngine
from meridian_risk.forex import (
    ConversionResult,
    ConversionRoute,
    ForexError,
    SizingResult,
    calculate_position_size,
    convert_quote_to_account,
    loss_at_stop,
    margin_required,
    pip_value_per_lot,
    stop_distance_pips,
)
from meridian_risk.limits import LimitSet, Tighten, compose
from meridian_risk.portfolio import PortfolioRiskEngine, SetEvaluation, rank_proposals
from meridian_risk.profiles import PROFILES, RiskProfile, ThrottleBand, get_profile
from meridian_risk.propfirm import (
    GENERIC_TWO_PHASE,
    PROP_PROFILES,
    DrawdownType,
    LossBasis,
    PropAccountState,
    PropFirmProfile,
    RuleEvaluation,
    RuleStatus,
    evaluate_profile,
)

__all__ = [
    "GENERIC_TWO_PHASE",
    "PROFILES",
    "PROP_PROFILES",
    "AccountState",
    "ConversionResult",
    "ConversionRoute",
    "DecisionForgeryError",
    "DrawdownType",
    "ForexError",
    "LimitSet",
    "LossBasis",
    "MarketState",
    "OpenPosition",
    "PortfolioRiskEngine",
    "PortfolioState",
    "PropAccountState",
    "PropFirmProfile",
    "RiskContext",
    "RiskDecision",
    "RiskEngine",
    "RiskProfile",
    "RuleEvaluation",
    "RuleOutcome",
    "RuleStatus",
    "SetEvaluation",
    "SizingResult",
    "ThrottleBand",
    "Tighten",
    "TradeProposal",
    "calculate_position_size",
    "compose",
    "convert_quote_to_account",
    "evaluate_profile",
    "get_profile",
    "loss_at_stop",
    "margin_required",
    "pip_value_per_lot",
    "rank_proposals",
    "stop_distance_pips",
]
