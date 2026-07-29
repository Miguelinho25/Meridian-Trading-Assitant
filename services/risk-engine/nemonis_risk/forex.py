"""Forex arithmetic (risk-engine.md §3).

The calculations most often got wrong, specified exactly and tested to the edge.
Pure functions: no clock, no I/O, no float. Every quantity is ``Decimal``.

The asymmetry to remember: **lot quantisation floors**. Rounding a lot size up
would risk more than the operator authorised, so every rounding decision here is
made in the direction that reduces risk.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from nemonis_marketdata.instruments import InstrumentSpec
from nemonis_schemas.enums import Direction, RejectionCode
from nemonis_schemas.money import floor_to_step, quantise_money

#: Smallest money increment. Budgets floor to this so a rounding step can never
#: authorise more risk than was requested.
MONEY_STEP = Decimal("0.01")


class ForexError(ValueError):
    """A forex calculation could not be completed safely."""

    def __init__(self, message: str, code: RejectionCode) -> None:
        super().__init__(message)
        self.code = code


class ConversionRoute(StrEnum):
    """How a quote-to-account conversion was obtained. Recorded for audit."""

    IDENTITY = "IDENTITY"  # quote currency is the account currency
    DIRECT = "DIRECT"  # QUOTE/ACCT quoted directly
    INVERSE = "INVERSE"  # ACCT/QUOTE quoted; use 1/rate
    TRIANGULATED = "TRIANGULATED"  # via USD


@dataclass(frozen=True, slots=True)
class ConversionResult:
    rate: Decimal
    route: ConversionRoute
    #: Pairs consulted, so a surprising size can be traced to its inputs.
    via: tuple[str, ...]


def pip_value_per_lot(spec: InstrumentSpec, fx_rate_quote_to_account: Decimal) -> Decimal:
    """Account-currency value of one pip, for one standard lot.

    pip_value_quote = pip_size × contract_size
    pip_value_acct  = pip_value_quote × fx(QUOTE → ACCT)
    """
    if fx_rate_quote_to_account <= 0:
        raise ForexError(
            f"Non-positive FX rate {fx_rate_quote_to_account} for {spec.symbol}",
            RejectionCode.FX_CONVERSION_UNAVAILABLE,
        )
    return spec.pip_size * spec.contract_size * fx_rate_quote_to_account


def convert_quote_to_account(
    quote_ccy: str,
    account_ccy: str,
    rates: dict[str, Decimal],
    *,
    conservative: bool = True,
) -> ConversionResult:
    """Resolve QUOTE → ACCOUNT, in the documented order.

    ``rates`` maps a 6-letter pair to its price, e.g. ``{"EURUSD": 1.085}``.

    Resolution order is identity, direct, inverse, triangulate via USD, then
    **reject**. There is deliberately no fallback to 1.0: a missing rate means we
    do not know the account-currency value of the position, and guessing produces
    a position size that is wrong by the size of the FX move — silently.
    """
    if quote_ccy == account_ccy:
        return ConversionResult(Decimal(1), ConversionRoute.IDENTITY, ())

    direct = f"{quote_ccy}{account_ccy}"
    if direct in rates and rates[direct] > 0:
        return ConversionResult(rates[direct], ConversionRoute.DIRECT, (direct,))

    inverse = f"{account_ccy}{quote_ccy}"
    if inverse in rates and rates[inverse] > 0:
        return ConversionResult(Decimal(1) / rates[inverse], ConversionRoute.INVERSE, (inverse,))

    # Triangulate via USD, the near-universal vehicle currency.
    if quote_ccy != "USD" and account_ccy != "USD":
        quote_usd = _leg_to_usd(quote_ccy, rates)
        usd_account = _usd_to_leg(account_ccy, rates)
        if quote_usd is not None and usd_account is not None:
            rate, legs = quote_usd
            rate2, legs2 = usd_account
            return ConversionResult(rate * rate2, ConversionRoute.TRIANGULATED, legs + legs2)

    raise ForexError(
        f"No conversion route from {quote_ccy} to {account_ccy}. Tried direct "
        f"({direct}), inverse ({inverse}) and triangulation via USD. Refusing to "
        f"assume a rate — an assumed rate produces a silently wrong position size.",
        RejectionCode.FX_CONVERSION_UNAVAILABLE,
    )


def _leg_to_usd(ccy: str, rates: dict[str, Decimal]) -> tuple[Decimal, tuple[str, ...]] | None:
    """Value of one unit of ``ccy`` in USD."""
    if ccy == "USD":
        return Decimal(1), ()
    pair = f"{ccy}USD"
    if pair in rates and rates[pair] > 0:
        return rates[pair], (pair,)
    inverse = f"USD{ccy}"
    if inverse in rates and rates[inverse] > 0:
        return Decimal(1) / rates[inverse], (inverse,)
    return None


def _usd_to_leg(ccy: str, rates: dict[str, Decimal]) -> tuple[Decimal, tuple[str, ...]] | None:
    """Value of one USD in ``ccy``."""
    if ccy == "USD":
        return Decimal(1), ()
    pair = f"USD{ccy}"
    if pair in rates and rates[pair] > 0:
        return rates[pair], (pair,)
    inverse = f"{ccy}USD"
    if inverse in rates and rates[inverse] > 0:
        return Decimal(1) / rates[inverse], (inverse,)
    return None


def stop_distance_pips(
    spec: InstrumentSpec, entry: Decimal, stop: Decimal, direction: Direction
) -> Decimal:
    """Distance from entry to stop, in pips.

    Validates that the stop is on the correct side. A long with its stop above
    entry is not a wide stop — it is a sign convention error, and sizing from
    ``abs()`` alone would silently produce a plausible lot size for a nonsensical
    trade.
    """
    if entry <= 0 or stop <= 0:
        raise ForexError(
            f"Non-positive price: entry={entry} stop={stop}",
            RejectionCode.STOP_DISTANCE_INVALID,
        )
    if entry == stop:
        raise ForexError(
            "Stop equals entry: stop distance is zero, position size is undefined",
            RejectionCode.STOP_DISTANCE_INVALID,
        )
    if direction is Direction.LONG and stop >= entry:
        raise ForexError(
            f"LONG stop {stop} is at or above entry {entry} — wrong side",
            RejectionCode.STOP_DISTANCE_INVALID,
        )
    if direction is Direction.SHORT and stop <= entry:
        raise ForexError(
            f"SHORT stop {stop} is at or below entry {entry} — wrong side",
            RejectionCode.STOP_DISTANCE_INVALID,
        )
    return abs(entry - stop) / spec.pip_size


@dataclass(frozen=True, slots=True)
class SizingResult:
    lots: Decimal
    risk_amount_account_ccy: Decimal
    #: What the trade actually risks after flooring — always ≤ requested.
    realised_risk_account_ccy: Decimal
    realised_risk_pct: Decimal
    stop_pips: Decimal
    pip_value_per_lot: Decimal
    conversion: ConversionResult


def calculate_position_size(
    *,
    spec: InstrumentSpec,
    equity: Decimal,
    risk_pct: Decimal,
    entry: Decimal,
    stop: Decimal,
    direction: Direction,
    account_ccy: str,
    rates: dict[str, Decimal],
) -> SizingResult:
    """Position size from risk, stop distance and pip value (risk-engine.md §3.3).

        risk_amount = equity × risk_pct
        raw_lots    = risk_amount / (stop_pips × pip_value_per_lot)
        lots        = floor_to_step(raw_lots)   ← always down
        lots        = clamp(lots, min_lot, max_lot)

    Raises rather than returning an unusable size, so a caller cannot accidentally
    trade on a rejected calculation.
    """
    if equity <= 0:
        raise ForexError(f"Non-positive equity {equity}", RejectionCode.ACCOUNT_STATE_AMBIGUOUS)
    if risk_pct <= 0:
        raise ForexError(f"Non-positive risk {risk_pct}%", RejectionCode.STOP_DISTANCE_INVALID)

    pips = stop_distance_pips(spec, entry, stop, direction)
    conversion = convert_quote_to_account(spec.quote_ccy, account_ccy, rates)
    per_lot = pip_value_per_lot(spec, conversion.rate)

    # Floored, not rounded. Half-even rounding would push a budget of 102.295 up
    # to 102.30 — authorising fractionally more than the operator asked for, and
    # letting realised risk exceed the requested *percentage* even though it stays
    # under the rounded amount. Found by a property test, not by inspection.
    risk_amount = floor_to_step(equity * risk_pct / Decimal(100), MONEY_STEP)
    denominator = pips * per_lot
    if denominator <= 0:
        raise ForexError(
            f"Degenerate sizing denominator for {spec.symbol}: {pips} pips × {per_lot} per lot",
            RejectionCode.SIZING_INVARIANT_VIOLATED,
        )

    raw_lots = risk_amount / denominator
    lots = floor_to_step(raw_lots, spec.lot_step)

    if lots > spec.max_lot:
        lots = floor_to_step(spec.max_lot, spec.lot_step)

    if lots < spec.min_lot:
        raise ForexError(
            f"Computed size {lots} is below the {spec.min_lot} minimum lot for "
            f"{spec.symbol}. Risking {risk_pct}% over a {pips:.1f}-pip stop needs "
            f"{raw_lots:.4f} lots; the account is too small for this stop distance.",
            RejectionCode.SIZE_BELOW_MINIMUM_LOT,
        )

    realised = floor_to_step(lots * denominator, MONEY_STEP)

    # Belt and braces on invariant I3. Should be unreachable — floor_to_step
    # guarantees it — but if it ever fires, failing loudly beats trading.
    if realised > risk_amount:
        raise ForexError(
            f"Sizing invariant violated: {lots} lots risks {realised} against an "
            f"authorised {risk_amount}. This is a defect, not a market condition.",
            RejectionCode.SIZING_INVARIANT_VIOLATED,
        )

    realised_pct = (realised / equity) * Decimal(100) if equity else Decimal(0)

    return SizingResult(
        lots=lots,
        risk_amount_account_ccy=risk_amount,
        realised_risk_account_ccy=realised,
        realised_risk_pct=realised_pct,
        stop_pips=pips,
        pip_value_per_lot=per_lot,
        conversion=conversion,
    )


def loss_at_stop(
    *, spec: InstrumentSpec, lots: Decimal, stop_pips: Decimal, pip_value_per_lot: Decimal
) -> Decimal:
    """Account-currency loss if the stop is hit exactly, before slippage."""
    return quantise_money(lots * stop_pips * pip_value_per_lot)


def margin_required(
    *, spec: InstrumentSpec, lots: Decimal, price: Decimal, fx_base_to_account: Decimal
) -> Decimal:
    """Margin for a position, in account currency.

    Uses the *base* currency conversion: notional is base-denominated
    (``lots × contract_size`` units of base), so converting from the quote
    currency here would be wrong by the exchange rate.
    """
    notional_base = lots * spec.contract_size
    return quantise_money(notional_base * fx_base_to_account * spec.margin_rate)
