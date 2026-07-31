"use client";

import { useEffect, useState } from "react";
import { api, ApiError, type NoteDetail, type NoteSummary, type VaultStatus } from "@/lib/api";

/**
 * Journal — the research vault.
 *
 * Read-only here on purpose. The vault holds the operator's *interpretation* of
 * what happened; the record of what happened lives in the database. Editing
 * happens in Obsidian, against the files, which is where an operator actually
 * writes — and where an edit cannot reach a balance, an order or an audit entry.
 *
 * Machine-written frontmatter and operator-written fields are shown separately
 * rather than merged. A note is two documents sharing a file, and blurring them
 * is how generated values start looking like observations.
 */

function money(value: string) {
  const n = Number(value);
  if (!Number.isFinite(n)) return value || "—";
  return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export default function Journal() {
  const [status, setStatus] = useState<VaultStatus | null>(null);
  const [notes, setNotes] = useState<NoteSummary[]>([]);
  const [selected, setSelected] = useState<NoteDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [s, n] = await Promise.all([api.journalStatus(), api.journal()]);
        if (cancelled) return;
        setStatus(s);
        setNotes(n);
        setError(null);
      } catch (e) {
        if (cancelled) return;
        setStatus(null);
        setNotes([]);
        setError(e instanceof ApiError ? e.message : "The vault is unavailable.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function open(note: NoteSummary) {
    try {
      setSelected(await api.note(note.folder, note.filename));
      setError(null);
    } catch (e) {
      setSelected(null);
      setError(e instanceof ApiError ? e.message : "That note could not be read.");
    }
  }

  const written = notes.filter((n) => n.has_notes).length;

  return (
    <div className="mx-auto max-w-6xl">
      <header className="mb-8">
        <h1 className="text-[20px] font-medium tracking-tight">Journal</h1>
        <p className="mt-1.5 text-[13px] text-[var(--text-secondary)]">
          One note per trade, written to the vault as it closes.
        </p>
      </header>

      {loading && <p className="text-[13px] text-[var(--text-secondary)]">Reading the vault…</p>}

      {error && (
        <div className="mb-6 rounded-sm border border-[var(--negative)] bg-[var(--surface-2)] p-5">
          <p className="text-[13px] text-[var(--negative)]">{error}</p>
        </div>
      )}

      {status && (
        <section className="mb-8">
          <div className="rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] p-5">
            <div className="flex flex-wrap gap-x-10 gap-y-3">
              <div>
                <dt className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
                  Notes
                </dt>
                <dd className="tabular mt-1 text-[16px]">{status.note_count}</dd>
              </div>
              <div>
                <dt className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
                  With your notes
                </dt>
                <dd className="tabular mt-1 text-[16px]">{written}</dd>
              </div>
              <div>
                <dt className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
                  Sync
                </dt>
                <dd className="mt-1 text-[13px]">
                  {status.sync_enabled ? "enabled" : "disabled"}
                </dd>
              </div>
            </div>
            <p className="mt-4 break-all text-[11.5px] text-[var(--text-tertiary)]">
              {status.path}
            </p>
            <p className="mt-3 text-[12px] leading-relaxed text-[var(--text-secondary)]">
              {status.notice}
            </p>
          </div>
        </section>
      )}

      {notes.length === 0 && !loading && !error && (
        <div className="rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] p-5">
          <p className="text-[13px]">No notes yet.</p>
          <p className="mt-2 text-[12.5px] text-[var(--text-secondary)]">
            Notes are written as trades close. Run a paper session to produce some.
          </p>
        </div>
      )}

      {notes.length > 0 && (
        <div className="grid gap-6 lg:grid-cols-[1fr_1.2fr]">
          <section>
            <h2 className="mb-3 text-[11px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
              Notes
            </h2>
            <div className="max-h-[560px] overflow-y-auto rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)]">
              {notes.map((n) => (
                <button
                  key={n.filename}
                  onClick={() => open(n)}
                  className={`flex w-full items-baseline justify-between gap-3 border-b border-[var(--border-subtle)] px-4 py-2.5 text-left last:border-0 hover:bg-[var(--surface-2)] ${
                    selected?.filename === n.filename ? "bg-[var(--surface-2)]" : ""
                  }`}
                >
                  <span className="text-[12.5px]">
                    {n.instrument}{" "}
                    <span
                      style={{
                        color:
                          n.direction === "long" ? "var(--positive)" : "var(--negative)",
                      }}
                    >
                      {n.direction}
                    </span>
                    {n.has_notes && (
                      <span className="ml-2 text-[10px] uppercase tracking-wider text-[var(--text-tertiary)]">
                        annotated
                      </span>
                    )}
                  </span>
                  <span
                    className="tabular text-[12px]"
                    style={{
                      color: Number(n.pnl) < 0 ? "var(--negative)" : "var(--positive)",
                    }}
                  >
                    {money(n.pnl)}
                  </span>
                </button>
              ))}
            </div>
          </section>

          <section>
            <h2 className="mb-3 text-[11px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
              {selected ? selected.filename : "Select a note"}
            </h2>
            <div className="rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] p-5">
              {!selected ? (
                <p className="text-[13px] text-[var(--text-secondary)]">
                  Choose a note to read it.
                </p>
              ) : (
                <>
                  {selected.synthetic && (
                    // A simulated trade must never read as real performance.
                    <p className="mb-4 rounded-sm border border-[var(--caution)] px-3 py-2 text-[12px] text-[var(--caution)]">
                      Simulated. This trade did not happen in a live market.
                    </p>
                  )}

                  <h3 className="text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
                    Recorded by the system
                  </h3>
                  <dl className="mt-2 grid grid-cols-2 gap-x-6 gap-y-1.5">
                    {Object.entries(selected.user_fields)
                      .filter(([, v]) => v && v !== "null")
                      .slice(0, 14)
                      .map(([k, v]) => (
                        <div key={k} className="flex justify-between gap-3">
                          <dt className="text-[11.5px] text-[var(--text-tertiary)]">
                            {k.replace(/_/g, " ")}
                          </dt>
                          <dd className="tabular text-[11.5px]">{v}</dd>
                        </div>
                      ))}
                  </dl>

                  <h3 className="mt-5 text-[10px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
                    Note
                  </h3>
                  <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap text-[12px] leading-relaxed text-[var(--text-secondary)]">
                    {selected.body}
                  </pre>

                  <p className="mt-4 text-[11.5px] text-[var(--text-tertiary)]">
                    Edit this note in Obsidian. Only the fields under Review travel back,
                    and they never reach an account, an order or the audit log.
                  </p>
                </>
              )}
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
