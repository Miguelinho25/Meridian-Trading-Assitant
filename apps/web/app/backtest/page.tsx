"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  api,
  ApiError,
  type DeterminismBreak,
  type RunSummary,
} from "@/lib/api";

/**
 * Backtest Lab.
 *
 * The index of recorded runs. Two things are given more prominence than the
 * P&L, because both determine whether the P&L means anything at all: whether a
 * run is *evidence*, and whether it is *reproducible*.
 *
 * A profitable in-sample run that cannot be reproduced is not a result. Showing
 * its return in large type next to a validated one would invite exactly the
 * comparison this platform exists to prevent.
 */

function EvidenceBadge({ run }: { run: RunSummary }) {
  // Order matters: the strongest disqualifier wins. A run can be irreproducible
  // *and* in-sample, and the irreproducibility is the more fundamental problem.
  if (!run.is_reproducible) {
    return (
      <span
        className="text-[10px] uppercase tracking-wider text-[var(--negative)]"
        title="The working tree was dirty; the commit does not identify the code that ran"
      >
        irreproducible
      </span>
    );
  }
  if (run.survives_all === null) {
    return (
      <span
        className="text-[10px] uppercase tracking-wider text-[var(--text-tertiary)]"
        title="Validation was not run. That is not the same as passing."
      >
        unvalidated
      </span>
    );
  }
  if (!run.survives_all) {
    return (
      <span className="text-[10px] uppercase tracking-wider text-[var(--caution)]">
        failed validation
      </span>
    );
  }
  return (
    <span className="text-[10px] uppercase tracking-wider text-[var(--positive)]">
      survives all
    </span>
  );
}

function money(value: string | null) {
  if (value === null) return "—";
  const n = Number(value);
  // Display only — the string stays authoritative. Never fed back to the API.
  return n.toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 });
}

