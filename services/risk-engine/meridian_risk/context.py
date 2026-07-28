"""Risk evaluation inputs.

The engine is pure (I6): it reads no clock, opens no connection and queries no
database. Everything it needs arrives in a ``RiskContext`` assembled by the
caller, which is what makes an evaluation reproducible from stored inputs alone —
and therefore what makes a disputed decision auditable months later.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from meridian_config.settings import ApprovalMode, Mode, RiskProfileName
from meridian_marketdata.instruments import InstrumentSpec
from meridian_marketdata.quality import QualityReport
from meridian_schemas.enums import Direction, Session


@dataclass(frozen=True, slots=True)
class TradeProposal:
    """A strategy's request. Expresses concepts, never a lot size."""

    proposal_id: str
    strategy_id: str
    strategy_version: str
    instrument: str
    direction: Direction
    entry: Decimal
    stop: Decimal
    target: Decimal | None
    #: What the strategy asks to risk. The engine may reduce it, never raise it.
    requested_risk_pct: Decimal
    #: From the strategy, or from the allocator once it exists.
    confidence: Decimal
    #: When the decision is being made — the bar open, not the close.
    decision_time: datetime
    setup_type: str = ""
    regime_label: str = ""

    @property
    def content_hash(self) -> str:
        """Canonical hash of the economically meaningful fields.

        Covers what a mutation would need to change to matter: instrument,
        direction, entry, stop and target. Deliberately excludes ``confidence``
        and ``setup_type``, which do not alter the trade's risk — binding those
        would invalidate tokens on cosmetic edits without adding safety.
        """
        payload = {
            "instrument": self.instrument,
            "direction": self.direction.value,
            "entry": str(self.entry),
            "stop": str(self.stop),
            "target": str(self.target) if self.target is not None else None,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"

    @property
    def reward_risk(self) -> Decimal | None:
        """Reward-to-risk from the concepts. None when no target is given."""
        if self.target is None:
            return None
        risk = abs(self.entry - self.stop)
        if risk == 0:
            return None
        return abs(self.target - self.entry) / risk


@dataclass(frozen=True, slots=True)
class OpenPosition:
    """An existing position, for exposure accounting."""

    instrument: str
    direction: Direction
    lots: Decimal
    entry: Decimal
    stop: Decimal
    strategy_id: str
    #: Risk still at stake if the stop is hit, as a percentage of equity.
    open_risk_pct: Decimal


@dataclass(frozen=True, slots=True)
class AccountState:
    account_id: str
    currency: str
    balance: Decimal
    equity: Decimal
    high_water_mark: Decimal

    #: Drawdown allowance consumed, as a fraction in [0, 1]. Supplied by the
    #: prop-firm engine so the throttle's denominator matches the active profile.
    drawdown_consumed: Decimal

    daily_loss_used: Decimal
    daily_loss_limit: Decimal
    total_loss_used: Decimal
    total_loss_limit: Decimal

    consecutive_losses: int = 0
    trades_this_session: int = 0
    margin_used: Decimal = Decimal(0)

    #: False when reconciliation failed. Blocks trading (I7).
    is_reconciled: bool = True

    @property
    def daily_loss_remaining(self) -> Decimal:
        return max(Decimal(0), self.daily_loss_limit - self.daily_loss_used)

    @property
    def total_loss_remaining(self) -> Decimal:
        return max(Decimal(0), self.total_loss_limit - self.total_loss_used)


@dataclass(frozen=True, slots=True)
class MarketState:
    """Market conditions at the decision instant."""

    spec: InstrumentSpec
    bid: Decimal
    ask: Decimal
    quality: QualityReport
    session: Session
    is_weekend: bool
    is_rollover: bool
    #: Minutes to the nearest high-impact economic event, or None if unknown.
    #: None is treated as *unknown*, not *clear* — the news gate fails closed.
    minutes_to_news: int | None
    atr: Decimal | None
    #: Current spread relative to the instrument's typical, as a multiple.
    spread_multiple: Decimal
    #: Volatility relative to its own recent norm.
    volatility_ratio: Decimal | None = None

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


@dataclass(frozen=True, slots=True)
class PortfolioState:
    """Everything already at risk, across all strategies."""

    open_positions: tuple[OpenPosition, ...] = ()
    #: Proposals already approved earlier in this same evaluation set. Their risk
    #: is committed even though no fill has occurred, so it must count against
    #: the budget — otherwise a set of proposals could jointly overspend.
    pending_risk_pct: Decimal = Decimal(0)

    @property
    def open_risk_pct(self) -> Decimal:
        return (
            sum((p.open_risk_pct for p in self.open_positions), Decimal(0)) + self.pending_risk_pct
        )

    @property
    def position_count(self) -> int:
        return len(self.open_positions)

    def risk_in_instrument(self, instrument: str) -> Decimal:
        return sum(
            (p.open_risk_pct for p in self.open_positions if p.instrument == instrument),
            Decimal(0),
        )

    def risk_by_strategy(self, strategy_id: str) -> Decimal:
        return sum(
            (p.open_risk_pct for p in self.open_positions if p.strategy_id == strategy_id),
            Decimal(0),
        )

    def currency_exposure(self, specs: dict[str, InstrumentSpec]) -> dict[str, Decimal]:
        """Net risk per currency, counting **both legs**.

        Long EURUSD is long EUR *and* short USD. Ignoring the quote leg is a
        common omission that understates concentration — a book of long EURUSD,
        long GBPUSD and long AUDUSD is one large short-USD bet, not three
        diversified positions.
        """
        exposure: dict[str, Decimal] = {}
        for position in self.open_positions:
            spec = specs.get(position.instrument)
            if spec is None:
                continue
            sign = Decimal(1) if position.direction is Direction.LONG else Decimal(-1)
            exposure[spec.base_ccy] = (
                exposure.get(spec.base_ccy, Decimal(0)) + sign * position.open_risk_pct
            )
            exposure[spec.quote_ccy] = (
                exposure.get(spec.quote_ccy, Decimal(0)) - sign * position.open_risk_pct
            )
        return exposure


@dataclass(frozen=True, slots=True)
class RiskContext:
    """Complete, self-contained input to one evaluation."""

    proposal: TradeProposal
    account: AccountState
    market: MarketState
    portfolio: PortfolioState
    mode: Mode
    approval_mode: ApprovalMode
    profile_name: RiskProfileName
    #: Fail-closed: if the kill-switch state could not be read, pass True.
    kill_switch_engaged: bool
    #: FX rates for account-currency conversion.
    rates: dict[str, Decimal] = field(default_factory=dict)
    #: Specs for every instrument held, for both-leg exposure accounting.
    specs: dict[str, InstrumentSpec] = field(default_factory=dict)
    #: Content hashes of orders already live, for duplicate detection.
    active_order_hashes: frozenset[str] = frozenset()
    emergency_shutdown: bool = False
    strategy_approved: bool = True
    instrument_approved: bool = True
    session_approved: bool = True
    cooldown_active: bool = False
