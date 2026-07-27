"use client";

import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type Health } from "./api";

export const HEALTH_POLL_MS = 10_000;

export interface SystemHealth {
  /** The response, or null whenever it cannot be trusted as current. */
  health: Health | null;
  /** True when the most recent attempt failed. */
  failed: boolean;
  /** True while an attempt is in flight and nothing usable is held. */
  loading: boolean;
  message: string | null;
}

/**
 * System-state poll for the risk indicator and Command Centre.
 *
 * Deliberately drives its own interval rather than relying on React Query's
 * `refetchInterval`. That option stops firing once a query settles into an
 * error state and does not fire at all while the document is hidden, both of
 * which were observed here: the dashboard froze showing a healthy reading
 * after the backend had gone away, and never recovered when it returned.
 *
 * For a panel whose entire job is to answer "is it safe to trade right now",
 * an explicit unconditional interval is worth more than idiomatic caching.
 *
 * The second rule is that a failed attempt invalidates the retained response.
 * React Query keeps the last success on error; presenting a minute-old
 * "broker execution: disabled" as current asserts something we cannot support,
 * so `health` goes null and the caller shows the failure instead.
 */
export function useSystemHealth(): SystemHealth {
  const { data, isError, error, isFetching, refetch } = useQuery({
    queryKey: ["health"],
    queryFn: api.health,
    refetchInterval: false,
    retry: false,
    gcTime: 0,
  });

  useEffect(() => {
    const id = setInterval(() => {
      void refetch();
    }, HEALTH_POLL_MS);
    return () => clearInterval(id);
  }, [refetch]);

  return {
    health: isError ? null : (data ?? null),
    failed: isError,
    loading: isFetching && !data,
    message: isError
      ? error instanceof Error
        ? error.message
        : "System state is unavailable."
      : null,
  };
}
