"use client";

/**
 * Persistent risk indicator — required in the application shell at all times.
 *
 * Shows mode, risk profile, drawdown, daily loss remaining and kill-switch
 * state. Deliberately the most prominent element in the chrome: an operator
 * should never have to navigate to discover what the system is permitted to do.
 *
 * Stage A wires mode, profile and kill switch to the live API. Drawdown and
 * daily-loss figures render as "—" until the accounting service exists in
 * Stage D; they are shown rather than hidden so the layout is honest about what
 * is not yet measured.
 */

import type { ExecutionSafety, KillSwitchState } from "@/lib/api";
import { KillSwitchControl } from "./KillSwitchControl";

function Field({
  label,
  value,
  tone = "neutral",
  title,
}: {
  label: string;
  value: string;
  tone?: "neutral" | "positive" | "caution" | "negative" | "info";
  title?: string;
}) {
  const toneColor = {
    neutral: "var(--text-primary)",
    positive: "var(--positive)",
    caution: "var(--caution)",
    negative: "var(--negative)",
    info: "var(--info)",
  }[tone];

  return (
    <div
      className="flex shrink-0 flex-col justify-center gap-0.5 whitespace-nowrap px-4"
      title={title}
    >
      <span className="text-[10px] uppercase tracking-[0.12em] text-[var(--text-tertiary)]">
        {label}
      </span>
      <span className="tabular text-[13px] font-medium leading-none" style={{ color: toneColor }}>
        {value}
      </span>
    </div>
  );
}

export function RiskIndicator({
  safety,
  degraded,
  killSwitch,
  onKillSwitchChanged,
}: {
  safety: ExecutionSafety | null;
  degraded: boolean;
  killSwitch: KillSwitchState | null;
  onKillSwitchChanged: () => void;
}) {
  // Fail-closed presentation: if state is unknown, say so rather than
  // implying a safe value we have not confirmed.
  if (!safety) {
    return (
      <div className="flex h-14 items-center border-b border-[var(--border-strong)] bg-[var(--surface-1)] px-6">
        <span className="text-[13px] text-[var(--caution)]">
          System state unavailable — treat as unsafe to trade
        </span>
      </div>
    );
  }

  // /health and the kill-switch endpoint both report this and are kept in
  // agreement server-side. The dedicated reading is preferred when present
  // because it also carries *why*, and whether the state is merely assumed.
  const killEngaged = killSwitch?.engaged ?? safety.kill_switch_engaged;

  return (
    <div
      // Scrolls horizontally rather than clipping: a risk figure that is cut off
      // is worse than one the operator has to scroll to reach.
      className="flex min-h-14 items-stretch overflow-x-auto border-b bg-[var(--surface-1)]"
      style={{
        borderColor: killEngaged ? "var(--negative)" : "var(--border-strong)",
      }}
    >
      <div className="flex shrink-0 items-stretch divide-x divide-[var(--border-subtle)] py-2">
        <Field
          label="Mode"
          value={safety.mode.toUpperCase()}
          tone="info"
          title="Research, backtest and paper only — no broker adapter exists"
        />
        <Field label="Risk profile" value={safety.risk_profile} />
        <Field
          label="Max risk / trade"
          value={`${safety.max_risk_per_trade_pct}%`}
          title="System hard ceiling. No profile or control may exceed it."
        />
        <Field
          label="Drawdown used"
          value="—"
          title="Available once account accounting lands in Stage D"
        />
        <Field
          label="Daily loss remaining"
          value="—"
          title="Available once account accounting lands in Stage D"
        />
        <Field label="Approval" value={safety.approval_mode.replace(/_/g, " ")} />
      </div>

      <div className="ml-auto flex shrink-0 items-center gap-3 whitespace-nowrap px-6">
        {degraded && (
          <span className="rounded-sm border border-[var(--caution)] px-2 py-1 text-[11px] uppercase tracking-wider text-[var(--caution)]">
            Degraded
          </span>
        )}
        <KillSwitchControl
          engaged={killEngaged}
          indeterminate={killSwitch?.indeterminate ?? false}
          fromConfiguration={killSwitch?.from_configuration ?? false}
          onChanged={onKillSwitchChanged}
        />
      </div>
    </div>
  );
}
