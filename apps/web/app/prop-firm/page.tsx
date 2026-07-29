"use client";

import { useEffect, useState } from "react";
import { api, ApiError, type PropFirmProfile } from "@/lib/api";

/**
 * Prop Firm.
 *
 * Verification leads, because an unverified rule set is more dangerous than
 * none: precise-looking numbers invite a confidence they have not earned. The
 * bundled profile is an invented example, and this page says so before it says
 * anything else.
 *
 * Below that, the definitional choices rather than the headline percentages.
 * Every firm publishes "5% daily" — what decides whether an account survives is
 * whether that 5% counts floating losses, and whether it is measured from the
 * day's open or the day's peak. Each is shown with its consequence, since
 * "TRAILING" alone tells an operator nothing they can act on.
 */

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
        {label}
      </dt>
      <dd className="tabular mt-1 text-[13px]">{value}</dd>
    </div>
  );
}

function ProfileCard({ profile: p }: { profile: PropFirmProfile }) {
  const v = p.verification;

  return (
    <div className="rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h3 className="text-[14px] font-medium">{p.name}</h3>
        <span className="text-[11px] text-[var(--text-tertiary)]">
          {p.profile_id} · v{p.version} · {p.phase.replace(/_/g, " ")}
        </span>
      </div>

      {/* Before the numbers, always. */}
      {v.warning && (
        <div
          className="mt-4 rounded-sm border p-4"
          style={{
            borderColor: v.is_verified ? "var(--caution)" : "var(--negative)",
            background: "var(--surface-2)",
          }}
        >
          <p
            className="text-[10px] uppercase tracking-[0.14em]"
            style={{ color: v.is_verified ? "var(--caution)" : "var(--negative)" }}
          >
            {v.is_verified ? "Verification stale" : "Unverified rules"}
          </p>
          <p className="mt-2 text-[12.5px] leading-relaxed">{v.warning}</p>
          <p className="mt-2 text-[11px] text-[var(--text-tertiary)]">Source: {v.source}</p>
        </div>
      )}

      <dl className="mt-5 grid grid-cols-2 gap-x-8 gap-y-4 sm:grid-cols-4">
        <Row label="Starting balance" value={`${p.starting_balance} ${p.account_currency}`} />
        <Row label="Profit target" value={p.profit_target_pct ? `${p.profit_target_pct}%` : "—"} />
        <Row label="Max daily loss" value={`${p.max_daily_loss_pct}%`} />
        <Row label="Max total loss" value={`${p.max_total_loss_pct}%`} />
        <Row label="Daily reset" value={`${p.reset_time} ${p.reset_timezone}`} />
        <Row label="Min trading days" value={String(p.min_trading_days)} />
        <Row
          label="Inactivity limit"
          value={p.inactivity_days ? `${p.inactivity_days} days` : "none"}
        />
        <Row label="Buffer warning at" value={`${p.buffer_warning_pct}% of allowance`} />
      </dl>

      <div className="mt-6">
        <h4 className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
          How the limits are measured
        </h4>
        <p className="mt-1.5 text-[12px] text-[var(--text-secondary)]">
          These decide whether an account survives. The headline percentages rarely do.
        </p>
        <div className="mt-3 flex flex-col gap-3">
          {p.definitions.map((d) => (
            <div
              key={d.field_name}
              className="rounded-sm border border-[var(--border-subtle)] p-3"
            >
              <div className="flex flex-wrap items-baseline gap-x-3">
                <span className="text-[12px] text-[var(--text-secondary)]">
                  {d.field_name.replace(/_/g, " ")}
                </span>
                <span
                  className="text-[12.5px]"
                  style={{ color: d.stricter_option ? "var(--caution)" : "var(--text-primary)" }}
                >
                  {d.value}
                </span>
                {d.stricter_option && (
                  <span className="text-[10px] uppercase tracking-wider text-[var(--caution)]">
                    stricter of the two
                  </span>
                )}
              </div>
              <p className="mt-1.5 text-[12.5px] leading-relaxed text-[var(--text-secondary)]">
                {d.consequence}
              </p>
            </div>
          ))}
        </div>
      </div>

      <div className="mt-6">
        <h4 className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
          Restrictions
        </h4>
        <ul className="mt-2 flex flex-wrap gap-x-6 gap-y-1 text-[12.5px] text-[var(--text-secondary)]">
          <li>Weekend holding {p.weekend_holding_allowed ? "allowed" : "NOT allowed"}</li>
          <li>Overnight holding {p.overnight_holding_allowed ? "allowed" : "NOT allowed"}</li>
          <li>
            News trading{" "}
            {p.news_trading_restricted
              ? `restricted (${p.news_buffer_minutes} min buffer)`
              : "unrestricted"}
          </li>
          <li>Automated systems {p.ea_allowed ? "allowed" : "NOT allowed"}</li>
          <li>
            Consistency rule{" "}
            {p.consistency_rule_enabled
              ? `on (max ${p.max_single_day_profit_pct_of_total}% of profit in one day)`
              : "off"}
          </li>
        </ul>
      </div>

      {p.notes && (
        <p className="mt-5 text-[12px] text-[var(--text-tertiary)]">{p.notes}</p>
      )}
    </div>
  );
}

export default function PropFirm() {
  const [profiles, setProfiles] = useState<PropFirmProfile[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const p = await api.propFirm();
        if (cancelled) return;
        setProfiles(p);
        setError(null);
      } catch (e) {
        if (cancelled) return;
        setProfiles(null);
        setError(e instanceof ApiError ? e.message : "Prop-firm profiles are unavailable.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const unverified = (profiles ?? []).filter((p) => !p.verification.is_verified).length;

  return (
    <div className="mx-auto max-w-5xl">
      <header className="mb-8">
        <h1 className="text-[20px] font-medium tracking-tight">Prop Firm</h1>
        <p className="mt-1.5 text-[13px] text-[var(--text-secondary)]">
          The rules an evaluation is actually judged against, and how they are measured.
        </p>
      </header>

      {loading && (
        <p className="text-[13px] text-[var(--text-secondary)]">Reading rule profiles…</p>
      )}

      {error && (
        <div className="rounded-sm border border-[var(--negative)] bg-[var(--surface-2)] p-5">
          <p className="text-[13px] text-[var(--negative)]">{error}</p>
        </div>
      )}

      {profiles && (
        <>
          {unverified > 0 && (
            <div className="mb-8 rounded-sm border border-[var(--negative)] bg-[var(--surface-2)] p-5">
              <p className="text-[11px] uppercase tracking-[0.14em] text-[var(--negative)]">
                No real firm&rsquo;s rules are bundled
              </p>
              <p className="mt-2 text-[13px] leading-relaxed">
                {unverified} of {profiles.length} profile{profiles.length === 1 ? "" : "s"}{" "}
                {unverified === 1 ? "is" : "are"} an invented example. Replace every value
                with the firm&rsquo;s published terms, and record who verified them and
                when, before running an evaluation against this.
              </p>
            </div>
          )}

          <div className="flex flex-col gap-5">
            {profiles.map((p) => (
              <ProfileCard key={p.profile_id} profile={p} />
            ))}
          </div>
        </>
      )}
    </div>
  );
}
