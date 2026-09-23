"""Cross-filter state with exclude-self semantics.

The single behaviour that separates a BI tool from a static report: click a bar and
everything else on the page narrows to it.

The subtlety is in what the clicked chart itself shows afterwards. Apply the new
filter to every panel including the one that emitted it, and the Territory chart
collapses to the one bar you just clicked -- so there is nothing left to click, no
visible indication of what the other territories looked like, and no way back except
a reset. Exclude-self means a chart never filters itself: it keeps all its bars,
stays clickable, and lets you move between territories directly while the rest of the
page follows.

Panels that emit nothing (KPI tiles, detail grids) consume every filter, because for
them there is no self to exclude.
"""

from __future__ import annotations

from typing import Any, Iterable

import pandas as pd
import streamlit as st

from . import compat

_KEY = "_filters"
_FP = "_filter_fingerprints"


def _state() -> dict[str, Any]:
    if _KEY not in st.session_state:
        st.session_state[_KEY] = {}
    return st.session_state[_KEY]


def hydrate_from_url(allowed: Iterable[str]) -> None:
    """Restore filters from the URL so a filtered view is shareable.

    Only recognised dimensions are accepted: a stale or hand-edited link should
    produce a slightly wrong page, not a KeyError.
    """
    if _KEY in st.session_state:
        return
    allowed = set(allowed)
    params = compat.get_query_params()
    restored = {
        k[2:]: [p for p in str(v).split("|") if p]
        for k, v in params.items()
        if k.startswith("f_") and k[2:] in allowed
    }
    st.session_state[_KEY] = {k: v for k, v in restored.items() if v}


def sync_to_url(page: str, **extra: Any) -> None:
    """Mirror page, filters and drill depth into the URL. Local runs only.

    Deliberately a no-op inside Snowflake. A deployed app runs in a Snowsight iframe
    whose URL Snowsight owns; writing st.query_params from inside it fought the host, and
    the observable effect was that every button stopped working -- Drill down, Reset all
    filters and Ask all looked dead, because the state each one set was discarded before
    the next render. Text input still committed, which is exactly what made it read as a
    click-handling fault rather than a URL one.

    Nothing is lost by skipping it: a Snowsight app URL cannot carry app query parameters
    anyway, so shareable-filter links only ever applied to local runs.
    """
    if compat.IN_SNOWSIGHT:
        return
    compat.set_query_params(
        page=page,
        **{f"f_{dim}": "|".join(map(str, vals))
           for dim, vals in _state().items() if vals},
        **{k: v for k, v in extra.items() if v},
    )


def active() -> dict[str, list]:
    return {k: list(v) for k, v in _state().items() if v}


def set_filter(dimension: str, values: list) -> None:
    if values:
        _state()[dimension] = list(values)
    else:
        _state().pop(dimension, None)


def toggle(dimension: str, value: Any) -> None:
    current = _state().get(dimension, [])
    _state()[dimension] = [] if value in current else [value]
    if not _state()[dimension]:
        _state().pop(dimension, None)


def clear(dimension: str | None = None) -> None:
    if dimension is None:
        st.session_state[_KEY] = {}
    else:
        _state().pop(dimension, None)


def apply(df: pd.DataFrame, *, emits: str | None = None) -> pd.DataFrame:
    """Filter a frame, skipping the requesting panel's own dimension.

    `emits` is the dimension this panel is responsible for. Pass None for KPIs and
    grids, which consume everything.
    """
    if df is None or df.empty:
        return df
    out = df
    for dim, values in _state().items():
        if dim == emits or dim not in out.columns or not values:
            continue
        out = out[out[dim].astype(str).isin([str(v) for v in values])]
    return out


def capture_selection(event: Any, key: str, dimension: str,
                      labels: list) -> bool:
    """Read a Plotly selection event and record it as a filter.

    Returns True when state changed, which the caller uses to decide whether to
    rerun.

    Fingerprinting is not an optimisation. Streamlit replays the selection event on
    every rerun, so writing state unconditionally means: write, rerun, same event
    replays, write again -- an infinite loop that presents as a permanently spinning
    app.
    """
    if event is None:
        return False
    try:
        points = event.selection["points"] if hasattr(event, "selection") \
            else event.get("selection", {}).get("points", [])
    except (AttributeError, KeyError, TypeError):
        return False
    if not points:
        return False

    picked: list = []
    for p in points:
        idx = p.get("point_index", p.get("pointIndex"))
        if idx is not None and 0 <= idx < len(labels):
            picked.append(labels[idx])
        elif p.get("x") is not None:
            picked.append(p["x"])
    if not picked:
        return False

    fingerprint = f"{dimension}:{sorted(map(str, picked))}"
    prints = st.session_state.setdefault(_FP, {})
    if prints.get(key) == fingerprint:
        return False
    prints[key] = fingerprint
    set_filter(dimension, picked)
    return True


def render_chips(labels: dict[str, str] | None = None) -> None:
    """Active filters as removable chips, plus a clear-all.

    Without a visible indication of what is filtering, a user who scrolled past a
    click sees wrong totals and no reason for them.
    """
    current = active()
    if not current:
        return
    labels = labels or {}
    st.caption("Filtered by")
    cols = compat.columns(min(len(current) + 1, 6))
    for i, (dim, vals) in enumerate(current.items()):
        shown = ", ".join(map(str, vals[:2])) + ("..." if len(vals) > 2 else "")
        with cols[i % len(cols)]:
            if st.button(f"{labels.get(dim, dim)}: {shown}  x",
                         key=f"chip_{dim}", use_container_width=True):
                clear(dim)
                compat.rerun()
    with cols[-1]:
        if st.button("Clear all", key="chip_clear_all", use_container_width=True):
            clear()
            compat.rerun()
