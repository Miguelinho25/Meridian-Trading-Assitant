"use client";

import { use, useEffect, useState } from "react";
import Link from "next/link";
import { api, ApiError, type EquityPoint, type RunDetail } from "@/lib/api";

/**
 * One run, with the manifest that makes it reproducible.
 *
 * The manifest is shown in full rather than summarised. Its whole purpose is to
 * let someone re-run this exact experiment years from now, and a summary of the
 * inputs is not the inputs.
 */

function Field({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: string | undefined;
}) {
  return (
    <div>
      <dt className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
        {label}
      </dt>
      <dd className="tabular mt-1 text-[12.5px]" style={tone ? { color: tone } : undefined}>
        {value}
      </dd>
    </div>
  );
}

function EquityChart({ points }: { points: EquityPoint[] }) {
  const first = points[0];
  const last = points[points.length - 1];
  // Narrows both ends for the compiler and covers the real empty/single case.
  if (points.length < 2 || first === undefined || last === undefined) return null;

  const values = points.map((p) => Number(p.equity));
  const start = values[0] ?? 0;
  const end = values[values.length - 1] ?? start;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const W = 900;
  const H = 200;

  const path = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * W;
      const y = H - ((v - min) / span) * H;
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  const zeroY = H - ((start - min) / span) * H;

  return (
    <div className="overflow-x-auto rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] p-5">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ height: 200 }}>
        {/* Starting balance, so a curve below it reads as a loss at a glance. */}
        <line
          x1="0"
          y1={zeroY}
          x2={W}
          y2={zeroY}
          stroke="var(--border-subtle)"
          strokeDasharray="3 3"
        />
        <path
          d={path}
          fill="none"
          stroke={end >= start ? "var(--positive)" : "var(--negative)"}
          strokeWidth="1.5"
        />
      </svg>
      <div className="mt-2 flex justify-between text-[11px] text-[var(--text-tertiary)]">
        <span>{new Date(first.at).toLocaleDateString()}</span>
        <span className="tabular">
          {min.toLocaleString(undefined, { maximumFractionDigits: 0 })} –{" "}
          {max.toLocaleString(undefined, { maximumFractionDigits: 0 })}
        </span>
        <span>{new Date(last.at).toLocaleDateString()}</span>
      </div>
    </div>
  );
}

