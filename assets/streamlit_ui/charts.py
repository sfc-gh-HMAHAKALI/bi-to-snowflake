"""Chart library. Locked visual vocabulary for generated Streamlit pages.

Chart conventions follow the Enterprise Dashboard Standard, and each one exists
because its absence produced a specific visible defect:

* `graph_objects`, not `express` -- explicit control of traces and reference lines.
* y-axis headroom computed from the data, plus `cliponaxis=False`. Outside value
  labels clipping at the plot edge was the single most common visual defect.
* Long tails collapsed into "Other". More than eight bars is unreadable, and dropping
  the tail silently would make the total wrong.
* Ranked horizontal bars rather than donuts for share-of-total. Humans compare bar
  lengths far more accurately than pie angles.
* `dragmode=False` and `scrollZoom=False`. A dashboard chart that pans on drag or
  hijacks page scroll feels broken.

On the chart vocabulary. This app deliberately carries eight shapes rather than the
two it started with, and the reason is not decoration: the source Cognos model
contains no report or chart definitions at all -- Framework Manager is a modelling
layer -- so there is no fidelity argument for staying plain. Each shape below is
chosen for the question it answers, and every one of them aggregates a frame the page
has already fetched. No visual in this module issues a query.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from . import ui as bim_ui
from . import metrics
from . import compat, filters

# Series colours: the Snowflake palette, shared with both HTML documents so a figure
# reads the same on a slide and in the running app.
#
# Valencia Orange is absent on purpose. It is reserved for emphasis -- the prior-year
# marker, the Pareto cumulative curve, the one bar we want looked at -- and a colour
# used for both "look here" and "is the fifth category" means neither.
SERIES = [bim_ui.SF_BLUE,      # Snowflake Blue
          bim_ui.SF_STAR,      # Star Blue
          bim_ui.SF_TEAL,      # Teal
          bim_ui.SF_PURPLE,    # First Light purple
          "#2E8B9A",           # deep teal
          "#5A6ACF",           # indigo
          "#9DD9EF",           # pale blue
          bim_ui.SF_MIDNIGHT]  # Midnight
ACCENT = bim_ui.SF_ACCENT
GRID = "rgba(128,140,150,.20)"
MAX_CATEGORIES = 8

# Sequential ramp for the heatmap. Monotonic in lightness, which is what makes a
# heatmap readable -- a ramp that dips and rises in brightness reads as two separate
# bands of intensity rather than one scale.
SF_SCALE = [[0.0, "#F4FAFD"], [0.20, "#CDEAF6"], [0.45, "#8ED9EC"],
            [0.70, bim_ui.SF_BLUE], [1.0, bim_ui.SF_STAR]]

# Chip labels, shared so a dimension is named the same wherever it appears.
CHIP_LABELS = {
    "FISCAL_YEAR_NAME": "Fiscal year",
    "FISCAL_QUARTER_YEAR": "Fiscal quarter",
    "TERRITORY_LEVEL1": "Region",
    "TERRITORY_LEVEL2": "Territory L2",
    "TERRITORY_LEVEL3": "Territory L3",
    "TERRITORY_LEVEL4": "Territory L4",
    "GPC1": "Product group",
    "GFP_GROUP": "Product family",
    "CUSTOMER_COUNTRY": "Country",
}


def _base_layout(**overrides: Any) -> dict:
    """Layout defaults merged with per-chart overrides.

    A function rather than a module-level dict because `update_layout(**BASE,
    showlegend=False)` raises when BASE already carries `showlegend`. Merging here
    means the collision cannot be expressed.
    """
    base = {
        "template": "plotly_white",
        "margin": {"l": 8, "r": 8, "t": 28, "b": 8},
        "height": 340,
        "dragmode": False,
        "showlegend": False,
        "font": {"size": 12},
        "xaxis": {"gridcolor": GRID, "automargin": True},
        "yaxis": {"gridcolor": GRID, "automargin": True},
        "hovermode": "closest",
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


def _num(v: Any) -> float:
    """Coerce a measure value to float before this module does arithmetic on it.

    The Snowflake connector returns NUMBER columns as ``decimal.Decimal``, and
    every RPT_ view's measures are NUMBER. ``Decimal * float`` raises TypeError,
    so an axis-headroom calculation like ``peak * 1.12`` blows up at render time
    inside this library rather than in the page that forgot to cast. A composed
    app should still cast in its own data layer -- this is a floor, not a
    licence to skip that.
    """
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def collapse_tail(df: pd.DataFrame, label_col: str, value_col: str,
                  *, max_categories: int = MAX_CATEGORIES) -> pd.DataFrame:
    """Keep the top N-1 categories and aggregate the rest into "Other".

    The total is preserved deliberately: truncating to a top-N and leaving the
    remainder off the chart makes the bars disagree with the KPI above them.
    """
    if len(df) <= max_categories:
        return df.sort_values(value_col, ascending=False)
    ranked = df.sort_values(value_col, ascending=False)
    head = ranked.iloc[: max_categories - 1]
    tail = ranked.iloc[max_categories - 1:]
    other = {label_col: f"Other ({len(tail)})", value_col: tail[value_col].sum()}
    for col in df.columns:
        if col not in other:
            other[col] = tail[col].sum() if pd.api.types.is_numeric_dtype(df[col]) else ""
    return pd.concat([head, pd.DataFrame([other])], ignore_index=True)


def ranked_bar(df: pd.DataFrame, label_col: str, value_col: str, *,
               title: str, key: str, emits: str | None = None,
               subtitle: str | None = None) -> None:
    """Horizontal ranked bars with click-to-filter."""
    bim_ui.section(title, subtitle)
    frame = collapse_tail(df.groupby(label_col, as_index=False)[value_col].sum(),
                          label_col, value_col)
    if bim_ui.guard(frame, on_clear=filters.clear, what=title.lower()):
        return

    frame = frame.sort_values(value_col)
    labels = frame[label_col].astype(str).tolist()
    values = frame[value_col].tolist()
    peak = _num(max(values) if values else 0)

    active_vals = filters.active().get(emits or "", [])
    colours = [
        SERIES[0] if not active_vals or str(lab) in map(str, active_vals)
        else "rgba(41,181,232,.26)"
        for lab in labels
    ]

    fig = go.Figure(go.Bar(
        x=values, y=labels, orientation="h",
        marker_color=colours, cliponaxis=False,
        text=[metrics.format_value(value_col, v) for v in values],
        textposition="outside",
        hovertemplate="%{y}: %{text}<extra></extra>",
    ))
    fig.update_layout(**_base_layout(
        height=max(240, 34 * len(labels) + 50),
        margin={"l": 8, "r": 8, "t": 10, "b": 8},
        xaxis={"range": [0, peak * 1.18] if peak else None, "showticklabels": False},
    ))

    event = compat.plotly_chart(fig, key=key,
                               on_select="rerun" if emits else None)
    if emits and filters.capture_selection(event, key, emits, labels):
        compat.rerun()


def trend(df: pd.DataFrame, x_col: str, series: dict[str, str], *,
          title: str, key: str, sort_months: bool = True,
          subtitle: str | None = None) -> None:
    """Multi-series line chart over an ordered axis."""
    bim_ui.section(title, subtitle)
    if bim_ui.guard(df, on_clear=filters.clear, what=title.lower()):
        return
    frame = df.copy()
    if sort_months and "FISCAL_MONTH_ID" in frame.columns:
        frame = frame.sort_values("FISCAL_MONTH_ID")
    elif sort_months:
        frame = frame.assign(_k=frame[x_col].map(metrics.month_sort_key)) \
                     .sort_values("_k")

    fig = go.Figure()
    for i, (col, label) in enumerate(series.items()):
        if col not in frame.columns:
            continue
        fig.add_trace(go.Scatter(
            x=frame[x_col], y=frame[col], name=label, mode="lines+markers",
            line={"color": SERIES[i % len(SERIES)], "width": 2.2},
            marker={"size": 5},
            hovertemplate=f"{label} %{{x}}: %{{y:,.0f}}<extra></extra>",
        ))
    peak = _num(max((frame[c].max() for c in series if c in frame.columns), default=0))
    fig.update_layout(**_base_layout(
        showlegend=True,
        # Legend above the plot so it cannot clip the rotated x-axis labels below. The
        # chart title is rendered as markdown by the caller rather than by Plotly: with
        # both a Plotly title and a legend competing for the space above the plot they
        # overprinted each other on the same baseline, which on the deployed app showed
        # as garbled overstruck text.
        legend={"orientation": "h", "y": 1.02, "x": 0, "yanchor": "bottom"},
        margin={"l": 8, "r": 8, "t": 34, "b": 64},
        xaxis={"tickangle": -45, "automargin": True},
        yaxis={"range": [0, peak * 1.12] if peak else None},
    ))
    compat.plotly_chart(fig, key=key)


def grid(df: pd.DataFrame, label_col: str, *, title: str,
         show_total: bool = True, key: str | None = None) -> None:
    """Detail grid with derived column config and a correct total row.

    ``key`` is accepted for signature parity with every other function here, all
    of which require one. It is optional because this function renders no
    selectable chart, but two grids on one page still collide on the download
    button's key if they share a title -- pass ``key`` to disambiguate them.
    """
    if bim_ui.guard(df, on_clear=filters.clear, what=title.lower()):
        return
    frame = df.copy()
    if show_total:
        frame = pd.concat(
            [frame, metrics.total_row(frame, label_col).to_frame().T],
            ignore_index=True,
        )

    config: dict[str, Any] = {}
    for col in frame.columns:
        kind = metrics.kind_of(col)
        pretty = col.replace("_", " ").title()
        if kind == "currency":
            config[col] = st.column_config.NumberColumn(pretty, format="$%.0f")
        elif kind == "percent":
            config[col] = st.column_config.NumberColumn(pretty, format="%.1f%%")
        elif kind == "count":
            config[col] = st.column_config.NumberColumn(pretty, format="%d")
        else:
            config[col] = st.column_config.TextColumn(pretty)

    bim_ui.section(title)
    compat.dataframe(
        frame,
        column_config=config,
        column_order=[label_col] + [c for c in frame.columns if c != label_col],
        height=min(560, 36 * len(frame) + 44),
    )
    st.download_button(
        "Download CSV", frame.to_csv(index=False).encode("utf-8"),
        file_name=f"{title.lower().replace(' ', '_')}.csv", mime="text/csv",
        key=f"dl_{key or title}",
    )


# ---------------------------------------------------------------------------
# The rest of the chart vocabulary. Six shapes, each answering a question the
# ranked bar and the trend line cannot, and every one of them aggregating a frame
# the calling page already holds.
# ---------------------------------------------------------------------------
def area_gap(df: pd.DataFrame, x_col: str, lower_col: str, upper_col: str, *,
             title: str, key: str, lower_label: str, upper_label: str,
             gap_label: str, subtitle: str | None = None) -> None:
    """Two series with the space between them filled.

    Used for sales against bookings, where the filled band is not decoration: the gap
    literally *is* backlog, so the chart explains the metric instead of requiring a
    footnote. Ordering is load-bearing -- `fill="tonexty"` fills to the *previous*
    trace, so the lower series must be added first or the band inverts.
    """
    bim_ui.section(title, subtitle)
    if bim_ui.guard(df, on_clear=filters.clear, what=title.lower()):
        return
    frame = df.sort_values("FISCAL_MONTH_ID") if "FISCAL_MONTH_ID" in df.columns \
        else df.copy()

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=frame[x_col], y=frame[lower_col], name=lower_label,
        mode="lines", line={"color": bim_ui.SF_STAR, "width": 2.2},
        fill="tozeroy", fillcolor="rgba(17,86,127,.16)",
        hovertemplate=f"{lower_label} %{{x}}: %{{y:$,.0f}}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=frame[x_col], y=frame[upper_col], name=upper_label,
        mode="lines", line={"color": bim_ui.SF_BLUE, "width": 2.2},
        fill="tonexty", fillcolor="rgba(255,159,54,.26)",
        hovertemplate=f"{upper_label} %{{x}}: %{{y:$,.0f}}<extra></extra>",
    ))
    # A legend entry for the band itself. Plotly will not label a fill, so this is an
    # empty trace whose only job is to put the word "backlog" in the legend -- without
    # it the most informative part of the chart is the one part with no name.
    fig.add_trace(go.Scatter(
        x=[None], y=[None], name=gap_label, mode="markers",
        marker={"size": 11, "color": "rgba(255,159,54,.55)", "symbol": "square"},
        hoverinfo="skip",
    ))

    peak = float(frame[upper_col].max() or 0)
    fig.update_layout(**_base_layout(
        showlegend=True,
        legend={"orientation": "h", "y": 1.02, "x": 0, "yanchor": "bottom"},
        margin={"l": 8, "r": 8, "t": 34, "b": 64},
        xaxis={"tickangle": -45, "automargin": True},
        yaxis={"range": [0, peak * 1.12] if peak else None, "tickprefix": "$"},
        hovermode="x unified",
    ))
    compat.plotly_chart(fig, key=key)


def waterfall(df: pd.DataFrame, category_col: str, *, year_col: str,
              value_col: str, title: str, key: str) -> None:
    """Year-over-year movement decomposed by category.

    The most executive-legible chart there is, and the one most easily made
    dishonest. The trap here is specific and it fired on the first build: the most
    recent fiscal year is three months old, so comparing the latest two years drew a
    75M cliff that is elapsed time rather than lost business.

    So the comparison years are the two most recent *complete* ones, chosen from the
    data by counting distinct fiscal months, and any partial year excluded is named
    rather than silently dropped. This is the same judgement the agent makes
    unprompted when asked for sales by fiscal year, and it should not be the case
    that the chart is less careful than the chatbot.
    """
    bim_ui.section(title)
    if bim_ui.guard(df, on_clear=filters.clear, what=title.lower()):
        return

    years = sorted(y for y in df[year_col].dropna().unique())
    if len(years) < 2:
        compat.info("Two fiscal years are needed to show a movement. "
                    "The current filters leave fewer.")
        return

    months = (df.groupby(year_col)["FISCAL_MONTH_ID"].nunique()
              if "FISCAL_MONTH_ID" in df.columns else None)
    excluded: list[str] = []
    if months is not None and len(months):
        full = int(months.max())
        complete = [y for y in years if int(months.get(y, 0)) >= full]
        if len(complete) >= 2:
            prior, latest = complete[-2], complete[-1]
            excluded = [f"{y} ({int(months.get(y, 0))} of {full} months)"
                        for y in years if y > latest]
        else:
            prior, latest = years[-2], years[-1]
    else:
        prior, latest = years[-2], years[-1]

    grouped = (df[df[year_col].isin([prior, latest])]
               .groupby([year_col, category_col], as_index=False)[value_col].sum()
               .pivot(index=category_col, columns=year_col, values=value_col)
               .fillna(0.0))
    grouped["_delta"] = grouped[latest] - grouped[prior]
    grouped = grouped.reindex(
        grouped["_delta"].abs().sort_values(ascending=False).index)

    # Collapse the tail into one bar rather than dropping it: an incomplete waterfall
    # does not reconcile to its own end bar, which is the one thing a waterfall is for.
    if len(grouped) > MAX_CATEGORIES - 1:
        head = grouped.iloc[: MAX_CATEGORIES - 2].copy()
        tail = grouped.iloc[MAX_CATEGORIES - 2:]
        head.loc[f"Other ({len(tail)})"] = tail.sum()
        grouped = head

    labels = [str(prior)] + [str(i) for i in grouped.index] + [str(latest)]
    values = ([float(grouped[prior].sum())]
              + [float(v) for v in grouped["_delta"]]
              + [float(grouped[latest].sum())])
    measures = ["absolute"] + ["relative"] * len(grouped) + ["total"]

    fig = go.Figure(go.Waterfall(
        orientation="v", measure=measures, x=labels, y=values,
        text=[metrics.usd(v) for v in values], textposition="outside",
        cliponaxis=False,
        connector={"line": {"color": GRID, "width": 1}},
        increasing={"marker": {"color": bim_ui.SF_BLUE}},
        decreasing={"marker": {"color": ACCENT}},
        totals={"marker": {"color": bim_ui.SF_STAR}},
        hovertemplate="%{x}: %{text}<extra></extra>",
    ))
    top = max(values[0], values[-1]) if values else 0
    fig.update_layout(**_base_layout(
        height=380,
        margin={"l": 8, "r": 8, "t": 30, "b": 70},
        xaxis={"tickangle": -35, "automargin": True},
        yaxis={"range": [0, top * 1.20] if top else None, "tickprefix": "$"},
    ))
    compat.plotly_chart(fig, key=key)

    movement = metrics.safe_ratio(values[-1] - values[0], values[0])
    caption = (f"{prior} to {latest}: {metrics.pct(movement)}. "
               f"Blue bars grew, orange shrank, and the two dark bars are the year "
               f"totals they reconcile to.")
    if excluded:
        caption += (f" {', '.join(excluded)} is excluded as a partial year -- on a "
                    f"waterfall it would draw as a collapse rather than as elapsed "
                    f"time.")
    st.caption(caption)


def heatmap(df: pd.DataFrame, row_col: str, col_col: str, value_col: str, *,
            title: str, key: str, subtitle: str | None = None,
            col_order: list | None = None) -> None:
    """Two categorical axes against one measure.

    Surfaces concentration that no bar chart shows, because a bar chart has to pick
    one of the two axes and sum away the other.
    """
    bim_ui.section(title, subtitle)
    if bim_ui.guard(df, on_clear=filters.clear, what=title.lower()):
        return

    frame = df.groupby([row_col, col_col], as_index=False)[value_col].sum()
    pivot = frame.pivot(index=row_col, columns=col_col, values=value_col)
    if col_order:
        pivot = pivot.reindex(columns=[c for c in col_order if c in pivot.columns])
    # Rows ordered by total so the densest band sits at the top rather than wherever
    # the alphabet happens to put it.
    pivot = pivot.reindex(pivot.sum(axis=1).sort_values().index)

    fig = go.Figure(go.Heatmap(
        z=pivot.values, x=[str(c) for c in pivot.columns],
        y=[str(i) for i in pivot.index],
        colorscale=SF_SCALE, hoverongaps=False,
        colorbar={"tickprefix": "$", "thickness": 12, "outlinewidth": 0,
                  "tickfont": {"size": 10}},
        hovertemplate="%{y} / %{x}: %{z:$,.0f}<extra></extra>",
    ))
    fig.update_layout(**_base_layout(
        height=max(260, 42 * len(pivot) + 90),
        margin={"l": 8, "r": 8, "t": 20, "b": 60},
        xaxis={"tickangle": -45, "automargin": True, "showgrid": False},
        yaxis={"automargin": True, "showgrid": False},
    ))
    compat.plotly_chart(fig, key=key)


def _hierarchy_arrays(df: pd.DataFrame, levels: list[str],
                      value_col: str) -> tuple[list, list, list, list]:
    """Build ids/labels/parents/values for a treemap or sunburst.

    Ids are path-joined rather than bare labels. A label can legitimately repeat under
    two parents -- the same product group sells in two regions -- and bare labels make
    Plotly merge those into one node, which quietly doubles a branch.
    """
    ids: list[str] = []
    labels: list[str] = []
    parents: list[str] = []
    values: list[float] = []
    seen: set[str] = set()

    frame = df.dropna(subset=levels[:1]).copy()
    for depth in range(1, len(levels) + 1):
        cols = levels[:depth]
        grouped = frame.groupby(cols, as_index=False, dropna=True)[value_col].sum()
        for _, row in grouped.iterrows():
            path = [str(row[c]) for c in cols]
            node_id = " / ".join(path)
            if node_id in seen:
                continue
            seen.add(node_id)
            ids.append(node_id)
            labels.append(path[-1])
            parents.append(" / ".join(path[:-1]))
            values.append(float(row[value_col]))
    return ids, labels, parents, values


def treemap(df: pd.DataFrame, levels: list[str], value_col: str, *,
            title: str, key: str, subtitle: str | None = None) -> None:
    """Two hierarchy levels at once, sized by measure. Where the business is, at a
    glance, without making anyone read an axis."""
    bim_ui.section(title, subtitle)
    if bim_ui.guard(df, on_clear=filters.clear, what=title.lower()):
        return
    ids, labels, parents, values = _hierarchy_arrays(df, levels, value_col)
    if not ids:
        compat.info("No hierarchy rows match the current filters.")
        return

    fig = go.Figure(go.Treemap(
        ids=ids, labels=labels, parents=parents, values=values,
        # "total" not "remainder": the values passed in are already the full subtotal
        # for each node, so asking Plotly to add children on top double-counts.
        branchvalues="total",
        marker={"colors": [SERIES[i % len(SERIES)] for i in range(len(ids))],
                "line": {"width": 1.5, "color": "rgba(255,255,255,.7)"}},
        tiling={"packing": "squarify"},
        textinfo="label+value+percent parent",
        texttemplate="<b>%{label}</b><br>%{value:$.3s}<br>%{percentParent}",
        hovertemplate="%{id}<br>%{value:$,.0f}<extra></extra>",
    ))
    fig.update_layout(**_base_layout(
        height=430, margin={"l": 0, "r": 0, "t": 10, "b": 0}))
    compat.plotly_chart(fig, key=key)


def sunburst(df: pd.DataFrame, levels: list[str], value_col: str, *,
             title: str, key: str, subtitle: str | None = None) -> None:
    """The drill structure itself, as a shape. Uses the deep hierarchies the source
    model declares rather than a flattened two levels."""
    bim_ui.section(title, subtitle)
    if bim_ui.guard(df, on_clear=filters.clear, what=title.lower()):
        return
    present = [c for c in levels if c in df.columns]
    ids, labels, parents, values = _hierarchy_arrays(df, present, value_col)
    if not ids:
        compat.info("No hierarchy rows match the current filters.")
        return

    fig = go.Figure(go.Sunburst(
        ids=ids, labels=labels, parents=parents, values=values,
        branchvalues="total", maxdepth=3,
        marker={"colors": [SERIES[i % len(SERIES)] for i in range(len(ids))],
                "line": {"width": 1.2, "color": "rgba(255,255,255,.75)"}},
        hovertemplate="%{id}<br>%{value:$,.0f}<extra></extra>",
        insidetextorientation="radial",
    ))
    fig.update_layout(**_base_layout(
        height=430, margin={"l": 0, "r": 0, "t": 10, "b": 0}))
    compat.plotly_chart(fig, key=key)
    st.caption("Click a segment to zoom a branch; click the centre to come back out. "
               "Levels are the ones the source model declares, not levels we invented.")


def mix_bar(df: pd.DataFrame, x_col: str, series_col: str, value_col: str, *,
            title: str, key: str, subtitle: str | None = None) -> None:
    """Share of total by period. Normalised deliberately: absolute stacked bars answer
    "did we grow", which the trend line already answers, and hide the only thing this
    chart is for -- whether the mix is moving."""
    bim_ui.section(title, subtitle)
    if bim_ui.guard(df, on_clear=filters.clear, what=title.lower()):
        return

    frame = df.groupby([x_col, series_col], as_index=False)[value_col].sum()

    # Fold the tail into "Other", the same MAX_CATEGORIES rule the ranked bar uses.
    #
    # Not cosmetic. The regional split has thirteen values, and thirteen legend entries
    # wrap to three rows and cover the top of the bars -- observed in the browser, not
    # theorised. Folding also keeps every period summing to exactly 100%, because the tail
    # is aggregated rather than dropped.
    ranked_cats = (frame.groupby(series_col)[value_col].sum()
                        .sort_values(ascending=False).index.tolist())
    if len(ranked_cats) > MAX_CATEGORIES:
        keep = set(ranked_cats[: MAX_CATEGORIES - 1])
        other = f"Other ({len(ranked_cats) - len(keep)})"
        frame[series_col] = [c if c in keep else other for c in frame[series_col]]
        frame = frame.groupby([x_col, series_col], as_index=False)[value_col].sum()

    totals = frame.groupby(x_col)[value_col].transform("sum")
    frame["_share"] = [metrics.safe_ratio(v, t) * 100.0
                       for v, t in zip(frame[value_col], totals)]

    order = [str(c) for c in
             frame.groupby(series_col)[value_col].sum()
             .sort_values(ascending=False).index]
    x_order = sorted(str(v) for v in frame[x_col].unique())

    fig = go.Figure()
    for i, cat in enumerate(order):
        part = frame[frame[series_col].astype(str) == cat]
        fig.add_trace(go.Bar(
            x=part[x_col].astype(str), y=part["_share"], name=cat,
            marker_color=SERIES[i % len(SERIES)],
            hovertemplate=f"{cat} %{{x}}: %{{y:.1f}}%<extra></extra>",
        ))
    fig.update_layout(**_base_layout(
        barmode="stack", showlegend=True,
        legend={"orientation": "h", "y": 1.02, "x": 0, "yanchor": "bottom"},
        margin={"l": 8, "r": 8, "t": 34, "b": 40},
        height=360,
        xaxis={"categoryorder": "array", "categoryarray": x_order},
        yaxis={"range": [0, 100], "ticksuffix": "%"},
    ))
    compat.plotly_chart(fig, key=key)


def pareto(df: pd.DataFrame, label_col: str, value_col: str, *,
           title: str, key: str, top_n: int = 20,
           subtitle: str | None = None) -> None:
    """Ranked bars with a cumulative share curve.

    The 80% reference line is the point: revenue concentration is a risk question, and
    "eleven customers are 80% of the book" lands in a way a ranked list does not. The
    cumulative curve is computed over *every* row, not just the bars drawn, so the
    percentage is true rather than true-of-the-top-twenty.
    """
    bim_ui.section(title, subtitle)
    if bim_ui.guard(df, on_clear=filters.clear, what=title.lower()):
        return

    ranked = (df.groupby(label_col, as_index=False)[value_col].sum()
                .sort_values(value_col, ascending=False))
    grand = float(ranked[value_col].sum())
    if grand <= 0:
        compat.info("Nothing to rank under the current filters.")
        return
    ranked["_cum"] = ranked[value_col].cumsum() / grand * 100.0
    crossing = ranked[ranked["_cum"] >= 80.0]
    n_for_80 = int(ranked.index.get_indexer([crossing.index[0]])[0]) + 1 \
        if not crossing.empty else len(ranked)

    shown = ranked.head(top_n)

    # The cumulative axis is scaled to the curve actually drawn, not fixed at 0-100%.
    #
    # This data is close to uniform -- 104 of 266 customers make up 80% -- so the top
    # twenty only reach about 20% cumulative. Against a 0-100% axis that curve is a flat
    # line along the floor with no readable shape, and the 80% reference line sits in
    # empty space above it pretending to be informative. Scaling gives the curve shape,
    # and the 80% line is drawn only when it is actually in range. The concentration
    # figure is stated in the caption either way, which is where it belongs: it is a fact
    # about every customer, not about the twenty bars.
    max_cum = float(shown["_cum"].max() or 0.0)
    # Round up to the next 5% above the curve plus a small margin, so the top tick sits
    # just above the last point rather than a whole decade above it.
    right_max = 105 if max_cum >= 70 else min(105, int(-(-(max_cum * 1.15) // 5)) * 5)

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=shown[label_col].astype(str), y=shown[value_col], name="Sales",
        marker_color=bim_ui.SF_BLUE,
        hovertemplate="%{x}: %{y:$,.0f}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=shown[label_col].astype(str), y=shown["_cum"], name="Cumulative share",
        yaxis="y2", mode="lines+markers",
        line={"color": ACCENT, "width": 2.4}, marker={"size": 5},
        hovertemplate="%{x}: %{y:.1f}% of total<extra></extra>",
    ))
    if right_max >= 80:
        fig.add_shape(type="line", xref="x domain", x0=0, x1=1,
                      yref="y2", y0=80, y1=80,
                      line={"color": bim_ui.SF_PURPLE, "width": 1.2, "dash": "dash"})

    peak = float(shown[value_col].max() or 0)
    fig.update_layout(**_base_layout(
        showlegend=True, height=380,
        legend={"orientation": "h", "y": 1.02, "x": 0, "yanchor": "bottom"},
        margin={"l": 8, "r": 8, "t": 34, "b": 110},
        xaxis={"tickangle": -45, "automargin": True},
        yaxis={"range": [0, peak * 1.12] if peak else None, "tickprefix": "$"},
        yaxis2={"overlaying": "y", "side": "right", "range": [0, right_max],
                "ticksuffix": "%", "showgrid": False},
    ))
    compat.plotly_chart(fig, key=key)
    st.caption(
        f"{n_for_80:,} of {len(ranked):,} account for 80% of sales in this scope -- "
        f"revenue here is **not** concentrated in a handful of accounts. The curve is "
        f"cumulative over all {len(ranked):,}, so the top {len(shown):,} shown reach "
        f"{max_cum:.1f}%."
    )


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def _spark(frame: pd.DataFrame, col: str, months: int = 12) -> list[float]:
    """Last N fiscal months of a measure, in fiscal order.

    Sorted by FISCAL_MONTH_ID rather than by month name: sorting month labels
    alphabetically puts Apr before Jan and produces a sparkline whose trend direction
    is simply wrong.
    """
    if frame.empty or col not in frame.columns:
        return []
    series = (frame.groupby("FISCAL_MONTH_ID", as_index=False)[col].sum()
                   .sort_values("FISCAL_MONTH_ID"))
    return [float(v) for v in series[col].tolist()[-months:]]


def _spark_backlog(frame: pd.DataFrame, months: int = 12) -> list[float]:
    """Backlog sparkline: bookings less sales, differenced per fiscal month.

    Backlog has no column of its own, so it is differenced rather than summed. Zipping two
    independent sparklines would misalign wherever a month carries one measure and not the
    other.
    """
    if frame.empty or "FISCAL_MONTH_ID" not in frame.columns:
        return []
    grouped = (frame.groupby("FISCAL_MONTH_ID", as_index=False)[
                   ["BOOKED_AMOUNT_TOTAL", "SALES_AMOUNT_TOTAL"]].sum()
                    .sort_values("FISCAL_MONTH_ID"))
    diff = grouped["BOOKED_AMOUNT_TOTAL"] - grouped["SALES_AMOUNT_TOTAL"]
    return [float(v) for v in diff.tolist()[-months:]]
