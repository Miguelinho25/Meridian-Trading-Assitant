"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  api,
  type ComponentHealth,
  type DeterminismBreak,
  type Registry,
  type RunSummary,
} from "@/lib/api";
import { useSystemHealthContext } from "@/lib/SystemHealthContext";
import { SAFETY_NOTICE } from "@/config/product";

/**
 * Command Centre.
 *
 * What this page does *not* show is deliberate. There is no account balance, no
 * open exposure and no drawdown figure, because nothing runs a continuous paper
 * session — the broker executes inside a backtest and stops when it does. A
 * panel reading "equity 100,000" would be describing an account that does not
 * exist, and on a risk dashboard a plausible fabricated number is worse than a
 * visible gap.
 *
 * So it reports the two things that are real: whether the system can trade, and
 * what the research has actually found.
 */

const STATUS_TONE: Record<ComponentHealth["status"], string> = {
  ok: "var(--positive)",
  degraded: "var(--caution)",
  down: "var(--negative)",
  disabled: "var(--text-tertiary)",
};

function ComponentRow({ name, health }: { name: string; health: ComponentHealth }) {
  return (
    <tr className="border-b border-[var(--border-subtle)] last:border-0">
      <td className="py-2.5 pr-6 text-[13px] text-[var(--text-primary)]">
        {name.replace(/_/g, " ")}
      </td>
      <td className="py-2.5 pr-6">
        <span
          className="text-[11px] uppercase tracking-wider"
          style={{ color: STATUS_TONE[health.status] }}
        >
          {health.status}
        </span>
      </td>
      <td className="tabular py-2.5 text-[12.5px] text-[var(--text-secondary)]">
        {health.detail ?? "—"}
      </td>
    </tr>
  );
}

function Stat({
  label,
  value,
  tone,
  href,
}: {
  label: string;
  value: string;
  tone?: string | undefined;
  href?: string | undefined;
}) {
  const body = (
    <>
      <dt className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
        {label}
      </dt>
      <dd className="tabular mt-1 text-[16px]" style={tone ? { color: tone } : undefined}>
        {value}
      </dd>
    </>
  );
  return href ? (
    <Link href={href} className="block hover:opacity-80">
      {body}
    </Link>
  ) : (
    <div>{body}</div>
  );
}

