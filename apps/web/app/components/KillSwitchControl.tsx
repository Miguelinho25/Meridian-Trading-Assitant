"use client";

import { useState } from "react";
import { api, ApiError } from "@/lib/api";

/**
 * The kill switch, operable from the chrome.
 *
 * Reachable from every page on purpose: an operator needing to stop trading
 * should not have to navigate to a settings screen first.
 *
 * The asymmetry the API enforces is mirrored here rather than argued with.
 * Engaging is one click — moving toward safety must never be gated on a form.
 * Releasing opens a panel that requires a written reason and a second, explicit
 * confirmation, because an unexplained release is indistinguishable from an
 * accidental one.
 */

export function KillSwitchControl({
  engaged,
  indeterminate,
  fromConfiguration,
  onChanged,
}: {
  engaged: boolean;
  indeterminate: boolean;
  fromConfiguration: boolean;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function doEngage() {
    setBusy(true);
    setError(null);
    try {
      await api.engageKillSwitch("Engaged from the dashboard");
      onChanged();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not engage the kill switch.");
    } finally {
      setBusy(false);
    }
  }

  async function doRelease() {
    setBusy(true);
    setError(null);
    try {
      await api.disengageKillSwitch(reason);
      setOpen(false);
      setReason("");
      onChanged();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not release the kill switch.");
    } finally {
      setBusy(false);
    }
  }

  if (!engaged) {
    return (
      <button
        onClick={doEngage}
        disabled={busy}
        title="Stop new trades immediately. Open positions keep being managed."
        className="rounded-sm border border-[var(--negative)] px-2.5 py-1 text-[11px] uppercase tracking-wider text-[var(--negative)] hover:bg-[var(--surface-2)] disabled:opacity-50"
      >
        {busy ? "Engaging…" : "Engage kill switch"}
      </button>
    );
  }

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        // A configuration halt is a deployment decision and cannot be released
        // here; the control says so rather than failing on click.
        disabled={fromConfiguration}
        title={
          fromConfiguration
            ? "Engaged by configuration — release it there, not here"
            : indeterminate
              ? "State could not be read, so it is treated as engaged"
              : "Engaged. Releasing requires a reason."
        }
        className="rounded-sm border border-[var(--negative)] bg-[var(--surface-2)] px-2.5 py-1 text-[11px] uppercase tracking-wider text-[var(--negative)] disabled:opacity-60"
      >
        {indeterminate ? "Kill switch ENGAGED (state unknown)" : "Kill switch ENGAGED"}
      </button>

      {/* Fixed, not absolute. The risk banner is an overflow-x-auto container so
          a wide row scrolls rather than clipping figures — but that same overflow
          context clips an absolutely positioned child, which hid this panel
          entirely and left the operator with no way to release the switch.
          Fixed positioning escapes it. */}
      {open && !fromConfiguration && (
        // whitespace-normal is not decoration: this panel is a DOM child of the
        // risk banner's chrome, which sets whitespace-nowrap so the status
        // figures never wrap mid-value. Fixed positioning escapes that element's
        // *overflow* but not its inherited styles, so the explanation text ran
        // off the edge until this was set.
        <div className="fixed right-4 top-16 z-50 w-96 whitespace-normal rounded-sm border border-[var(--border-strong)] bg-[var(--surface-1)] p-4 shadow-lg">
          <p className="text-[11px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
            Release the kill switch
          </p>
          <p className="mt-2 text-[12.5px] leading-relaxed text-[var(--text-secondary)]">
            Trading resumes on the next decision. Say why — the record is what makes
            this reviewable afterwards.
          </p>
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            rows={3}
            placeholder="e.g. Feed recovered, spreads normal for 30 minutes"
            className="mt-3 w-full rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-2)] p-2 text-[12.5px] text-[var(--text-primary)]"
          />
          {error && <p className="mt-2 text-[12px] text-[var(--negative)]">{error}</p>}
          <div className="mt-3 flex items-center justify-end gap-2">
            <button
              onClick={() => {
                setOpen(false);
                setError(null);
              }}
              className="px-2.5 py-1 text-[11.5px] text-[var(--text-secondary)]"
            >
              Cancel
            </button>
            <button
              onClick={doRelease}
              // Mirrors the API's floor. Disabling rather than rejecting on
              // submit tells the operator the requirement before they commit.
              disabled={busy || reason.trim().length < 10}
              className="rounded-sm border border-[var(--border-strong)] px-2.5 py-1 text-[11.5px] disabled:opacity-40"
            >
              {busy ? "Releasing…" : "Confirm release"}
            </button>
          </div>
        </div>
      )}

      {error && !open && (
        <p className="fixed right-4 top-16 z-50 rounded-sm border border-[var(--negative)] bg-[var(--surface-1)] px-2 py-1 text-[11px] text-[var(--negative)]">
          {error}
        </p>
      )}
    </div>
  );
}
