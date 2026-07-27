"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Health } from "./api";

export const HEALTH_POLL_MS = 10_000;

export interface SystemHealth {
  /** The response, or null whenever it cannot be trusted as current. */
  health: Health | null;
  /** True when the most recent attempt failed. */
  failed: boolean;
  /** True while the first attempt is in flight and nothing is held yet. */
  loading: boolean;
  message: string | null;
  /** When the last successful read completed. */
  lastOkAt: number | null;
  /** Force an immediate read. */
  refresh: () => void;
}

/**
 * System-state poll for the risk indicator and Command Centre.
 *
 * Deliberately a plain fetch loop rather than a cached query. Two earlier
 * attempts using React Query here both failed in ways that left the dashboard
 * asserting a healthy state after the backend had gone away:
 *
 *   1. `refetchInterval` stops firing once a query settles into an error state,
 *      and does not fire at all while the document is hidden — which a dashboard
 *      on a second monitor always is.
 *   2. Driving `refetch()` from an explicit interval fixed the polling, but the
 *      UI still failed to recover after a long outage: requests returned 200
 *      while the rendered state stayed stuck on the failure.
 *
 * This panel needs no caching, no deduplication and no shared invalidation. It
 * needs to be obviously correct, because its entire job is to answer "is it safe
 * to trade right now". A loop with no hidden state machine is worth more here
 * than idiomatic data fetching.
 *
 * The second rule is that a failed attempt discards the previous response.
 * Presenting a minute-old "broker execution: disabled" as current asserts
 * something we can no longer support, so `health` goes null and the caller
 * renders the failure instead.
 */
export function useSystemHealth(): SystemHealth {
  const [health, setHealth] = useState<Health | null>(null);
  const [failed, setFailed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState<string | null>(null);
  const [lastOkAt, setLastOkAt] = useState<number | null>(null);

  const mounted = useRef(true);
  // Guards against a slow failing request landing after a later fast success
  // and overwriting good state with a stale error.
  const seq = useRef(0);

  const read = useCallback(async () => {
    const ticket = ++seq.current;
    try {
      const result = await api.health();
      if (!mounted.current || ticket !== seq.current) return;
      setHealth(result);
      setFailed(false);
      setMessage(null);
      setLastOkAt(Date.now());
    } catch (error) {
      if (!mounted.current || ticket !== seq.current) return;
      setHealth(null);
      setFailed(true);
      setMessage(
        error instanceof Error ? error.message : "System state is unavailable.",
      );
    } finally {
      if (mounted.current && ticket === seq.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void read();

    const id = setInterval(() => void read(), HEALTH_POLL_MS);
    // Recover promptly when the operator returns to the tab, without waiting
    // out the interval.
    const onVisible = () => {
      if (!document.hidden) void read();
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onVisible);

    return () => {
      mounted.current = false;
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onVisible);
    };
  }, [read]);

  return { health, failed, loading, message, lastOkAt, refresh: read };
}
