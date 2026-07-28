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

/**
 * Limits arrive as strings and stay strings.
 *
 * Parsing "0.35" into a JS number would make it a float, and every subsequent
 * render would be one rounding away from showing a risk limit that is not the
 * one enforced. Nothing here does arithmetic on these values — they are
 * displayed, compared as strings, and never recomputed client-side.
 */
export const tierValueSchema = z.object({
  tier: z.string(),
  value: z.string().nullable(),
});

export const effectiveLimitSchema = z.object({
  field_name: z.string(),
  value: z.string().nullable(),
  tightens: z.enum(["LOWER", "HIGHER"]),
  bound_by: z.array(z.string()),
  tier_values: z.array(tierValueSchema),
  was_tightened: z.boolean(),
  unset: z.boolean(),
});

export const limitsSchema = z.object({
  risk_profile: z.string(),
  profile_description: z.string(),
  mode: z.string(),
  profile_allows_mode: z.boolean(),
  limits: z.array(effectiveLimitSchema),
  notice: z.string(),
});

export const throttleBandSchema = z.object({
  from_consumed: z.string(),
  to_consumed: z.string(),
  risk_multiplier: z.string(),
  confidence_uplift: z.string(),
  reward_risk_uplift: z.string(),
});

export const profileSummarySchema = z.object({
  name: z.string(),
  description: z.string(),
  recommended: z.boolean(),
  allowed_modes: z.array(z.string()),
  active: z.boolean(),
});

export type Limits = z.infer<typeof limitsSchema>;
export type EffectiveLimit = z.infer<typeof effectiveLimitSchema>;
export type ThrottleBand = z.infer<typeof throttleBandSchema>;
export type ProfileSummary = z.infer<typeof profileSummarySchema>;

export const api = {
  health: () => request("/health", healthSchema),
  limits: () => request("/api/risk/limits", limitsSchema),
  throttle: () => request("/api/risk/throttle", z.array(throttleBandSchema)),
  profiles: () => request("/api/risk/profiles", z.array(profileSummarySchema)),
};
