"use client";

import { useEffect, useState } from "react";
import { api, ApiError, type Registry, type Strategy } from "@/lib/api";

/**
 * Strategy Lab.
 *
 * The hypothesis leads, not the performance. A strategy is evaluated against
 * whether its stated belief held; "it backtested well" is not a hypothesis, and
 * a lab that puts returns first invites exactly that substitution.
 *
 * The other thing this page is careful about: capability constraints and priors
 * look similar and mean opposite things. `supported_instruments` is a hard
 * filter. `expected_regimes` is what the author *believes* — recorded so the
 * platform can later report whether they were right. Rendering them the same way
 * would imply the system filters on regime, which would make the belief
 * unfalsifiable.
 */

const STATUS_TONE: Record<string, string> = {
  REGISTERED: "var(--text-tertiary)",
  CANDIDATE: "var(--text-secondary)",
  PAPER: "var(--caution)",
  ACTIVE: "var(--positive)",
  RETIRED: "var(--text-tertiary)",
  QUARANTINED: "var(--negative)",
};

function Chips({ values, tone }: { values: string[]; tone?: string }) {
  return (
    <span className="flex flex-wrap gap-1.5">
      {values.map((v) => (
        <span
          key={v}
          className="rounded-sm border border-[var(--border-subtle)] px-1.5 py-0.5 text-[11px]"
          style={tone ? { color: tone } : undefined}
        >
          {v}
        </span>
      ))}
    </span>
  );
}

function StrategyCard({ strategy: s }: { strategy: Strategy }) {
  return (
    <div className="rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <div>
          <h3 className="text-[14px] font-medium">{s.key}</h3>
          <p className="mt-0.5 text-[11px] uppercase tracking-wider text-[var(--text-tertiary)]">
            {s.author} · lookback {s.lookback_bars} bars · max {s.max_signals_per_day}/day
            {s.deterministic ? " · deterministic" : " · NON-DETERMINISTIC"}
          </p>
        </div>
        <span
          className="text-[10px] uppercase tracking-wider"
          style={{ color: STATUS_TONE[s.status] ?? "var(--text-secondary)" }}
        >
          {s.status}
        </span>
      </div>

      {s.quarantine_reason && (
        <p className="mt-3 text-[12.5px] text-[var(--negative)]">
          Quarantined by the platform: {s.quarantine_reason}
        </p>
      )}

      {/* First, and in the reading colour. What it claims is the thing to judge. */}
      <div className="mt-4">
        <p className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
          Hypothesis
        </p>
        <p className="mt-1.5 text-[13px] leading-relaxed text-[var(--text-primary)]">
          {s.hypothesis}
        </p>
      </div>

      <dl className="mt-5 grid gap-4 sm:grid-cols-2">
        <div>
          <dt className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
            Runs on — hard filter
          </dt>
          <dd className="mt-1.5 text-[12px]">
            {s.supported_instruments === null ? (
              <span className="text-[var(--text-secondary)]">any instrument</span>
            ) : (
              <Chips values={s.supported_instruments} />
            )}
          </dd>
        </div>

        <div>
          <dt className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
            Author expects — prior, not a filter
          </dt>
          <dd className="mt-1.5 text-[12px]">
            {s.expected_regimes === null ? (
              <span className="text-[var(--text-secondary)]">no stated expectation</span>
            ) : (
              // Muted and labelled: it does not gate anything. The platform runs
              // the strategy in every regime and reports whether the author was
              // right, which is only possible if it is never filtered on.
              <Chips values={s.expected_regimes} tone="var(--text-tertiary)" />
            )}
          </dd>
        </div>

        <div>
          <dt className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
            Required features
          </dt>
          <dd className="mt-1.5 text-[12px]">
            <Chips values={s.required_features} />
          </dd>
        </div>

        <div>
          <dt className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
            Health
          </dt>
          <dd className="tabular mt-1.5 text-[12px] text-[var(--text-secondary)]">
            {s.health.calls === 0 ? (
              "not yet exercised in this process"
            ) : (
              <>
                {s.health.calls} calls · {s.health.faults} faults ·{" "}
                {s.health.mean_micros} µs mean
              </>
            )}
          </dd>
        </div>
      </dl>
    </div>
  );
}

export default function StrategyLab() {
  const [registry, setRegistry] = useState<Registry | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await api.strategies();
        if (cancelled) return;
        setRegistry(r);
        setError(null);
      } catch (e) {
        if (cancelled) return;
        setRegistry(null);
        setError(e instanceof ApiError ? e.message : "The registry is unavailable.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="mx-auto max-w-5xl">
      <header className="mb-8">
        <h1 className="text-[20px] font-medium tracking-tight">Strategy Lab</h1>
        <p className="mt-1.5 text-[13px] text-[var(--text-secondary)]">
          What each strategy believes, and where it sits in the promotion funnel.
        </p>
      </header>

      {loading && (
        <p className="text-[13px] text-[var(--text-secondary)]">Reading the registry…</p>
      )}

      {error && (
        <div className="rounded-sm border border-[var(--negative)] bg-[var(--surface-2)] p-5">
          <p className="text-[13px] text-[var(--negative)]">{error}</p>
        </div>
      )}

      {registry && (
        <>
          <section className="mb-9">
            <h2 className="mb-3 text-[11px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
              Promotion funnel
            </h2>
            <div className="overflow-x-auto rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] p-5">
              <div className="flex flex-wrap gap-x-8 gap-y-4">
                {registry.funnel.map((stage) => (
                  <div key={stage.status}>
                    <dt className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
                      {stage.status}
                    </dt>
                    <dd
                      className="tabular mt-1 text-[18px]"
                      style={{
                        color:
                          stage.count === 0
                            ? "var(--text-tertiary)"
                            : (STATUS_TONE[stage.status] ?? "var(--text-primary)"),
                      }}
                    >
                      {stage.count}
                    </dd>
                  </div>
                ))}
              </div>
              <p className="mt-5 text-[12px] text-[var(--text-tertiary)]">{registry.notice}</p>
            </div>
          </section>

          <section>
            <h2 className="mb-3 text-[11px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
              Registered strategies
            </h2>
            <div className="flex flex-col gap-4">
              {registry.strategies.map((s) => (
                <StrategyCard key={s.key} strategy={s} />
              ))}
            </div>
            <p className="mt-4 text-[12px] text-[var(--text-tertiary)]">
              There is no promotion control here. Moving a strategy toward ACTIVE is an
              evidence decision that requires validated out-of-sample performance, and a
              button that skipped that would be the quickest route to trading an
              unvalidated strategy.
            </p>
          </section>
        </>
      )}
    </div>
  );
}
