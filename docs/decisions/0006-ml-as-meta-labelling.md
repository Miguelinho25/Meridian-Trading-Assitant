# ADR-0006 — ML enters as meta-labelling, not price prediction

**Status:** Accepted · **Date:** 2026-07-27

## Context

The brief anticipates statistical learning, ensemble voting, regime-switching models
and "optional PyTorch modules behind interfaces". Milestone 1 deferred all of it, but
nothing specified *how* a model would be trained, validated, versioned or promoted —
so the deferral had no plan behind it.

Two Stage B design choices depend on the answer and are expensive to retrofit: whether
the feature pipeline persists a point-in-time feature store, and whether features carry
independent versioning.

## Decision

**1. ML enters as meta-labelling.** A deterministic strategy chooses direction; a model
predicts whether that signal is worth taking, emitting a bounded confidence. The model
never selects a side, a size or a price.

**2. Default model class is gradient-boosted trees**, not neural networks. Deep learning
is deferred behind an explicit test: it must beat a gradient-boosted baseline
out-of-sample before being considered.

**3. A model is a strategy version.** Same promotion pipeline, same gates, same
`ResultProvenance` labelling. No parallel path, and no automatic retraining on
production data.

**4. The feature store is immutable and point-in-time from Stage B.** Features are
never recomputed; a correction is a new `feature_version`.

Full reasoning in [machine-learning.md](../machine-learning.md).

## Rationale

**Why not price prediction.** Next-bar direction in liquid FX is close to
unpredictable, and the effective sample size is far smaller than the row count suggests
— bars overlap in information, pairs share drivers, and the unit that matters is the
trade. Fitting a high-capacity model to a few hundred effective observations produces
apparent structure reliably and real structure rarely.

Meta-labelling decomposes the problem so the model gets the tractable half. "Has this
setup, in this regime, worked before?" is answerable from data we will actually have.
"Where does EURUSD go next?" is not.

**Why it fits the existing design.** `Signal.confidence` already exists; the risk engine
already has a `BELOW_MIN_CONFIDENCE` code and a per-profile minimum. Meta-labelling
populates a slot that is already wired, so the model's blast radius is one number
feeding a gate that already exists — rather than a new authority in the execution path.

**Why immutability matters more than it sounds.** Recomputing a feature after a bug fix
rewrites history with future-informed values. The training set becomes corrupt in a way
no unit test detects, and the resulting backtest looks *better*, not worse. Immutability
plus versioning is the only reliable defence.

## Consequences

- Stage B carries extra work: a persisted versioned feature store rather than an
  in-memory frame, and explicit lookback declarations per feature (which also give
  purge and embargo lengths for free).
- Purged K-fold with embargo becomes mandatory for any model evaluation. Un-purged CV
  is the most common source of meaningless ML backtest results and is not offered.
- Trial counts must be recorded, including abandoned runs, so Sharpe ratios can be
  deflated for multiple testing.
- Feature storage grows monotonically. Acceptable at research scale; Parquet
  partitioning already planned.
- **Accepted limitation:** this ceiling is lower than "an AI that learns to trade". It
  is also the version that can be measured honestly. A system that respects the limit
  will outperform one that does not.

## Revisit if

A gradient-boosted meta-model reaches out-of-sample significance and drift monitoring
stays stable for a meaningful paper-trading period. At that point higher-capacity
models and richer feature representations become worth the burden of proof — not
before.
