"""Contract specifications for the default watchlist.

Every value here is a **plausible retail-broker default, not a verified fact about
any specific broker**. Contract specs differ between brokers and change without
notice: symbol names, pip size, lot step, stop level and swap rates all vary.
Nothing in the system may infer a spec from a symbol string.

Before connecting any broker, these must be replaced with that broker's published
specification and `spec_verified_at` set. Until then they carry
``spec_source="SYNTHETIC DEFAULT"`` so their provenance is never in doubt.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Final


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    symbol: str
    base_ccy: str
    quote_ccy: str
    digits: int
    pip_size: Decimal
    contract_size: Decimal
    min_lot: Decimal
    lot_step: Decimal
    max_lot: Decimal
    stop_level_points: int
    margin_rate: Decimal
    commission_per_lot: Decimal
    swap_long: Decimal
    swap_short: Decimal
    #: Typical spread in pips under normal liquidity. Drives the synthetic
    #: generator and the abnormal-spread threshold.
    typical_spread_pips: Decimal
    spec_source: str = "SYNTHETIC DEFAULT — verify against your broker"

    @property
    def is_jpy_quoted(self) -> bool:
        return self.quote_ccy == "JPY"

    def pips_to_price(self, pips: Decimal) -> Decimal:
        return pips * self.pip_size

    def price_to_pips(self, price_delta: Decimal) -> Decimal:
        return price_delta / self.pip_size

    @property
    def tick_size(self) -> Decimal:
        """The smallest price increment this instrument is quoted in.

        One unit in the last quoted digit: 0.00001 on a 5-digit pair, 0.001 on a
        3-digit JPY pair. Every price sent to a venue must be a multiple of this.
        An order priced between ticks is not "close enough" — it is rejected.
        """
        return Decimal(1).scaleb(-self.digits)

    def quantise_away_from(self, price: Decimal, reference: Decimal) -> Decimal:
        """Snap ``price`` onto the tick grid, moving *away* from ``reference``.

        For stops. Strategy levels are conceptual and arrive at full division
        precision — an ATR-scaled stop carries the repeating decimal of a 14-bar
        average — so they must be snapped to the grid before they can be placed.

        The direction of that snap is a safety property, not a rounding
        preference. Nearest-tick rounding would sometimes pull a stop *closer* to
        entry, shortening the distance the position was sized against and making
        realised risk exceed what the operator authorised. Rounding outward can
        only ever widen the stop, so the residual error is always in the safe
        direction. Same asymmetry, same reason as the lot flooring in
        :func:`nemonis_schemas.money.floor_to_step` (invariant I3).
        """
        rounding = ROUND_FLOOR if price < reference else ROUND_CEILING
        return price.quantize(self.tick_size, rounding=rounding)

    def quantise_toward(self, price: Decimal, reference: Decimal) -> Decimal:
        """Snap ``price`` onto the tick grid, moving *toward* ``reference``.

        The mirror of :meth:`quantise_away_from`, for targets. A target is only
        ever pulled nearer entry, so quantisation can shave a fraction of a tick
        off expected reward but can never inflate it — reported reward-to-risk
        stays honest rather than flattering.

        A target closer to entry than one tick collapses onto entry. That is
        degenerate geometry the strategies cannot produce (theirs sit multiple
        ATRs out), and it fails safe: the trade's reward goes to zero rather than
        its risk going up.
        """
        rounding = ROUND_CEILING if price < reference else ROUND_FLOOR
        return price.quantize(self.tick_size, rounding=rounding)


def _spec(
    symbol: str,
    base: str,
    quote: str,
    *,
    spread: str,
    swap_long: str = "-0.50",
    swap_short: str = "-0.30",
) -> InstrumentSpec:
    """Build a spec, deriving pip size and digits from the quote currency.

    JPY-quoted pairs use 0.01 and 3 digits; everything else 0.0001 and 5. The
    derivation lives here, once, rather than being re-guessed at call sites —
    but it is still recorded as an explicit field on the spec, because a broker
    may disagree and the field is what the system reads.
    """
    jpy = quote == "JPY"
    return InstrumentSpec(
        symbol=symbol,
        base_ccy=base,
        quote_ccy=quote,
        digits=3 if jpy else 5,
        pip_size=Decimal("0.01") if jpy else Decimal("0.0001"),
        contract_size=Decimal("100000"),
        min_lot=Decimal("0.01"),
        lot_step=Decimal("0.01"),
        max_lot=Decimal("50"),
        stop_level_points=0,
        margin_rate=Decimal("0.0333"),  # ~30:1 retail leverage
        commission_per_lot=Decimal("3.50"),
        swap_long=Decimal(swap_long),
        swap_short=Decimal(swap_short),
        typical_spread_pips=Decimal(spread),
    )


#: The default watchlist from the brief: majors, minors and crosses.
WATCHLIST: Final[dict[str, InstrumentSpec]] = {
    s.symbol: s
    for s in (
        _spec("EURUSD", "EUR", "USD", spread="0.8"),
        _spec("GBPUSD", "GBP", "USD", spread="1.1"),
        _spec("USDJPY", "USD", "JPY", spread="0.9"),
        _spec("USDCHF", "USD", "CHF", spread="1.3"),
        _spec("AUDUSD", "AUD", "USD", spread="1.0"),
        _spec("NZDUSD", "NZD", "USD", spread="1.6"),
        _spec("USDCAD", "USD", "CAD", spread="1.4"),
        _spec("EURGBP", "EUR", "GBP", spread="1.2"),
        _spec("EURJPY", "EUR", "JPY", spread="1.4"),
        _spec("GBPJPY", "GBP", "JPY", spread="2.2"),
    )
}


def get_spec(symbol: str) -> InstrumentSpec:
    """Look up a contract spec. Unknown symbols raise rather than defaulting."""
    try:
        return WATCHLIST[symbol]
    except KeyError:
        raise KeyError(
            f"No contract specification for {symbol!r}. Specs are never inferred "
            f"from a symbol string — add it to WATCHLIST with verified values."
        ) from None


#: Rough correlation clusters, used by the risk engine's correlated-exposure rule.
#: Deliberately coarse and static in this milestone: a rolling estimate is a Stage E
#: concern, and an unstable estimate would make position limits jitter.
CORRELATION_CLUSTERS: Final[dict[str, tuple[str, ...]]] = {
    "USD_MAJOR": ("EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"),
    "USD_QUOTE_INVERSE": ("USDCHF", "USDCAD", "USDJPY"),
    "JPY_CROSS": ("USDJPY", "EURJPY", "GBPJPY"),
    "EUR_CROSS": ("EURUSD", "EURGBP", "EURJPY"),
    "GBP_CROSS": ("GBPUSD", "EURGBP", "GBPJPY"),
    "COMMODITY": ("AUDUSD", "NZDUSD", "USDCAD"),
}
