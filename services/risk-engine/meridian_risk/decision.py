"""RiskDecision — the authorisation token (architecture.md §5, invariants I1/I8).

"The risk engine cannot be overridden" is only true if there is no other code path
to an order. Two mechanisms make that structural rather than aspirational:

**Binding.** The decision carries a hash over the proposal's economic content
*and the final size*. The broker re-derives it from the order it is handed and
refuses on mismatch. This defeats the realistic failure: approve 0.2 lots, submit
2.0.

**Unforgeability.** ``RiskDecision`` cannot be constructed outside this module. A
module-private token is required by ``__post_init__``, and only the factory below
holds it. A strategy cannot fabricate an approval, and the test suite asserts it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from meridian_schemas.enums import RejectionCode, RiskVerdict

#: Held only by this module. Anything else constructing a RiskDecision must pass
#: it, and cannot.
_CONSTRUCTION_TOKEN: Final = object()


class DecisionForgeryError(RuntimeError):
    """A RiskDecision was constructed outside the risk engine."""


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    """One rule's verdict, retained so a rejection can list every reason (I5)."""

    code: RejectionCode
    passed: bool
    detail: str = ""
    #: Set when the rule clamps rather than rejects.
    clamp_to_lots: Decimal | None = None
    #: Before/after values for the operator-facing explanation.
    before: str | None = None
    after: str | None = None
    limit: str | None = None


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """The sole authorisation for an order. Immutable, hash-bound, unforgeable."""

    decision_id: str
    proposal_id: str
    proposal_hash: str
    verdict: RiskVerdict

    requested_size_lots: Decimal
    final_size_lots: Decimal
    requested_risk_pct: Decimal
    final_risk_pct: Decimal
    risk_amount_account_ccy: Decimal

    binding_constraint: RejectionCode | None
    reason_codes: tuple[RejectionCode, ...]
    explanation: str
    before_after: dict[str, dict[str, str]]

    rules_evaluated: int
    rules_passed: int
    rule_profile_version: str
    prop_profile_version: str | None
    evaluated_at: datetime
    outcomes: tuple[RuleOutcome, ...] = ()

    _token: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _CONSTRUCTION_TOKEN:
            raise DecisionForgeryError(
                "RiskDecision may only be created by the risk engine. Constructing "
                "one elsewhere would forge an authorisation and bypass every rule "
                "(invariant I8)."
            )

    @property
    def authorisation_hash(self) -> str:
        """Binds the proposal's content to the authorised size.

        The broker recomputes this from the order it receives. Changing either
        the trade's economics or its size invalidates the token.
        """
        material = f"{self.proposal_hash}|{self.final_size_lots}"
        return f"sha256:{hashlib.sha256(material.encode()).hexdigest()}"

    @property
    def is_approved(self) -> bool:
        return self.verdict in {RiskVerdict.APPROVED, RiskVerdict.APPROVED_REDUCED}

    @property
    def was_reduced(self) -> bool:
        return self.verdict is RiskVerdict.APPROVED_REDUCED

    def authorises(self, *, proposal_hash: str, size_lots: Decimal) -> bool:
        """Whether this decision authorises exactly this order.

        The check the paper broker performs before every fill. Both the economics
        and the size must match; either alone is insufficient.
        """
        if not self.is_approved:
            return False
        if proposal_hash != self.proposal_hash:
            return False
        return size_lots == self.final_size_lots


def build_decision(
    *,
    decision_id: str,
    proposal_id: str,
    proposal_hash: str,
    verdict: RiskVerdict,
    requested_size_lots: Decimal,
    final_size_lots: Decimal,
    requested_risk_pct: Decimal,
    final_risk_pct: Decimal,
    risk_amount_account_ccy: Decimal,
    binding_constraint: RejectionCode | None,
    reason_codes: tuple[RejectionCode, ...],
    explanation: str,
    before_after: dict[str, dict[str, str]],
    rules_evaluated: int,
    rules_passed: int,
    rule_profile_version: str,
    prop_profile_version: str | None,
    evaluated_at: datetime,
    outcomes: tuple[RuleOutcome, ...],
) -> RiskDecision:
    """The only constructor. Internal to the risk engine."""
    return RiskDecision(
        decision_id=decision_id,
        proposal_id=proposal_id,
        proposal_hash=proposal_hash,
        verdict=verdict,
        requested_size_lots=requested_size_lots,
        final_size_lots=final_size_lots,
        requested_risk_pct=requested_risk_pct,
        final_risk_pct=final_risk_pct,
        risk_amount_account_ccy=risk_amount_account_ccy,
        binding_constraint=binding_constraint,
        reason_codes=reason_codes,
        explanation=explanation,
        before_after=before_after,
        rules_evaluated=rules_evaluated,
        rules_passed=rules_passed,
        rule_profile_version=rule_profile_version,
        prop_profile_version=prop_profile_version,
        evaluated_at=evaluated_at,
        outcomes=outcomes,
        _token=_CONSTRUCTION_TOKEN,
    )
