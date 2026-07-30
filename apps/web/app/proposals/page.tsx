"use client";

import { useEffect, useState } from "react";
import { api, ApiError, type DecisionGroup, type PaperSession } from "@/lib/api";

/**
 * Proposals — every risk decision, grouped by the rule that bound.
 *
 * This page exists because a rejection *count* cannot answer the question anyone
 * actually asks of a running session: why is it not trading? The engine was
 * tallying ten thousand rejections and discarding their reasons.
 *
 * Rejections lead. On a system where 1.7% of proposals approve cleanly, the
 * approvals are the footnote and the blockers are the story.
 */

const VERDICT_TONE: Record<string, string> = {
  APPROVED: "var(--positive)",
  APPROVED_REDUCED: "var(--caution)",
  REJECTED: "var(--negative)",
};

/**
 * Plain-English readings of the codes that dominate.
 *
 * Deliberately partial: a code without an entry renders as itself rather than as
 * an invented explanation. Guessing at a rule's meaning in the UI is how an
 * operator ends up pursuing the wrong remedy.
 */
const MEANING: Record<string, string> = {
  SIZE_BELOW_MINIMUM_LOT:
    "The account cannot fund one minimum lot over this stop. Raise the balance or tighten the stop — this is not a limit binding.",
  SIZE_BELOW_MINIMUM_LOT_AFTER_CLAMP:
    "The account could fund the trade, then a limit clamped the size below one minimum lot. The limit that bound is the reason code on this row — change that, not the balance.",
  BELOW_MIN_CONFIDENCE:
    "The strategy's own confidence sat under the profile's floor. Tightened further by the drawdown throttle.",
  MAX_CORRELATED_EXPOSURE:
    "Correlated open risk was near its ceiling, so the size was cut to fit — or rejected outright if nothing tradeable was left.",
  MAX_SIMULTANEOUS_POSITIONS: "The position count was already at the profile's limit.",
  MAX_STRATEGY_BUDGET:
    "This strategy's own risk allocation was nearly spent, so the size was cut to fit — or rejected outright if nothing tradeable was left.",
  MAX_INSTRUMENT_EXPOSURE:
    "Open risk on this instrument was near its ceiling, so the size was cut to fit — or rejected outright if nothing tradeable was left.",
  DRAWDOWN_THROTTLE:
    "Size cut because drawdown had consumed part of the allowance. Recovery is convex against the account, so size falls as the hole deepens.",
  ACCOUNT_STATE_AMBIGUOUS:
    "Equity could not be valued — a position had no usable price. Trading is blocked while that persists rather than sized on a guess.",
  DAILY_LOSS_WOULD_BREACH:
    "The loss at this trade's stop would take the day past its limit.",
};

function Bar({ share, tone }: { share: number; tone: string }) {
  return (
    <div className="h-1.5 w-full rounded-sm bg-[var(--surface-2)]">
      <div
        className="h-1.5 rounded-sm"
        style={{ width: `${Math.max(share * 100, 0.5)}%`, background: tone }}
      />
    </div>
  );
}

