# Meridian — Risk Engine

The risk engine is the most important component in the system. It is the only path to
an order, and it is deliberately the most boring code in the repository: pure
functions, exact arithmetic, no I/O, no clock, no cleverness.

---

## 1. Invariants

These hold for every evaluation, in every mode, forever. Each maps to a named test in
`services/risk-engine/tests/test_invariants.py`.

| # | Invariant |
|---|---|
| **I1** | No order exists without a `RiskDecision` whose `proposal_hash` matches its content. |
| **I2** | Limits compose by **tightening only**. No profile, strategy, prompt, UI control or API call can loosen a limit set at a higher tier. |
| **I3** | Size quantisation always **rounds down**. Realised risk ≤ intended risk, never greater. |
| **I4** | All monetary, price, size and percentage arithmetic uses `Decimal`. A `float` reaching a sizing or limit calculation is a bug, caught by a runtime type guard in debug builds. |
| **I5** | Evaluation is **total**: every rule runs, every time. No short-circuit. A rejection reports *all* reasons, not the first. |
| **I6** | Evaluation is **pure**: same inputs ⇒ same decision, byte-identical. No clock read, no RNG, no network, no database. |
| **I7** | Unknown, missing or unparseable state ⇒ **reject**. Never "assume fine". |
| **I8** | A `RiskDecision` cannot be constructed outside the risk-engine module. |
| **I9** | Reducing risk never requires confirmation. Increasing risk always does. |
| **I10** | Every evaluation appends exactly one immutable audit record, whatever the verdict. |

---

## 2. Limit tiers and composition

```
    SYSTEM HARD LIMITS        packages/config — env + code. Ceiling for everything.
            ↓ min()
    ACCOUNT LIMITS            Prop-firm profile + account settings.
            ↓ min()
    PROFILE LIMITS            PRESERVATION / CHALLENGE / ASSERTIVE / EXPERIMENTAL / CUSTOM
            ↓ min()
    STRATEGY + INSTRUMENT     Per-strategy and per-instrument overrides.
            ↓
    EFFECTIVE LIMIT           What actually applies.
```

Composition is `min()` for ceilings and `max()` for floors — **monotone tightening**.
`EXPERIMENTAL` requesting 1.00% under a system cap of 0.50% yields 0.50%, silently and
correctly. The reverse can never happen.

Tested by property-based test: for random tier configurations, the effective limit is
never looser than any contributing tier.

---

## 3. Forex arithmetic

The calculations most often got wrong, specified exactly.

### 3.1 Pip size

```
pip_size = 0.01    if quote currency is JPY   (and a few exotics — per instrument metadata)
pip_size = 0.0001  otherwise
```

Never inferred from the symbol string. It is a field on `Instrument`, sourced from
broker metadata, because brokers disagree.

### 3.2 Value of one pip

For instrument `BASE/QUOTE`, account currency `ACCT`:

```
pip_value_quote  = pip_size × contract_size × lots
pip_value_acct   = pip_value_quote × fx_rate(QUOTE → ACCT)
```

`fx_rate(QUOTE → ACCT)` resolution order:
1. `QUOTE == ACCT` → `1`.
2. Direct pair `QUOTE/ACCT` quoted → use it.
3. Inverse pair `ACCT/QUOTE` quoted → use `1 / rate`.
4. Triangulate via USD.
5. **Otherwise reject the proposal** with `FX_CONVERSION_UNAVAILABLE`. Never guess,
   never default to 1.

Conversion uses the **conservative side** of the spread: the rate that makes the
position larger in account terms when sizing, so rounding error cannot understate risk.

### 3.3 Position size

```
risk_amount_acct  = equity × risk_pct                       # Decimal
stop_distance     = |entry − stop|                          # price units
stop_distance_pips= stop_distance / pip_size
value_per_pip     = pip_size × contract_size × fx(QUOTE→ACCT)   # per 1.00 lot

raw_lots          = risk_amount_acct / (stop_distance_pips × value_per_pip)

lots = floor_to_step(raw_lots, lot_step)     # ALWAYS floor — invariant I3
lots = clamp(lots, min_lot, max_lot)
```

