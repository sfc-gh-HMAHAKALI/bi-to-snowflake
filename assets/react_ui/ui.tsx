/**
 * Page chrome for a composed React report: KPI cards, section headers, the
 * provenance block and the empty-state guard.
 *
 * This file exists to close a gap. `assets/streamlit_ui/ui.py` ships `kpi_card`,
 * `kpi_row`, `section`, `provenance` and `guard`; the React side shipped none of
 * them, so a composed React page had to hand-build its KPI band, its headings and
 * its provenance page -- which collides head-on with the rule that a generated page
 * never writes chart or chrome code. The two surfaces are meant to be
 * indistinguishable to a viewer, and they cannot be if one of them improvises.
 *
 * The API deliberately mirrors `ui.py` name for name and argument for argument, so
 * a page composed for one surface reads the same as the page composed for the
 * other. Colours come from `theme.ts` only; nothing here introduces a hex.
 */
"use client";

import type { ReactNode } from "react";
import { SF, NEUTRAL, STATUS } from "./theme";

/** A section heading with an optional subtitle. Mirrors ui.py's `section`. */
export function Section({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div className="mb-2 mt-6">
      <h2 className="text-[15px] font-semibold" style={{ color: SF.star }}>
        {title}
      </h2>
      {subtitle ? (
        <p className="mt-0.5 text-[11px] leading-relaxed" style={{ color: NEUTRAL.muted }}>
          {subtitle}
        </p>
      ) : null}
    </div>
  );
}

/** Inline SVG sparkline, drawn the same way `charts.tsx` draws its own. */
function Spark({ values, colour }: { values: number[]; colour: string }) {
  const pts = values.filter((v) => Number.isFinite(v));
  if (pts.length < 2) return null;
  const lo = Math.min(...pts);
  const hi = Math.max(...pts);
  const span = hi - lo || 1;
  const w = 104;
  const h = 24;
  const step = w / (pts.length - 1);
  const coords = pts
    .map((v, i) => `${(i * step).toFixed(1)},${(h - ((v - lo) / span) * (h - 4) - 2).toFixed(1)}`)
    .join(" ");
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none"
         className="mt-1 block">
      <polyline points={coords} fill="none" stroke={colour} strokeWidth={1.6}
                strokeLinejoin="round" />
    </svg>
  );
}

export type KpiProps = {
  label: string;
  /** Pre-formatted. Formatting belongs to the page's metrics layer, not here. */
  value: string;
  delta?: string;
  /** Whether a positive delta is good. Drives the delta colour, nothing else. */
  higherIsBetter?: boolean;
  trend?: number[];
  /** 0..1. Draws a progress rule under the value. */
  attainment?: number;
  /** What the figure covers, e.g. "FY2026 to date". */
  scope?: string;
  /** Emphasis tile. One per band at most: everything emphasised is nothing emphasised. */
  accent?: boolean;
};

/** One KPI tile. Mirrors ui.py's `kpi_card`. */
export function KpiCard({
  label, value, delta, higherIsBetter = true, trend, attainment, scope, accent = false,
}: KpiProps) {
  const up = delta?.trim().startsWith("-") === false && delta?.trim() !== "";
  const deltaColour = delta
    ? (up === higherIsBetter ? STATUS.good : STATUS.bad)
    : NEUTRAL.muted;
  return (
    <div className="flex-1 rounded-md p-3"
         style={{
           background: NEUTRAL.surface,
           border: `1px solid ${NEUTRAL.line}`,
           borderTop: `2px solid ${accent ? SF.accent : SF.blue}`,
         }}>
      <div className="text-[11px] uppercase tracking-wide" style={{ color: NEUTRAL.muted }}>
        {label}
      </div>
      <div className="mt-1 text-[22px] font-semibold leading-none"
           style={{ color: NEUTRAL.ink }}>
        {value}
      </div>
      {delta ? (
        <div className="mt-1 text-[12px] font-medium" style={{ color: deltaColour }}>
          {delta}
        </div>
      ) : null}
      {typeof attainment === "number" ? (
        <svg width="100%" height="4" className="mt-1.5 block">
          <rect width="100%" height="4" rx="2" fill={NEUTRAL.line} />
          <rect width={`${Math.max(0, Math.min(1, attainment)) * 100}%`} height="4" rx="2"
                fill={attainment >= 1 ? STATUS.good : SF.blue} />
        </svg>
      ) : null}
      {trend?.length ? <Spark values={trend} colour={accent ? SF.accent : SF.blue} /> : null}
      {scope ? (
        <div className="mt-1 text-[10px]" style={{ color: NEUTRAL.muted }}>{scope}</div>
      ) : null}
    </div>
  );
}

/** The KPI band. Mirrors ui.py's `kpi_row`. Four tiles is the readable maximum. */
export function KpiRow({ cards }: { cards: KpiProps[] }) {
  return (
    <div className="flex flex-wrap gap-3">
      {cards.map((c) => <KpiCard key={c.label} {...c} />)}
    </div>
  );
}

/**
 * Empty-state guard. Mirrors ui.py's `guard`.
 *
 * Returns the children when there is something to draw, and an explicit empty state
 * when there is not. The empty state is the one a demo actually hits -- filters that
 * exclude everything -- so it says what happened and offers the way out.
 */
export function Guard({
  rows, what = "data", onClear, children,
}: {
  rows: number;
  what?: string;
  onClear?: () => void;
  children: ReactNode;
}) {
  if (rows > 0) return <>{children}</>;
  return (
    <div className="py-8 text-sm" style={{ color: NEUTRAL.muted }}>
      No {what} for the current filters.
      {onClear ? (
        <button onClick={onClear} className="ml-2 underline" style={{ color: SF.blue }}>
          Clear filters
        </button>
      ) : null}
    </div>
  );
}

/**
 * Provenance block. Mirrors ui.py's `provenance`.
 *
 * Every figure on a page should be traceable to the thing that produced it. Where a
 * page shows a number that was not measured through the semantic view, it has to say
 * so where the number appears -- silence reads as a claim.
 */
export function Provenance({
  rows, elapsedMs, source, warehouse, semanticView, note, shownRows,
}: {
  rows: number;
  elapsedMs: number;
  source: string;
  warehouse: string;
  semanticView: string;
  note?: string;
  shownRows?: number;
}) {
  const counted = typeof shownRows === "number" && shownRows !== rows
    ? `${shownRows.toLocaleString()} of ${rows.toLocaleString()} rows`
    : `${rows.toLocaleString()} rows`;
  return (
    <div className="mt-6 rounded-md p-3 text-[11px] leading-relaxed"
         style={{ background: NEUTRAL.surface, border: `1px solid ${NEUTRAL.line}`,
                  color: NEUTRAL.muted }}>
      <div><span style={{ color: NEUTRAL.ink }}>Source</span> {source}</div>
      <div><span style={{ color: NEUTRAL.ink }}>Semantic view</span> {semanticView}</div>
      <div><span style={{ color: NEUTRAL.ink }}>Warehouse</span> {warehouse}</div>
      <div>
        <span style={{ color: NEUTRAL.ink }}>Read</span> {counted} in{" "}
        {(elapsedMs / 1000).toFixed(2)}s
      </div>
      {note ? <div className="mt-1">{note}</div> : null}
    </div>
  );
}
