# ADR-0002 — SQLite-first, Postgres-ready; in-process event bus instead of Redis

**Status:** Accepted (user-approved) · **Date:** 2026-07-27

## Context

The brief specifies PostgreSQL as source of truth, Redis for caching and event
coordination, and Docker Compose for local development.

Environment inspection found **none of the three installed**: no `docker`, no `psql`,
no `redis-server`. Homebrew is present, so all three could be installed, but doing so
adds always-on background services to the user's machine.

The brief also requires that the vertical slice let a user "start the application
locally" — a requirement in direct tension with a stack that does not exist on the
machine.

## Decision

Presented as a choice; the user selected **SQLite-first, Postgres-ready**.

- One dialect-neutral SQLAlchemy schema and one Alembic chain serve both backends.
  Rules in [data-model.md §4](../data-model.md#4-dialect-neutrality-rules).
- Postgres-only capabilities sit behind interfaces with working SQLite fallbacks:
  `pgvector` → numpy brute-force `VectorStore`; partitioning → indexes; append-only
  triggers → repository guards.
- Redis is replaced by `EventBus` and `Cache` protocols with in-process
  implementations. A Redis adapter is a later addition behind the same interface, with
  no call-site changes.
- `docker-compose.yml` is still written, defining Postgres and Redis, so a machine with
  Docker can use the production-shaped stack immediately.

## Consequences

- **Gained:** the application runs today, on this machine, with zero installs. This is
  what makes the vertical slice's acceptance criteria actually achievable.
- **Risk — dialect drift.** SQLite is permissive where Postgres is strict; code that
  works locally could fail on Postgres. **Mitigation:** the entire test suite runs
  against Postgres whenever `NEMONIS_TEST_POSTGRES_URL` is set, which CI will set. Any
  drift fails a test rather than surfacing later.
- **Risk — concurrency.** SQLite's writer lock is unsuitable for concurrent workers.
  Acceptable for single-user local research; it is a real reason to move to Postgres
  before any multi-worker deployment, and is documented as such.
- **Not compromised:** decimal arithmetic (TypeDecorator on both), append-only
  semantics (enforced on both), audit hash-chaining (backend-independent).

## Migration path

Set `NEMONIS_DATABASE_URL` to a Postgres URL, run `alembic upgrade head`, and the
Postgres-conditional migration branches apply partitioning, native triggers and
pgvector. No application code changes.
