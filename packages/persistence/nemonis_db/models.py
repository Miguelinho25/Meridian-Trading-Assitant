"""ORM models.

Stage A defines the safety-critical spine: accounts, instruments, proposals, risk
assessments, orders and the hash-chained audit log. The remaining entities from
data-model.md arrive with the services that own them.

Two constraints here carry architectural guarantees rather than mere hygiene:

* ``orders.risk_decision_id`` is NOT NULL — an order without a risk decision is a
  schema violation, not a convention (invariant I1).
* ``audit_events`` is hash-chained and append-only.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from nemonis_db.types import DecimalText, UTCDateTime


class Base(DeclarativeBase):
    pass


def _enum_check(column: str, values: type) -> CheckConstraint:
    """CHECK constraint instead of a native ENUM (data-model.md §4).

    Native enums differ between backends and adding a value on Postgres is a
    special operation. A VARCHAR plus CHECK behaves identically on both.
    """
    allowed = ", ".join(f"'{v.value}'" for v in values)  # type: ignore[attr-defined]
    return CheckConstraint(f"{column} IN ({allowed})", name=f"ck_{column}")


class Instrument(Base):
    """Contract specification. Never inferred from the symbol string."""

    __tablename__ = "instruments"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    broker_symbol: Mapped[str | None] = mapped_column(String(40))
    base_ccy: Mapped[str] = mapped_column(String(3))
    quote_ccy: Mapped[str] = mapped_column(String(3))

    digits: Mapped[int] = mapped_column(Integer)
    pip_size: Mapped[Decimal] = mapped_column(DecimalText)
    contract_size: Mapped[Decimal] = mapped_column(DecimalText)
    min_lot: Mapped[Decimal] = mapped_column(DecimalText)
    lot_step: Mapped[Decimal] = mapped_column(DecimalText)
    max_lot: Mapped[Decimal] = mapped_column(DecimalText)
    stop_level_points: Mapped[int] = mapped_column(Integer, default=0)
    freeze_level_points: Mapped[int] = mapped_column(Integer, default=0)
    margin_rate: Mapped[Decimal] = mapped_column(DecimalText)
    commission_per_lot: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal("0"))
    swap_long: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal("0"))
    swap_short: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal("0"))

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    spec_source: Mapped[str | None] = mapped_column(String(200))
    spec_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)

    __table_args__ = (
        CheckConstraint("pip_size > '0'", name="ck_instrument_pip_size_positive"),
        CheckConstraint("min_lot > '0'", name="ck_instrument_min_lot_positive"),
        CheckConstraint("lot_step > '0'", name="ck_instrument_lot_step_positive"),
    )


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    currency: Mapped[str] = mapped_column(String(3))
    starting_balance: Mapped[Decimal] = mapped_column(DecimalText)
    balance: Mapped[Decimal] = mapped_column(DecimalText)
    equity: Mapped[Decimal] = mapped_column(DecimalText)
    high_water_mark: Mapped[Decimal] = mapped_column(DecimalText)

    #: Synthetic accounts can never be confused with real ones downstream.
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=True)
    prop_profile_id: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class TradeProposal(Base):
    __tablename__ = "trade_proposals"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(40), ForeignKey("accounts.id"))
    instrument_id: Mapped[str] = mapped_column(String(40), ForeignKey("instruments.id"))
    strategy_version_id: Mapped[str | None] = mapped_column(String(40))

    direction: Mapped[str] = mapped_column(String(10))
    entry_price: Mapped[Decimal] = mapped_column(DecimalText)
    stop_price: Mapped[Decimal] = mapped_column(DecimalText)
    target_price: Mapped[Decimal | None] = mapped_column(DecimalText)
    requested_risk_pct: Mapped[Decimal] = mapped_column(DecimalText)
    confidence: Mapped[Decimal | None] = mapped_column(DecimalText)

    #: Canonical hash binding this proposal's content. The risk decision references
    #: it, and the broker re-derives it — architecture.md §5.
    proposal_hash: Mapped[str] = mapped_column(String(80), index=True)

    event_time: Mapped[datetime] = mapped_column(UTCDateTime)
    decision_time: Mapped[datetime] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)

    __table_args__ = (
        CheckConstraint("direction IN ('LONG', 'SHORT')", name="ck_proposal_direction"),
        CheckConstraint("entry_price > '0'", name="ck_proposal_entry_positive"),
        CheckConstraint("stop_price > '0'", name="ck_proposal_stop_positive"),
        Index("ix_proposal_account_time", "account_id", "event_time"),
    )


class RiskAssessment(Base):
    """Immutable record of a risk decision. Append-only."""

    __tablename__ = "risk_assessments"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(String(40), ForeignKey("trade_proposals.id"))
    proposal_hash: Mapped[str] = mapped_column(String(80), index=True)

    verdict: Mapped[str] = mapped_column(String(20))
    requested_size_lots: Mapped[Decimal] = mapped_column(DecimalText)
    final_size_lots: Mapped[Decimal] = mapped_column(DecimalText)
    requested_risk_pct: Mapped[Decimal] = mapped_column(DecimalText)
    final_risk_pct: Mapped[Decimal] = mapped_column(DecimalText)
    risk_amount_account_ccy: Mapped[Decimal] = mapped_column(DecimalText)

    binding_constraint: Mapped[str | None] = mapped_column(String(60))
    reason_codes: Mapped[str] = mapped_column(Text, default="[]")  # JSON array
    explanation: Mapped[str] = mapped_column(Text)
    before_after: Mapped[str] = mapped_column(Text, default="{}")  # JSON object

    rules_evaluated: Mapped[int] = mapped_column(Integer)
    rules_passed: Mapped[int] = mapped_column(Integer)
    rule_profile_version: Mapped[str] = mapped_column(String(60))
    prop_profile_version: Mapped[str | None] = mapped_column(String(60))

    evaluated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)

    __table_args__ = (
        CheckConstraint(
            "verdict IN ('APPROVED', 'APPROVED_REDUCED', 'REJECTED')",
            name="ck_risk_verdict",
        ),
        CheckConstraint("final_size_lots >= '0'", name="ck_risk_size_non_negative"),
    )


class Order(Base):
    """An order. Cannot exist without a risk decision — invariant I1."""

    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(40), ForeignKey("accounts.id"))
    instrument_id: Mapped[str] = mapped_column(String(40), ForeignKey("instruments.id"))
    proposal_id: Mapped[str] = mapped_column(String(40), ForeignKey("trade_proposals.id"))

    #: NOT NULL by design. The database refuses an unauthorised order.
    risk_decision_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("risk_assessments.id"), nullable=False, index=True
    )

    order_type: Mapped[str] = mapped_column(String(20))
    direction: Mapped[str] = mapped_column(String(10))
    size_lots: Mapped[Decimal] = mapped_column(DecimalText)
    limit_price: Mapped[Decimal | None] = mapped_column(DecimalText)
    stop_loss: Mapped[Decimal | None] = mapped_column(DecimalText)
    take_profit: Mapped[Decimal | None] = mapped_column(DecimalText)

    state: Mapped[str] = mapped_column(String(30), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)

    risk_assessment: Mapped[RiskAssessment] = relationship()

    __table_args__ = (
        CheckConstraint("size_lots > '0'", name="ck_order_size_positive"),
        CheckConstraint("direction IN ('LONG', 'SHORT')", name="ck_order_direction"),
    )


class OrderStateTransition(Base):
    __tablename__ = "order_state_transitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(40), ForeignKey("orders.id"), index=True)
    from_state: Mapped[str | None] = mapped_column(String(30))
    to_state: Mapped[str] = mapped_column(String(30))
    actor: Mapped[str] = mapped_column(String(60))
    reason: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(UTCDateTime)


class AuditEvent(Base):
    """Append-only, hash-chained audit record (security.md §5).

    ``sequence`` is a dense integer rather than a timestamp because two events in
    the same millisecond must still have an unambiguous order for the chain to
    verify.
    """

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)

    #: Assigned explicitly by ``append_event`` from the chain head, not by the
    #: database: SQLite autoincrements only an INTEGER PRIMARY KEY, and the head
    #: is already being read to obtain ``prev_hash``, so this costs nothing extra.
    #: Under concurrent writers the UNIQUE constraint turns a race into a failed
    #: transaction rather than a corrupted chain — fail-closed, as intended.
    sequence: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(40), index=True)

    entity_type: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(40), index=True)
    actor: Mapped[str] = mapped_column(String(60))
    request_id: Mapped[str | None] = mapped_column(String(40))

    payload: Mapped[str] = mapped_column(Text)  # canonical JSON, redacted
    prev_hash: Mapped[str] = mapped_column(String(80))
    hash: Mapped[str] = mapped_column(String(80), unique=True)

    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)

    __table_args__ = (UniqueConstraint("sequence", name="uq_audit_sequence"),)


class KillSwitchEvent(Base):
    __tablename__ = "kill_switch_events"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    engaged: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(60))
    at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    severity: Mapped[str] = mapped_column(String(20), index=True)
    category: Mapped[str] = mapped_column(String(60))
    summary: Mapped[str] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    raised_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        CheckConstraint(
            "severity IN ('INFO', 'WARNING', 'ERROR', 'CRITICAL')", name="ck_incident_severity"
        ),
    )


#: Tables that must never be updated or deleted (data-model.md §1).
APPEND_ONLY_TABLES: frozenset[str] = frozenset(
    {"audit_events", "risk_assessments", "order_state_transitions", "kill_switch_events"}
)


# --- Backtest research records ---------------------------------------------
#
# A backtest result is worthless without the exact inputs that produced it, so
# these tables store the full reproducibility manifest alongside the numbers.
#
# Rows are inserted once, when a run finishes, and never updated. A status
# column that transitions RUNNING -> COMPLETED would require an UPDATE and give
# up append-only, which is the guarantee that makes an archive trustworthy years
# later. A process that dies mid-run therefore leaves no row — acceptable,
# because an unfinished run has no results to archive and its manifest can be
# recomputed from the same inputs.


class BacktestRun(Base):
    """One backtest, with everything needed to reproduce it.

    ``manifest_hash`` is the reproducibility key: two runs sharing it must
    produce the same ``result_hash``. A divergence means determinism has broken
    and is detectable by query rather than by memory.
    """

    __tablename__ = "backtest_runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)

    #: Hash over every input. The comparison key for reproducibility.
    manifest_hash: Mapped[str] = mapped_column(String(80), index=True)
    #: Hash over every output, including per-trade fingerprints.
    result_hash: Mapped[str] = mapped_column(String(80), index=True)
    #: The complete canonical manifest as JSON. Stored whole, not only as
    #: columns, so a run stays readable if the manifest dataclass later changes
    #: shape — the columns are for querying, this is the record of truth.
    manifest_json: Mapped[str] = mapped_column(Text)
    manifest_version: Mapped[str] = mapped_column(String(20))

    status: Mapped[str] = mapped_column(String(20))

    # --- Code identity ---
    strategy_key: Mapped[str] = mapped_column(String(80), index=True)
    strategy_version: Mapped[str] = mapped_column(String(40))
    strategy_lifecycle: Mapped[str] = mapped_column(String(30), default="")
    git_commit: Mapped[str] = mapped_column(String(64), default="")
    git_branch: Mapped[str] = mapped_column(String(120), default="")
    #: True when tracked files differed from the commit at run time.
    git_dirty: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Denormalised from git state so the Lab can filter without parsing JSON.
    #: A dirty tree means the commit does not identify the code that ran, and
    #: the uncommitted edits are unrecoverable — permanently irreproducible.
    is_reproducible: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    engine_version: Mapped[str] = mapped_column(String(20), default="")
    feature_pipeline_version: Mapped[str] = mapped_column(String(20), default="")
    risk_profile_version: Mapped[str] = mapped_column(String(40), default="")

    # --- Data identity ---
    market_data_provider: Mapped[str] = mapped_column(String(40), default="")
    dataset_version: Mapped[str] = mapped_column(String(40), default="")
    instruments: Mapped[str] = mapped_column(Text, default="[]")  # JSON array
    timeframe: Mapped[str] = mapped_column(String(10), default="")
    data_start: Mapped[datetime | None] = mapped_column(UTCDateTime)
    data_end: Mapped[datetime | None] = mapped_column(UTCDateTime)
    bar_count: Mapped[int] = mapped_column(Integer, default=0)
    #: SYNTHETIC results must never be mistaken for real ones.
    provenance: Mapped[str] = mapped_column(String(20), default="", index=True)
    spread_assumed: Mapped[bool] = mapped_column(Boolean, default=False)

    # --- Execution model ---
    spread_model: Mapped[str] = mapped_column(String(40), default="")
    slippage_model: Mapped[str] = mapped_column(String(40), default="")
    commission_model: Mapped[str] = mapped_column(String(40), default="")
    risk_profile: Mapped[str] = mapped_column(String(30), default="")
    starting_balance: Mapped[Decimal | None] = mapped_column(DecimalText)
    account_currency: Mapped[str] = mapped_column(String(10), default="")
    seed: Mapped[int] = mapped_column(Integer, default=0)
    parameters: Mapped[str] = mapped_column(Text, default="{}")  # JSON object
    #: {role: "model_id@version"}. Empty is the normal, honest case.
    ai_models: Mapped[str] = mapped_column(Text, default="{}")

    # --- Results ---
    metrics: Mapped[str] = mapped_column(Text, default="{}")  # JSON object
    validation: Mapped[str] = mapped_column(Text, default="{}")  # JSON object
    #: Whether every validation gate passed. Nullable: absent means validation
    #: was not run, which is different from run-and-failed and must not be
    #: displayed as a pass.
    survives_all: Mapped[bool | None] = mapped_column(Boolean, index=True)
    #: Whether the metrics may be described as evidence at all — sufficient
    #: sample, out-of-sample provenance, no disqualifying flag.
    is_evidence: Mapped[bool] = mapped_column(Boolean, default=False)
    trade_count: Mapped[int] = mapped_column(Integer, default=0)
    final_balance: Mapped[Decimal | None] = mapped_column(DecimalText)
    net_pnl: Mapped[Decimal | None] = mapped_column(DecimalText)
    max_drawdown_pct: Mapped[Decimal | None] = mapped_column(DecimalText)
    signals_generated: Mapped[int] = mapped_column(Integer, default=0)
    proposals_made: Mapped[int] = mapped_column(Integer, default=0)
    rejections: Mapped[int] = mapped_column(Integer, default=0)

    # --- Timing ---
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)

    notes: Mapped[str] = mapped_column(Text, default="")

    # lazy="raise": under asyncio a lazy load raises MissingGreenlet at the
    # point of access, which surfaces far from the cause. Refusing outright
    # turns that into an immediate, legible error and forces callers through
    # get_equity_curve/get_trades, which the list view can then skip entirely
    # rather than dragging every equity point into a summary query.
    equity_curve: Mapped[list[BacktestEquityPoint]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="raise"
    )
    trades: Mapped[list[BacktestTrade]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (
        CheckConstraint("status IN ('COMPLETED', 'FAILED')", name="ck_backtest_status"),
        Index("ix_backtest_strategy_created", "strategy_key", "created_at"),
        # Finds determinism breaks: same inputs, different outputs.
        Index("ix_backtest_reproducibility", "manifest_hash", "result_hash"),
    )


class BacktestEquityPoint(Base):
    """One point on the equity and drawdown curves.

    Drawdown is derivable from equity, but is stored rather than recomputed:
    a chart that recalculates it could disagree with the peak the engine
    actually enforced limits against.
    """

    __tablename__ = "backtest_equity_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(40), ForeignKey("backtest_runs.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    at: Mapped[datetime] = mapped_column(UTCDateTime)
    equity: Mapped[Decimal] = mapped_column(DecimalText)
    balance: Mapped[Decimal] = mapped_column(DecimalText)
    drawdown_pct: Mapped[Decimal] = mapped_column(DecimalText)
    open_positions: Mapped[int] = mapped_column(Integer, default=0)

    run: Mapped[BacktestRun] = relationship(back_populates="equity_curve", lazy="raise")

    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_equity_run_sequence"),)


class BacktestTrade(Base):
    """One closed trade generated by a run.

    Stored individually rather than only in aggregate: two runs can reach the
    same net P&L through entirely different trades, and the summary alone would
    hide that determinism had broken.
    """

    __tablename__ = "backtest_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(40), ForeignKey("backtest_runs.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)

    instrument: Mapped[str] = mapped_column(String(20), index=True)
    direction: Mapped[str] = mapped_column(String(10))
    strategy_key: Mapped[str] = mapped_column(String(80), default="")

    entry_at: Mapped[datetime] = mapped_column(UTCDateTime)
    exit_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    entry_price: Mapped[Decimal] = mapped_column(DecimalText)
    exit_price: Mapped[Decimal | None] = mapped_column(DecimalText)
    stop_price: Mapped[Decimal | None] = mapped_column(DecimalText)
    target_price: Mapped[Decimal | None] = mapped_column(DecimalText)
    lots: Mapped[Decimal | None] = mapped_column(DecimalText)

    exit_reason: Mapped[str] = mapped_column(String(30), default="")
    pnl: Mapped[Decimal | None] = mapped_column(DecimalText)
    r_multiple: Mapped[Decimal | None] = mapped_column(DecimalText)
    commission: Mapped[Decimal | None] = mapped_column(DecimalText)
    #: Session and regime at entry, for the conditional-performance analysis the
    #: platform is meant to discover rather than have hardcoded.
    session: Mapped[str] = mapped_column(String(20), default="")
    regime_label: Mapped[str] = mapped_column(String(30), default="")

    run: Mapped[BacktestRun] = relationship(back_populates="trades", lazy="raise")

    __table_args__ = (
        CheckConstraint("direction IN ('LONG', 'SHORT')", name="ck_backtest_trade_direction"),
        UniqueConstraint("run_id", "sequence", name="uq_trade_run_sequence"),
    )
