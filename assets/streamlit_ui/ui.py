"""KPI cards, empty states and the provenance strip."""

from __future__ import annotations

from typing import Any, Callable, Sequence

import pandas as pd
import streamlit as st

# Package-relative. In the app this was lifted from, `metrics` was a top-level module
# sitting beside the UI package, so the import had to be absolute; packaging it here as
# `streamlit_ui.metrics` makes the relative form the correct one. A scaffolded app must
# therefore import the library as a package (`from streamlit_ui import ...`) rather than
# flattening its modules into the app root, or these imports break.
from . import compat, metrics

# Status colours are deliberately NOT drawn from the Snowflake palette: a category
# should never be painted the same red the app uses to mean "missing target", and a
# brand colour pressed into service as a status colour stops being either. These pairs
# are contrast-checked against a light background.
GOOD, WARN, BAD = "#1B7F3B", "#B45309", "#B4232C"
MUTED = "#5A6672"

# Snowflake brand palette. Shared with pages_impl.SERIES and with both HTML documents
# so that one figure reads identically on a slide and in the running app -- the
# perceived-quality jump from that consistency is larger than any individual chart.
SF_BLUE = "#29B5E8"
SF_STAR = "#11567F"
SF_TEAL = "#71D3DC"
SF_PURPLE = "#7D44CF"
SF_MIDNIGHT = "#0B1E2D"
# Valencia Orange is reserved for emphasis -- the one mark we want someone to look at.
# It is deliberately absent from the categorical series: used for both, "highlighted"
# and "is the fifth category" become indistinguishable.
SF_ACCENT = "#FF9F36"


def _sparkline(values: Sequence[float], colour: str = SF_BLUE) -> str:
    """Inline SVG sparkline. SVG rather than a chart object because this sits inside
    an HTML card, and embedding a Plotly figure per tile costs far more than the
    trend line is worth."""
    pts = [v for v in values if v is not None and not pd.isna(v)]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0
    w, h = 104, 24
    step = w / (len(pts) - 1)
    coords = " ".join(
        f"{i*step:.1f},{h - ((v - lo) / span) * (h - 4) - 2:.1f}"
        for i, v in enumerate(pts)
    )
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'preserveAspectRatio="none" style="display:block">'
            f'<polyline points="{coords}" fill="none" stroke="{colour}" '
            f'stroke-width="1.6" stroke-linejoin="round"/></svg>')


def _progress(fraction: float, colour: str) -> str:
    f = max(0.0, min(1.0, fraction))
    return (f'<svg width="100%" height="4" style="display:block;margin-top:6px">'
            f'<rect width="100%" height="4" rx="2" fill="#E3E7EB"/>'
            f'<rect width="{f*100:.1f}%" height="4" rx="2" fill="{colour}"/></svg>')


def section(title: str, subtitle: str | None = None) -> None:
    """Chart heading with a Snowflake-blue rule.

    Rendered as markup rather than as a Plotly title: with both a Plotly title and a
    legend competing for the space above the plot they overprint on the same baseline,
    which on the deployed app showed as garbled overstruck text.
    """
    sub = f'<div class="bim-sec-sub">{subtitle}</div>' if subtitle else ""
    st.markdown(
        f'<div class="bim-sec"><div class="bim-sec-t">{title}</div>{sub}</div>',
        unsafe_allow_html=True,
    )


def kpi_card(label: str, value: str, *, delta: str | None = None,
             higher_is_better: bool = True, trend: Sequence[float] | None = None,
             attainment: float | None = None, scope: str | None = None,
             accent: bool = False) -> str:
    """One KPI as HTML.

    `scope` renders the filter context under the label. Without it, two tiles can
    legitimately show the same number -- "Sales" filtered to the current fiscal year
    equals "Fiscal YTD" -- and look like a bug. Stating the scope is cheaper than
    explaining the coincidence live.
    """
    colour = MUTED
    if delta:
        rising = delta.strip().startswith("+")
        colour = GOOD if rising == higher_is_better else BAD

    arrow = ""
    if delta:
        arrow = "&#9650;" if delta.strip().startswith("+") else "&#9660;"

    parts = [
        f'<div class="bim-kpi{" bim-kpi-accent" if accent else ""}">',
        f'<div class="bim-kpi-label">{label}</div>',
    ]
    if scope:
        parts.append(f'<div class="bim-kpi-scope">{scope}</div>')
    parts.append(f'<div class="bim-kpi-value">{value}</div>')
    if delta:
        parts.append(f'<div class="bim-kpi-delta" style="color:{colour}">'
                     f'{arrow} {delta}</div>')
    if trend:
        parts.append(_sparkline(trend, colour if delta else SF_BLUE))
    if attainment is not None:
        parts.append(_progress(attainment,
                               GOOD if attainment >= 1 else
                               WARN if attainment >= 0.9 else BAD))
    parts.append("</div>")
    return "".join(parts)