Then, unconditionally:

```
if lots < min_lot:                → reject  SIZE_BELOW_MINIMUM_LOT
if stop_distance < stop_level:    → reject  STOP_INSIDE_BROKER_STOP_LEVEL
if realised_risk(lots) > risk_amount_acct:  → reject  SIZING_INVARIANT_VIOLATED
```

The last check is a belt-and-braces assertion of I3. It should be unreachable. If it
ever fires, that is a defect, and it fails loudly rather than trading.

### 3.4 Quantisation policy

| Quantity | Precision | Rounding |
|---|---|---|
| Lots | instrument `lot_step` | **floor** |
| Prices | instrument `digits` | half-even, then validated against tick size |
| Account money | 2 dp (or currency minor units) | half-even |
| Risk percentages | 4 dp | half-even |
| Pip distances | 1 dp | half-even |

Floor-on-lots is the only asymmetric rule, and it is asymmetric on purpose.

---

## 4. Rule catalogue

Every rule is `(RiskContext) -> RuleResult`, where `RuleResult` is one of `PASS`,
`REJECT(code, detail)`, or `CLAMP(max_size, code, detail)`. All rules run; the final
size is the **minimum** of all clamps; the verdict is `REJECTED` if any rule rejects.

### Tier A — Blocking gates (evaluated first for reporting clarity, never skipped)

| Code | Rule |
|---|---|
| `KILL_SWITCH_ENGAGED` | Global kill switch on, or state unreadable |
| `MODE_FORBIDS_EXECUTION` | Mode is `research` or approval mode `OBSERVE_ONLY` |
| `MARKET_DATA_STALE` | Age > `MAX_DATA_AGE_SECONDS` |
| `MARKET_DATA_INVALID` | Bid ≥ ask, non-positive, or NaN |
| `MARKET_CLOSED` | Instrument session closed |
| `WEEKEND_BLOCK` | Weekend window |
| `ROLLOVER_BLOCK` | Within rollover window |
| `NEWS_WINDOW_BLOCK` | Within economic-event buffer |
| `ABNORMAL_SPREAD` | Spread above instrument threshold or percentile |
| `ABNORMAL_VOLATILITY` | ATR ratio beyond configured band |
| `DUPLICATE_ORDER` | Matching open/pending order within dedupe window |
| `ACCOUNT_STATE_AMBIGUOUS` | Reconciliation mismatch |
| `EMERGENCY_SHUTDOWN` | Emergency stop active |

### Tier B — Account and prop-firm limits (non-overridable)

| Code | Rule |
|---|---|
| `DAILY_LOSS_LIMIT_REACHED` | Realised+floating daily loss ≥ limit |
| `DAILY_LOSS_WOULD_BREACH` | Loss at stop would breach the daily limit |
| `TOTAL_LOSS_LIMIT_REACHED` | Total drawdown ≥ limit |
| `TOTAL_LOSS_WOULD_BREACH` | Loss at stop would breach the total limit |
| `TRAILING_DRAWDOWN_BREACH` | Trailing high-water drawdown breach |
| `PROP_RULE_VIOLATION` | Any enabled prop-firm rule fails |
| `PROP_RULE_BUFFER` | Within the configured near-violation buffer |

### Tier C — Exposure limits (clamping)

| Code | Rule |
|---|---|
| `MAX_OPEN_RISK` | Sum of open risk + this trade > budget |
| `MAX_INSTRUMENT_EXPOSURE` | Per-instrument cap |
| `MAX_CURRENCY_EXPOSURE` | Net per-currency cap (both legs counted) |
| `MAX_CORRELATED_EXPOSURE` | Correlation-clustered cap |
| `MAX_SIMULTANEOUS_POSITIONS` | Open position count |
| `MAX_TRADES_PER_SESSION` | Session trade count |
| `MAX_MARGIN_UTILISATION` | Projected margin use |

Currency exposure counts **both legs**: long EURUSD is long EUR *and* short USD.
Ignoring the quote leg is a common and expensive omission.

