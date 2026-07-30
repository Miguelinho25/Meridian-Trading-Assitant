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

/**
 * Backtest records. Numbers stay strings — see the note on limits above.
 *
 * `survives_all` is nullable on purpose: null means validation was not run,
 * which is not the same as run-and-failed and must never render as a pass.
 */
export const runSummarySchema = z.object({
  id: z.string(),
  strategy_key: z.string(),
  strategy_version: z.string(),
  created_at: z.string(),
  duration_ms: z.number(),
  trade_count: z.number(),
  provenance: z.string(),
  net_pnl: z.string().nullable(),
  max_drawdown_pct: z.string().nullable(),
  survives_all: z.boolean().nullable(),
  is_evidence: z.boolean(),
  is_reproducible: z.boolean(),
  git_dirty: z.boolean(),
  manifest_hash: z.string(),
  result_hash: z.string(),
  instruments: z.array(z.string()),
  timeframe: z.string(),
});

export const runDetailSchema = runSummarySchema.extend({
  manifest: z.record(z.string(), z.unknown()),
  manifest_version: z.string(),
  metrics: z.record(z.string(), z.unknown()),
  validation: z.record(z.string(), z.unknown()),
  git_commit: z.string(),
  git_branch: z.string(),
  engine_version: z.string(),
  feature_pipeline_version: z.string(),
  risk_profile_version: z.string(),
  market_data_provider: z.string(),
  dataset_version: z.string(),
  data_start: z.string().nullable(),
  data_end: z.string().nullable(),
  bar_count: z.number(),
  spread_assumed: z.boolean(),
  spread_model: z.string(),
  slippage_model: z.string(),
  commission_model: z.string(),
  risk_profile: z.string(),
  starting_balance: z.string().nullable(),
  account_currency: z.string(),
  seed: z.number(),
  parameters: z.record(z.string(), z.unknown()),
  signals_generated: z.number(),
  proposals_made: z.number(),
  rejections: z.number(),
  notes: z.string(),
  irreproducible_reason: z.string(),
});

export const equityPointSchema = z.object({
  at: z.string(),
  equity: z.string(),
  drawdown_pct: z.string(),
});

export const determinismBreakSchema = z.object({
  manifest_hash: z.string(),
  run_ids: z.array(z.string()),
  result_hashes: z.array(z.string()),
  summary: z.string(),
});

export type RunSummary = z.infer<typeof runSummarySchema>;
export type RunDetail = z.infer<typeof runDetailSchema>;
export type EquityPoint = z.infer<typeof equityPointSchema>;
export type DeterminismBreak = z.infer<typeof determinismBreakSchema>;

export const strategyHealthSchema = z.object({
  calls: z.number(),
  faults: z.number(),
  timeouts: z.number(),
  fault_rate: z.string(),
  mean_micros: z.string(),
  last_fault: z.string().nullable(),
  last_fault_at: z.string().nullable(),
});

/**
 * `supported_*` are hard filters; `expected_regimes` is a prior.
 *
 * They are separate fields on purpose. Filtering on the prior would make the
 * author's belief unfalsifiable and suppress the signals that would reveal they
 * were wrong, so the UI must never present the two the same way.
 */
export const strategySchema = z.object({
  key: z.string(),
  id: z.string(),
  version: z.string(),
  author: z.string(),
  hypothesis: z.string(),
  description: z.string(),
  status: z.string(),
  is_runnable: z.boolean(),
  quarantine_reason: z.string().nullable(),
  deterministic: z.boolean(),
  required_features: z.array(z.string()),
  lookback_bars: z.number(),
  max_signals_per_day: z.number(),
  supported_instruments: z.array(z.string()).nullable(),
  supported_sessions: z.array(z.string()).nullable(),
  expected_regimes: z.array(z.string()).nullable(),
  health: strategyHealthSchema,
});

export const registrySchema = z.object({
  strategies: z.array(strategySchema),
  funnel: z.array(
    z.object({ status: z.string(), count: z.number(), runnable: z.boolean() }),
  ),
  notice: z.string(),
});

export type Strategy = z.infer<typeof strategySchema>;
export type Registry = z.infer<typeof registrySchema>;

