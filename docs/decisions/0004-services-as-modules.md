# ADR-0004 — `services/*` are in-process modules, not microservices

**Status:** Accepted · **Date:** 2026-07-27

## Context

The brief's monorepo layout puts `market-data`, `risk-engine`, `backtest-engine`,
`paper-broker` and others under `/services`, a name that usually implies separate
deployable processes. The same brief also forbids Kubernetes, Kafka and unnecessary
distributed architecture in v1.

## Decision

`services/*` are **Python packages imported in-process** by `apps/api` and
`apps/worker`. The directory name is kept for alignment with the brief; the boundary is
a module boundary enforced by import-linting, not a network boundary.

## Rationale

Network boundaries would actively damage the core requirements:

- **Determinism.** A backtest calling the risk engine over HTTP inherits network
  timing, retries and partial failures. Byte-identical replay becomes impossible. The
  determinism requirement effectively forbids network calls inside the event loop.
- **Performance.** A backtest evaluates risk on every signal across years of bars.
  In-process is nanoseconds; HTTP is milliseconds — a 10⁴–10⁶× cost for no benefit.
- **Correctness.** The proposal-hash authorisation ([architecture.md §5](../architecture.md#5-risk-engine-finality-is-structural))
  is simpler and stronger when the token cannot be replayed across a network.
- **Operational cost.** One user, one machine. Distributed systems buy independent
  scaling and deployment, neither of which is needed, and cost debugging difficulty,
  which is not.

## Enforcement

Import-linting keeps the boundary honest:

- `services/*` may not import `apps/*`.
- `services/risk-engine` may not import `packages/model-router` — the structural
  guarantee that no LLM reaches the risk path.
- `services/*` may not import the network stack (`httpx`, `requests`).
- Cross-service imports go through published interfaces, not internal modules.

Because the boundary is real, extracting a service into a process later is mechanical
if it is ever justified. It is not justified now.
