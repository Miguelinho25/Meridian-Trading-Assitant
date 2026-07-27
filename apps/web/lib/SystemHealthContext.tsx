"use client";

import { createContext, useContext } from "react";
import { useSystemHealth, type SystemHealth } from "./useSystemHealth";

const SystemHealthContext = createContext<SystemHealth | null>(null);

/**
 * Single shared system-state poll.
 *
 * The risk indicator in the shell and the Command Centre both read this. If
 * each ran its own poll they could briefly disagree — the persistent risk bar
 * showing "clear" while the panel below it showed a failure. On a dashboard
 * whose purpose is to state whether trading is safe, two contradictory answers
 * on one screen is worse than either answer alone.
 */
export function SystemHealthProvider({ children }: { children: React.ReactNode }) {
  const value = useSystemHealth();
  return (
    <SystemHealthContext.Provider value={value}>{children}</SystemHealthContext.Provider>
  );
}

export function useSystemHealthContext(): SystemHealth {
  const value = useContext(SystemHealthContext);
  if (value === null) {
    throw new Error("useSystemHealthContext must be used inside <SystemHealthProvider>");
  }
  return value;
}
