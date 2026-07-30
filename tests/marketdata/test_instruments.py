"""Tick-grid quantisation: prices a venue would actually accept.

Strategy levels arrive as concepts at whatever precision the arithmetic left
behind — an ATR-scaled stop carries the repeating decimal of a 14-bar mean, so a
EURUSD stop can reach the pipeline as 1.053292142857142857142857143. No venue
accepts a price between ticks; such an order is rejected, not rounded politely.

The *direction* of the snap is the part that matters for safety, and it is
asymmetric on purpose. Nearest-tick rounding would sometimes pull a stop closer
to entry, shortening the distance the position was sized against and making
realised risk exceed authorised risk. Stops therefore round outward and targets
inward, so the residual fraction of a tick is always spent against the trade.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from nemonis_marketdata.instruments import WATCHLIST, InstrumentSpec, get_spec

EURUSD = get_spec("EURUSD")
USDJPY = get_spec("USDJPY")

#: The value observed on a live paper position, and the reason this module
#: exists. 1/7 repeats, so an ATR-14 mean never terminates.
OFF_GRID_STOP = Decimal("1.053292142857142857142857143")


def on_grid(price: Decimal, spec: InstrumentSpec) -> bool:
    """True when ``price`` is an exact multiple of the instrument's tick."""
    return price % spec.tick_size == 0


def awkward_offsets() -> list[Decimal]:
    """Offsets whose decimal expansions do not terminate.

    Dividing by 7, 3 and 11 reproduces the shape of a real ATR mean. Terminating
    offsets would sit on the grid already and quietly assert nothing.
    """
    return [Decimal(n) / Decimal(d) / Decimal(10000) for n in (1, 5, 23, 97) for d in (3, 7, 11)]


class TestTheGridMatchesTheQuoteConvention:
    def test_a_five_digit_pair_quotes_in_hundred_thousandths(self) -> None:
        assert EURUSD.digits == 5
        assert EURUSD.tick_size == Decimal("0.00001")

    def test_a_jpy_pair_quotes_in_thousandths(self) -> None:
        assert USDJPY.digits == 3
        assert USDJPY.tick_size == Decimal("0.001")

    @pytest.mark.parametrize("spec", WATCHLIST.values(), ids=lambda s: s.symbol)
    def test_a_pip_is_ten_ticks_on_every_instrument(self, spec: InstrumentSpec) -> None:
        """The two scales are separate fields and could drift apart. A pip is a
        tenth of a pip's worth of ticks on every retail FX convention."""
        assert spec.pip_size == spec.tick_size * 10


class TestPricesLandOnTheGrid:
    def test_the_observed_repeating_stop_is_snapped(self) -> None:
        entry = Decimal("1.08000")
        quantised = EURUSD.quantise_away_from(OFF_GRID_STOP, entry)

        assert on_grid(quantised, EURUSD)
        assert quantised == Decimal("1.05329")

    def test_a_price_already_on_the_grid_is_left_alone(self) -> None:
        entry = Decimal("1.08000")
        for method in (EURUSD.quantise_away_from, EURUSD.quantise_toward):
            assert method(Decimal("1.05329"), entry) == Decimal("1.05329")

    @pytest.mark.parametrize("spec", WATCHLIST.values(), ids=lambda s: s.symbol)
    def test_every_instrument_snaps_both_levels(self, spec: InstrumentSpec) -> None:
        entry = Decimal("100") if spec.is_jpy_quoted else Decimal("1.08")
        for offset in awkward_offsets():
            for raw in (entry - offset, entry + offset):
                assert on_grid(spec.quantise_away_from(raw, entry), spec)
                assert on_grid(spec.quantise_toward(raw, entry), spec)


class TestQuantisingNeverTightensAStop:
    """The safety property. A stop may end further from entry, never nearer."""

    @pytest.mark.parametrize("spec", WATCHLIST.values(), ids=lambda s: s.symbol)
    def test_the_stop_distance_never_shrinks(self, spec: InstrumentSpec) -> None:
        entry = Decimal("100") if spec.is_jpy_quoted else Decimal("1.08")
        for offset in awkward_offsets():
            for raw in (entry - offset, entry + offset):
                quantised = spec.quantise_away_from(raw, entry)
                assert abs(entry - quantised) >= abs(entry - raw), (
                    f"{spec.symbol}: quantising {raw} against entry {entry} produced "
                    f"{quantised}, which is nearer entry than the strategy asked for. "
                    f"The position was sized against the wider distance, so realised "
                    f"risk now exceeds authorised risk."
                )

    def test_a_long_stop_moves_down_or_stays(self) -> None:
        entry = Decimal("1.08000")
        assert EURUSD.quantise_away_from(Decimal("1.0532999"), entry) == Decimal("1.05329")

    def test_a_short_stop_moves_up_or_stays(self) -> None:
        """Above entry, "away" is upward — the mirror image, and the case a
        single ROUND_FLOOR would silently get wrong."""
        entry = Decimal("1.08000")
        assert EURUSD.quantise_away_from(Decimal("1.1067001"), entry) == Decimal("1.10671")

    def test_the_signal_ordering_still_holds_after_snapping(self) -> None:
        """A LONG stop must stay strictly below entry, or ``Signal`` rejects it.
        Rounding away can only increase the gap, so this cannot regress."""
        entry = Decimal("1.08000")
        for offset in awkward_offsets():
            assert EURUSD.quantise_away_from(entry - offset, entry) < entry
            assert EURUSD.quantise_away_from(entry + offset, entry) > entry


class TestQuantisingNeverFlattersATarget:
    """The mirror property. Reward may shrink by a fraction of a tick, never grow."""

    @pytest.mark.parametrize("spec", WATCHLIST.values(), ids=lambda s: s.symbol)
    def test_the_target_distance_never_grows(self, spec: InstrumentSpec) -> None:
        entry = Decimal("100") if spec.is_jpy_quoted else Decimal("1.08")
        for offset in awkward_offsets():
            for raw in (entry - offset, entry + offset):
                quantised = spec.quantise_toward(raw, entry)
                assert abs(entry - quantised) <= abs(entry - raw), (
                    f"{spec.symbol}: quantising target {raw} against entry {entry} "
                    f"produced {quantised}, further out than the strategy claimed. "
                    f"Reported reward-to-risk would flatter the trade."
                )

    def test_a_long_target_moves_down_or_stays(self) -> None:
        entry = Decimal("1.08000")
        assert EURUSD.quantise_toward(Decimal("1.1067999"), entry) == Decimal("1.10679")

    def test_a_short_target_moves_down_or_stays(self) -> None:
        entry = Decimal("1.08000")
        assert EURUSD.quantise_toward(Decimal("1.0532001"), entry) == Decimal("1.05321")


class TestTheTwoDirectionsDisagree:
    def test_an_off_grid_price_snaps_to_different_ticks(self) -> None:
        """Guards against both methods collapsing to the same rounding mode — a
        refactor could do that and every on-grid assertion above would still pass."""
        entry = Decimal("1.08000")
        raw = OFF_GRID_STOP

        away = EURUSD.quantise_away_from(raw, entry)
        toward = EURUSD.quantise_toward(raw, entry)

        assert away < raw < toward
        assert toward - away == EURUSD.tick_size
