"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { api, type KillSwitchState } from "@/lib/api";
import { useSystemHealthContext } from "@/lib/SystemHealthContext";
import { PRODUCT_NAME } from "@/config/product";
import { RiskIndicator } from "./RiskIndicator";

const NAV = [
  { label: "Command Centre", href: "/", ready: true },
  { label: "Live Market", href: "/market", ready: true },
  { label: "Proposals", href: "/proposals", ready: true },
  { label: "Risk Lab", href: "/risk", ready: true },
  { label: "Prop Firm", href: "/prop-firm", ready: true },
  { label: "Backtest Lab", href: "/backtest", ready: true },
  { label: "Strategy Lab", href: "/strategy", ready: true },
  { label: "Journal", href: "/journal", ready: true },
  { label: "Neural Memory", href: "/memory", ready: false },
  { label: "Analytics", href: "/analytics", ready: true },
  { label: "AI Research", href: "/research", ready: false },
  { label: "System", href: "/system", ready: true },
];

export function Shell({ children }: { children: React.ReactNode }) {
  const { health, failed } = useSystemHealthContext();
  const pathname = usePathname();
  const [killSwitch, setKillSwitch] = useState<KillSwitchState | null>(null);

  const refreshKillSwitch = useCallback(async () => {
    try {
      setKillSwitch(await api.killSwitch());
    } catch {
      // Discard rather than retain. A stale "clear" reading shown as current is
      // the single most dangerous thing this chrome could display, so the header
      // falls back to /health, which fails closed on its own.
      setKillSwitch(null);
    }
  }, []);

  useEffect(() => {
    void refreshKillSwitch();
    const timer = setInterval(() => void refreshKillSwitch(), 5000);
    return () => clearInterval(timer);
  }, [refreshKillSwitch]);

  return (
    <div className="flex min-h-screen flex-col">
      <RiskIndicator
        safety={health?.execution_safety ?? null}
        degraded={failed || health?.status === "degraded" || health?.status === "down"}
        killSwitch={killSwitch}
        onKillSwitchChanged={() => void refreshKillSwitch()}
      />

      <div className="flex flex-1">
        <nav className="w-52 shrink-0 border-r border-[var(--border-subtle)] bg-[var(--surface-1)] py-5">
          <div className="px-5 pb-5">
            <span className="text-[13px] font-semibold tracking-[0.2em] text-[var(--text-primary)]">
              {PRODUCT_NAME.toUpperCase()}
            </span>
          </div>
          <ul className="flex flex-col">
            {NAV.map((item) => {
              const active = pathname === item.href;
              const shared = `flex items-center justify-between px-5 py-2 text-[12.5px] ${
                active
                  ? "border-l-2 border-[var(--text-primary)] bg-[var(--surface-2)] pl-[18px] text-[var(--text-primary)]"
                  : ""
              }`;

              // Unbuilt pages stay spans rather than dead links: a nav item that
              // navigates to a 404 is worse than one that plainly says "soon".
              return (
                <li key={item.href}>
                  {item.ready ? (
                    <Link
                      href={item.href}
                      aria-current={active ? "page" : undefined}
                      className={`${shared} text-[var(--text-primary)] hover:bg-[var(--surface-2)]`}
                    >
                      {item.label}
                    </Link>
                  ) : (
                    <span
                      className={`${shared} cursor-not-allowed text-[var(--text-tertiary)]`}
                      title="Not built yet — later milestone stage"
                    >
                      {item.label}
                      <span className="text-[9px] uppercase tracking-wider opacity-60">
                        soon
                      </span>
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        </nav>

        <main className="grid-surface flex-1 overflow-auto p-8">{children}</main>
      </div>
    </div>
  );
}
