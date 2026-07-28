"""Risk engine — the only path to an order.

Pure and deterministic: no clock, no I/O, no network, no float. The import-linter
contracts in pyproject.toml enforce that it can never reach the model router or
the database.
"""

from __future__ import annotations

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

__all__ = [
    "ConversionResult",
    "ConversionRoute",
    "ForexError",
    "SizingResult",
    "calculate_position_size",
    "convert_quote_to_account",
    "loss_at_stop",
    "margin_required",
    "pip_value_per_lot",
    "stop_distance_pips",
]
