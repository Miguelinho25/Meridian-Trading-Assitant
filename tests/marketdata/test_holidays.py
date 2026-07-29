"""Holiday closures are market structure, not data faults."""

from __future__ import annotations

from datetime import date

import pytest
from nemonis_marketdata.holidays import (
    easter_sunday,
    expected_closures,
    holidays_between,
    is_market_holiday,
)


class TestEaster:
    @pytest.mark.parametrize(
        ("year", "expected"),
        [
            (2010, date(2010, 4, 4)),
            (2015, date(2015, 4, 5)),
            (2020, date(2020, 4, 12)),
            (2024, date(2024, 3, 31)),
            (2025, date(2025, 4, 20)),
            (2026, date(2026, 4, 5)),
            (2030, date(2030, 4, 21)),
        ],
    )
    def test_known_dates(self, year: int, expected: date) -> None:
        """Computed, not tabulated — a table silently expires."""
        assert easter_sunday(year) == expected

    def test_always_a_sunday(self) -> None:
        for year in range(2000, 2050):
            assert easter_sunday(year).weekday() == 6


class TestClosures:
    def test_fixed_closures(self) -> None:
        assert is_market_holiday(date(2025, 1, 1))
        assert is_market_holiday(date(2025, 12, 25))
        assert is_market_holiday(date(2025, 12, 26))

    def test_good_friday_and_easter_monday(self) -> None:
        """The gap observed at 2025-04-17 -> 2025-04-22 in real data."""
        assert is_market_holiday(date(2025, 4, 18))  # Good Friday
        assert is_market_holiday(date(2025, 4, 21))  # Easter Monday

    def test_ordinary_days_are_not_holidays(self) -> None:
        assert not is_market_holiday(date(2025, 6, 11))
        assert not is_market_holiday(date(2025, 3, 4))

    def test_national_holidays_are_not_listed(self) -> None:
        """FX stays open and thin on these — a session-liquidity concern, not
        a data-quality one."""
        assert not is_market_holiday(date(2025, 11, 27))  # US Thanksgiving
        assert not is_market_holiday(date(2025, 7, 4))  # US Independence Day

    def test_range_query(self) -> None:
        days = holidays_between(date(2025, 1, 1), date(2025, 12, 31))
        assert date(2025, 1, 1) in days
        assert date(2025, 12, 25) in days
        assert date(2024, 12, 25) not in days

    def test_weekend_holidays_do_not_count_as_lost_days(self) -> None:
        """A holiday falling at a weekend costs no trading day, so counting it
        would over-forgive genuinely missing bars.

        In 2027 Christmas is a Saturday and Boxing Day a Sunday, so that window
        loses nothing.
        """
        assert date(2027, 12, 25).weekday() == 5  # Saturday
        assert date(2027, 12, 26).weekday() == 6  # Sunday
        assert expected_closures(date(2027, 12, 24), date(2027, 12, 27)) == 0

    def test_weekday_holidays_do_count(self) -> None:
        """The converse — otherwise the test above would pass on a broken
        function that always returned zero."""
        assert date(2025, 12, 25).weekday() == 3  # Thursday
        assert expected_closures(date(2025, 12, 24), date(2025, 12, 27)) == 2
