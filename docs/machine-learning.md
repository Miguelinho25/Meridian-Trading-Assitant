# Ñemonis — Machine Learning and Pattern Learning

Where statistical learning fits, what it is allowed to do, and — more importantly —
what it is not allowed to claim.

> **Naming.** The "Neural Memory" dashboard is a **knowledge graph**, not a neural
> network. It renders linked notes and shared attributes. It performs no inference and
> its adjacency proves nothing. The two are unrelated; see
> [obsidian-memory.md §6](obsidian-memory.md#6-links).

---

## 1. Position

ML sits in the **advisory tier** ([architecture.md §2](architecture.md#2-the-organising-principle-the-determinism-boundary)),
alongside the LLM router and for the same reason: it is a fallible component whose
output may be wrong in ways that are hard to detect.

| A model may | A model may not |
|---|---|
| Emit a bounded confidence in `[0,1]` | Emit a lot size, price, or stop |
| Filter or veto a deterministic signal | Originate a trade |
| Classify a market regime | Alter a risk limit |
| Rank retrieved historical cases | Bypass or influence the risk engine |

The risk engine does not import the model layer, and the import-linter contract in
`pyproject.toml` will enforce that the moment `nemonis_ml` exists. A model's entire
influence on the system is one number that feeds a confidence gate the risk engine
already applies.

---

## 2. The honest problem statement

This section exists because the failure mode of ML in trading is not "it doesn't
work" — it is "it appears to work spectacularly, then loses money".

**Signal-to-noise is brutal.** Next-bar direction in liquid FX is close to
unpredictable. Model capacity is not the binding constraint. A high-capacity model
will not find structure that is absent; it will find *apparent* structure, convincingly.

**Effective sample size is far smaller than row count.** Ten years of hourly bars on
ten pairs looks like ~600,000 rows. But bars overlap in information, pairs share
drivers (a USD move hits seven of the ten), and the unit that actually matters is the
**trade**, of which there may be a few hundred. Estimating a model with thousands of
parameters from a few hundred effective observations is not a modelling problem, it is
a wishful-thinking problem.

**Non-stationarity is the real adversary.** The mapping from features to outcomes
drifts. A model fitted through one regime can be not merely less accurate later but
*inverted*. Standard ML assumes a fixed data-generating process; markets deny it.

**Multiple testing inflates everything.** Trying 200 configurations and reporting the
best one's Sharpe ratio is not a result, it is a selection artefact. This must be
corrected for explicitly (§5.4).

**Consequence:** the plan favours strong features, small regularised models and
punishing validation over model complexity. Gradient-boosted trees on well-constructed
features are the default. Deep learning is a later experiment with a high burden of
proof, not a foundation — and on an 8 GB machine
([ADR-0003](decisions/0003-local-model-selection.md)) it is also impractical to train
at any scale.

---

## 3. Three tractable applications

Ordered by expected value per unit of effort.

### 3.1 Meta-labelling — the primary application

Do not ask a model to predict price. Ask it whether a signal the deterministic system
already produced is worth taking.

```mermaid
graph LR
    A[Deterministic strategy] -->|side: LONG/SHORT<br/>entry, stop, target| B[Trade proposal]
    B --> C[Meta-model]
    C -->|P win in 0..1| D[Confidence field]
    D --> E[Risk engine<br/>min-confidence gate + throttle]
    E --> F[Size, or rejection]

    classDef ml fill:#2a2210,stroke:#8a7433,color:#f5efe0
    classDef det fill:#0d2818,stroke:#2d7a4d,color:#e8f5ee
    class C ml
    class A,B,E,F det
```

This decoupling is the whole point. **The model never chooses direction.** Direction is
the hard, low-signal problem and stays with transparent deterministic logic. The model
answers the easier, better-posed question: *given this setup, in this regime, with
these features, how often has this worked?*

Concretely it also fits what already exists: `Signal.confidence` is already a field, and
the risk engine already has a `BELOW_MIN_CONFIDENCE` rejection code and a profile-level
minimum confidence. Meta-labelling populates a slot that is already wired.

### 3.2 Regime classification

Unsupervised, modest claims. Hidden Markov models or clustering over volatility, trend
and spread features. The regime-classifier interface is already specified to accept a
replacement ([architecture.md](architecture.md)), and every classification already
carries a confidence, an alternative regime and a classifier version.

Value: regime-conditional performance attribution, and a throttle input. Not a signal
source.

### 3.3 Confidence calibration

A raw model score is not a probability. Isotonic or Platt calibration turns it into one,
which matters because the risk engine consumes confidence as a gate. An uncalibrated
0.8 that wins 55% of the time corrupts every decision downstream of it.

Calibration is measured with reliability curves and Brier score, reported alongside
accuracy — accuracy alone is close to meaningless here.

---

## 4. Labelling

### 4.1 Triple-barrier method

Label each signal by which barrier it hits **first**: profit target, stop loss, or a
time limit.

```
        ┌─────────────────── target ──────── +1
 entry ─┼···········································  time limit → 0
        └─────────────────── stop ────────── −1
```

This is the correct label because it is what actually happens to the trade. Labelling
by "return over the next N bars" is the common alternative and it is wrong here: it
ignores that a position would have been stopped out on the way, so it trains the model
on outcomes that were unreachable in practice.

The barriers come from the signal itself — entry, stop and target concepts already
exist on `Signal`, so labels are derived from the same values the trade would have used.

### 4.2 Sample weighting by uniqueness

Overlapping label windows mean samples are not independent. Two signals whose barrier
windows overlap share outcome information, and treating them as independent inflates
effective sample size and understates variance.

Each sample is weighted by the inverse of its **concurrency** — how many other label
windows overlap it. Cheap to compute, and it materially changes cross-validation
results.

---

## 5. Validation

This is where ML in trading is won or lost. The protocol extends
[backtesting-methodology.md](backtesting-methodology.md); everything there still applies.

### 5.1 Purged K-fold with embargo

Standard K-fold leaks with time-series labels. If a training sample's label window
overlaps a test sample's, the training set contains information about the test period.

```
folds:   [── train ──][── TEST ──][──── train ────]
                    ↑↑           ↑↑
                  purge        embargo
         drop training samples   drop a further
         whose label windows     window after the
         overlap the test set    test set
```

- **Purge**: remove training samples whose label windows overlap the test window.
- **Embargo**: drop a further period after the test window, because features are
  serially correlated and a sample immediately after the test set still carries its
  information.

Non-negotiable. Un-purged CV is the single most common source of spectacular,
meaningless ML backtest results.

### 5.2 Walk-forward, not just CV

CV estimates generalisation within the sample period. Walk-forward estimates what
matters: performance on data after the fitting window. Both are reported; only
walk-forward gets treated as evidence.

The existing walk-forward machinery is reused unchanged — a model is refit per window
exactly as parameters are.

### 5.3 The model is evaluated on trades, not predictions

Accuracy, AUC and F1 are diagnostics. They are **not** results. A model with 52%
accuracy can be excellent and one with 70% can lose money, because the payoff is not
symmetric across predictions.

The reported result is always the trade-level outcome after costs: expectancy, profit
factor, max drawdown, prop-firm pass probability — the same metric set as any other
strategy, with the same `ResultProvenance` label attached.

### 5.4 Correcting for multiple testing

Every trained configuration is recorded, including abandoned ones. The number of trials
feeds a **deflated Sharpe ratio**, and the naive Sharpe is never reported without it.

Recording failures is not bookkeeping fussiness — without the trial count, the best
result of 200 attempts is indistinguishable from a genuine edge, and the two have very
different consequences.

### 5.5 Degradation monitoring

After deployment: feature-distribution drift against the training distribution
(population stability index), rolling live-vs-expected performance, and calibration
decay. Breaching a threshold flags the model for review and, past a hard threshold,
retires it automatically to the baseline deterministic path.

---

## 6. A model is a strategy version

No parallel promotion pipeline. A trained model artefact is versioned and promoted
exactly like code, through the existing gates:

```
HYPOTHESIS → EXPERIMENT → BACKTEST → VALIDATION → OUT-OF-SAMPLE
           → PAPER DEPLOYMENT → REVIEW → MANUAL PROMOTION
```

`model_versions` records, and reproducibility requires all of it:

| Field | Why |
|---|---|
| `model_hash` | Artefact identity |
| `training_data_hash` + date range | Exactly what it saw |
| `feature_version` | Which feature definitions produced the inputs |
| `label_config` | Barrier definitions used |
| `hyperparameters`, `seed` | Reproducible refit |
| `cv_config` | Purge and embargo settings |
| `trials_count` | Multiple-testing correction |
| `promotion_status` | Same enum as strategies |
| `training_library_versions` | Silent behaviour changes between releases |

**No automatic retraining on production data.** A model does not update itself from
live outcomes. Retraining is an experiment that re-enters the pipeline at the top. The
one-trade-changes-the-system failure mode is already forbidden for strategies and
applies identically here.

---

## 7. The feature store — the decision that affects Stage B

Point-in-time correctness is the binding requirement, and it must be built in from the
start.

**Features are immutable once written.** A feature row records what was computable at
its decision timestamp, and is never recomputed. This is not tidiness — recomputing a
feature after a bug fix silently rewrites history with future-informed values, and the
resulting training set is corrupt in a way no test will catch.

A corrected feature therefore becomes a **new `feature_version`**, and old rows remain
for reproducing old results.

```
feature_rows:
  instrument_id, timeframe, decision_time (PK)
  feature_version         ← definitions that produced this row
  values (JSON / columnar)
  source_bar_hash         ← ties the row to exact input bars
  computed_at
```

Joining features to labels is a point-in-time join on `decision_time`, never on
ingestion time.

**Consequences for Stage B, adopted:**

1. The feature pipeline writes an immutable, versioned feature store from day one — not
   an in-memory frame reconstructed per backtest.
2. Features carry their own `feature_version`, independent of strategy versions, since
   several strategies share features and a feature fix must not silently invalidate
   unrelated strategy lineage.
3. Every feature declares its lookback window explicitly, so purge and embargo lengths
   are derived rather than guessed.
4. `BarView` already makes look-ahead raise; the feature store makes it *auditable after
   the fact*, which is the property needed when a result looks too good.

---

## 8. Guardrails

| Risk | Control |
|---|---|
| Leakage | `BarView` raises; purged CV; immutable point-in-time features |
| Overfitting | Small models, walk-forward, trial counting, deflated Sharpe |
| Overconfidence | Calibration required; reliability curves reported |
| Silent decay | Drift and calibration monitoring; automatic retirement |
| Scope creep into execution | Advisory tier; bounded output; import-linter contract |
| Irreproducibility | Full lineage in `model_versions` |
| False authority | ML confidence is one input to a gate, never a sizing term |

---

## 9. Sequencing

| Stage | Work |
|---|---|
| **Stage B** (now) | Immutable versioned feature store; explicit lookback declarations |
| **Stage E** | Triple-barrier labelling; uniqueness weights; purged CV harness |
| **Milestone 2** | Meta-labelling model; calibration; walk-forward evaluation; `model_versions` |
| **Milestone 2** | HMM / clustering regime classifier behind the existing interface |
| **Milestone 3+** | Drift monitoring, automatic retirement, ensembles |
| **Deferred** | Deep learning, subject to beating a gradient-boosted baseline out-of-sample |

Nothing here is on the Milestone 1 critical path. The infrastructure Milestone 1 builds
is what makes the rest evaluable — which is the reason for doing it in this order.

---

## 10. What this will not do

It will not predict price. It will not find a reliable edge in liquid FX by pattern
recognition alone. It will not replace the deterministic strategies.

What it can plausibly do is improve *selection* among signals a transparent system has
already generated, and it can be measured honestly enough that you would know if it
were not working. On current evidence that is the realistic ceiling, and a system that
respects it will outperform one that does not.
