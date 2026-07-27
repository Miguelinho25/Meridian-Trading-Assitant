"use client";

import { useSystemHealth } from "@/lib/useSystemHealth";
import { PRODUCT_NAME } from "@/config/product";
import { RiskIndicator } from "./RiskIndicator";

const NAV = [
  { label: "Command Centre", href: "/", ready: true },
  { label: "Live Market", href: "/market", ready: false },
  { label: "Proposals", href: "/proposals", ready: false },
  { label: "Risk Lab", href: "/risk", ready: false },
  { label: "Prop Firm", href: "/prop-firm", ready: false },
  { label: "Backtest Lab", href: "/backtest", ready: false },
  { label: "Strategy Lab", href: "/strategy", ready: false },
  { label: "Journal", href: "/journal", ready: false },
  { label: "Neural Memory", href: "/memory", ready: false },
  { label: "Analytics", href: "/analytics", ready: false },
  { label: "AI Research", href: "/research", ready: false },
  { label: "System", href: "/system", ready: false },
];

export function Shell({ children }: { children: React.ReactNode }) {
  const { health, failed } = useSystemHealth();

  return (
    <div className="flex min-h-screen flex-col">
      <RiskIndicator
        safety={health?.execution_safety ?? null}
        degraded={failed || health?.status === "degraded" || health?.status === "down"}
      />

      <div className="flex flex-1">
        <nav className="w-52 shrink-0 border-r border-[var(--border-subtle)] bg-[var(--surface-1)] py-5">
          <div className="px-5 pb-5">
            <span className="text-[13px] font-semibold tracking-[0.2em] text-[var(--text-primary)]">
              {PRODUCT_NAME.toUpperCase()}
            </span>
          </div>
          <ul className="flex flex-col">
            {NAV.map((item) => (
              <li key={item.href}>
                <span
                  className={`flex items-center justify-between px-5 py-2 text-[12.5px] ${
                    item.ready
                      ? "text-[var(--text-primary)]"
                      : "cursor-not-allowed text-[var(--text-tertiary)]"
                  }`}
                  title={item.ready ? undefined : "Not built yet — later milestone stage"}
                >
                  {item.label}
                  {!item.ready && (
                    <span className="text-[9px] uppercase tracking-wider opacity-60">
                      soon
                    </span>
                  )}
                </span>
              </li>
            ))}
          </ul>
        </nav>

        <main className="grid-surface flex-1 overflow-auto p-8">{children}</main>
      </div>
    </div>
  );
}
