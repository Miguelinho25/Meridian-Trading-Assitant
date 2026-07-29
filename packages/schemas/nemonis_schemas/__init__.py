"""Shared domain schemas — the single source of truth for cross-boundary types.

Pydantic models here generate JSON Schema, which generates TypeScript for the
frontend. The frontend never hand-writes a domain type.
"""

from __future__ import annotations

from nemonis_schemas.enums import (
    AICritiqueDecision,
    AuditEventType,
    DataQualityVerdict,
    Direction,
    OrderState,
    OrderType,
    PromotionStatus,
    RejectionCode,
    ResultProvenance,
    RiskVerdict,
    Session,
    Timeframe,
)
from nemonis_schemas.identifiers import IdPrefix, new_id, parse_prefix, validate_id
from nemonis_schemas.money import (
    DecimalStr,
    Lots,
    Money,
    Percent,
    Pips,
    PrecisionError,
    Price,
    clamp,
    floor_to_step,
    quantise_money,
    quantise_percent,
    quantise_pips,
    quantise_price,
    to_decimal,
)

__all__ = [
    "AICritiqueDecision",
    "AuditEventType",
    "DataQualityVerdict",
    "DecimalStr",
    "Direction",
    "IdPrefix",
    "Lots",
    "Money",
    "OrderState",
    "OrderType",
    "Percent",
    "Pips",
    "PrecisionError",
    "Price",
    "PromotionStatus",
    "RejectionCode",
    "ResultProvenance",
    "RiskVerdict",
    "Session",
    "Timeframe",
    "clamp",
    "floor_to_step",
    "new_id",
    "parse_prefix",
    "quantise_money",
    "quantise_percent",
    "quantise_pips",
    "quantise_price",
    "to_decimal",
    "validate_id",
]