export default function RunDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [run, setRun] = useState<RunDetail | null>(null);
  const [equity, setEquity] = useState<EquityPoint[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [d, e] = await Promise.all([api.backtest(id), api.backtestEquity(id)]);
        if (cancelled) return;
        setRun(d);
        setEquity(e);
        setError(null);
      } catch (e) {
        if (cancelled) return;
        setRun(null);
        setError(e instanceof ApiError ? e.message : "This run is unavailable.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [id]);

  return (
    <div className="mx-auto max-w-5xl">
      <Link
        href="/backtest"
        className="text-[12px] text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
      >
        ← Backtest Lab
      </Link>

      {error && (
        <div className="mt-6 rounded-sm border border-[var(--negative)] bg-[var(--surface-2)] p-5">
          <p className="text-[13px] text-[var(--negative)]">{error}</p>
        </div>
      )}

      {run && (
        <>
          <header className="mb-8 mt-4">
            <h1 className="text-[20px] font-medium tracking-tight">{run.strategy_key}</h1>
            <p className="mt-1.5 text-[12.5px] text-[var(--text-secondary)]">
              {run.id} · {run.instruments.join(", ")} · {run.timeframe} ·{" "}
              {new Date(run.created_at).toLocaleString()}
            </p>
          </header>

          {/* Reproducibility outranks the result. A number produced by code that
              can no longer be identified is not a finding. */}
          {!run.is_reproducible && (
            <div className="mb-8 rounded-sm border border-[var(--negative)] bg-[var(--surface-2)] p-5">
              <p className="text-[11px] uppercase tracking-[0.14em] text-[var(--negative)]">
                Not reproducible
              </p>
              <p className="mt-2 text-[13px] text-[var(--text-primary)]">
                {run.irreproducible_reason}
              </p>
            </div>
          )}

          {run.survives_all === null && (
            <div className="mb-8 rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] p-5">
              <p className="text-[13px] text-[var(--text-secondary)]">
                Validation was not run for this backtest. That is not the same as passing —
                walk-forward, Monte Carlo and stress results are simply absent.
              </p>
            </div>
          )}

          <section className="mb-9">
            <h2 className="mb-3 text-[11px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
              Equity curve
            </h2>
            <EquityChart points={equity} />
          </section>

          <section className="mb-9">
            <h2 className="mb-3 text-[11px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
              Result
            </h2>
            <div className="rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] p-5">
              <dl className="grid grid-cols-2 gap-x-10 gap-y-4 sm:grid-cols-4">
                <Field label="Trades" value={String(run.trade_count)} />
                <Field
                  label="Net P&L"
                  value={run.net_pnl ?? "—"}
                  tone={
                    run.net_pnl && Number(run.net_pnl) < 0
                      ? "var(--negative)"
                      : "var(--positive)"
                  }
                />
                <Field
                  label="Max drawdown"
                  // Already a percentage — see the note in the index page.
                  value={
                    run.max_drawdown_pct
                      ? `${Number(run.max_drawdown_pct).toFixed(2)}%`
                      : "—"
                  }
                />
                <Field label="Provenance" value={run.provenance} />
                <Field label="Signals" value={String(run.signals_generated)} />
                <Field label="Proposals" value={String(run.proposals_made)} />
                <Field
                  label="Rejected by risk"
                  value={String(run.rejections)}
                  tone="var(--text-secondary)"
                />
                <Field label="Duration" value={`${run.duration_ms} ms`} />
              </dl>
              {!run.is_evidence && (
                <p className="mt-5 text-[12px] text-[var(--caution)]">
                  These numbers do not qualify as evidence. That requires a sufficient
                  sample, out-of-sample or walk-forward provenance, and no disqualifying
                  bias flag.
                </p>
              )}
            </div>
          </section>

          <section>
            <h2 className="mb-3 text-[11px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
              Reproducibility manifest
            </h2>
            <div className="rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] p-5">
              <dl className="grid grid-cols-2 gap-x-10 gap-y-4 sm:grid-cols-3">
                <Field label="Manifest hash" value={run.manifest_hash.slice(7, 25)} />
                <Field label="Result hash" value={run.result_hash.slice(7, 25)} />
                <Field label="Manifest version" value={run.manifest_version} />
                <Field
                  label="Commit"
                  value={run.git_commit ? run.git_commit.slice(0, 12) : "none"}
                  tone={run.git_dirty ? "var(--negative)" : undefined}
                />
                <Field label="Branch" value={run.git_branch || "—"} />
                <Field
                  label="Working tree"
                  value={run.git_dirty ? "dirty" : "clean"}
                  tone={run.git_dirty ? "var(--negative)" : "var(--positive)"}
                />
                <Field label="Seed" value={String(run.seed)} />
                <Field label="Slippage model" value={run.slippage_model} />
                <Field label="Commission model" value={run.commission_model} />
                <Field
                  label="Spread model"
                  value={run.spread_model}
                  tone={run.spread_assumed ? "var(--caution)" : undefined}
                />
                <Field label="Risk profile" value={run.risk_profile} />
                <Field
                  label="Starting balance"
                  value={`${run.starting_balance ?? "—"} ${run.account_currency}`}
                />
                <Field label="Data provider" value={run.market_data_provider} />
                <Field label="Dataset version" value={run.dataset_version} />
                <Field label="Bars" value={String(run.bar_count)} />
                <Field label="Engine" value={run.engine_version} />
                <Field label="Feature pipeline" value={run.feature_pipeline_version} />
                <Field label="Risk profiles" value={run.risk_profile_version} />
              </dl>

              {run.spread_assumed && (
                <p className="mt-5 text-[12px] text-[var(--caution)]">
                  The spread was synthesised from mid prices, not taken from the source.
                  Costs are only as realistic as that assumption.
                </p>
              )}
            </div>
          </section>
        </>
      )}
    </div>
  );
}