def kpi_row(cards: list[str]) -> None:
    """Render KPI cards four to a row.

    Four, not seven: seven equal columns squeeze values until they clip at a 900px
    viewport with the sidebar open.
    """
    for start in range(0, len(cards), 4):
        chunk = cards[start:start + 4]
        cols = compat.columns(len(chunk))
        for col, card in zip(cols, chunk):
            with col:
                st.markdown(card, unsafe_allow_html=True)


def guard(df: pd.DataFrame | None, *, on_clear: Callable[[], None] | None = None,
          what: str = "data") -> bool:
    """Return True when there is nothing to render, having said so.

    Every render function opens with this. A filter combination that excludes
    everything must explain itself and offer a way out; a blank panel reads as a
    broken app, and a raw traceback reads worse.
    """
    if df is not None and not df.empty:
        return False
    compat.info(f"No {what} matches the current filters.")
    if on_clear and st.button("Clear filters", key=f"clear_{what}"):
        on_clear()
        compat.rerun()
    return True


def provenance(*, rows: int, elapsed_ms: int, source: str, warehouse: str,
               semantic_view: str, note: str | None = None,
               shown_rows: int | None = None) -> None:
    """Where the numbers came from.

    An enterprise viewer will not act on a figure they cannot trace, and "which table
    is this?" asked live is a question worth pre-empting.

    `shown_rows` distinguishes what was fetched from what is on screen. Cross-filtering
    happens in memory, so the query row count does not move when a filter is applied --
    reporting only the fetched count next to a filtered chart invites the reasonable
    conclusion that the filter did nothing.
    """
    fetched = (f"{rows:,} rows fetched in {elapsed_ms:,} ms"
               if shown_rows is None or shown_rows == rows
               else f"{shown_rows:,} of {rows:,} rows shown, fetched in {elapsed_ms:,} ms")
    bits = [
        fetched,
        f"source {source}",
        f"semantic view {semantic_view}",
        f"warehouse {warehouse}",
    ]
    st.markdown(
        f'<div class="bim-prov">{" &middot; ".join(bits)}</div>',
        unsafe_allow_html=True,
    )
    if note:
        compat.warning(note)


CSS = """
<style>
.bim-kpi {
  border: 1px solid rgba(128,140,150,.28);
  border-radius: 10px; padding: 12px 14px 14px;
  background: var(--secondary-background-color, #F7F9FA);
  height: 100%; position: relative; overflow: hidden;
}
/* Snowflake-blue capline. Three pixels of brand per tile is enough to tie the app to
   the deck without turning a KPI into a logo. */
.bim-kpi::before {
  content: ""; position: absolute; inset: 0 0 auto 0; height: 3px;
  background: linear-gradient(90deg, #29B5E8, #71D3DC);
}
.bim-kpi.bim-kpi-accent::before {
  background: linear-gradient(90deg, #7D44CF, #29B5E8);
}
.bim-sec { margin: 6px 0 2px; }
.bim-sec-t {
  font-size: .95rem; font-weight: 680; color: #11567F;
  display: flex; align-items: center; gap: 9px;
}
.bim-sec-t::after {
  content: ""; flex: 1; height: 2px;
  background: linear-gradient(90deg, rgba(41,181,232,.45), transparent);
}
.bim-sec-sub { font-size: .76rem; opacity: .62; margin-top: 1px; }
@media (prefers-color-scheme: dark) {
  .bim-sec-t { color: #71D3DC; }
}
.bim-kpi-label {
  font-size: .72rem; font-weight: 700; letter-spacing: .04em;
  text-transform: uppercase; opacity: .72;
  overflow-wrap: break-word; word-break: break-word;
}
.bim-kpi-scope { font-size: .68rem; opacity: .55; margin-top: 1px; }
.bim-kpi-value {
  font-size: 1.72rem; font-weight: 650; line-height: 1.15; margin-top: 4px;
}
.bim-kpi-delta { font-size: .78rem; font-weight: 600; margin-top: 2px; }
.bim-prov {
  font-size: .74rem; opacity: .68; padding: 6px 2px 10px;
  border-bottom: 1px solid rgba(128,140,150,.22); margin-bottom: 14px;
}
</style>
"""
