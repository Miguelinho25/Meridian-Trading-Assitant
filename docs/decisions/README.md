# Architecture Decision Records

Every deviation from the original brief is recorded here with its justification, so a
future reader can tell a considered trade-off from an oversight.

| ADR | Decision | Deviates from brief? |
|---|---|---|
| [0001](0001-python-314-not-312.md) | Python 3.14.6 instead of 3.12 | Yes — verified equivalent |
| [0002](0002-sqlite-first-postgres-ready.md) | SQLite-first, Postgres-ready; in-process event bus instead of Redis | Yes — user-approved |
| [0003](0003-local-model-selection.md) | `llama3.2:3b` as local worker, not `qwen3:8b` | Yes — hardware constraint |
| [0004](0004-services-as-modules.md) | `services/*` are in-process modules, not microservices | No — clarifies brief |
| [0005](0005-repo-location.md) | Standalone repo directory; Desktop never a git repo | No — safety |
| [0006](0006-ml-as-meta-labelling.md) | ML enters as meta-labelling, not price prediction; GBT before deep learning | No — specifies the brief's "statistical learning" |
