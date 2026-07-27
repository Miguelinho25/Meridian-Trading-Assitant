# ADR-0003 — Local worker model: `llama3.2:3b`, not `qwen3:8b`

**Status:** Accepted · **Date:** 2026-07-27

## Context

The brief specifies "a local Ollama model such as Qwen3 8B" as the local memory,
tagging, summarisation and classification worker.

I initially recommended pulling `qwen3:8b` on the grounds that it is materially better
at schema-constrained output than the installed `llama3.2:3b`, and the user approved
that recommendation.

**The recommendation was wrong, and I made it without checking available memory.**

## Evidence

After pulling the model (5.2 GB on disk), the machine reports:

```
RAM: 8.0 GB total
ollama ps: qwen3:8b  5.8 GB  20%/80% CPU/GPU
```

A single inference request failed after 272 seconds:

```
HTTP 500 — {"error":"Post \"http://127.0.0.1:51367/tokenize\": EOF"}
```

The runner process died. 5.8 GB of model on an 8 GB machine leaves nothing for the OS,
and the split to 20% CPU confirms it did not fit in the GPU allocation. This is before
accounting for Postgres, Next.js, FastAPI and a Python quant stack running concurrently
during normal development — the realistic condition.

`llama3.2:3b` (2.0 GB) on the same machine, same request shape:

```
HTTP 200 in 19.7 s (including cold load)
response: {"decision":"ABSTAIN","confidence":0.4}
```

Valid JSON, correct schema, `format: json` honoured.

## Decision

`llama3.2:3b` is the registered local worker. `nomic-embed-text` (274 MB) handles
embeddings. `qwen3:8b` stays in the registry with `enabled: false` and a comment
pointing at this ADR, so it becomes a one-line change on a machine with more memory.

## Consequences

- **Accepted cost:** a 3B model produces weaker critiques and will fail schema
  validation more often than an 8B would. This is tolerable precisely because of the
  architecture: validation failure degrades to `ABSTAIN`, and `ABSTAIN` costs nothing
  because AI critique is `INFORMATIONAL` by default. A weak local model cannot damage
  a system where the model has no authority.
- The 5.2 GB `qwen3:8b` download remains on disk. Remove with `ollama rm qwen3:8b` if
  the space is wanted.
- If more RAM becomes available, flip `enabled` in `packages/config/models.yaml`.
  No code change.

## Lesson

Check hardware limits before recommending a model, not after downloading it. The
5-minute failed inference and the 5.2 GB download were both avoidable with one
`sysctl hw.memsize` at inspection time.