export default function BacktestLab() {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [breaks, setBreaks] = useState<DeterminismBreak[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reproducibleOnly, setReproducibleOnly] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [r, b] = await Promise.all([api.backtests(), api.determinismBreaks()]);
        if (cancelled) return;
        setRuns(r);
        setBreaks(b);
        setError(null);
      } catch (e) {
        if (cancelled) return;
        setRuns(null);
        setError(e instanceof ApiError ? e.message : "Backtest records are unavailable.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const visible = (runs ?? []).filter((r) => !reproducibleOnly || r.is_reproducible);
  const evidenceCount = (runs ?? []).filter((r) => r.is_evidence).length;

  return (
    <div className="mx-auto max-w-6xl">
      <header className="mb-8">
        <h1 className="text-[20px] font-medium tracking-tight">Backtest Lab</h1>
        <p className="mt-1.5 text-[13px] text-[var(--text-secondary)]">
          Every run is stored with the exact inputs that produced it.
        </p>
      </header>

      {loading && (
        <p className="text-[13px] text-[var(--text-secondary)]">Reading research records…</p>
      )}

      {error && (
        <div className="rounded-sm border border-[var(--negative)] bg-[var(--surface-2)] p-5">
          <p className="text-[13px] text-[var(--negative)]">{error}</p>
        </div>
      )}

      {/* Should always be empty. When it is not, the statistics of every run
          involved are meaningless, so it outranks everything else on the page. */}
      {breaks.length > 0 && (
        <div className="mb-8 rounded-sm border border-[var(--negative)] bg-[var(--surface-2)] p-5">
          <p className="text-[11px] uppercase tracking-[0.14em] text-[var(--negative)]">
            Determinism broken
          </p>
          {breaks.map((b) => (
            <p key={b.manifest_hash} className="mt-2 text-[13px] text-[var(--text-primary)]">
              {b.summary}
            </p>
          ))}
        </div>
      )}

      {runs && runs.length === 0 && (
        <div className="rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] p-5">
          <p className="text-[13px]">No runs recorded yet.</p>
          <p className="mt-2 text-[12.5px] text-[var(--text-secondary)]">
            Record one with{" "}
            <code className="text-[var(--text-primary)]">
              python scripts/run_backtest.py --instruments EURUSD GBPUSD --validate
            </code>
            .
          </p>
        </div>
      )}

      {runs && runs.length > 0 && (
        <>
          <div className="mb-4 flex items-center justify-between">
            <p className="text-[12.5px] text-[var(--text-secondary)]">
              {runs.length} run{runs.length === 1 ? "" : "s"} ·{" "}
              <span className={evidenceCount === 0 ? "text-[var(--caution)]" : undefined}>
                {evidenceCount} qualifying as evidence
              </span>
            </p>
            <label className="flex cursor-pointer items-center gap-2 text-[12px] text-[var(--text-secondary)]">
              <input
                type="checkbox"
                checked={reproducibleOnly}
                onChange={(e) => setReproducibleOnly(e.target.checked)}
                className="accent-[var(--text-primary)]"
              />
              Reproducible only
            </label>
          </div>

          <div className="overflow-x-auto rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] px-5">
            <table className="w-full min-w-[880px]">
              <thead>
                <tr className="border-b border-[var(--border-subtle)]">
                  {[
                    "Run",
                    "Strategy",
                    "Instruments",
                    "Trades",
                    "Net P&L",
                    "Max DD",
                    "Status",
                    "Recorded",
                  ].map((h) => (
                    <th
                      key={h}
                      className="py-2.5 pr-6 text-left text-[10px] font-normal uppercase tracking-[0.14em] text-[var(--text-tertiary)]"
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {visible.map((run) => (
                  <tr
                    key={run.id}
                    className="border-b border-[var(--border-subtle)] last:border-0"
                  >
                    <td className="py-2.5 pr-6 text-[12.5px]">
                      <Link
                        href={`/backtest/${run.id}`}
                        className="text-[var(--text-primary)] underline decoration-[var(--border-subtle)] underline-offset-4 hover:decoration-[var(--text-secondary)]"
                      >
                        {run.id.replace("bt_", "").slice(0, 10)}
                      </Link>
                    </td>
                    <td className="py-2.5 pr-6 text-[12.5px] text-[var(--text-secondary)]">
                      {run.strategy_key}
                    </td>
                    <td className="py-2.5 pr-6 text-[12.5px] text-[var(--text-secondary)]">
                      {run.instruments.join(", ")} · {run.timeframe}
                    </td>
                    <td className="tabular py-2.5 pr-6 text-[12.5px]">{run.trade_count}</td>
                    <td
                      className="tabular py-2.5 pr-6 text-[12.5px]"
                      style={{
                        color:
                          run.net_pnl && Number(run.net_pnl) < 0
                            ? "var(--negative)"
                            : "var(--positive)",
                      }}
                    >
                      {money(run.net_pnl)}
                    </td>
                    <td className="tabular py-2.5 pr-6 text-[12.5px] text-[var(--text-secondary)]">
                      {/* Already a percentage: engine.py computes
                          (peak - equity) / peak * 100. Multiplying again here
                          rendered a 5.36% drawdown as 535.7%. */}
                      {run.max_drawdown_pct
                        ? `${Number(run.max_drawdown_pct).toFixed(2)}%`
                        : "—"}
                    </td>
                    <td className="py-2.5 pr-6">
                      <EvidenceBadge run={run} />
                    </td>
                    <td className="py-2.5 text-[12px] text-[var(--text-tertiary)]">
                      {new Date(run.created_at).toLocaleString(undefined, {
                        dateStyle: "short",
                        timeStyle: "short",
                      })}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {evidenceCount === 0 && (
            <p className="mt-4 text-[12px] text-[var(--text-tertiary)]">
              No run here qualifies as evidence. That requires a sufficient sample,
              out-of-sample or walk-forward provenance, and no disqualifying bias flag —
              an in-sample result never qualifies, however good the numbers look.
            </p>
          )}
        </>
      )}
    </div>
  );
}