export const propFirmProfileSchema = z.object({
  profile_id: z.string(),
  name: z.string(),
  version: z.string(),
  phase: z.string(),
  enabled: z.boolean(),
  starting_balance: z.string(),
  account_currency: z.string(),
  profit_target_pct: z.string().nullable(),
  max_daily_loss_pct: z.string(),
  max_total_loss_pct: z.string(),
  reset_time: z.string(),
  reset_timezone: z.string(),
  trailing_stops_at_initial_balance: z.boolean(),
  min_trading_days: z.number(),
  max_trading_days: z.number().nullable(),
  inactivity_days: z.number().nullable(),
  consistency_rule_enabled: z.boolean(),
  max_single_day_profit_pct_of_total: z.string().nullable(),
  weekend_holding_allowed: z.boolean(),
  overnight_holding_allowed: z.boolean(),
  news_trading_restricted: z.boolean(),
  news_buffer_minutes: z.number(),
  ea_allowed: z.boolean(),
  instrument_restrictions: z.array(z.string()),
  buffer_warning_pct: z.string(),
  notes: z.string(),
  definitions: z.array(
    z.object({
      field_name: z.string(),
      value: z.string(),
      consequence: z.string(),
      stricter_option: z.boolean(),
    }),
  ),
  verification: z.object({
    is_verified: z.boolean(),
    source: z.string(),
    last_verified_at: z.string().nullable(),
    verified_by: z.string().nullable(),
    verification_age_days: z.number().nullable(),
    is_stale: z.boolean(),
    warning: z.string(),
  }),
});

export type PropFirmProfile = z.infer<typeof propFirmProfileSchema>;

export const paperSessionSchema = z.object({
  id: z.string(),
  status: z.string(),
  mode: z.string(),
  bar_source: z.string(),
  instruments: z.array(z.string()),
  timeframe: z.string(),
  starting_balance: z.string(),
  balance: z.string(),
  equity: z.string(),
  ticks: z.number(),
  closed_trade_count: z.number(),
  open_position_count: z.number(),
  last_tick_at: z.string().nullable(),
  started_at: z.string(),
  halt_reason: z.string(),
});

export const paperPositionSchema = z.object({
  position_id: z.string(),
  instrument: z.string(),
  direction: z.string(),
  lots: z.string(),
  entry_price: z.string(),
  opened_at: z.string(),
  strategy_id: z.string(),
  stop_loss: z.string().nullable(),
  take_profit: z.string().nullable(),
});

export const paperSessionDetailSchema = paperSessionSchema.extend({
  approval_mode: z.string(),
  risk_profile: z.string(),
  prop_profile_id: z.string(),
  strategy_keys: z.array(z.string()),
  high_water_mark: z.string(),
  balance_at_day_start: z.string(),
  realised_pnl: z.string(),
  total_commission: z.string(),
  signals_generated: z.number(),
  proposals_made: z.number(),
  orders_submitted: z.number(),
  rejections: z.number(),
  working_order_count: z.number(),
  positions: z.array(paperPositionSchema),
});

export const decisionGroupSchema = z.object({
  verdict: z.string(),
  reason_code: z.string(),
  count: z.number(),
  share: z.string(),
});

export const paperTradeSchema = z.object({
  trade_id: z.string(),
  instrument: z.string(),
  direction: z.string(),
  lots: z.string(),
  entry_price: z.string(),
  exit_price: z.string(),
  opened_at: z.string(),
  closed_at: z.string(),
  pnl: z.string(),
  exit_reason: z.string(),
  mfe_pips: z.string().nullable(),
  mae_pips: z.string().nullable(),
});

export type PaperSession = z.infer<typeof paperSessionSchema>;
export type PaperSessionDetail = z.infer<typeof paperSessionDetailSchema>;
export type PaperPosition = z.infer<typeof paperPositionSchema>;
export type DecisionGroup = z.infer<typeof decisionGroupSchema>;
export type PaperTrade = z.infer<typeof paperTradeSchema>;

export const api = {
  health: () => request("/health", healthSchema),
  paperSessions: () => request("/api/paper", z.array(paperSessionSchema)),
  paperSession: (id: string) => request(`/api/paper/${id}`, paperSessionDetailSchema),
  paperEquity: (id: string) =>
    request(`/api/paper/${id}/equity`, z.array(equityPointSchema)),
  paperTrades: (id: string) =>
    request(`/api/paper/${id}/trades`, z.array(paperTradeSchema)),
  paperDecisions: (id: string) =>
    request(`/api/paper/${id}/decisions`, z.array(decisionGroupSchema)),
  propFirm: () => request("/api/prop-firm", z.array(propFirmProfileSchema)),
  strategies: () => request("/api/strategies", registrySchema),
  backtests: () => request("/api/backtests", z.array(runSummarySchema)),
  backtest: (id: string) => request(`/api/backtests/${id}`, runDetailSchema),
  backtestEquity: (id: string) =>
    request(`/api/backtests/${id}/equity`, z.array(equityPointSchema)),
  determinismBreaks: () =>
    request("/api/backtests/determinism-breaks", z.array(determinismBreakSchema)),
  limits: () => request("/api/risk/limits", limitsSchema),
  throttle: () => request("/api/risk/throttle", z.array(throttleBandSchema)),
  profiles: () => request("/api/risk/profiles", z.array(profileSummarySchema)),
};
