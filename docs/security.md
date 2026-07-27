# Meridian — Security

Threat model for a single-user local research platform holding market data, trading
research and (eventually) broker credentials.

---

## 1. Principal threats

| # | Threat | Control |
|---|---|---|
| T1 | Secret committed to git | `.gitignore` written before first commit; pre-commit secret scan; `.env.example` carries no real values |
| T2 | Secret leaked to an LLM | Redaction middleware on every outbound payload; prompt hashes stored, never prompt bodies |
| T3 | Secret in logs | Redaction filter on every log record before any sink |
| T4 | Secret in the Obsidian vault | Vault writer runs the same redaction; no secret-bearing field is ever a note field |
| T5 | Secret in the frontend bundle | Backend-only key access; `NEXT_PUBLIC_` lint rule; keys reported as present/absent, never by value |
| T6 | Prompt injection via journal notes or retrieved content | Untrusted-content delimiters, injection scan, output validation, adversarial tests |
| T7 | Model attempting to override risk rules | Schema has no field able to express it; no code path from model output to an order |
| T8 | Order submitted without/beyond risk approval | Decision token bound to proposal hash, validated by the broker |
| T9 | Audit tampering | Append-only tables, hash-chained events, verification job |
| T10 | Look-ahead or leakage in research | `BarView` raises; determinism tests; automatic bias flags |
| T11 | Dependency compromise | Pinned versions with hashes; lockfiles committed; no post-install scripts in CI |
| T12 | Local API exposed to the network | Binds `127.0.0.1`; strict CORS allowlist; no `0.0.0.0` default |

---

## 2. Secret handling

Keys live only in `.env`, read once at startup into a `SecretStr` settings object.

- `SecretStr` prevents accidental interpolation — `repr()` and `str()` yield `**********`,
  so a stray f-string or traceback cannot print a key.
- No key is ever returned by an API endpoint. Health endpoints report
  `{"openai": "configured"}` or `"unconfigured"` — presence, never value.
- `SecretStore` abstracts retrieval so a future OS keychain backend is a drop-in change.
- Rotation is a config edit and a restart; nothing caches a key elsewhere.

**Broker credentials do not exist in this build.** There are no fields for them, because
there is no live broker adapter. See [architecture.md §9](architecture.md#9-why-live-trading-is-absent-rather-than-disabled).

---

## 3. Redaction

One implementation, `packages/config/redaction.py`, used by the logging filter, the
model router and the vault writer. A single implementation means a gap gets fixed once,
not three times.

Redacted: API-key patterns (`sk-`, `AKIA`, bearer tokens, PEM blocks), account numbers,
absolute balances (→ percentages), email addresses, absolute filesystem paths,
values of any settings field typed `SecretStr`.

Tested with fixtures containing realistic secrets, asserting that no fixture value
survives into logs, prompts or notes.

---

## 4. API surface

Request IDs on every request, propagated into logs and audit events. Structured JSON
logs. Input validation by Pydantic at the boundary — no unvalidated dict reaches a
service. Output validation too, so an internal field cannot leak through a response
model.

Rate limiting on model-invoking and backtest-triggering endpoints (both can be made
expensive by repetition). CORS restricted to the configured origin. `SameSite=Lax`,
`HttpOnly`, `Secure`-when-TLS cookies, with CSRF tokens on state-changing requests.
Permission architecture is role-ready — a single local user today, with the
authorisation check already threaded so multi-user is not a retrofit.

---

## 5. Audit log

Append-only and hash-chained: `hash = H(prev_hash || canonical_json(payload))`. Any
mutation or deletion breaks the chain, detectable by a verification job.

Recorded: every proposal, risk decision (approved, reduced or rejected), order state
transition, simulated fill, risk-setting change (with the mandatory reason for
increases), profile change, mode change, kill-switch action, model invocation
(metadata + prompt hash, never the prompt), vault sync, and every incident.

The audit log answers "what did the system do, and why" months later. Debugging a
trading system without one is guesswork.

---

## 6. Development practices

Dependencies pinned with hashes; lockfiles committed; a scheduled vulnerability scan.
Migrations are reviewed for destructive operations, are tested forward and backward, and
run against a copy before anything real.

Pre-commit hooks: secret scan, ruff, mypy, eslint, tsc, and a check that `.env` is not
staged.

**If a secret is exposed:** rotate the key first, then fix the leak, then purge history
if it was committed. In that order — a key in git history is compromised the moment it
lands, and rewriting history without rotating first leaves the window open.