### Tier D — Quality gates (profile-driven)

| Code | Rule |
|---|---|
| `BELOW_MIN_REWARD_RISK` | R:R below profile minimum |
| `BELOW_MIN_CONFIDENCE` | Signal confidence below profile threshold |
| `STOP_DISTANCE_INVALID` | Zero, negative, or wrong side of entry |
| `STOP_TOO_TIGHT` / `STOP_TOO_WIDE` | Outside ATR-relative bounds |
| `EXCESSIVE_SLIPPAGE_ESTIMATE` | Estimated slippage above tolerance |
| `CONSECUTIVE_LOSS_COOLDOWN` | Loss-streak cooldown active |
| `DAILY_LOSS_COOLDOWN` / `WEEKLY_LOSS_COOLDOWN` | Cooldown active |
| `STRATEGY_NOT_APPROVED` | Strategy version not approved for this mode |
| `INSTRUMENT_NOT_APPROVED` | Instrument not in approved list |
| `SESSION_NOT_APPROVED` | Session not permitted by profile |

### Tier E — Throttle (clamping)

| Code | Rule |
|---|---|
| `DRAWDOWN_THROTTLE` | Size scaled by drawdown-response curve (§6) |

---

## 5. Decision output

```json
{
  "decision_id": "rd_01JQ...",
  "proposal_hash": "sha256:9f2c…",
  "verdict": "APPROVED_REDUCED",
  "evaluated_at": "2026-07-27T14:22:31.442Z",
  "clock_source": "SystemClock",

  "requested_size_lots": "0.42",
  "final_size_lots": "0.18",
  "requested_risk_pct": "0.50",
  "final_risk_pct": "0.21",
  "risk_amount_account_ccy": "210.00",

  "binding_constraint": "DRAWDOWN_THROTTLE",
  "reason_codes": ["DRAWDOWN_THROTTLE", "MAX_CORRELATED_EXPOSURE"],
  "explanation": "Size reduced from 0.42 to 0.18 lots. Drawdown throttle is the binding constraint: 47% of allowed drawdown is consumed, which caps risk at 50% of configured. Correlated EUR exposure would independently have capped size at 0.24 lots.",

  "before_after": {
    "open_risk_pct":      { "before": "1.10", "after": "1.31" },
    "daily_loss_used_pct":{ "before": "1.90", "after": "1.90" },
    "margin_used_pct":    { "before": "4.20", "after": "5.05" }
  },

  "rules_evaluated": 41,
  "rules_passed": 39,
  "rule_profile_version": "risk-profiles@0.1.0",
  "prop_profile_version": "generic-2phase@0.1.0",
  "audit_event_id": "ae_01JQ..."
}
```

`binding_constraint` names *the* rule that produced the final size — the one thing a
trader actually needs to see. `reason_codes` gives the complete picture. The
`explanation` is generated from templates by **deterministic code, not an LLM**: it must
be reproducible and must never hallucinate a number.

---

## 6. Drawdown throttle

Risk multiplier as a function of allowed-drawdown consumed. **These are configurable
defaults, not fixed law** — `packages/config/risk_profiles.yaml`.

| Drawdown consumed | Risk multiplier | Additional effect |
|---|---|---|
| 0 – 20% | 1.00 | Normal operation |
| 20 – 40% | 0.75 | — |
| 40 – 60% | 0.50 | Minimum confidence +0.10, min R:R +0.25 |
| 60 – 75% | 0.25 | Preservation limits applied wholesale |
| 75 – 90% | 0.00 | New trades blocked; management of open positions only |
| > 90% | 0.00 | Kill switch armed; `EMERGENCY_SHUTDOWN` |

Rationale for the shape: drawdown recovery is convex against you — a 20% loss needs 25%
to recover, 50% needs 100%. Cutting size as drawdown deepens converts a potentially
terminal path into a survivable one, at the cost of slower recovery. For a prop-firm
evaluation, where breaching the limit ends the account outright, that trade is
overwhelmingly correct.

Interpolation between bands is **stepwise by default** (predictable, auditable);
`throttle_interpolation: linear` is available for a smooth curve.

