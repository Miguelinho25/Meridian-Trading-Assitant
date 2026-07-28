"use client";

import { useEffect, useState } from "react";
import { api, ApiError, type EffectiveLimit, type Limits, type ThrottleBand } from "@/lib/api";

/**
 * Risk Lab.
 *
 * Read-only, permanently. The risk engine has final authority (invariant I5), so
 * there is no control on this page that can change a limit — and that absence is
 * the point of the page, not a gap in it. What it shows instead is *provenance*:
 * which of the four tiers bound each limit, and what looser value it overrode.
 */

const LABELS: Record<string, string> = {
  risk_per_trade_pct: "Risk per trade",
  daily_risk_budget_pct: "Daily risk budget",
  max_open_risk_pct: "Max open risk",
  max_instrument_exposure_pct: "Max instrument exposure",
  max_currency_exposure_pct: "Max currency exposure",
  max_correlated_exposure_pct: "Max correlated exposure",
  max_strategy_budget_pct: "Max strategy budget",
  max_margin_utilisation_pct: "Max margin utilisation",
  max_positions: "Max simultaneous positions",
  max_trades_per_session: "Max trades per session",
  max_slippage_pips: "Max slippage",
  loss_streak_cooldown_after: "Loss-streak cooldown after",
  max_stop_atr_multiple: "Max stop distance",
  min_reward_risk: "Min reward:risk",
  min_confidence: "Min confidence",
  news_buffer_minutes: "News buffer",
  min_stop_atr_multiple: "Min stop distance",
};

const UNITS: Record<string, string> = {
  risk_per_trade_pct: "%",
  daily_risk_budget_pct: "%",
  max_open_risk_pct: "%",
  max_instrument_exposure_pct: "%",
  max_currency_exposure_pct: "%",
  max_correlated_exposure_pct: "%",
  max_strategy_budget_pct: "%",
  max_margin_utilisation_pct: "%",
  max_slippage_pips: " pips",
  news_buffer_minutes: " min",
  max_stop_atr_multiple: "× ATR",
  min_stop_atr_multiple: "× ATR",
};

function LimitRow({ limit }: { limit: EffectiveLimit }) {
  const unit = UNITS[limit.field_name] ?? "";
  const superseded = limit.tier_values.filter(
    (t) => t.value !== null && !limit.bound_by.includes(t.tier),
  );

  return (
    <tr className="border-b border-[var(--border-subtle)] last:border-0 align-top">
      <td className="py-2.5 pr-6 text-[13px] text-[var(--text-primary)]">
        {LABELS[limit.field_name] ?? limit.field_name.replace(/_/g, " ")}
        <span className="ml-2 text-[10px] uppercase tracking-wider text-[var(--text-tertiary)]">
          {limit.tightens === "LOWER" ? "ceiling" : "floor"}
        </span>
      </td>

      <td className="tabular py-2.5 pr-6 text-[13px] whitespace-nowrap">
        {limit.unset ? (
          // Not a blank field: no tier set it, and the engine rejects rather
          // than inventing a default, so this blocks trading.
          <span className="text-[var(--negative)]" title="No tier defines this limit">
            unset — blocks
          </span>
        ) : (
          <span>
            {limit.value}
            <span className="text-[var(--text-tertiary)]">{unit}</span>
          </span>
        )}
      </td>

      <td className="py-2.5 pr-6 text-[12px] whitespace-nowrap">
        {limit.bound_by.length === 0 ? (
          <span className="text-[var(--text-tertiary)]">—</span>
        ) : (
          <span className="text-[var(--text-secondary)]">{limit.bound_by.join(" + ")}</span>
        )}
      </td>

      <td className="py-2.5 text-[12px] text-[var(--text-tertiary)]">
        {limit.was_tightened ? (
          <span title="A looser value at another tier was overridden">
            tightened from{" "}
            {superseded.map((t) => `${t.value}${unit} (${t.tier})`).join(", ")}
          </span>
        ) : (
          <span className="opacity-50">—</span>
        )}
      </td>
    </tr>
  );
}

