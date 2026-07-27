"use client";

import { SystemHealthProvider } from "@/lib/SystemHealthContext";

/**
 * Application providers.
 *
 * React Query was removed here: the only consumer was the system-health poll,
 * which now runs as a plain loop (see lib/useSystemHealth.ts for why). It will
 * be reintroduced when there is data that genuinely benefits from caching and
 * invalidation — backtest runs and journal queries in later stages.
 */
export function Providers({ children }: { children: React.ReactNode }) {
  return <SystemHealthProvider>{children}</SystemHealthProvider>;
}
