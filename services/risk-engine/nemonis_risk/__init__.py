"""Risk engine — the only path to an order.

Pure and deterministic: no clock, no I/O, no network, no float. The import-linter
contracts in pyproject.toml enforce that it can never reach the model router or
the database.
"""

from __future__ import annotations

from nemonis_risk.context import (
    AccountState,
    MarketState,
    OpenPosition,
    PortfolioState,
    RiskContext,
    TradeProposal,
)
from nemonis_risk.decision import (
    DecisionForgeryError,
    RiskDecision,
    RuleOutcome,
)
from nemonis_risk.engine import RiskEngine
from nemonis_risk.forex import (
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
from nemonis_risk.limits import LimitOrigin, LimitSet, Tighten, compose, explain
from nemonis_risk.portfolio import PortfolioRiskEngine, SetEvaluation, rank_proposals
from nemonis_risk.profiles import PROFILES, RiskProfile, ThrottleBand, get_profile
from nemonis_risk.propfirm import (
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
    "LimitOrigin",
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
    "explain",
    "get_profile",
    "loss_at_stop",
    "margin_required",
    "pip_value_per_lot",
    "rank_proposals",
    "stop_distance_pips",
]
