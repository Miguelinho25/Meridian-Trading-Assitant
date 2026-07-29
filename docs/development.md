# Ñemonis — Development

> **Current state:** architecture and planning only. No application code exists yet.
> The commands below describe the target from Milestone 1 onward and will not run until
> Phase 2 lands. See [milestone-1.md](milestone-1.md).

---

## Verified environment (2026-07-27)

| Component | Present | Note |
|---|---|---|
| Python | 3.14.6 | Full quant stack verified — [ADR-0001](decisions/0001-python-314-not-312.md) |
| Node | 25.1.0 / npm 11.6.2 | No pnpm; npm workspaces used |
| Ollama | running | `llama3.2:3b`, `nomic-embed-text`, `qwen3:8b` (disabled) |
| RAM | **8 GB** | Binding constraint — [ADR-0003](decisions/0003-local-model-selection.md) |
| PostgreSQL | ✗ | SQLite locally — [ADR-0002](decisions/0002-sqlite-first-postgres-ready.md) |
| Redis | ✗ | In-process event bus |
| Docker | ✗ | `docker-compose.yml` provided for machines that have it |
| Homebrew | ✓ | Available if the production stack is wanted |

---

## Setup (from Milestone 1)

```bash
git clone <repo> && cd nemonis
cp .env.example .env          # defaults are safe: research mode, broker disabled

make setup                    # venv + deps + npm install + alembic upgrade head
make seed                     # synthetic market data + sample strategies
make dev                      # API on :8787, web on :3000
```

`make setup` refuses to run if `.env` contains a value that would enable broker
execution — a paranoid check against a footgun that does not yet exist, kept so it
never can.

---

## Commands

| Command | Does |
|---|---|
| `make dev` | API + web with reload |
| `make test` | Full suite |
| `make test-risk` | Risk-engine tests only — run these before any risk change |
| `make test-determinism` | Runs a fixed backtest twice, diffs the ledger |
| `make test-postgres` | Suite against Postgres (needs `NEMONIS_TEST_POSTGRES_URL`) |
| `make lint` | ruff + mypy + eslint + tsc --noEmit |
| `make e2e` | Playwright |
| `make backtest CONFIG=…` | Headless backtest |
| `make audit-verify` | Verifies the audit hash chain |
| `make check` | lint + test + determinism — the pre-commit gate |

---

## Working on the risk engine

The risk engine has stricter rules than the rest of the codebase.

1. Write the test first. Every rule needs a passing case, a failing case, and a
   boundary case at exactly the limit.
2. Never use `float`. `Decimal` throughout. The debug type guard will catch it, but do
   not rely on that.
3. Never read the clock. Accept a `Clock`.
4. Never perform I/O. The engine takes a fully-populated context.
5. Adding a rule means adding a reason code to the enum, and reason codes are a stable
   public contract — they appear in stored audit records. Renaming one is a migration.
6. Run `make test-risk` and `make test-determinism` before committing.

A change that makes any invariant in [risk-engine.md §1](risk-engine.md#1-invariants)
untrue is not acceptable regardless of what it enables.

---

## Adding a strategy

Implement the `Strategy` protocol, register it, and give it a version. Return a
`Signal` or `NoAction` — never a lot size, never an order. Use only `BarView`; reaching
around it to the underlying series is the one unforgivable sin, and
`make test-determinism` plus the `LookAheadError` guard exist to catch it.

New strategies start at `promotion_status: DRAFT` and advance only through the
promotion pipeline in [backtesting-methodology.md](backtesting-methodology.md). There is
no path from "it looked good in-sample" to paper deployment.

---

## Troubleshooting

**Ollama slow or 500ing.** 8 GB machine. Check `ollama ps`; if a model larger than ~3 GB
is loaded, unload it. The app is fully functional with `NEMONIS_OLLAMA_ENABLED=false`.

**Determinism test failing.** Something read the wall clock, used an unseeded RNG, or
iterated an unordered collection. Those three account for nearly every case.

**`LookAheadError`.** Working as intended — a feature or strategy tried to read future
data. The traceback names it. Do not widen the view to make it pass.

**Migration fails on Postgres but passes on SQLite.** Dialect drift. See
[data-model.md §4](data-model.md#4-dialect-neutrality-rules).
