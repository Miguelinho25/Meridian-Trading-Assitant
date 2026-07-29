"""Configuration, safety limits, clocks, redaction and logging.

The lowest layer. Imports nothing from the rest of the system.
"""

from __future__ import annotations

from nemonis_config.clock import Clock, ClockError, FrozenClock, ReplayClock, SystemClock
from nemonis_config.logging import configure_logging, get_logger
from nemonis_config.product import PRODUCT_NAME, PRODUCT_SLUG, SAFETY_NOTICE, VERSION
from nemonis_config.redaction import contains_secret, redact_mapping, redact_text
from nemonis_config.settings import (
    ApprovalMode,
    ConfigurationError,
    Mode,
    RiskProfileName,
    Settings,
    get_settings,
    reset_settings_cache,
)

__all__ = [
    "PRODUCT_NAME",
    "PRODUCT_SLUG",
    "SAFETY_NOTICE",
    "VERSION",
    "ApprovalMode",
    "Clock",
    "ClockError",
    "ConfigurationError",
    "FrozenClock",
    "Mode",
    "ReplayClock",
    "RiskProfileName",
    "Settings",
    "SystemClock",
    "configure_logging",
    "contains_secret",
    "get_logger",
    "get_settings",
    "redact_mapping",
    "redact_text",
    "reset_settings_cache",
]