function ThrottleTable({ bands }: { bands: ThrottleBand[] }) {
  const pct = (v: string) => `${(Number(v) * 100).toFixed(0)}%`;

  return (
    <div className="overflow-x-auto rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] px-5">
      <table className="w-full min-w-[560px]">
        <thead>
          <tr className="border-b border-[var(--border-subtle)]">
            {["Drawdown consumed", "Size", "Confidence floor", "Reward:risk floor"].map((h) => (
              <th
                key={h}
                className="py-2.5 pr-6 text-left text-[10px] font-normal uppercase tracking-[0.14em] text-[var(--text-tertiary)]"
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {bands.map((band) => {
            const blocked = Number(band.risk_multiplier) === 0;
            return (
              <tr
                key={band.from_consumed}
                className="border-b border-[var(--border-subtle)] last:border-0"
              >
                <td className="tabular py-2.5 pr-6 text-[13px]">
                  {pct(band.from_consumed)} –{" "}
                  {Number(band.to_consumed) > 1 ? "beyond" : pct(band.to_consumed)}
                </td>
                <td
                  className="tabular py-2.5 pr-6 text-[13px]"
                  style={{ color: blocked ? "var(--negative)" : undefined }}
                >
                  {blocked ? "no new trades" : `${(Number(band.risk_multiplier) * 100).toFixed(0)}%`}
                </td>
                {/* Uplifts are dead parameters once size is zero — showing "+0"
                    there would imply a live setting that had been relaxed. */}
                <td className="tabular py-2.5 pr-6 text-[13px] text-[var(--text-secondary)]">
                  {blocked ? "—" : `+${band.confidence_uplift}`}
                </td>
                <td className="tabular py-2.5 text-[13px] text-[var(--text-secondary)]">
                  {blocked ? "—" : `+${band.reward_risk_uplift}`}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default function RiskLab() {
  const [limits, setLimits] = useState<Limits | null>(null);
  const [throttle, setThrottle] = useState<ThrottleBand[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const [l, t] = await Promise.all([api.limits(), api.throttle()]);
        if (cancelled) return;
        setLimits(l);
        setThrottle(t);
        setError(null);
      } catch (e) {
        if (cancelled) return;
        // Discard any previously held data: stale limits shown as current are
        // the specific failure this page must not have.
        setLimits(null);
        setThrottle(null);
        setError(e instanceof ApiError ? e.message : "Risk configuration is unavailable.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  const unset = limits?.limits.filter((l) => l.unset) ?? [];

  return (
    <div className="mx-auto max-w-5xl">
      <header className="mb-8">
        <h1 className="text-[20px] font-medium tracking-tight">Risk Lab</h1>
        <p className="mt-1.5 text-[13px] text-[var(--text-secondary)]">
          The limits actually in force, and which tier set each one.
        </p>
      </header>

      {loading && (
        <p className="text-[13px] text-[var(--text-secondary)]">Reading risk configuration…</p>
      )}

      {error && (
        <div className="rounded-sm border border-[var(--negative)] bg-[var(--surface-2)] p-5">
          <p className="text-[13px] text-[var(--negative)]">{error}</p>
          <p className="mt-2 text-[12.5px] text-[var(--text-secondary)]">
            No limits can be shown until the API responds. An unknown limit must be treated
            as unsafe to trade, never as unrestricted.
          </p>
        </div>
      )}

      {limits && throttle && (
        <>
          <section className="mb-9">
            <div className="rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] p-5">
              <div className="flex flex-wrap items-baseline gap-x-10 gap-y-3">
                <div>
                  <dt className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
                    Active profile
                  </dt>
                  <dd className="mt-1 text-[13px]">{limits.risk_profile}</dd>
                </div>
                <div>
                  <dt className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
                    Mode
                  </dt>
                  <dd className="mt-1 text-[13px]">
                    {limits.mode}
                    {!limits.profile_allows_mode && (
                      <span className="ml-2 text-[var(--negative)]">
                        profile does not permit this mode
                      </span>
                    )}
                  </dd>
                </div>
              </div>
              <p className="mt-4 text-[12.5px] text-[var(--text-secondary)]">
                {limits.profile_description}
              </p>
            </div>
          </section>

          {unset.length > 0 && (
            <div className="mb-9 rounded-sm border border-[var(--negative)] bg-[var(--surface-2)] p-5">
              <p className="text-[13px] text-[var(--negative)]">
                {unset.length} limit{unset.length === 1 ? " has" : "s have"} no value at any
                tier.
              </p>
              <p className="mt-2 text-[12.5px] text-[var(--text-secondary)]">
                The engine rejects rather than substituting a default, so proposals touching{" "}
                {unset.map((l) => LABELS[l.field_name] ?? l.field_name).join(", ")} will be
                refused.
              </p>
            </div>
          )}

          <section className="mb-9">
            <h2 className="mb-3 text-[11px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
              Effective limits
            </h2>
            <div className="overflow-x-auto rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] px-5">
              <table className="w-full min-w-[720px]">
                <thead>
                  <tr className="border-b border-[var(--border-subtle)]">
                    {["Limit", "In force", "Set by", "Provenance"].map((h) => (
                      <th
                        key={h}
                        className="py-2.5 pr-6 text-left text-[10px] font-normal uppercase tracking-[0.14em] text-[var(--text-tertiary)]"
                      >
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {limits.limits.map((limit) => (
                    <LimitRow key={limit.field_name} limit={limit} />
                  ))}
                </tbody>
              </table>
            </div>
            <p className="mt-3 text-[12px] text-[var(--text-tertiary)]">{limits.notice}</p>
          </section>

          <section>
            <h2 className="mb-3 text-[11px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
              Drawdown throttle
            </h2>
            <ThrottleTable bands={throttle} />
            <p className="mt-3 text-[12px] text-[var(--text-tertiary)]">
              Recovery is convex against the account — 20% lost needs 25% to regain, 50% needs
              100% — so size falls and the quality floors rise as drawdown deepens.
            </p>
          </section>
        </>
      )}
    </div>
  );
}
