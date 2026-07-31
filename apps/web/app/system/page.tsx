"use client";

import { useEffect, useState } from "react";
import {
  api,
  ApiError,
  type AuditStatus,
  type SystemConfig,
  type Versions,
} from "@/lib/api";

/**
 * System — versions, configuration and the integrity of the record.
 *
 * Provider credentials are reported as configured or absent, never by value.
 * Knowing whether a key is set is operationally necessary; knowing what it is
 * never is, and a dashboard is exactly the wrong place for it to leak.
 *
 * The audit chain leads. A hash-chained log nobody verifies is a log nobody has
 * reason to trust, and a broken chain means the record has been altered — which
 * outranks every other fact on this page.
 */

function Row({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-6 border-b border-[var(--border-subtle)] py-2 last:border-0">
      <span className="text-[12.5px] text-[var(--text-secondary)]">{label}</span>
      <span className="tabular text-[12.5px]" style={tone ? { color: tone } : undefined}>
        {value}
      </span>
    </div>
  );
}

export default function System() {
  const [versions, setVersions] = useState<Versions | null>(null);
  const [config, setConfig] = useState<SystemConfig | null>(null);
  const [audit, setAudit] = useState<AuditStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [v, c, a] = await Promise.all([
          api.versions(),
          api.systemConfig(),
          api.auditStatus(),
        ]);
        if (cancelled) return;
        setVersions(v);
        setConfig(c);
        setAudit(a);
        setError(null);
      } catch (e) {
        if (cancelled) return;
        setVersions(null);
        setConfig(null);
        setAudit(null);
        setError(e instanceof ApiError ? e.message : "System state is unavailable.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="mx-auto max-w-4xl">
      <header className="mb-8">
        <h1 className="text-[20px] font-medium tracking-tight">System</h1>
        <p className="mt-1.5 text-[13px] text-[var(--text-secondary)]">
          What is running, how it is configured, and whether the record is intact.
        </p>
      </header>

      {loading && <p className="text-[13px] text-[var(--text-secondary)]">Reading state…</p>}

      {error && (
        <div className="rounded-sm border border-[var(--negative)] bg-[var(--surface-2)] p-5">
          <p className="text-[13px] text-[var(--negative)]">{error}</p>
        </div>
      )}

      {/* Above everything: a broken chain means the record has been altered. */}
      {audit && (
        <section className="mb-8">
          <div
            className="rounded-sm border bg-[var(--surface-1)] p-5"
            style={{
              borderColor: audit.valid ? "var(--border-subtle)" : "var(--negative)",
            }}
          >
            <div className="flex flex-wrap items-baseline justify-between gap-3">
              <span className="text-[11px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
                Audit chain
              </span>
              <span
                className="text-[11px] uppercase tracking-wider"
                style={{ color: audit.valid ? "var(--positive)" : "var(--negative)" }}
              >
                {audit.valid ? "verified" : "BROKEN"}
              </span>
            </div>
            <p className="tabular mt-3 text-[13px]">
              {audit.events.toLocaleString()} events · head {audit.head.slice(0, 20)}…
            </p>
            {!audit.valid && (
              <p className="mt-2 text-[12.5px] text-[var(--negative)]">
                {audit.broken_at ? `Broke at ${audit.broken_at}. ` : ""}
                {audit.detail}
              </p>
            )}
            <p className="mt-3 text-[12px] leading-relaxed text-[var(--text-tertiary)]">
              {audit.notice}
            </p>
          </div>
        </section>
      )}

      {versions && (
        <section className="mb-8">
          <h2 className="mb-3 text-[11px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
            Versions
          </h2>
          <div className="rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] px-5 py-1">
            <Row label="Product" value={`${versions.product} ${versions.version}`} />
            <Row label="Environment" value={versions.environment} />
            <Row label="Feature pipeline" value={versions.feature_pipeline} />
            <Row label="Risk profiles" value={versions.risk_profiles} />
            <Row label="Manifest schema" value={versions.manifest_schema} />
          </div>
          <p className="mt-3 text-[12px] text-[var(--text-tertiary)]">
            A backtest recorded under different values is a different experiment. These
            are part of every reproducibility manifest.
          </p>
        </section>
      )}

      {config && (
        <>
          <section className="mb-8">
            <h2 className="mb-3 text-[11px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
              Configuration
            </h2>
            <div className="rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] px-5 py-1">
              <Row label="Mode" value={config.mode} />
              <Row label="Approval mode" value={config.approval_mode.replace(/_/g, " ")} />
              <Row label="Risk profile" value={config.risk_profile} />
              <Row
                label="Broker execution"
                value={config.broker_execution_enabled ? "ENABLED" : "disabled"}
                tone={
                  config.broker_execution_enabled ? "var(--negative)" : "var(--positive)"
                }
              />
              <Row label="Storage" value={config.storage_backend} />
              <Row label="Market data" value={config.market_data_provider} />
              <Row
                label="Vault sync"
                value={config.vault_sync_enabled ? "enabled" : "disabled"}
              />
              <Row
                label="Local model"
                value={config.ollama_enabled ? config.ollama_model : "disabled"}
              />
            </div>
          </section>

          <section>
            <h2 className="mb-3 text-[11px] uppercase tracking-[0.14em] text-[var(--text-tertiary)]">
              Model providers
            </h2>
            <div className="rounded-sm border border-[var(--border-subtle)] bg-[var(--surface-1)] px-5 py-1">
              {Object.entries(config.providers).map(([name, state]) => (
                <Row
                  key={name}
                  label={name}
                  value={state}
                  tone={
                    state === "configured" || state === "enabled"
                      ? "var(--positive)"
                      : "var(--text-tertiary)"
                  }
                />
              ))}
            </div>
            <p className="mt-3 text-[12px] leading-relaxed text-[var(--text-tertiary)]">
              Presence only. No credential value is readable from this API — knowing a key
              is set is operationally necessary, knowing what it is never is.
            </p>
          </section>
        </>
      )}
    </div>
  );
}
