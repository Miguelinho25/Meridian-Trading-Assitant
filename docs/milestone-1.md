# Milestone 1 — Foundation Vertical Slice

Branch `build/foundation-vertical-slice`. Proposed scope for approval before
implementation begins.

---

## Objective

One honest path through the system, end to end: synthetic data → features → baseline
strategy → deterministic risk decision → paper fill → journal note → Obsidian → local AI
critique → kill switch. Narrow and complete, rather than broad and hollow.

**Success is a user completing all twelve steps in the brief's acceptance list on their
own machine, with no paid API and no Docker.**

---

## Build order

Ordered so each stage is testable before the next depends on it. Risk engine before
paper broker before backtest engine is deliberate — the broker's authorisation check
cannot be written before the decision token exists, and a backtest without a broker is
just a loop.

### Stage A — Foundation
`packages/config` (settings, hard limits, redaction) · `packages/schemas` (Pydantic →
JSON Schema → TypeScript) · SQLAlchemy models + first Alembic migration · repositories
with append-only guards · structured logging · FastAPI shell with request IDs and
health · Next.js shell · pytest/vitest/Playwright harness · Makefile · docker-compose.yml.

**Done when:** `make setup && make test` passes; API health endpoint reports component
status; audit hash chain verifies on an empty database.

### Stage B — Market data and features
Provider interface · synthetic generator (seeded, realistic sessions, spreads, gaps,
weekend closure) · Parquet/CSV importer · replay provider · quality scoring · `BarView`
with `LookAheadError` · feature pipeline · regime classifier v0 · ten-instrument
watchlist with full contract specs.

Additionally, per [ADR-0006](decisions/0006-ml-as-meta-labelling.md):
**immutable point-in-time feature store** with independent `feature_version`, and every
feature declaring its lookback window explicitly (which also yields purge and embargo
lengths for later model validation). Both are far cheaper now than retrofitted.

**Done when:** replaying the same seed twice is byte-identical; a deliberate look-ahead
attempt raises; quality gate blocks on stale and crossed quotes; a stored feature row is
reproducible from its `source_bar_hash` and cannot be silently recomputed.

### Stage C — Risk engine ← *the critical stage*
Decimal arithmetic (pip value, fx conversion, sizing) · all five rule tiers · limit
composition · five profiles · drawdown throttle · prop-firm engine + generic profile ·
`RiskDecision` with proposal hash · audit integration.

**Done when:** the full test matrix in [risk-engine.md §9](risk-engine.md#9-test-matrix)
passes, including every adversarial case. This stage is not "done" at 90%.

### Stage D — Execution
Order state machine with validated transitions · paper broker (market/limit/stop, SL/TP,
modify, cancel) · authorisation-token verification · fill model with bid/ask, spread,
slippage, commission, gaps · position and account accounting · reconciliation invariant.

**Done when:** a mutated proposal against a valid token is rejected and raises an
incident; accounting reconciles after a randomised order sequence.

### Stage E — Backtest and analytics
Event loop · walk-forward · Monte Carlo · stress tests · metrics with sample sizes and
intervals · bias flags · two labelled baseline strategies.

**Done when:** identical runs produce identical ledgers; bias flags fire on
deliberately-overfit fixtures.

### Stage F — Memory and AI
Vault writer (atomic, backup, traversal-safe) · templates · link generation ·
`journal_notes` sync with allowlist · embeddings via `nomic-embed-text` · `VectorStore`
with numpy backend · metadata-filtered retrieval · model registry and router · Ollama
provider · structured critique with validation and injection defence.

**Done when:** the full suite passes with `MERIDIAN_OLLAMA_ENABLED=false`; injection
fixtures are rejected; a malformed response degrades to `ABSTAIN`.

### Stage G — Interface
Shell with the persistent risk indicator (mode, profile, drawdown, daily loss remaining,
kill switch) · Command Centre · Risk Lab with asymmetric risk controls · Backtest Lab ·
Journal · kill switch. Real data throughout — no disconnected mock screens.

**Done when:** the twelve acceptance steps complete in a Playwright run.

### Stage H — Validation and handoff
Full suite, static analysis, type checks, migration tests, determinism, leakage checks,
security review, seed data, README, the 15-point handoff report.

---

## In scope

Ten forex pairs · synthetic + replay + importer providers · two baseline strategies
(explicitly labelled research infrastructure, not profitable systems) · complete risk
engine · generic prop-firm profile + editor · paper broker · backtest with walk-forward,
Monte Carlo and stress tests · five dashboards · Obsidian generation and sync ·
local-model critique · kill switch · tests · docs.

## Out of scope for Milestone 1

Live broker execution (**not built, by design**) · the remaining seven dashboards
(Live Market, Trade Proposals, Prop-Firm Control, Strategy Lab, Neural Memory,
Analytics, AI Research Desk — shells only) · cloud model providers (interfaces built,
disabled, untested against real endpoints) · all trained models, incl. meta-labelling
and HMM/clustering regime classifiers ([ADR-0006](decisions/0006-ml-as-meta-labelling.md)) ·
real market-data vendors · multi-user · deployment.

---

## Acceptance — the twelve steps

Each becomes a Playwright test in Stage H.

1. Start the application locally.
2. Load synthetic or imported historical data.
3. Configure a prop-firm rule profile.
4. Select a risk profile.
5. Run a backtest.
6. Inspect the equity curve and drawdown.
7. Review accepted and rejected trades.
8. Replay the strategy through the paper broker.
9. Generate Obsidian trade notes.
10. Search for related historical trades.
11. Request a structured local-model critique.
12. Activate the global kill switch.

---

## Known risks

| Risk | Mitigation |
|---|---|
| Risk engine is large and correctness-critical | Built test-first; its own test target; not "done" until the full matrix passes |
| SQLite/Postgres dialect drift | Suite runs against both; CI sets the Postgres URL |
| 8 GB RAM limits local AI | 3B worker; no-LLM mode is a tested configuration |
| Synthetic data can flatter a strategy | Baselines labelled as infrastructure tests; bias flags always on; no profitability claims |
| Scope is large for one milestone | Staged with explicit done-criteria; stages A–E deliver value even if F–G slip |

---

## Explicit non-goal

**Milestone 1 does not produce a profitable trading strategy, and does not attempt to.**
It produces the apparatus that could eventually evaluate whether a strategy is worth
anything. The two baseline strategies exist to exercise the plumbing. Any performance
number they produce is a test fixture, not a finding.
