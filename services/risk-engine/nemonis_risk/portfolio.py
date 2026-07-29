"""Set-based evaluation (ADR-0007, invariant I11).

With one strategy, per-trade risk dominates. With fifty, the binding constraint
moves to the portfolio: fifty proposals can each pass their own checks and
jointly be long EUR across the whole book.

Evaluating them one at a time as they arrive has two defects. It lets the set
overspend the shared budget, because no proposal knows what the others committed.
And it makes the outcome depend on **arrival order**, which is not reproducible —
a backtest replayed twice could allocate the budget differently, which would
quietly destroy determinism everywhere downstream.

So a set is evaluated as a set: ranked deterministically, then processed in rank
order with each proposal seeing the risk already committed by higher-ranked ones.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from nemonis_schemas.enums import RiskVerdict

from nemonis_risk.context import OpenPosition, RiskContext
from nemonis_risk.decision import RiskDecision
from nemonis_risk.engine import RiskEngine


@dataclass(frozen=True, slots=True)
class SetEvaluation:
    """Outcome of evaluating concurrent proposals against one budget."""

    decisions: tuple[RiskDecision, ...]
    #: Proposal IDs in the order they were evaluated. Reproducible.
    ranking: tuple[str, ...]
    total_approved_risk_pct: Decimal

    @property
    def approved(self) -> tuple[RiskDecision, ...]:
        return tuple(d for d in self.decisions if d.is_approved)

    @property
    def rejected(self) -> tuple[RiskDecision, ...]:
        return tuple(d for d in self.decisions if not d.is_approved)

    def for_proposal(self, proposal_id: str) -> RiskDecision | None:
        return next((d for d in self.decisions if d.proposal_id == proposal_id), None)


def rank_proposals(contexts: Sequence[RiskContext]) -> tuple[RiskContext, ...]:
    """Order proposals deterministically for budget allocation.

    Primary key is allocator confidence, descending — the strategy with the most
    evidence behind it gets first call on the budget.

    Ties break on the proposal's **content hash**, not on arrival order, list
    position or timestamp. The hash is a stable function of the trade's
    economics, so the same set of proposals ranks identically on every machine
    and every replay. Arrival order would be none of those things.

    Two further keys follow, because content hash alone is not a total order:
    two strategies can propose an economically identical trade — same instrument,
    entry, stop and target — and hash the same. Falling through to Python's
    stable sort would then preserve *input order*, reintroducing exactly the
    arrival-order dependence this ranking exists to remove. ``strategy_id`` and
    ``proposal_id`` complete the ordering.
    """
    return tuple(
        sorted(
            contexts,
            key=lambda c: (
                -c.proposal.confidence,
                c.proposal.content_hash,
                c.proposal.strategy_id,
                c.proposal.proposal_id,
            ),
        )
    )


class PortfolioRiskEngine:
    """Evaluates concurrent proposals against a shared budget."""

    def __init__(self, engine: RiskEngine | None = None) -> None:
        self.engine = engine or RiskEngine()

    def evaluate_set(
        self, contexts: Sequence[RiskContext], *, evaluated_at: datetime
    ) -> SetEvaluation:
        """Evaluate proposals as a set.

        Approvals enter the book as provisional positions, so each subsequent
        proposal sees not just how much risk the set has committed but *where* —
        which is what lets the correlation, currency and instrument rules apply
        across a set rather than only against pre-existing positions.

        A proposal that would have passed alone can therefore be reduced or
        rejected because the budget is already spoken for. That is the correct
        behaviour and the whole point of set evaluation.
        """
        if not contexts:
            return SetEvaluation((), (), Decimal(0))

        ordered = rank_proposals(contexts)
        decisions: list[RiskDecision] = []
        committed = Decimal(0)
        provisional: list[OpenPosition] = []

        for ctx in ordered:
            adjusted = replace(
                ctx,
                portfolio=replace(
                    ctx.portfolio,
                    open_positions=ctx.portfolio.open_positions + tuple(provisional),
                    pending_risk_pct=ctx.portfolio.pending_risk_pct,
                ),
            )
            decision = self.engine.evaluate(adjusted, evaluated_at=evaluated_at)
            decisions.append(decision)

            if decision.verdict in {RiskVerdict.APPROVED, RiskVerdict.APPROVED_REDUCED}:
                committed += decision.final_risk_pct
                # Enter the book provisionally. Tracking only a total would leave
                # the correlation and currency rules blind to what the set has
                # already taken on — so three long USD-majors would each pass as
                # though independent, when they are one short-USD bet.
                provisional.append(
                    OpenPosition(
                        instrument=ctx.proposal.instrument,
                        direction=ctx.proposal.direction,
                        lots=decision.final_size_lots,
                        entry=ctx.proposal.entry,
                        stop=ctx.proposal.stop,
                        strategy_id=ctx.proposal.strategy_id,
                        open_risk_pct=decision.final_risk_pct,
                    )
                )

        return SetEvaluation(
            decisions=tuple(decisions),
            ranking=tuple(c.proposal.proposal_id for c in ordered),
            total_approved_risk_pct=committed,
        )
