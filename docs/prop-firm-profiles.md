# Ñemonis — Prop-Firm Rule Profiles

No firm is hard-coded. A profile is data: a configurable rule set the engine evaluates
against account state, and which the simulator can replay history against.

---

## 1. Why this is configuration, not code

Evaluation rules differ between firms, change without notice, and differ between phases
of the same programme. A hard-coded rule set is wrong the moment terms change, and
silently wrong. Encoding rules as versioned, dated, user-verifiable data means a change
is an edit with an audit trail rather than a release.

**Ñemonis ships no real firm's rules.** The bundled profile is a clearly-labelled
generic example with invented values. Real rules must be entered and verified by the
user against the firm's official current terms.

---

## 2. Profile schema

```yaml
profile_id: generic-2phase
name: "Generic Two-Phase Evaluation (EXAMPLE — NOT A REAL FIRM)"
version: "0.1.0"
enabled: true

source: "SYNTHETIC EXAMPLE — values invented for testing"
rule_source_date: null
last_verified_at: null
verified_by: null
notes: "Replace every value with the firm's published terms before relying on this."

phase: EVALUATION_1          # EVALUATION_1 | EVALUATION_2 | FUNDED
starting_balance: "100000.00"
account_currency: USD

profit_target_pct: "8.00"

max_daily_loss_pct: "5.00"
daily_loss_basis: EQUITY          # EQUITY | BALANCE
daily_loss_reference: BALANCE_AT_RESET   # BALANCE_AT_RESET | HIGHEST_EQUITY
reset_time: "00:00"
reset_timezone: "America/New_York"

max_total_loss_pct: "10.00"
total_loss_type: STATIC           # STATIC | TRAILING
trailing_basis: HIGHEST_EQUITY    # if TRAILING
trailing_stops_at_initial_balance: true

min_trading_days: 4
max_trading_days: null
inactivity_days: 30

consistency_rule_enabled: false
max_single_day_profit_pct_of_total: "40.00"

weekend_holding_allowed: true
overnight_holding_allowed: true
news_trading_restricted: false
news_buffer_minutes: 2
ea_allowed: true
instrument_restrictions: []
max_lots_per_position: null
max_total_lots: null

buffer_warning_pct: "20.00"   # warn when within 20% of any limit
```

Fields deliberately separated because they are commonly conflated:

- **`daily_loss_basis`** — equity (includes floating) or balance (closed only). The
  difference decides whether an open loser breaches your limit. Getting this wrong is
  one of the most common ways evaluations are failed.
- **`daily_loss_reference`** — measured from the balance at reset, or from the day's
  highest equity. The latter is much stricter.
- **`total_loss_type`** — static floor vs trailing. Trailing drawdown that follows
  equity upward is a fundamentally different game from a fixed floor.
- **`reset_timezone`** — a real, named timezone with real DST behaviour. Getting a
  daily reset wrong by an hour can breach a limit that appeared to have room.

---

## 3. Verification discipline

Every profile displays, unavoidably, in the UI:

> ⚠ **Verify these rules against the firm's current official terms.**
> Source: *(field)* · Last verified: *(date or "never")* · Version: *(n)*

An unverified profile (`last_verified_at == null`) shows an amber banner. A profile
verified more than 90 days ago shows a staleness warning. Version history is retained,
so a rule change can be correlated against results produced under the previous version.

The system cannot check a firm's terms for you. It can refuse to let you forget that
you have not.

---

## 4. Evaluation

`RuleEvaluation` runs on every account snapshot and every proposal:

```json
{
  "profile_id": "generic-2phase",
  "profile_version": "0.1.0",
  "evaluated_at": "2026-07-27T14:22:31Z",
  "status": "WITHIN_LIMITS",
  "rules": [
    { "rule": "max_daily_loss", "status": "OK",
      "limit": "5000.00", "used": "1900.00", "remaining": "3100.00",
      "used_pct_of_limit": "38.00" },
    { "rule": "max_total_loss", "status": "BUFFER_WARNING",
      "limit": "10000.00", "used": "8300.00", "remaining": "1700.00",
      "used_pct_of_limit": "83.00" },
    { "rule": "min_trading_days", "status": "IN_PROGRESS",
      "required": 4, "completed": 2 }
  ],
  "blocking": [],
  "warnings": ["max_total_loss within buffer"],
  "projected_after_proposal": { "max_daily_loss_used_pct": "42.10" }
}
```

`projected_after_proposal` is the important field: it evaluates the rule against the
account state **as it would be if this trade hit its stop**. Blocking only on breaches
already incurred is too late. The engine blocks trades that *would* breach a limit,
which is the entire point.

---

## 5. Rule simulator

Replays a stored trade history against any profile, answering:

- Would this history have passed the profile? If not, which trade broke it, and when?
- How close did it come to each limit?
- How would it fare under trailing rather than static drawdown?
- How would it fare with equity-based rather than balance-based daily loss?
- What is the Monte Carlo pass probability under reshuffled trade order?

That last one matters most. A history that passes once may pass only because the
sequence was kind. Reshuffling to get a distribution of outcomes gives an estimate of
pass probability rather than a single anecdote — and for a system whose stated objective
is *maximising the probability of satisfying an evaluation profile*, the pass
probability under reshuffling is the objective function, not net return.

The simulator is also how profile changes are assessed before adopting them.

---

## 6. Interaction with the risk engine

The prop-firm engine supplies limits to the risk engine at **Tier B**
([risk-engine.md §2](risk-engine.md#2-limit-tiers-and-composition)) — account-level and
non-overridable. A risk profile can only tighten them, never loosen. Selecting a more
aggressive risk profile cannot widen a prop-firm daily loss limit, by construction.

The drawdown throttle reads its denominator from the active prop-firm profile, so
"40% of allowed drawdown consumed" means 40% of *this firm's* allowance, and the throttle
adapts automatically when the profile changes.
