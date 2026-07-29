# Ñemonis — Backtesting Methodology

A backtest is a hypothesis test, not a demonstration. This document defines what the
engine does, what claims the results support, and what claims they never support.

---

## 1. Claim discipline

The system enforces vocabulary, because loose language about backtests is how people
lose money.

| Term | Means | May be shown as |
|---|---|---|
| **In-sample result** | Parameters were chosen on this data | Never a performance claim. Always labelled `IN-SAMPLE — NOT EVIDENCE`. |
| **Validation result** | Held out during fitting, used for selection | Weak evidence. Selection pressure applies. |
| **Out-of-sample result** | Touched exactly once, after everything was frozen | The only result that may be called a *result* |
| **Walk-forward result** | Repeated re-fit and forward test | The strongest available evidence |
| **Paper result** | Forward, simulated, real-time data | Evidence about the system, still not live |

Every metric carries its provenance label through the database, the API and the UI.
A number cannot be displayed without it.

**The system never states that a strategy is profitable.** It reports estimates,
intervals and sample sizes. Statistical validation gates that language: below the
thresholds in §5, the UI shows `INSUFFICIENT EVIDENCE` in place of a verdict.

The out-of-sample period is used **once**. Repeated evaluation converts it into a
validation set. The engine records every access to a strategy version's OOS window and
warns loudly on the second — this is the most common way honest researchers fool
themselves.

---

## 2. Event loop

Strictly ordered, one bar at a time. Order matters enormously.

```
for each bar i:
    1. advance ReplayClock to bar[i].open_time
    2. ingest bar i  →  data-quality gate
    3. mark open positions to bar i (floating P&L, MFE/MAE, equity)
    4. process pending orders against bar i   ← fills for decisions made at i-1
    5. evaluate stops / targets / expiries
    6. update account snapshot + drawdown state
    7. build BarView(≤ i)                     ← structurally cannot see i+1
    8. compute features → classify regime → run strategies
    9. proposals → risk engine → queue orders for bar i+1
```

Step 7 before step 8, and step 9 queueing for `i+1`, are the anti-look-ahead spine.
Signals generated on bar `i` can only ever fill at bar `i+1`. Steps 3–6 complete before
any decision, so a strategy sees a fully settled account state, never a half-updated one.

---

## 3. Cost and fill model

See [architecture.md §6](architecture.md#6-fill-realism) for fill rules. Costs applied
to every trade:

| Cost | Model |
|---|---|
| Spread | Real observed spread when available; otherwise per-instrument distribution by session, with a widening multiplier around news and rollover |
| Commission | Per lot per side, from instrument spec |
| Slippage | Configurable: `none` / `fixed` / `proportional_to_spread` / `volatility_scaled` / `stochastic(seed)` |
| Swap | Applied at rollover; triple on the configured weekday |
| Latency | Configurable decision→submission delay, default one bar |

Defaults are **pessimistic**. A strategy that only works under optimistic costs does not
work. Every backtest report itemises total spread cost, commission and slippage against
gross P&L — if costs dominate the edge, that is the headline finding, not a footnote.

---

## 4. Required validation protocol

A strategy version reaches `VALIDATED` only after all of this:

**Split.** Chronological, never random. Default 50% in-sample, 20% validation, 30%
out-of-sample. Random splits leak future information into the past through
autocorrelation and are not offered.

**Walk-forward.** Rolling and expanding windows. Re-fit on each training window, test
forward. Report per-window results, not just the aggregate — an aggregate hides the
fact that all the profit came from one window.

**Parameter stability.** Sweep the neighbourhood of chosen parameters. A robust
strategy sits on a plateau; an overfit one sits on a spike. Report the performance
surface and the degradation at ±1 and ±2 parameter steps.

**Monte Carlo.** Reshuffle trade order (≥ 10,000 runs) to produce distributions for
maximum drawdown, terminal equity and ruin probability. Trade sequence is largely
luck; the realised drawdown is one draw from a distribution, and the tail matters more
than the median for a prop-firm account.

**Stress tests.** Each run repeated under: doubled slippage, doubled spread, one-bar
extra entry delay, 5% of bars randomly missing, and the worst contiguous 10% of the
period removed.

---

## 5. Automatic warnings

Emitted with every result, in the report and the UI:

| Flag | Trigger |
|---|---|
| `INSUFFICIENT_TRADES` | < 100 trades (< 30 = results suppressed entirely) |
| `SHORT_PERIOD` | < 2 years, or no regime variety |
| `SINGLE_INSTRUMENT_DEPENDENCE` | > 60% of P&L from one instrument |
| `REGIME_CONCENTRATION` | > 60% of P&L from one regime |
| `PERIOD_CONCENTRATION` | > 40% of P&L from < 10% of the period |
| `PARAMETER_SENSITIVITY` | > 30% metric degradation at ±1 step |
| `OVERFITTING_SUSPECTED` | In-sample Sharpe > 2× out-of-sample |
| `UNREALISTIC_FILLS` | Fills better than the bar's traded range |
| `LOOKAHEAD_SUSPECTED` | `LookAheadError` caught, or win rate > 80% with R > 2 |
| `COST_DOMINATED` | Costs > 50% of gross profit |
| `SURVIVORSHIP_RISK` | Instrument set chosen with hindsight |
| `LOW_OOS_SAMPLE` | < 30 out-of-sample trades |

`INSUFFICIENT_TRADES` at fewer than 30 **suppresses the metrics entirely** rather than
displaying them with a caveat. Caveats next to a big green number get ignored; an
absent number does not.

---

## 6. Metrics

Return: net return, gross profit, gross loss, profit factor, expectancy, average and
median R, win/loss rate, average winner/loser, payoff ratio.

Risk: max drawdown (balance and equity), max daily loss, recovery factor, Sharpe,
Sortino, Calmar, volatility, downside deviation, VaR and CVaR (95/99), longest losing
and winning streaks, time under water.

Activity: average holding time, trade frequency, exposure time, commission, spread and
slippage cost.

Breakdowns: by pair, session, hour, weekday, setup, strategy version, regime and
confidence bucket.

Rules: violations, near-violations, rejected-trade outcome analysis.

**Every metric is reported with sample size and, where the statistic admits one, a
confidence interval** — bootstrap for path-dependent statistics such as drawdown,
analytic where valid. A profit factor of 1.6 from 40 trades and one from 4,000 trades
are different claims, and the interface must make that visible rather than showing two
identical-looking numbers.

**Rejected-trade outcome analysis** deserves emphasis: the engine simulates what
rejected proposals *would* have done. If the risk engine is systematically rejecting
winners, that is a finding worth having. It does not mean the rejections were wrong —
survival has value that per-trade expectancy does not capture — but it must be measured
rather than assumed.

---

## 7. Reproducibility

Every run stores: `strategy_version_id`, `code_hash`, `git_sha`, data range and content
hash, full config hash, RNG seed, engine version, and library versions. Re-running with
the same inputs must produce byte-identical results, asserted by a CI determinism test
that runs a fixed backtest twice and diffs the trade ledger.

Non-determinism in a backtester is a defect, not a quirk. If two runs disagree, no
result from that engine can be trusted.
