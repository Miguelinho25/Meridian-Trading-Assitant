"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  api,
  ApiError,
  type EquityPoint,
  type PaperPosition,
  type PaperSession,
  type PaperSessionDetail,
} from "@/lib/api";

/**
 * Live Market — paper sessions and their open exposure.
 *
 * `bar_source` is given the same prominence as the balance. A session fed
 * historical bars at speed behaves identically to one fed a live feed, so a
 * REPLAY equity curve looks exactly like paper performance. Showing the number
 * without the label would be the most misleading thing on this page.
 *
 * A position with no stop is shown in the alarm colour rather than as an empty
 * cell: unprotected exposure is a finding, not a gap in the data.
 */

function Field({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: string | undefined;
}) {
  return (
    <div>
      <dt className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
        {label}
      </dt>
      <dd className="tabular mt-1 text-[13px]" style={tone ? { color: tone } : undefined}>
        {value}
      </dd>
    </div>
  );
}

function EquityChart({ points, opening }: { points: EquityPoint[]; opening: number }) {
  const first = points[0];
  const last = points[points.length - 1];
  if (points.length < 2 || first === undefined || last === undefined) return null;

  const values = points.map((p) => Number(p.equity));
  const min = Math.min(...values, opening);
  const max = Math.max(...values, opening);
  const span = max - min || 1;
  const W = 900;
  const H = 180;

  const path = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * W;
      const y = H - ((v - min) / span) * H;
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  const openingY = H - ((opening - min) / span) * H;
  const end = values[values.length - 1] ?? opening;

  return (
    <div className="overflow-x-auto rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] p-5">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ height: 180 }}>
        {/* The opening balance, so a curve below it reads as a loss at a glance. */}
        <line
          x1="0"
          y1={openingY}
          x2={W}
          y2={openingY}
          stroke="var(--border-subtle)"
          strokeDasharray="3 3"
        />
        <path
          d={path}
          fill="none"
          stroke={end >= opening ? "var(--positive)" : "var(--negative)"}
          strokeWidth="1.5"
        />
      </svg>
      <div className="mt-2 flex justify-between text-[11px] text-[var(--text-tertiary)]">
        <span>{new Date(first.at).toLocaleDateString()}</span>
        <span className="tabular">
          {min.toLocaleString(undefined, { maximumFractionDigits: 0 })} –{" "}
          {max.toLocaleString(undefined, { maximumFractionDigits: 0 })}
        </span>
        <span>{new Date(last.at).toLocaleDateString()}</span>
      </div>
    </div>
  );
}

function PositionRow({ position: p }: { position: PaperPosition }) {
  return (
    <tr className="border-b border-[var(--border-subtle)] last:border-0">
      <td className="py-2.5 pr-6 text-[12.5px]">{p.instrument}</td>
      <td className="py-2.5 pr-6 text-[12.5px]">
        <span
          style={{
            color: p.direction === "LONG" ? "var(--positive)" : "var(--negative)",
          }}
        >
          {p.direction}
        </span>
      </td>
      <td className="tabular py-2.5 pr-6 text-[12.5px]">{p.lots}</td>
      <td className="tabular py-2.5 pr-6 text-[12.5px]">{p.entry_price}</td>
      <td className="tabular py-2.5 pr-6 text-[12.5px]">
        {p.stop_loss === null ? (
          // Unprotected exposure is a finding, not missing data.
          <span className="text-[var(--negative)]">no stop</span>
        ) : (
          p.stop_loss
        )}
      </td>
      <td className="tabular py-2.5 pr-6 text-[12.5px] text-[var(--text-secondary)]">
        {p.take_profit ?? "—"}
      </td>
      <td className="py-2.5 pr-6 text-[12px] text-[var(--text-secondary)]">{p.strategy_id}</td>
      <td className="py-2.5 text-[12px] text-[var(--text-tertiary)]">
        {new Date(p.opened_at).toLocaleDateString()}
      </td>
    </tr>
  );
}

