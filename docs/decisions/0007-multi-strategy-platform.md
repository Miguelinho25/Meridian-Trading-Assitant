# ADR-0007 — Multi-strategy platform, not a strategy runner

**Status:** Accepted · **Date:** 2026-07-27 · **Supersedes part of** [ADR-0006](0006-ml-as-meta-labelling.md)

## Context

The original brief described a plugin-style strategy interface and two baseline
strategies. The design honoured that, but treated multi-strategy support as a
future extension of a single-strategy pipeline.

The user has redirected: the objective is an operating system hosting many
competing strategies — dozens now, hundreds eventually — that allocates confidence
dynamically on evidence, discovers regime- and session-conditional performance
without being told, and accumulates the whole research history as an interconnected
knowledge graph.

The timing is deliberate. This lands **before** Stage C, because it changes the
risk engine's shape, and retrofitting it afterwards would mean rewriting the
component with the highest correctness burden in the system.

## Decision

1. **Strategies are isolated plugins** with a declarative manifest, fault
   containment, time budgets and private state. A strategy is a pure function of
   market state and cannot see the account.

2. **An allocator sits between strategies and the risk engine**, emitting a
   confidence that feeds the existing `BELOW_MIN_CONFIDENCE` gate. It never emits
   a size. The risk engine remains sole authority.

3. **Allocation uses Thompson sampling** over per-strategy posteriors, not
   performance ranking.

4. **Discovery is gated by shrinkage, FDR correction, minimum-evidence rules and
   out-of-sample confirmation.** `Hypothesis` and `Finding` are distinct types;
   only a `Finding` can move an allocation.

5. **The risk engine evaluates proposals as a set**, not one at a time.

6. **Obsidian becomes the research laboratory**, with hypothesis, experiment,
   finding, failure and psychological-observation note types, each carrying its
   evidence level.

Full design in [strategy-platform.md](../strategy-platform.md).

## Rationale

**Why an allocator rather than ranking.** "Weight by recent performance"
concentrates on whichever strategy got lucky and starves strategies before they
have evidence — a good strategy opening with three losses may never recover its
allocation. Thompson sampling handles both: wide-uncertainty strategies still get
sampled, narrow-negative ones fade without being banned, and allocation decays on
evidence with no threshold to tune. It directly implements "never become attached
to one strategy" rather than approximating it.

**Why the statistical controls are not optional.** 100 strategies × 4 regimes × 4
sessions is 1,600 cells; testing each at 5% yields roughly **80 significant
findings from pure noise**. Every one looks like "Strategy C works around London
Open" — specific, plausible, actionable, imaginary. A naive comparator does not
merely fail to avoid this; it converts noise into capital allocation and reports
rising confidence as it concentrates.

Shrinkage is the highest-leverage control because it acts on the estimate rather
than the decision: a cell with 8 trades barely moves off the prior no matter how
good it looks.

**Why the risk engine now takes a set.** With fifty strategies, the binding
constraint moves from per-trade risk to portfolio risk — fifty proposals can each
pass individually and jointly be long EUR across the book. Sequential evaluation
also makes the outcome depend on arrival order, which is not reproducible in
replay. This is the concrete Stage C change.

**Why priors are not filters.** A manifest's `expected_regimes` records what the
author believed. Hard-filtering on it would make the belief unfalsifiable and
suppress exactly the signals that would reveal the author was wrong.

**Why "disable after N losses" gets the same treatment.** A 45% win-rate strategy
produces four consecutive losses roughly every 15 trades. Concluding it "stops
working" after four is the gambler's fallacy, and the platform must not be able to
make that error automatically. The deterministic loss-streak cooldown stays as a
risk control applied to everything, which is a different claim.

## Consequences

- **Stage C grows.** Portfolio-level joint evaluation, per-strategy sub-budgets
  and deterministic tie-breaking are added. Worth it: these are exactly the parts
  that would be most painful to retrofit into a validated risk engine.
- **Hundreds of strategies live is not the goal.** Evidence, not compute, is the
  constraint: a strategy needs ~100+ trades before its expectancy means much. The
  architecture is a funnel — hundreds registered, a handful active.
- **The allocator is Milestone 2.** It needs trade history that does not exist
  yet. Stage C builds the plugin registry, manifest, isolation and portfolio-aware
  risk engine; allocation arrives once there is evidence to allocate on.
- **Accepted cost:** the discovery mechanism will miss real effects a looser
  system would "find". It will also decline the ~80 imaginary ones in the same
  grid. That trade is the point.
- Retired strategies are never deleted — a 2026 failure may be right in 2029, and
  the reason for failure is itself research data.

## Revisit if

Evidence accumulates that the minimum-evidence gates are too strict — measurable
as findings that later confirm out-of-sample despite having been withheld. That
would be an argument for loosening specific gates, backed by data, rather than a
general relaxation.
