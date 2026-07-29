"""System-wide hard limits.

The ceiling every other tier is clamped against (risk-engine.md §2). Values here
can be *tightened* by an account, profile, strategy or instrument, and can never
be loosened by any of them.

Deliberately module-level constants rather than settings: raising a ceiling should
require a code change and a review, not an environment variable. The one exception
is ``MAX_RISK_PER_TRADE_PCT``, which may be lowered (never raised) from the
environment — see ``settings.py``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

# --- Per-trade risk -------------------------------------------------------

#: Absolute ceiling on per-trade risk as a percentage of account equity.
#: No profile, prompt, UI control or API call may exceed this.
MAX_RISK_PER_TRADE_PCT: Final = Decimal("1.00")

#: Floor below which a configured risk is treated as a mistake rather than caution.
MIN_RISK_PER_TRADE_PCT: Final = Decimal("0.01")

# --- Aggregate risk -------------------------------------------------------

MAX_OPEN_RISK_PCT: Final = Decimal("5.00")
MAX_DAILY_RISK_BUDGET_PCT: Final = Decimal("5.00")
MAX_SIMULTANEOUS_POSITIONS: Final = 20
MAX_TRADES_PER_SESSION: Final = 50

# --- Drawdown -------------------------------------------------------------

#: Fraction of allowed drawdown above which new trades are blocked outright.
DRAWDOWN_BLOCK_THRESHOLD: Final = Decimal("0.75")

#: Fraction of allowed drawdown that arms the kill switch.
DRAWDOWN_KILL_THRESHOLD: Final = Decimal("0.90")

# --- Data quality ---------------------------------------------------------

MAX_DATA_AGE_SECONDS_CEILING: Final = 300
MAX_SPREAD_MULTIPLE_OF_MEDIAN: Final = Decimal("5.0")

# --- Decimal precision (data-model.md §1) ---------------------------------

PRICE_DP: Final = 10
MONEY_DP: Final = 2
PERCENT_DP: Final = 4
PIP_DP: Final = 1
LOT_DP: Final = 2

# --- Execution ------------------------------------------------------------

#: Live broker execution is not implemented. This constant exists so that any
#: code path assuming otherwise fails a test rather than surprising someone.
LIVE_EXECUTION_IMPLEMENTED: Final = False