export default function LiveMarket() {
  const [sessions, setSessions] = useState<PaperSession[] | null>(null);
  const [detail, setDetail] = useState<PaperSessionDetail | null>(null);
  const [equity, setEquity] = useState<EquityPoint[]>([]);
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
          const [d, e] = await Promise.all([
            api.paperSession(newest.id),
            api.paperEquity(newest.id),
          ]);
          if (cancelled) return;
          setDetail(d);
          setEquity(e);
        }
        setError(null);
      } catch (e) {
        if (cancelled) return;
        // Discard rather than retain: stale exposure shown as current is the
        // failure this page must not have.
        setSessions(null);
        setDetail(null);
        setEquity([]);
        setError(e instanceof ApiError ? e.message : "Paper sessions are unavailable.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const replay = detail?.bar_source === "REPLAY";

  return (
    <div className="mx-auto max-w-6xl">
      <header className="mb-8">
        <h1 className="text-[20px] font-medium tracking-tight">Live Market</h1>
        <p className="mt-1.5 text-[13px] text-[var(--text-secondary)]">
          Paper sessions, their open exposure and how they got there.
        </p>
      </header>

      {loading && (
        <p className="text-[13px] text-[var(--text-secondary)]">Reading sessions…</p>
      )}

      {error && (
        <div className="rounded-sm border border-[var(--negative)] bg-[var(--surface-2)] p-5">
          <p className="text-[13px] text-[var(--negative)]">{error}</p>
        </div>
      )}

      {sessions && sessions.length === 0 && (
        <div className="rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] p-5">
          <p className="text-[13px]">No paper session has run yet.</p>
          <p className="mt-2 text-[12.5px] text-[var(--text-secondary)]">
            Start one with{" "}
            <code className="text-[var(--text-primary)]">
              python scripts/paper_loop.py --replay --instruments EURUSD GBPUSD
            </code>
            . Sessions are started as a process, not from this page — a button that
            spawned a trading loop would put the decision to trade behind a click.
          </p>
        </div>
      )}

      {detail && (
        <>
          {/* Given the same weight as the balance, deliberately. */}
          {replay && (
            <div className="mb-8 rounded-sm border border-[var(--caution)] bg-[var(--surface-2)] p-5">
              <p className="text-[11px] uppercase tracking-[0.14em] text-[var(--caution)]">
                Replay, not live
              </p>
              <p className="mt-2 text-[13px] leading-relaxed">
                This session was fed historical bars at speed. It behaves exactly like a
                live feed, which is why the distinction is labelled — the equity below is
                not paper performance against a real market.
              </p>
            </div>
          )}

          <section className="mb-9">
            <div className="rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] p-5">
              <div className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
                <span className="text-[13px]">{detail.id}</span>
                <span className="text-[11px] uppercase tracking-wider text-[var(--text-tertiary)]">
                  {detail.status} · {detail.mode} · {detail.bar_source} ·{" "}
                  {detail.instruments.join(", ")} {detail.timeframe}
                </span>
              </div>
              <dl className="grid grid-cols-2 gap-x-8 gap-y-4 sm:grid-cols-4">
                <Field label="Balance" value={detail.balance} />
                <Field
                  label="Equity"
                  value={detail.equity}
                  tone={
                    Number(detail.equity) < Number(detail.starting_balance)
                      ? "var(--negative)"
                      : "var(--positive)"
                  }
                />
                <Field label="Opened at" value={detail.starting_balance} />
                <Field label="High water" value={detail.high_water_mark} />
                <Field label="Ticks" value={String(detail.ticks)} />
                <Field label="Closed trades" value={String(detail.closed_trade_count)} />
                <Field label="Open positions" value={String(detail.open_position_count)} />
                <Field label="Working orders" value={String(detail.working_order_count)} />
              </dl>
              {detail.halt_reason && (
                <p className="mt-5 text-[12.5px] text-[var(--caution)]">
                  Stopped: {detail.halt_reason}
                </p>
              )}
            </div>
          </section>

          <section className="mb-9">
            <h2 className="mb-3 text-[11px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
              Equity
            </h2>
            <EquityChart points={equity} opening={Number(detail.starting_balance)} />
          </section>

          <section className="mb-9">
            <h2 className="mb-3 text-[11px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
              Open positions
            </h2>
            {detail.positions.length === 0 ? (
              <div className="rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] p-5">
                <p className="text-[13px] text-[var(--text-secondary)]">Flat.</p>
              </div>
            ) : (
              <div className="overflow-x-auto rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] px-5">
                <table className="w-full min-w-[760px]">
                  <thead>
                    <tr className="border-b border-[var(--border-subtle)]">
                      {[
                        "Instrument",
                        "Side",
                        "Lots",
                        "Entry",
                        "Stop",
                        "Target",
                        "Strategy",
                        "Opened",
                      ].map((h) => (
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
                    {detail.positions.map((p) => (
                      <PositionRow key={p.position_id} position={p} />
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section>
            <h2 className="mb-3 text-[11px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
              Decision flow
            </h2>
            <div className="rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] p-5">
              <dl className="grid grid-cols-2 gap-x-8 gap-y-4 sm:grid-cols-4">
                <Field label="Signals" value={String(detail.signals_generated)} />
                <Field label="Proposals" value={String(detail.proposals_made)} />
                <Field label="Orders submitted" value={String(detail.orders_submitted)} />
                <Field
                  label="Rejected by risk"
                  value={String(detail.rejections)}
                  tone="var(--caution)"
                />
              </dl>
              <p className="mt-5 text-[12px] text-[var(--text-tertiary)]">
                <Link href="/proposals" className="underline underline-offset-4">
                  Proposals
                </Link>{" "}
                breaks the rejections down by the rule that bound — which is the only way
                to answer why a session is not trading.
              </p>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
