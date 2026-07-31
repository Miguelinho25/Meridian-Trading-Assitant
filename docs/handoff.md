# Ñemonis — Handoff

State of the build, written to be checked rather than believed. Every claim below
is either verifiable by running something, or is marked as not done.

---

## The headline

**No strategy in this repository has passed validation, and none should be
traded.** The platform measures, records, halts and refuses correctly. What it
measures shows no edge.

The most recent full replay over 2010–2026 daily data ended at **92,662 from a
100,000 opening balance**. The most recent recorded backtest set: 9 runs, **0
reproducible, 0 validated, 0 qualifying as evidence**. That is the system working
as designed, not failing.

Both bundled strategies are labelled in their own hypotheses as baselines and
controls — *"not a candidate for capital"* — and are registered `CANDIDATE`, not
`ACTIVE`. Nothing is `ACTIVE`.

---

## Verify it yourself

```bash
make check                 # ruff, mypy strict, full suite, secret scan
.venv/bin/lint-imports     # 11 architectural boundary contracts
.venv/bin/python -m nemonis_db.verify     # audit chain integrity
```

At the time of writing: **991 tests pass**, 4 skipped, 11 contracts kept, mypy
clean over 82 source files, audit chain valid.

```bash
make dev                                              # API on :8787
make web                                              # UI on :3000
python scripts/run_backtest.py --instruments EURUSD GBPUSD --validate
python scripts/paper_loop.py --replay --instruments EURUSD GBPUSD
```

---

## The twelve acceptance steps

Eight are done. Four are partial, and the gap in each is stated rather than
rounded up.

| # | Step | State |
|---|---|---|
| 1 | Start the application locally | **Done** — `make dev` + `make web` |
| 2 | Load synthetic or imported historical data | **Done** — `FileProvider`, `SyntheticGenerator`, `scripts/import_yahoo.py` |
| 3 | Configure a prop-firm rule profile | **Partial** — profiles are viewable with their consequences on the Prop Firm page, but there is no editor. Profiles are defined in code (`nemonis_risk.propfirm`). |
| 4 | Select a risk profile | **Partial** — the effective limits and their provenance are visible in the Risk Lab, but selection is by configuration (`NEMONIS_RISK_PROFILE`), not from the UI. This is deliberate for limits (invariant I5 forbids loosening from a UI control); it is *not* deliberate for profile choice, which simply has no write path yet. |
| 5 | Run a backtest | **Done** — `scripts/run_backtest.py`, recorded with a full reproducibility manifest |
| 6 | Inspect the equity curve and drawdown | **Done** — Backtest Lab detail view |
| 7 | Review accepted and rejected trades | **Done** — Proposals page, grouped by the rule that bound |
| 8 | Replay the strategy through the paper broker | **Done** — `scripts/paper_loop.py`, resumable, proven equivalent to the backtest |
| 9 | Generate Obsidian trade notes | **Done** — one note per closed trade, `synthetic: true` |
| 10 | Search for related historical trades | **Partial** — embeddings and retrieval are built and tested, but the vector store is in-memory and does not survive the process. Nothing indexes the vault, and there is no UI. |
| 11 | Request a structured local-model critique | **Partial** — `CritiqueService` works against a real local model and degrades cleanly, but it is not wired to any endpoint or page. |
| 12 | Activate the global kill switch | **Done** — from the chrome on every page, verified against a running loop |

---

## What is deliberately absent

**Live broker execution.** No adapter exists. `Mode.BROKER` is rejected at
start-up, refused again by the paper session, and forbidden by a database CHECK
constraint. `make setup` refuses to proceed if `.env` enables it. Four
independent refusals, because a single one is one edit away from not existing.

**Any UI control that loosens a risk limit.** Invariant I5 gives the risk engine
final authority. The Risk Lab is read-only permanently, and a test asserts
`POST`/`PUT`/`PATCH`/`DELETE` stay unrouted. The kill switch is the only write
surface in the API, and only because it moves toward safety.

**Aggregate performance numbers.** The Analytics page shows no summed P&L. A
return aggregated over in-sample, unvalidated and irreproducible runs is a number
with no meaning that would nonetheless get quoted.

**Neural Memory and AI Research pages.** Marked "soon" rather than shipped blank.
See step 10 and 11 above.

---

## Findings that changed the system

These were caught during the build and are recorded because each was invisible
until something specific was checked.

**The daily loss limit was a lifetime limit.** `Account.start_new_day()` existed
with zero callers, so `daily_loss_used` measured loss since the *start of the
backtest*. A 2010–2026 run stopped trading in March 2017 and produced nothing
across the remaining 56% of the timeline while still reporting metrics as if it
had covered the whole period. Fixing it took the run from 311 trades to 723 — and
net P&L from −4,818 to **−7,076**, because the defect had been truncating the
losing tail. The whole 259-test suite passed throughout.

**The risk banner showed the loosest tier.** `/health` reported
`settings.max_risk_per_trade_pct` — the raw system ceiling — while the engine
enforced the composed 0.35%. The one number visible on every screen overstated
permitted risk by 2.86×.

**`SIZE_BELOW_MINIMUM_LOT` named the symptom, not the cause.** 51.4% of
rejections carried that code, which reads as "the account is too small". After
attribution was fixed, that number went to **zero** — every one was an exposure
clamp binding first, chiefly instrument exposure at 26.5%. Opposite remedies.

**`mypy packages apps` never checked `services/`.** The entire deterministic core
— risk engine, paper broker, backtest engine, market data, feature pipeline —
went unchecked for the whole build behind missing PEP 561 markers. 30 files
checked, 37 unchecked, gate reporting success.

**Notes reported operator text nobody wrote.** `extract_user_fields` used `\s*`
after each label, consuming the newline; an empty field captured the *next label*
as its value, and that text would have travelled back toward the database.

---

## The recurring defect class

Tests that pass for the wrong reason, repeatedly. Several regression tests here
went through two or three versions because the first ones passed with the fix
removed:

- The daily-reset test asserted emergent behaviour that depended on whether
  equity happened to recover; the synthetic fixture did, the real data did not.
- A tighter limit intended to force the latch was *below* one trade's risk, so
  every proposal was rejected on its own projected loss and no latch was
  demonstrated.
- A journal test built a fresh session per tick, which never clears warmup and so
  produced no decisions at all — it skipped, and proved nothing.

**The practice that works: break the guard and confirm it goes red before
trusting it green.** Every safety-relevant fix in this repository was
canary-verified that way, and the commit messages record it.

---

## Known gaps worth attention

| Gap | Why it matters |
|---|---|
| Market data is gitignored | Correct (licence), but a fresh clone cannot reproduce a run until it re-downloads. The `dataset_fingerprint` in each manifest is what lets it confirm it got the same history. |
| Every recorded run is irreproducible | All 9 ran against a dirty working tree. Commit before running, and the manifest will say `reproducible: yes`. |
| The embedding store is in-memory | Retrieval works but nothing persists it, so steps 10 and 11 have no UI. |
| `--live` is refused | The provider serves daily bars, so a live loop ticks about once a day. Stated rather than silently degraded. |
| No Playwright suite | The twelve steps above were verified by hand and by driving the running app, not by an automated browser suite. |
| GitHub repo name | Still `Meridian-Trading-Assitant`, from before the rename. Only the owner can change it. |

---

## If you take one thing from this document

The apparatus is sound and the results are negative. Those are compatible, and
the second is not a reason to distrust the first. A platform that could not
report a negative verdict would be the thing to worry about.
