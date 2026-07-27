/**
 * API client with runtime validation.
 *
 * Every response is parsed through Zod. A backend that changes shape produces a
 * clear validation error rather than `undefined` propagating into a component
 * that renders a risk figure — for this application, silently rendering a wrong
 * number is worse than rendering an error.
 */

import { z } from "zod";
import { API_URL } from "@/config/product";

export const componentHealthSchema = z.object({
  status: z.enum(["ok", "degraded", "down", "disabled"]),
  detail: z.string().nullable(),
});

export const executionSafetySchema = z.object({
  mode: z.string(),
  approval_mode: z.string(),
  risk_profile: z.string(),
  broker_execution_enabled: z.boolean(),
  live_execution_implemented: z.boolean(),
  kill_switch_engaged: z.boolean(),
  max_risk_per_trade_pct: z.string(),
  notice: z.string(),
});

export const healthSchema = z.object({
  product: z.string(),
  version: z.string(),
  environment: z.string(),
  status: z.enum(["ok", "degraded", "down", "disabled"]),
  execution_safety: executionSafetySchema,
  components: z.record(z.string(), componentHealthSchema),
});

export type Health = z.infer<typeof healthSchema>;
export type ExecutionSafety = z.infer<typeof executionSafetySchema>;
export type ComponentHealth = z.infer<typeof componentHealthSchema>;

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, schema: z.ZodType<T>): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
    });
  } catch {
    throw new ApiError(
      `Cannot reach the API at ${API_URL}. Is it running? (make dev)`,
    );
  }

  if (!response.ok) {
    throw new ApiError(`${path} returned ${response.status}`, response.status);
  }

  const parsed = schema.safeParse(await response.json());
  if (!parsed.success) {
    throw new ApiError(`Unexpected response shape from ${path}: ${parsed.error.message}`);
  }
  return parsed.data;
}

export const api = {
  health: () => request("/health", healthSchema),
};