export default function Proposals() {
  const [sessions, setSessions] = useState<PaperSession[] | null>(null);
  const [selected, setSelected] = useState<string>("");
  const [groups, setGroups] = useState<DecisionGroup[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const list = await api.paperSessions();
        if (cancelled) return;
        setSessions(list);
        const newest = list[0];
        if (newest) {
          setSelected(newest.id);
          const g = await api.paperDecisions(newest.id);
          if (cancelled) return;
          setGroups(g);
        }
        setError(null);
      } catch (e) {
        if (cancelled) return;
        setSessions(null);
        setGroups([]);
        setError(e instanceof ApiError ? e.message : "Decisions are unavailable.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function pick(id: string) {
    setSelected(id);
    try {
      setGroups(await api.paperDecisions(id));
      setError(null);
    } catch (e) {
      setGroups([]);
      setError(e instanceof ApiError ? e.message : "Decisions are unavailable.");
    }
  }

  const total = groups.reduce((n, g) => n + g.count, 0);
  const approvedClean = groups
    .filter((g) => g.verdict === "APPROVED")
    .reduce((n, g) => n + g.count, 0);
  const rejected = groups
    .filter((g) => g.verdict === "REJECTED")
    .reduce((n, g) => n + g.count, 0);

  return (
    <div className="mx-auto max-w-5xl">
      <header className="mb-8">
        <h1 className="text-[20px] font-medium tracking-tight">Proposals</h1>
        <p className="mt-1.5 text-[13px] text-[var(--text-secondary)]">
          Every risk decision, grouped by the rule that bound.
        </p>
      </header>

      {loading && (
        <p className="text-[13px] text-[var(--text-secondary)]">Reading decisions…</p>
      )}

      {error && (
        <div className="rounded-sm border border-[var(--negative)] bg-[var(--surface-2)] p-5">
          <p className="text-[13px] text-[var(--negative)]">{error}</p>
        </div>
      )}

      {sessions && sessions.length === 0 && (
        <div className="rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] p-5">
          <p className="text-[13px]">No session has produced decisions yet.</p>
        </div>
      )}

      {sessions && sessions.length > 1 && (
        <div className="mb-6 flex flex-wrap gap-2">
          {sessions.map((s) => (
            <button
              key={s.id}
              onClick={() => pick(s.id)}
              className={`rounded-sm border px-2.5 py-1 text-[11.5px] ${
                s.id === selected
                  ? "border-[var(--text-secondary)] text-[var(--text-primary)]"
                  : "border-[var(--border-subtle)] text-[var(--text-secondary)]"
              }`}
            >
              {s.id.replace("ps_", "")} · {s.bar_source} · {s.ticks} ticks
            </button>
          ))}
        </div>
      )}

      {total > 0 && (
        <>
          <section className="mb-8">
            <div className="rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] p-5">
              <div className="flex flex-wrap gap-x-10 gap-y-4">
                <div>
                  <dt className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
                    Decisions
                  </dt>
                  <dd className="tabular mt-1 text-[18px]">{total}</dd>
                </div>
                <div>
                  <dt className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
                    Approved outright
                  </dt>
                  <dd
                    className="tabular mt-1 text-[18px]"
                    style={{
                      color:
                        approvedClean / total < 0.05
                          ? "var(--caution)"
                          : "var(--positive)",
                    }}
                  >
                    {((approvedClean / total) * 100).toFixed(1)}%
                  </dd>
                </div>
                <div>
                  <dt className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
                    Rejected
                  </dt>
                  <dd className="tabular mt-1 text-[18px] text-[var(--negative)]">
                    {((rejected / total) * 100).toFixed(1)}%
                  </dd>
                </div>
              </div>
              {approvedClean / total < 0.05 && (
                <p className="mt-5 text-[12.5px] leading-relaxed text-[var(--caution)]">
                  Fewer than one proposal in twenty is approved at full size. That is the
                  risk engine working as designed, but it is worth knowing whether the
                  dominant blocker is a genuine risk judgement or a sizing artefact — the
                  two have opposite remedies.
                </p>
              )}
            </div>
          </section>

          <section>
            <h2 className="mb-3 text-[11px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
              What bound, and how often
            </h2>
            <div className="flex flex-col gap-3">
              {groups.map((g) => {
                const tone = VERDICT_TONE[g.verdict] ?? "var(--text-secondary)";
                const meaning = MEANING[g.reason_code];
                return (
                  <div
                    key={`${g.verdict}-${g.reason_code}`}
                    className="rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] p-4"
                  >
                    <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
                      <span className="text-[12.5px]">
                        {/* A clean approval has no binding rule. The absence is
                            meaningful, so it is named rather than left blank. */}
                        {g.reason_code || "no constraint bound"}
                      </span>
                      <span className="tabular text-[12px] text-[var(--text-secondary)]">
                        {g.count} · {(Number(g.share) * 100).toFixed(1)}%
                      </span>
                    </div>
                    <p
                      className="mt-1 text-[10px] uppercase tracking-wider"
                      style={{ color: tone }}
                    >
                      {g.verdict.replace(/_/g, " ")}
                    </p>
                    <div className="mt-2.5">
                      <Bar share={Number(g.share)} tone={tone} />
                    </div>
                    {meaning && (
                      <p className="mt-2.5 text-[12px] leading-relaxed text-[var(--text-secondary)]">
                        {meaning}
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
            <p className="mt-4 text-[12px] text-[var(--text-tertiary)]">
              Rejections are research data. A system that records only what it did cannot
              say what it declined, or why.
            </p>
          </section>
        </>
      )}
    </div>
  );
}
