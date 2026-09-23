"""Number formatting and safe arithmetic, in one place.

The reason this is a module rather than a few inline f-strings: the same figure
appears on a KPI tile, an axis tick, a tooltip and a grid cell, and if each surface
formats independently they drift. "$4.2M" on the tile, "4200000" on the axis and
"4,200,000.00" in the grid are all the same number, and a viewer reasonably concludes
one of them is wrong. Every surface in this app formats through the registry below.
"""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

# Field names that are labels even though they look numeric. FISCAL_MONTH_ID is the
# trap: it is an integer, so numeric inference formats it with thousands separators
# and renders month 202607 as "202,607".
_LABEL_LIKE = re.compile(
    r"(date|month|quarter|year|period|timestamp|_id$|_name$|_code$|level\d?)",
    re.IGNORECASE,
)

_CURRENCY = re.compile(r"(amount|sales|booking|booked|revenue|backlog|usd|cost|price)",
                       re.IGNORECASE)
_PERCENT = re.compile(r"(growth|pct|percent|share|ratio|attainment|margin)",
                      re.IGNORECASE)
_COUNT = re.compile(r"(quantity|units|count|rows|orders)", re.IGNORECASE)


def kind_of(field: str) -> str:
    """Classify a field for formatting.

    Order is load-bearing. Label runs first so FISCAL_MONTH_ID is not rendered as
    "202,607". Count runs before currency because a name like SALES_QUANTITY_TOTAL
    matches both "sales" and "quantity", and quantity is the more specific signal --
    checked the other way round, units render as "$1.2M".
    """
    if _LABEL_LIKE.search(field):
        return "label"
    if _PERCENT.search(field):
        return "percent"
    if _COUNT.search(field):
        return "count"
    if _CURRENCY.search(field):
        return "currency"
    return "number"


def safe_ratio(numerator: float | None, denominator: float | None) -> float:
    """Division that cannot raise or produce NaN/inf.

    Returns 0.0 on a zero or missing denominator. Attainment, growth and average
    deal size all divide by something that can legitimately be zero, and a single
    inf leaking into a chart axis makes every other bar invisible.

    Not implemented with `.replace(0, pd.NA).fillna(0.0)`: that path triggers a
    pandas object-dtype downcast FutureWarning which is scheduled to become an
    error.
    """
    try:
        n = float(numerator or 0.0)
        d = float(denominator or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if d == 0.0 or pd.isna(d) or pd.isna(n):
        return 0.0
    out = n / d
    return 0.0 if (pd.isna(out) or out in (float("inf"), float("-inf"))) else out


def usd(value: Any, *, compact: bool = True) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "--"
    if pd.isna(v):
        return "--"
    if not compact:
        return f"${v:,.0f}"
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a == 0:
        return "$0"                      # not "$0.0"
    if a >= 1e9:
        return f"{sign}${a/1e9:.1f}B"
    if a >= 1e6:
        return f"{sign}${a/1e6:.1f}M"
    if a >= 1e3:
        return f"{sign}${a/1e3:.0f}K"
    return f"{sign}${a:,.0f}"


def pct(value: Any, *, decimals: int = 1) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "--"
    if pd.isna(v):
        return "--"
    # Source metrics express growth as a fraction; anything beyond +/-5 is already
    # in percentage points and would otherwise render as "450.0%".
    if abs(v) <= 5:
        v *= 100.0
    return f"{v:+.{decimals}f}%"


def count(value: Any) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "--"
    return "--" if pd.isna(v) else f"{v:,.0f}"


def number(value: Any, *, decimals: int = 0) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "--"
    return "--" if pd.isna(v) else f"{v:,.{decimals}f}"


def format_value(field: str, value: Any) -> str:
    """Format by inferred field kind. The single entry point for every surface."""
    return {
        "currency": lambda v: usd(v),
        "percent": pct,
        "count": count,
        "label": lambda v: "" if v is None or pd.isna(v) else str(v),
    }.get(kind_of(field), number)(value)


def total_row(df: pd.DataFrame, label_col: str,
              label: str = "Total") -> pd.Series:
    """Build a totals row: SUM measures, MEAN ratios.

    Summing ratios is the classic error -- four territories at 90% attainment do
    not total 360%. Percent-like columns are averaged for that reason.
    """
    out: dict[str, Any] = {}
    for col in df.columns:
        if col == label_col:
            out[col] = label
            continue
        series = df[col]
        if not pd.api.types.is_numeric_dtype(series):
            # List columns feed sparklines and must stay homogeneous: an empty
            # string here makes PyArrow raise "cannot mix list and non-list".
            out[col] = [] if series.apply(lambda x: isinstance(x, list)).any() else ""
            continue
        out[col] = series.mean() if kind_of(col) == "percent" else series.sum()
    return pd.Series(out)


_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def month_sort_key(label: Any) -> int:
    """Calendar order for month labels.

    Sorting month names alphabetically puts Apr before Jan and produces a sparkline
    whose trend direction is simply wrong. Unrecognised labels sort last rather than
    raising.
    """
    s = str(label).strip().upper()[:3]
    return _MONTHS.index(s) if s in _MONTHS else 99