export default function CommandCentre() {
  const { health: usable, failed, loading, message } = useSystemHealthContext();

  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [registry, setRegistry] = useState<Registry | null>(null);
  const [breaks, setBreaks] = useState<DeterminismBreak[]>([]);
  const [researchFailed, setResearchFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [r, s, b] = await Promise.all([
          api.backtests(),
          api.strategies(),
          api.determinismBreaks(),
        ]);
        if (cancelled) return;
        setRuns(r);
        setRegistry(s);
        setBreaks(b);
        setResearchFailed(false);
      } catch {
        if (cancelled) return;
        // Discard rather than retain: a stale research summary presented as
        // current is the specific failure this dashboard must not have.
        setRuns([]);
        setRegistry(null);
        setBreaks([]);
        setResearchFailed(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const showFailure = failed || (!usable && !loading);
  const evidenceCount = runs.filter((r) => r.is_evidence).length;
  const active = registry?.funnel.find((f) => f.status === "ACTIVE")?.count ?? 0;
  const runnable = (registry?.strategies ?? []).filter((s) => s.is_runnable).length;

  return (
    <div className="mx-auto max-w-5xl">
      <header className="mb-8">
        <h1 className="text-[20px] font-medium tracking-tight">Command Centre</h1>
        <p className="mt-1.5 text-[13px] text-[var(--text-secondary)]">{SAFETY_NOTICE}</p>
      </header>

      {loading && (
        <p className="text-[13px] text-[var(--text-secondary)]">Reading system state…</p>
      )}

      {showFailure && (
        <div className="rounded-sm border border-[var(--negative)] bg-[var(--surface-2)] p-5">
          <p className="text-[13px] text-[var(--negative)]">
            {message ?? "System state is unavailable."}
          </p>
          <p className="mt-2 text-[12.5px] text-[var(--text-secondary)]">
            Start the backend with <code className="text-[var(--text-primary)]">make dev</code>.
            No system state can be shown until it responds, and an unknown state must be
            treated as unsafe to trade.
          </p>
        </div>
      )}

      {/* Outranks everything else: if this fires, no statistic on any other page
          means anything. */}
      {breaks.length > 0 && (
        <div className="mb-8 rounded-sm border border-[var(--negative)] bg-[var(--surface-2)] p-5">
          <p className="text-[11px] uppercase tracking-[0.14em] text-[var(--negative)]">
            Determinism broken
          </p>
          {breaks.map((b) => (
            <p key={b.manifest_hash} className="mt-2 text-[13px]">
              {b.summary}
            </p>
          ))}
        </div>
      )}

      {usable && (
        <>
          <section className="mb-9">
            <h2 className="mb-3 text-[11px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
              Execution safety
            </h2>
            <div className="rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] p-5">
              <dl className="grid grid-cols-2 gap-x-10 gap-y-3 sm:grid-cols-3">
                <div>
                  <dt className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
                    Mode
                  </dt>
                  <dd className="tabular mt-1 text-[13px]">{usable.execution_safety.mode}</dd>
                </div>
                <div>
                  <dt className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
                    Approval mode
                  </dt>
                  <dd className="tabular mt-1 text-[13px]">
                    {usable.execution_safety.approval_mode.replace(/_/g, " ")}
                  </dd>
                </div>
                <div>
                  <dt className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
                    Risk profile
                  </dt>
                  <dd className="tabular mt-1 text-[13px]">
                    <Link href="/risk" className="hover:opacity-80">
                      {usable.execution_safety.risk_profile}
                    </Link>
                  </dd>
                </div>
                <div>
                  <dt className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
                    Broker execution
                  </dt>
                  <dd
                    className="tabular mt-1 text-[13px]"
                    style={{
                      color: usable.execution_safety.broker_execution_enabled
                        ? "var(--negative)"
                        : "var(--positive)",
                    }}
                  >
                    {usable.execution_safety.broker_execution_enabled ? "ENABLED" : "disabled"}
                  </dd>
                </div>
                <div>
                  <dt className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
                    Live execution
                  </dt>
                  <dd className="tabular mt-1 text-[13px]">
                    {usable.execution_safety.live_execution_implemented
                      ? "implemented"
                      : "not implemented"}
                  </dd>
                </div>
                <div>
                  <dt className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
                    Max risk / trade
                  </dt>
                  <dd className="tabular mt-1 text-[13px]">
                    {usable.execution_safety.max_risk_per_trade_pct}%
                  </dd>
                </div>
              </dl>
            </div>
          </section>

          <section className="mb-9">
            <h2 className="mb-3 text-[11px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
              Research
            </h2>
            <div className="rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] p-5">
              {researchFailed ? (
                <p className="text-[13px] text-[var(--caution)]">
                  Research records are unavailable. No run or strategy figures can be shown.
                </p>
              ) : (
                <>
                  <dl className="grid grid-cols-2 gap-x-10 gap-y-4 sm:grid-cols-4">
                    <Stat label="Backtest runs" value={String(runs.length)} href="/backtest" />
                    <Stat
                      label="Qualifying as evidence"
                      value={String(evidenceCount)}
                      tone={evidenceCount === 0 ? "var(--caution)" : "var(--positive)"}
                      href="/backtest"
                    />
                    <Stat label="Runnable strategies" value={String(runnable)} href="/strategy" />
                    <Stat
                      label="Active strategies"
                      value={String(active)}
                      tone={active === 0 ? "var(--text-tertiary)" : "var(--positive)"}
                      href="/strategy"
                    />
                  </dl>
                  {evidenceCount === 0 && runs.length > 0 && (
                    <p className="mt-5 text-[12px] text-[var(--caution)]">
                      No strategy has passed validation. Every recorded run is in-sample,
                      unvalidated or not reproducible — none of them is evidence of an edge.
                    </p>
                  )}
                </>
              )}
            </div>
          </section>

          <section className="mb-9">
            <h2 className="mb-3 text-[11px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
              Components
            </h2>
            <div className="overflow-x-auto rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] px-5">
              <table className="w-full min-w-[520px]">
                <tbody>
                  {Object.entries(usable.components).map(([name, health]) => (
                    <ComponentRow key={name} name={name} health={health} />
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          {/* Replaces a note promising these panels "in Stage D". Stage D shipped;
              what is missing is not the broker but anything that runs it
              continuously. Saying so is more useful than a stale promise. */}
          <p className="text-[12px] leading-relaxed text-[var(--text-tertiary)]">
            No account balance, exposure or open-position panels appear here because no
            continuous paper session exists — the broker executes inside a backtest and
            stops when it does. They arrive with a live paper loop, and are omitted rather
            than shown against an account that does not exist.
          </p>
        </>
      )}
    </div>
  );
}
