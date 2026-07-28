"""The critique service — routing, invocation and validation in one place.

The only entry point callers need. Every path through it ends in a usable
``AICritique``: a valid one when the model cooperated, an ``ABSTAIN`` when
anything at all went wrong.

Callers must not need a try/except. If they did, some caller would eventually
omit it and a model outage would take out the trading loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from meridian_router.critique import (
    CRITIQUE_SCHEMA_PROMPT,
    AICritique,
    abstain,
    contains_injection,
    parse_critique,
    wrap_untrusted,
)
from meridian_router.provider import Invocation, ModelProvider, NullProvider
from meridian_router.registry import ModelRegistry, Task


@dataclass(frozen=True, slots=True)
class CritiqueRequest:
    """Everything a model is told about a proposal.

    Note what is absent: no balance, no account number, no absolute money. The
    model reasons in percentages and R multiples, which is both safer and more
    comparable across accounts (model-routing.md §4).
    """

    instrument: str
    direction: str
    setup_type: str
    regime_label: str
    regime_confidence: Decimal
    reward_risk: Decimal | None
    strategy_confidence: Decimal
    risk_pct: Decimal
    session: str
    data_quality: str
    #: Retrieved historical cases, already redacted. Untrusted — user-authored.
    similar_cases: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    #: Free-text journal excerpts. Untrusted, wrapped before use.
    journal_excerpts: tuple[str, ...] = field(default_factory=tuple)
    risk_notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def retrieved_ids(self) -> frozenset[str]:
        return frozenset(str(c.get("trade_id", "")) for c in self.similar_cases)


@dataclass(frozen=True, slots=True)
class CritiqueOutcome:
    critique: AICritique
    invocation: Invocation | None = None
    model_key: str = ""
    degraded: bool = False
    degradation_reason: str = ""
    #: True when a journal excerpt contained instruction-like text. Surfaced to
    #: the operator: someone pasted something odd into their notes.
    injection_detected: bool = False


def build_prompt(request: CritiqueRequest) -> str:
    """Assemble the prompt. Untrusted content is delimited, never inlined."""
    lines = [
        "You are reviewing a proposed forex trade. You are advising, not deciding.",
        "The trade will be sized and risk-checked by deterministic code regardless "
        "of what you say.",
        "",
        "## Proposal",
        f"- Instrument: {request.instrument}",
        f"- Direction: {request.direction}",
        f"- Setup: {request.setup_type or 'unspecified'}",
        f"- Session: {request.session}",
        f"- Regime: {request.regime_label} (classifier confidence {request.regime_confidence})",
        f"- Reward:risk: {request.reward_risk if request.reward_risk is not None else 'no target'}",
        f"- Strategy confidence: {request.strategy_confidence}",
        f"- Risk: {request.risk_pct}% of equity",
        f"- Data quality: {request.data_quality}",
    ]

    if request.risk_notes:
        lines += ["", "## Risk engine notes", *(f"- {n}" for n in request.risk_notes)]

    if request.similar_cases:
        lines += ["", "## Retrieved historical cases"]
        for case in request.similar_cases:
            lines.append(
                f"- {case.get('trade_id')}: {case.get('instrument')} "
                f"{case.get('direction')}, regime {case.get('regime')}, "
                f"result {case.get('outcome_r')}R, relevance {case.get('relevance')}"
            )
        lines.append(
            "Only cite trade_ids from this list. Report their actual outcomes; do "
            "not assume similarity implies a good trade."
        )

    if request.journal_excerpts:
        lines += ["", "## Journal excerpts"]
        for excerpt in request.journal_excerpts:
            lines.append(wrap_untrusted(excerpt, label="journal note"))

    lines += ["", CRITIQUE_SCHEMA_PROMPT]
    return "\n".join(lines)


class CritiqueService:
    """Routes, invokes and validates. Never raises."""

    def __init__(
        self,
        *,
        registry: ModelRegistry | None = None,
        provider: ModelProvider | None = None,
        available_keys: frozenset[str] | None = None,
        budget_exhausted: bool = False,
    ) -> None:
        self.registry = registry or ModelRegistry()
        self.provider = provider or NullProvider()
        self.available_keys = available_keys
        self.budget_exhausted = budget_exhausted

    async def critique(self, request: CritiqueRequest, *, seed: int = 0) -> CritiqueOutcome:
        """Obtain a critique, or degrade cleanly.

        The whole method is a sequence of ways to end up with ``ABSTAIN``, and
        that is deliberate. The only path to a non-abstaining critique is a
        model that answered and whose answer survived every validation step.
        """
        injection = any(contains_injection(e) for e in request.journal_excerpts)

        decision = self.registry.route(
            Task.CRITIQUE_PROPOSAL,
            available_keys=self.available_keys,
            budget_exhausted=self.budget_exhausted,
        )
        if not decision.routed or decision.model is None:
            return CritiqueOutcome(
                critique=abstain(decision.reason, code="NO_MODEL"),
                degraded=True,
                degradation_reason=decision.reason,
                injection_detected=injection,
            )

        result = await self.provider.generate(
            decision.model, build_prompt(request), json_mode=True, seed=seed
        )
        if not result.ok:
            return CritiqueOutcome(
                critique=abstain(result.error, code="PROVIDER_FAILED"),
                invocation=result.invocation,
                model_key=decision.model.key,
                degraded=True,
                degradation_reason=result.error,
                injection_detected=injection,
            )

        critique = parse_critique(result.text, retrieved_trade_ids=request.retrieved_ids)
        return CritiqueOutcome(
            critique=critique,
            invocation=result.invocation,
            model_key=decision.model.key,
            degraded=critique.downgrade_reason != "",
            degradation_reason=critique.downgrade_reason,
            injection_detected=injection,
        )
