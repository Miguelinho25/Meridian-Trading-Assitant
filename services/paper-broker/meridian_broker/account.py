"""Positions and account accounting.

The reconciliation invariant — ``equity == balance + floating P&L`` — is checked
rather than assumed. A mismatch means the ledger and the position book disagree,
and in that state no size, no drawdown figure and no limit check can be trusted.
The risk engine's ``ACCOUNT_STATE_AMBIGUOUS`` gate exists for exactly this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from meridian_marketdata.instruments import InstrumentSpec
from meridian_risk.forex import convert_quote_to_account, margin_required
from meridian_schemas.enums import Direction
from meridian_schemas.money import quantise_money


@dataclass(slots=True)
class Position:
    position_id: str
    instrument: str
    direction: Direction
    lots: Decimal
    entry_price: Decimal
    opened_at: datetime
    strategy_id: str
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    commission_paid: Decimal = Decimal(0)
    #: Best and worst prices seen while open, for MFE/MAE attribution.
    best_price: Decimal | None = None
    worst_price: Decimal | None = None

    def unrealised_quote(self, current_price: Decimal, spec: InstrumentSpec) -> Decimal:
        """Floating P&L in the quote currency."""
        move = current_price - self.entry_price
        if self.direction is Direction.SHORT:
            move = -move
        return move * self.lots * spec.contract_size

    def mark(self, price: Decimal) -> None:
        """Record excursion extremes. Called on every bar the position is open."""
        if self.best_price is None:
            self.best_price = self.worst_price = price
            return
        if self.direction is Direction.LONG:
            self.best_price = max(self.best_price, price)
            self.worst_price = min(self.worst_price or price, price)
        else:
            self.best_price = min(self.best_price, price)
            self.worst_price = max(self.worst_price or price, price)

    def excursions(self, spec: InstrumentSpec) -> tuple[Decimal, Decimal]:
        """(MFE, MAE) in pips. Zero while unmarked."""
        if self.best_price is None or self.worst_price is None:
            return Decimal(0), Decimal(0)
        if self.direction is Direction.LONG:
            mfe = (self.best_price - self.entry_price) / spec.pip_size
            mae = (self.entry_price - self.worst_price) / spec.pip_size
        else:
            mfe = (self.entry_price - self.best_price) / spec.pip_size
            mae = (self.worst_price - self.entry_price) / spec.pip_size
        return max(Decimal(0), mfe), max(Decimal(0), mae)


class ReconciliationError(RuntimeError):
    """The ledger and the position book disagree."""


@dataclass(slots=True)
class Account:
    account_id: str
    currency: str
    starting_balance: Decimal
    balance: Decimal
    high_water_mark: Decimal
    positions: dict[str, Position] = field(default_factory=dict)
    #: Balance at the last daily reset, for the prop-firm daily-loss rule.
    balance_at_day_start: Decimal = Decimal(0)
    highest_equity_today: Decimal = Decimal(0)
    realised_pnl: Decimal = Decimal(0)
    total_commission: Decimal = Decimal(0)
    #: Recomputed by ``reconcile`` on every call. Blocks trading while false.
    is_reconciled: bool = True

    def __post_init__(self) -> None:
        if self.balance_at_day_start == 0:
            self.balance_at_day_start = self.balance
        if self.highest_equity_today == 0:
            self.highest_equity_today = self.balance

    def unpriceable_positions(
        self, prices: dict[str, Decimal], specs: dict[str, InstrumentSpec]
    ) -> tuple[str, ...]:
        """Positions that cannot be valued from the supplied prices.

        A real feed has gaps — one instrument may have no bar on a date the
        others do. While that persists, equity is unknowable and trading must
        block; when the price returns, it must recover.
        """
        return tuple(
            p.position_id
            for p in self.positions.values()
            if specs.get(p.instrument) is None or prices.get(p.instrument) is None
        )

    def floating_pnl(
        self,
        prices: dict[str, Decimal],
        specs: dict[str, InstrumentSpec],
        rates: dict[str, Decimal],
    ) -> Decimal:
        """Unrealised P&L across all *priceable* positions, in account currency.

        Pure: it reports a number and mutates nothing. Callers needing to know
        whether the figure is complete must ask ``unpriceable_positions`` —
        an earlier version set ``is_reconciled`` here as a side effect, which
        latched permanently on the first data gap and silently disabled trading
        for the remainder of a run.
        """
        total = Decimal(0)
        for position in self.positions.values():
            spec = specs.get(position.instrument)
            price = prices.get(position.instrument)
            if spec is None or price is None:
                continue
            quote_pnl = position.unrealised_quote(price, spec)
            conversion = convert_quote_to_account(spec.quote_ccy, self.currency, rates)
            total += quote_pnl * conversion.rate
        return quantise_money(total)

    def equity(
        self,
        prices: dict[str, Decimal],
        specs: dict[str, InstrumentSpec],
        rates: dict[str, Decimal],
    ) -> Decimal:
        return quantise_money(self.balance + self.floating_pnl(prices, specs, rates))

    def margin_used(
        self,
        prices: dict[str, Decimal],
        specs: dict[str, InstrumentSpec],
        rates: dict[str, Decimal],
    ) -> Decimal:
        total = Decimal(0)
        for position in self.positions.values():
            spec = specs.get(position.instrument)
            price = prices.get(position.instrument)
            if spec is None or price is None:
                continue
            base_rate = convert_quote_to_account(spec.base_ccy, self.currency, rates).rate
            total += margin_required(
                spec=spec, lots=position.lots, price=price, fx_base_to_account=base_rate
            )
        return quantise_money(total)

    def free_margin(
        self,
        prices: dict[str, Decimal],
        specs: dict[str, InstrumentSpec],
        rates: dict[str, Decimal],
    ) -> Decimal:
        return self.equity(prices, specs, rates) - self.margin_used(prices, specs, rates)

    def reconcile(
        self,
        prices: dict[str, Decimal],
        specs: dict[str, InstrumentSpec],
        rates: dict[str, Decimal],
        *,
        tolerance: Decimal = Decimal("0.01"),
    ) -> bool:
        """Assert equity equals balance plus floating P&L.

        Should be trivially true — it is the definition — but it catches the
        cases that matter: a position closed without its P&L booked, a double
        credit, an unpriceable instrument. Any of those corrupts every downstream
        limit check, so the account is marked unreconciled and trading blocks.
        """
        unpriceable = self.unpriceable_positions(prices, specs)
        floating = self.floating_pnl(prices, specs, rates)
        expected = quantise_money(self.balance + floating)
        actual = self.equity(prices, specs, rates)
        arithmetic_ok = abs(expected - actual) <= tolerance

        # Recomputed fresh every call, never latched. A transient data gap must
        # block trading while it lasts and stop blocking once it clears.
        self.is_reconciled = arithmetic_ok and not unpriceable
        return self.is_reconciled

    def book_realised(self, amount: Decimal, *, commission: Decimal = Decimal(0)) -> None:
        """Book a closed trade's P&L and update the high-water mark."""
        self.realised_pnl += amount
        self.total_commission += commission
        self.balance = quantise_money(self.balance + amount - commission)
        self.high_water_mark = max(self.high_water_mark, self.balance)

    def start_new_day(self) -> None:
        """Reset the daily reference points. Called at the prop profile's reset."""
        self.balance_at_day_start = self.balance
        self.highest_equity_today = self.balance

    def mark_equity(self, equity: Decimal) -> None:
        self.highest_equity_today = max(self.highest_equity_today, equity)
