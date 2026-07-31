"""The kill switch.

The asymmetry running through these tests: engaging wrongly costs a halted paper
loop, releasing wrongly costs an unbounded amount. Every ambiguous case must
therefore resolve to engaged.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from nemonis_db.killswitch import (
    KillSwitchState,
    current_state,
    disengage,
    engage,
    history,
    resolve,
)
from sqlalchemy.ext.asyncio import AsyncSession

T = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


class TestTheCleanInitialState:
    async def test_no_events_means_clear(self, session: AsyncSession) -> None:
        """An empty log is a clean start, not an unreadable one."""
        state = await current_state(session)
        assert not state.engaged
        assert not state.indeterminate
        assert "Clear" in state.summary


class TestEngaging:
    async def test_engaging_blocks_trading(self, session: AsyncSession) -> None:
        state = await engage(session, reason="Spread blew out", actor="miguel", at=T)
        assert state.engaged
        assert state.blocks_trading
        assert state.reason == "Spread blew out"
        assert state.actor == "miguel"

    async def test_state_survives_a_fresh_read(self, session: AsyncSession) -> None:
        """The point of storing it: a running loop re-reads and sees the change
        without a restart."""
        await engage(session, reason="Data feed stale", actor="system", at=T)
        assert (await current_state(session)).engaged

    async def test_engaging_twice_is_not_an_error(self, session: AsyncSession) -> None:
        """An operator hitting the control twice during an incident must get the
        state they asked for, not an exception."""
        await engage(session, reason="first", actor="a", at=T)
        state = await engage(session, reason="second", actor="b", at=T + timedelta(minutes=1))
        assert state.engaged
        assert state.reason == "second"

    async def test_engaging_needs_no_reason(self, session: AsyncSession) -> None:
        """Moving toward safety must never be blocked on paperwork."""
        state = await engage(session, reason="", actor="miguel", at=T)
        assert state.engaged


class TestDisengagingIsHarder:
    """Engaging moves toward safety; releasing does not. They are not equally
    consequential and are deliberately not equally easy."""

    async def test_a_reason_is_mandatory(self, session: AsyncSession) -> None:
        await engage(session, reason="incident", actor="miguel", at=T)
        with pytest.raises(ValueError, match="requires a reason"):
            await disengage(session, reason="", actor="miguel", at=T + timedelta(minutes=5))

    async def test_whitespace_is_not_a_reason(self, session: AsyncSession) -> None:
        await engage(session, reason="incident", actor="miguel", at=T)
        with pytest.raises(ValueError, match="requires a reason"):
            await disengage(session, reason="   ", actor="miguel", at=T + timedelta(minutes=5))

    async def test_a_refused_release_leaves_it_engaged(self, session: AsyncSession) -> None:
        """The failure must not half-apply."""
        await engage(session, reason="incident", actor="miguel", at=T)
        with pytest.raises(ValueError, match="requires a reason"):
            await disengage(session, reason="", actor="miguel", at=T + timedelta(minutes=5))
        assert (await current_state(session)).engaged

    async def test_a_reasoned_release_works(self, session: AsyncSession) -> None:
        await engage(session, reason="incident", actor="miguel", at=T)
        state = await disengage(
            session,
            reason="Feed recovered, spreads normal for 30 minutes",
            actor="miguel",
            at=T + timedelta(minutes=30),
        )
        assert not state.engaged


class TestUnknownStateIsEngagedState:
    """Every component in this system fails closed. Halting a paper loop that
    did not need halting costs nothing; the reverse is unbounded."""

    async def test_an_unreadable_state_reports_engaged(self, session: AsyncSession) -> None:
        class Broken:
            async def execute(self, *_: object, **__: object) -> None:
                raise RuntimeError("database is gone")

        state = await current_state(Broken())  # type: ignore[arg-type]
        assert state.engaged
        assert state.indeterminate

    async def test_indeterminate_is_distinguishable_from_engaged(
        self, session: AsyncSession
    ) -> None:
        """An operator seeing ENGAGED deserves to know whether someone engaged
        it or whether the system simply cannot tell."""
        await engage(session, reason="deliberate", actor="miguel", at=T)
        deliberate = await current_state(session)
        assert deliberate.engaged
        assert not deliberate.indeterminate

        class Broken:
            async def execute(self, *_: object, **__: object) -> None:
                raise RuntimeError("gone")

        unknown = await current_state(Broken())  # type: ignore[arg-type]
        assert unknown.engaged
        assert unknown.indeterminate
        assert "could not be read" in unknown.summary


class TestConfigAndStoreAreInSeries:
    """Either source engaging it is enough. A deployment-level halt must not be
    overridable by a database write, and vice versa."""

    def test_config_engages_even_when_the_store_is_clear(self) -> None:
        clear = KillSwitchState(engaged=False, reason="", actor="", since=None)
        assert resolve(stored=clear, configured=True).engaged

    def test_the_store_engages_even_when_config_is_clear(self) -> None:
        stored = KillSwitchState(engaged=True, reason="incident", actor="m", since=None)
        assert resolve(stored=stored, configured=False).engaged

    def test_both_clear_permits_trading(self) -> None:
        clear = KillSwitchState(engaged=False, reason="", actor="", since=None)
        assert not resolve(stored=clear, configured=False).engaged

    def test_a_config_halt_names_itself(self) -> None:
        clear = KillSwitchState(engaged=False, reason="", actor="", since=None)
        assert "configuration" in resolve(stored=clear, configured=True).actor

    def test_an_indeterminate_store_stays_engaged_whatever_config_says(self) -> None:
        unknown = KillSwitchState(
            engaged=True, reason="unreadable", actor="system", since=None, indeterminate=True
        )
        assert resolve(stored=unknown, configured=False).engaged


class TestHistoryIsAppendOnly:
    async def test_every_transition_is_recorded(self, session: AsyncSession) -> None:
        await engage(session, reason="one", actor="a", at=T)
        await disengage(session, reason="fixed", actor="a", at=T + timedelta(minutes=5))
        await engage(session, reason="two", actor="b", at=T + timedelta(minutes=10))

        events = await history(session)
        assert len(events) == 3
        # Newest first: the current state leads.
        assert [e.engaged for e in events] == [True, False, True]
        assert events[0].reason == "two"

    async def test_releasing_does_not_erase_the_engagement(self, session: AsyncSession) -> None:
        """Why it was engaged is the first question asked afterwards."""
        await engage(session, reason="spread blew out", actor="system", at=T)
        await disengage(session, reason="recovered", actor="miguel", at=T + timedelta(hours=1))
        reasons = [e.reason for e in await history(session)]
        assert "spread blew out" in reasons
