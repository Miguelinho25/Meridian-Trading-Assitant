"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  api,
  ApiError,
  type DecisionGroup,
  type PaperSession,
  type RunSummary,
} from "@/lib/api";

/**
 * Analytics — what the research has actually established.
 *
 * Deliberately not a performance dashboard. Aggregating P&L across unvalidated,
 * in-sample and irreproducible runs would produce a number with no meaning that
 * would nonetheless get quoted, so this page reports the *state of the evidence*
 * instead: how much of it qualifies, and what is stopping the rest.
 *
 * When nothing qualifies, that is the headline rather than a caveat under a
 * chart.
 */

function Stat({
  label,
  value,
  tone,
  hint,
}: {
  label: string;
  value: string;
  tone?: string | undefined;
  hint?: string | undefined;
}) {
  return (
    <div>
      <dt className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
        {label}
      </dt>
      <dd className="tabular mt-1 text-[18px]" style={tone ? { color: tone } : undefined}>
        {value}
      </dd>
      {hint && <p className="mt-1 text-[11px] text-[var(--text-tertiary)]">{hint}</p>}
    </div>
  );
}

export default function Analytics() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [sessions, setSessions] = useState<PaperSession[]>([]);
  const [decisions, setDecisions] = useState<DecisionGroup[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [r, s] = await Promise.all([api.backtests(), api.paperSessions()]);
        if (cancelled) return;
        setRuns(r);
        setSessions(s);
        const newest = s[0];
        if (newest) {
          const d = await api.paperDecisions(newest.id);
          if (!cancelled) setDecisions(d);
        }
        setError(null);
      } catch (e) {
        if (cancelled) return;
        setRuns([]);
        setSessions([]);
        setDecisions([]);
        setError(e instanceof ApiError ? e.message : "Research records are unavailable.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const evidence = runs.filter((r) => r.is_evidence).length;
  const reproducible = runs.filter((r) => r.is_reproducible).length;
  const validated = runs.filter((r) => r.survives_all === true).length;
  const unvalidated = runs.filter((r) => r.survives_all === null).length;

  const totalDecisions = decisions.reduce((n, g) => n + g.count, 0);
  const blockers = decisions
    .filter((g) => g.verdict === "REJECTED")
    .slice(0, 3);

  return (
    <div className="mx-auto max-w-5xl">
      <header className="mb-8">
        <h1 className="text-[20px] font-medium tracking-tight">Analytics</h1>
        <p className="mt-1.5 text-[13px] text-[var(--text-secondary)]">
          The state of the evidence, not a performance summary.
        </p>
      </header>

      {loading && <p className="text-[13px] text-[var(--text-secondary)]">Reading records…</p>}

      {error && (
        <div className="rounded-sm border border-[var(--negative)] bg-[var(--surface-2)] p-5">
          <p className="text-[13px] text-[var(--negative)]">{error}</p>
        </div>
      )}

      {!loading && !error && (
        <>
          {/* The headline when nothing qualifies, not a footnote under a chart. */}
          {evidence === 0 && runs.length > 0 && (
            <div className="mb-8 rounded-sm border border-[var(--caution)] bg-[var(--surface-2)] p-5">
              <p className="text-[11px] uppercase tracking-[0.14em] text-[var(--caution)]">
                Nothing here is evidence yet
              </p>
              <p className="mt-2 text-[13px] leading-relaxed">
                Across {runs.length} recorded run{runs.length === 1 ? "" : "s"}, none
                qualifies. Evidence requires a sufficient sample, out-of-sample or
                walk-forward provenance, and no disqualifying bias flag. Aggregate returns
                are deliberately not shown: a P&amp;L summed over in-sample and
                irreproducible runs is a number with no meaning that would still get
                quoted.
              </p>
            </div>
          )}

          <section className="mb-9">
            <h2 className="mb-3 text-[11px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
              Backtests
            </h2>
            <div className="rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] p-5">
              <dl className="grid grid-cols-2 gap-x-8 gap-y-5 sm:grid-cols-4">
                <Stat label="Recorded" value={String(runs.length)} />
                <Stat
                  label="Reproducible"
                  value={String(reproducible)}
                  tone={
                    reproducible < runs.length ? "var(--caution)" : "var(--positive)"
                  }
                  hint={
                    reproducible < runs.length
                      ? `${runs.length - reproducible} ran against a dirty tree`
                      : undefined
                  }
                />
                <Stat
                  label="Validated"
                  value={String(validated)}
                  tone={validated === 0 ? "var(--caution)" : "var(--positive)"}
                  hint={unvalidated > 0 ? `${unvalidated} never validated` : undefined}
                />
                <Stat
                  label="Qualify as evidence"
                  value={String(evidence)}
                  tone={evidence === 0 ? "var(--caution)" : "var(--positive)"}
                />
              </dl>
              <p className="mt-5 text-[12px] text-[var(--text-tertiary)]">
                <Link href="/backtest" className="underline underline-offset-4">
                  Backtest Lab
                </Link>{" "}
                holds the runs and their manifests.
              </p>
            </div>
          </section>

          <section className="mb-9">
            <h2 className="mb-3 text-[11px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
              Paper sessions
            </h2>
            <div className="rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] p-5">
              {sessions.length === 0 ? (
                <p className="text-[13px] text-[var(--text-secondary)]">
                  No session has run yet.
                </p>
              ) : (
                <dl className="grid grid-cols-2 gap-x-8 gap-y-5 sm:grid-cols-4">
                  <Stat label="Sessions" value={String(sessions.length)} />
                  <Stat
                    label="Fed live data"
                    value={String(sessions.filter((s) => s.bar_source === "LIVE").length)}
                    hint={
                      sessions.every((s) => s.bar_source === "REPLAY")
                        ? "all replays so far"
                        : undefined
                    }
                  />
                  <Stat
                    label="Trades closed"
                    value={String(
                      sessions.reduce((n, s) => n + s.closed_trade_count, 0),
                    )}
                  />
                  <Stat
                    label="Ticks processed"
                    value={sessions.reduce((n, s) => n + s.ticks, 0).toLocaleString()}
                  />
                </dl>
              )}
            </div>
          </section>

          {totalDecisions > 0 && (
            <section>
              <h2 className="mb-3 text-[11px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
                What stops trades, most recent session
              </h2>
              <div className="rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] p-5">
                <div className="flex flex-col gap-3">
                  {blockers.map((g) => (
                    <div key={g.reason_code} className="flex items-baseline justify-between gap-4">
                      <span className="text-[12.5px]">{g.reason_code}</span>
                      <span className="tabular text-[12px] text-[var(--text-secondary)]">
                        {g.count} · {(Number(g.share) * 100).toFixed(1)}%
                      </span>
                    </div>
                  ))}
                </div>
                <p className="mt-5 text-[12px] text-[var(--text-tertiary)]">
                  A strategy that cannot get a trade past the risk engine has no
                  performance to measure.{" "}
                  <Link href="/proposals" className="underline underline-offset-4">
                    Proposals
                  </Link>{" "}
                  breaks this down in full.
                </p>
              </div>
            </section>
          )}
        </>
      )}
    </div>
  );
}