The throttle is monotone non-increasing in drawdown — a property test asserts that
deeper drawdown can never produce a larger multiplier.

---

## 7. Risk profiles

| | PRESERVATION | CHALLENGE | ASSERTIVE | EXPERIMENTAL |
|---|---|---|---|---|
| Risk/trade default | 0.15% | 0.35% | 0.60% | 1.00% |
| Risk/trade range | 0.10–0.25% | 0.25–0.50% | 0.50–0.75% | ≤ 1.00% |
| Daily risk budget | 0.50% | 1.50% | 2.50% | 3.00% |
| Max open risk | 0.50% | 1.50% | 2.50% | 3.00% |
| Max positions | 2 | 4 | 6 | 8 |
| Max trades/session | 2 | 5 | 8 | 12 |
| Min R:R | 2.0 | 1.5 | 1.2 | 1.0 |
| Min confidence | 0.70 | 0.55 | 0.45 | 0.30 |
| Correlated exposure | 0.30% | 0.75% | 1.25% | 1.50% |
| Loss-streak cooldown | 2 losses | 3 losses | 4 losses | 5 losses |
| News buffer | ±30 min | ±15 min | ±10 min | ±5 min |
| Throttle | aggressive | standard | standard | lenient |
| Modes allowed | all | all | all | **backtest + paper only** |

`CHALLENGE` is the default and the recommended profile for prop-firm evaluation. Its
design goal is **survival and rule compliance**, not return maximisation — it increases
selectivity under stress rather than frequency.

`EXPERIMENTAL` is marked experimental in the UI, is never described as recommended, and
**cannot migrate to a broker-connected mode** — a mode change while `EXPERIMENTAL` is
active forces a profile reset with explicit re-selection.

`CUSTOM` permits configuration within system hard limits, displays projected drawdown
consequences before applying, and requires explicit acknowledgement for material
changes.

There is deliberately no "maximum leverage", "aggressive+", "YOLO" or equivalent
profile, and none should be added. The profile ladder tops out at 1.00% because that is
already the outer edge of defensible per-trade risk for an evaluation account.

---

## 8. Asymmetric risk changes

| Direction | Flow |
|---|---|
| **Reducing** risk | Applied immediately. One click. No confirmation, no reason required. Audit event written. |
| **Increasing** risk | Impact preview → explicit typed confirmation → mandatory free-text reason → audit event with the reason → applied. |

The impact preview shows, computed by the deterministic engine:
previous vs proposed value, change in maximum loss per trade, change in lot size for a
representative setup, change in daily risk budget, change in prop-firm buffer
consumption, and a warning severity.

This asymmetry is intentional and must survive any UI redesign. Making risk reduction
frictionless and risk increase deliberate is one of the few interface decisions with a
direct effect on account survival.

---

## 9. Test matrix

Required coverage before the engine is considered fit for the vertical slice:

**Arithmetic** — pip value for JPY and non-JPY quotes; all four fx-conversion routes
plus the unavailable case; position sizing across instruments and account currencies;
floor-not-round quantisation; min/max lot clamps; stop-level and freeze-level
validation; margin estimation.

**Limits** — each Tier A gate blocks in isolation; each Tier B limit blocks at and
before breach; each Tier C limit clamps to the right size; both legs counted in
currency exposure; correlated clustering.

**Temporal** — daily reset across the configured timezone boundary; DST transitions;
week boundary; session boundaries; cooldown expiry; trailing high-water updates.

**Composition** — property test that composed limits are never looser than any tier;
profile switching mid-session; throttle monotonicity.

**Adversarial** — mutated proposal against a valid decision token (must be rejected as
`PROPOSAL_HASH_MISMATCH` and raise an incident); forged decision construction (must be
impossible); size inflation between approval and submission; duplicate submission;
concurrent submission of two proposals that individually pass but jointly breach open
risk.

**Fail-closed** — unreadable kill switch; missing account snapshot; database error
mid-evaluation; NaN in a feature; missing fx rate; clock moving backwards.
