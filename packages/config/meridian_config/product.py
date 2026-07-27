"""Product identity.

One of only three places the product name appears (the others being
``apps/web/config/product.ts`` and the ``MERIDIAN_`` environment prefix).
Renaming the product is an edit to these three, not a codebase-wide search.
"""

from __future__ import annotations

PRODUCT_NAME = "Meridian"
PRODUCT_SLUG = "meridian"
ENV_PREFIX = "MERIDIAN_"
VERSION = "0.1.0"

TAGLINE = "Forex research, backtesting and risk-control platform"

# Shown wherever the user could mistake this for a live trading system.
SAFETY_NOTICE = (
    "Research and paper-trading only. This build cannot place real-money orders: "
    "no broker adapter exists."
)
