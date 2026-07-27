"use client";

import { type ComponentHealth } from "@/lib/api";
import { useSystemHealthContext } from "@/lib/SystemHealthContext";
import { SAFETY_NOTICE } from "@/config/product";

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

export default function CommandCentre() {
  const { health: usable, failed, loading, message } = useSystemHealthContext();

  // A blank panel reads as a healthy empty one on a risk dashboard, so any
  // state that is neither "loading" nor "has trustworthy data" must explain
  // itself explicitly.
  const showFailure = failed || (!usable && !loading);

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

      {usable && (
        <>
          <section className="mb-9">
            <h2 className="mb-3 text-[11px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
              Execution safety
            </h2>
            <div className="rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] p-5">
              <dl className="grid grid-cols-2 gap-x-10 gap-y-3 sm:grid-cols-3">
                {[
                  ["Mode", usable.execution_safety.mode],
                  ["Approval mode", usable.execution_safety.approval_mode.replace(/_/g, " ")],
                  ["Risk profile", usable.execution_safety.risk_profile],
                  [
                    "Broker execution",
                    usable.execution_safety.broker_execution_enabled ? "ENABLED" : "disabled",
                  ],
                  [
                    "Live execution",
                    usable.execution_safety.live_execution_implemented
                      ? "implemented"
                      : "not implemented",
                  ],
                  ["Max risk / trade", `${usable.execution_safety.max_risk_per_trade_pct}%`],
                ].map(([label, value]) => (
                  <div key={label}>
                    <dt className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
                      {label}
                    </dt>
                    <dd className="tabular mt-1 text-[13px]">{value}</dd>
                  </div>
                ))}
              </dl>
            </div>
          </section>

          <section>
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

          <p className="mt-8 text-[12px] text-[var(--text-tertiary)]">
            Account, drawdown, exposure and position panels arrive with the accounting and
            paper-broker services in Stage D. They are omitted here rather than shown with
            placeholder figures.
          </p>
        </>
      )}
    </div>
  );
}
