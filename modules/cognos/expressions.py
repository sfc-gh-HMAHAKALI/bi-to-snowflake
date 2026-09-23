"""Cognos expression translation to Snowflake SQL.

Two distinct jobs:

1. **Scalar rewriting** -- map Cognos function syntax onto Snowflake. Mostly
   mechanical; the awkward cases are Cognos's underscore-prefixed date builtins
   (``_add_months``, ``_days_between``) whose argument order does not match
   Snowflake's ``DATEADD`` / ``DATEDIFF``.

2. **Relative-time reduction** -- the part that actually matters for this model.
   the reference model's 248 calculations do not compute date windows arithmetically. They
   reference members of a materialised relative-time dimension:

       [RELATIVE_TIME_DESC]->[all].[Week To Date]
     - [RELATIVE_TIME_DESC]->[all].[Prior Week To Date]

   That pattern is an ETL-maintained bucket table plus an OLAP member lookup. In
   Snowflake the same intent is a ``CASE`` over a date column bounded by
   ``DATE_TRUNC``/``DATEADD``, computed live -- which removes the ETL dependency
   entirely. This module recognises the member names and emits the window, so
   the translation is driven by the model's own declared semantics rather than by
   guessing from the calculation's display name.
"""

from __future__ import annotations

import re
from typing import Any

from ..common.logger import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Scalar function mapping
# ---------------------------------------------------------------------------

# Direct name-for-name substitutions, applied as whole-word replacements.
_SIMPLE_FUNCTION_MAP: dict[str, str] = {
    "sysdate": "CURRENT_DATE()",
    "current_date": "CURRENT_DATE()",
    "current_timestamp": "CURRENT_TIMESTAMP()",
    "char_length": "LENGTH",
    "ceiling": "CEIL",
    "substring": "SUBSTR",
    "_first_of_month": "DATE_TRUNC('month', {0})",
    "_last_of_month": "LAST_DAY({0})",
    "_day_of_week": "DAYOFWEEK({0})",
    "_day_of_year": "DAYOFYEAR({0})",
    "_week_of_year": "WEEKOFYEAR({0})",
}

# Cognos date builtins whose argument order differs from Snowflake's.
#   _add_months(date, n)      -> DATEADD('month', n, date)
#   _days_between(d1, d2)     -> DATEDIFF('day', d2, d1)
_ARG_REORDER_MAP: dict[str, tuple[str, str]] = {
    "_add_days": ("DATEADD", "day"),
    "_add_months": ("DATEADD", "month"),
    "_add_years": ("DATEADD", "year"),
    "_days_between": ("DATEDIFF", "day"),
    "_months_between": ("DATEDIFF", "month"),
    "_years_between": ("DATEDIFF", "year"),
}


def _split_args(arglist: str) -> list[str]:
    """Split a function argument list on top-level commas only."""
    args: list[str] = []
    depth = 0
    quote: str | None = None
    buf: list[str] = []
    for ch in arglist:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
        elif ch in "([":
            depth += 1
            buf.append(ch)
        elif ch in ")]":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        args.append("".join(buf).strip())
    return args


def _rewrite_reordered(expr: str) -> str:
    """Rewrite Cognos date builtins with Snowflake argument order."""
    for cognos_fn, (sf_fn, unit) in _ARG_REORDER_MAP.items():
        pattern = re.compile(re.escape(cognos_fn) + r"\s*\(", re.IGNORECASE)
        while True:
            m = pattern.search(expr)
            if not m:
                break
            # Find the matching close paren for this call.
            start = m.end()
            depth = 1
            i = start
            while i < len(expr) and depth:
                if expr[i] == "(":
                    depth += 1
                elif expr[i] == ")":
                    depth -= 1
                i += 1
            if depth:
                break  # unbalanced; leave the rest alone
            inner = expr[start : i - 1]
            args = _split_args(inner)
            if sf_fn == "DATEADD" and len(args) == 2:
                repl = f"DATEADD('{unit}', {args[1]}, {args[0]})"
            elif sf_fn == "DATEDIFF" and len(args) == 2:
                # Cognos _days_between(d1, d2) is d1 - d2.
                repl = f"DATEDIFF('{unit}', {args[1]}, {args[0]})"
            else:
                repl = f"{sf_fn}({inner})"
            expr = expr[: m.start()] + repl + expr[i:]
    return expr


def translate_scalar_expression(expression: str) -> str:
    """Rewrite Cognos scalar function syntax into Snowflake SQL.

    Does not attempt to resolve object references -- those are replaced
    separately, because the mapping from a Cognos ``[namespace].[object].[item]``
    to a physical column depends on the target schema layout.
    """
    if not expression:
        return ""

    expr = expression

    # Cognos string concatenation is ||, same as Snowflake. Cognos uses <> for
    # inequality, which Snowflake accepts. Nothing to do for either.
    expr = _rewrite_reordered(expr)

    for cognos_fn, sf in _SIMPLE_FUNCTION_MAP.items():
        if "{0}" in sf:
            pattern = re.compile(re.escape(cognos_fn) + r"\s*\(\s*([^()]*?)\s*\)", re.IGNORECASE)
            expr = pattern.sub(lambda m: sf.format(m.group(1)), expr)
        else:
            expr = re.sub(
                r"\b" + re.escape(cognos_fn) + r"\b",
                sf,
                expr,
                flags=re.IGNORECASE,
            )

    return expr.strip()


# ---------------------------------------------------------------------------
# Relative-time members
# ---------------------------------------------------------------------------

# Maps a Cognos relative-time member caption to a (start, end) pair of Snowflake
# date expressions, both inclusive. Anchored on CURRENT_DATE() so the window
# follows the query date and never needs a per-fiscal-year clone.
#
# The keys are the member captions as they appear in the model's
# EDW_OTH_COA_RELATIVE_TIME_DIM_TIME dimension.
_RELATIVE_TIME_WINDOWS: dict[str, tuple[str, str]] = {
    # Period-to-date
    "week to date": ("DATE_TRUNC('week', CURRENT_DATE())", "CURRENT_DATE()"),
    "prior week to date": (
        "DATEADD('week', -1, DATE_TRUNC('week', CURRENT_DATE()))",
        "DATEADD('week', -1, CURRENT_DATE())",
    ),
    "month to date": ("DATE_TRUNC('month', CURRENT_DATE())", "CURRENT_DATE()"),
    "prior month to date": (
        "DATEADD('month', -1, DATE_TRUNC('month', CURRENT_DATE()))",
        "DATEADD('month', -1, CURRENT_DATE())",
    ),
    "quarter to date": ("DATE_TRUNC('quarter', CURRENT_DATE())", "CURRENT_DATE()"),
    "prior quarter to date": (
        "DATEADD('quarter', -1, DATE_TRUNC('quarter', CURRENT_DATE()))",
        "DATEADD('quarter', -1, CURRENT_DATE())",
    ),
    "prior year quarter to date": (
        "DATEADD('year', -1, DATE_TRUNC('quarter', CURRENT_DATE()))",
        "DATEADD('year', -1, CURRENT_DATE())",
    ),
    "year to date": ("DATE_TRUNC('year', CURRENT_DATE())", "CURRENT_DATE()"),
    "prior year to date": (
        "DATEADD('year', -1, DATE_TRUNC('year', CURRENT_DATE()))",
        "DATEADD('year', -1, CURRENT_DATE())",
    ),
    # Completed full periods
    "last week": (
        "DATEADD('week', -1, DATE_TRUNC('week', CURRENT_DATE()))",
        "DATEADD('day', -1, DATE_TRUNC('week', CURRENT_DATE()))",
    ),
    "last month": (
        "DATE_TRUNC('month', DATEADD('month', -1, CURRENT_DATE()))",
        "DATEADD('day', -1, DATE_TRUNC('month', CURRENT_DATE()))",
    ),
    "previous month": (
        "DATE_TRUNC('month', DATEADD('month', -2, CURRENT_DATE()))",
        "DATEADD('day', -1, DATE_TRUNC('month', DATEADD('month', -1, CURRENT_DATE())))",
    ),
    "last quarter": (
        "DATE_TRUNC('quarter', DATEADD('quarter', -1, CURRENT_DATE()))",
        "DATEADD('day', -1, DATE_TRUNC('quarter', CURRENT_DATE()))",
    ),
    "previous quarter": (
        "DATE_TRUNC('quarter', DATEADD('quarter', -2, CURRENT_DATE()))",
        "DATEADD('day', -1, DATE_TRUNC('quarter', DATEADD('quarter', -1, CURRENT_DATE())))",
    ),
    "last year": (
        "DATE_TRUNC('year', DATEADD('year', -1, CURRENT_DATE()))",
        "DATEADD('day', -1, DATE_TRUNC('year', CURRENT_DATE()))",
    ),
    "previous year": (
        "DATE_TRUNC('year', DATEADD('year', -2, CURRENT_DATE()))",
        "DATEADD('day', -1, DATE_TRUNC('year', DATEADD('year', -1, CURRENT_DATE())))",
    ),
    # Trailing windows
    "last 3 months": ("DATEADD('month', -3, CURRENT_DATE())", "CURRENT_DATE()"),
    "previous 3 months": (
        "DATEADD('month', -6, CURRENT_DATE())",
        "DATEADD('day', -1, DATEADD('month', -3, CURRENT_DATE()))",
    ),
    "last 6 months": ("DATEADD('month', -6, CURRENT_DATE())", "CURRENT_DATE()"),
    "previous 6 months": (
        "DATEADD('month', -12, CURRENT_DATE())",
        "DATEADD('day', -1, DATEADD('month', -6, CURRENT_DATE()))",
    ),
    "last 12 months": ("DATEADD('month', -12, CURRENT_DATE())", "CURRENT_DATE()"),
    "previous 12 months": (
        "DATEADD('month', -24, CURRENT_DATE())",
        "DATEADD('day', -1, DATEADD('month', -12, CURRENT_DATE()))",
    ),
    # Cognos's quarter ladder is Last (-1), Previous (-2), Prior to Previous (-3).
    "prior to previous quarter": (
        "DATE_TRUNC('quarter', DATEADD('quarter', -3, CURRENT_DATE()))",
        "DATEADD('day', -1, DATE_TRUNC('quarter', DATEADD('quarter', -2, CURRENT_DATE())))",
    ),
    # Same completed period, one year earlier -- year-over-year comparisons.
    "last quarter - last year": (
        "DATEADD('year', -1, DATE_TRUNC('quarter', DATEADD('quarter', -1, CURRENT_DATE())))",
        "DATEADD('year', -1, DATEADD('day', -1, DATE_TRUNC('quarter', CURRENT_DATE())))",
    ),
    "last month - last year": (
        "DATEADD('year', -1, DATE_TRUNC('month', DATEADD('month', -1, CURRENT_DATE())))",
        "DATEADD('year', -1, DATEADD('day', -1, DATE_TRUNC('month', CURRENT_DATE())))",
    ),
}

# Cognos spells the same window several ways across the calculation library.
# Aliases resolve before lookup so one definition serves all spellings.
_MEMBER_ALIASES: dict[str, str] = {
    "prior last month": "previous month",
    "prior last 3 months": "previous 3 months",
    "prior last 6 months": "previous 6 months",
    "prior last 12 months": "previous 12 months",
    "previous (to last) 3 months": "previous 3 months",
    "previous (to last) 6 months": "previous 6 months",
    "previous (to last) 12 months": "previous 12 months",
    "prior 3 months": "previous 3 months",
    "prior 6 months": "previous 6 months",
    "prior 12 months": "previous 12 months",
}

# Members that depend on the reference model's fiscal calendar rather than the Gregorian one.
#
# These are deliberately NOT given CURRENT_DATE() arithmetic. the reference model's fiscal year
# is offset from the calendar year, and medical-device fiscal calendars are often
# 4-4-5 or 13-period, where quarter and month boundaries do not land on calendar
# boundaries at all. Deriving them with a flat DATEADD offset produces metrics
# that are wrong at every period boundary -- silently, and only at the
# boundaries, which is the worst failure mode available.
#
# The correct source is the fiscal columns already maintained in the model's own
# date dimension. Each entry names what the window needs from that dimension, so
# the caller can emit a predicate against DIM_DATE instead of guessing an offset.
_FISCAL_MEMBERS: dict[str, dict[str, str]] = {
    "fiscal year to date": {
        "grain": "year",
        "offset": "0",
        "needs": "fiscal year id + fiscal day-of-year to bound 'to date'",
    },
    "prior fiscal year to date": {
        "grain": "year",
        "offset": "-1",
        "needs": "fiscal year id + fiscal day-of-year",
    },
    "last fiscal year": {
        "grain": "year",
        "offset": "-1",
        "needs": "fiscal year id (full completed year)",
    },
    "previous (2nd) fiscal year": {
        "grain": "year",
        "offset": "-2",
        "needs": "fiscal year id (full completed year)",
    },
    "fiscal year qtr to date": {
        "grain": "quarter",
        "offset": "0",
        "needs": "fiscal quarter id + fiscal day-of-quarter",
    },
    "prior fiscal year qtr to date": {
        "grain": "quarter",
        "offset": "-1",
        "needs": "fiscal quarter id + fiscal day-of-quarter",
    },
}

# "[RELATIVE_TIME_DESC]->[all].[Week To Date]" -- capture the member caption.
_MEMBER_CAPTURE_RE = re.compile(r"->\s*\[all\]\s*\.\s*\[([^\]]+)\]", re.IGNORECASE)

# The other DMR shape, e.g.
#   [DMR View].[Last 3 Months].[Last 3 Months].[Last 3 Months]->[all]
# Here the window is the *dimension name* and the bare "->[all]" simply rolls the
# whole dimension up; there is no member caption after it. Capture the last
# bracketed part before the arrow. The negative lookahead keeps this from also
# matching the shape above.
_DIMENSION_ROLLUP_RE = re.compile(r"\[([^\]]+)\]\s*->\s*\[all\](?!\s*\.)", re.IGNORECASE)

# "1 Mon", "5 Mon" -- the single full month N months back.
_N_MON_RE = re.compile(r"^(\d+)\s*mon(?:th)?s?$", re.IGNORECASE)

# Trailing scalar arithmetic applied to a window: "/3" or "/3)*12".
_TRAILING_SCALAR_RE = re.compile(r"->\s*\[all\]\s*\)?\s*((?:[/*]\s*\d+\s*\)?\s*)+)$")


def _n_month_window(n: int) -> tuple[str, str]:
    """The single full calendar month N months before the current month."""
    start = f"DATE_TRUNC('month', DATEADD('month', -{n}, CURRENT_DATE()))"
    if n > 1:
        end = (
            f"DATEADD('day', -1, DATE_TRUNC('month', "
            f"DATEADD('month', -{n - 1}, CURRENT_DATE())))"
        )
    else:
        end = "DATEADD('day', -1, DATE_TRUNC('month', CURRENT_DATE()))"
    return start, end


def extract_scalar_tail(expression: str) -> str:
    """Return trailing scalar arithmetic applied to a window, e.g. ``/3*12``.

    Cognos writes averages and run rates as a window divided by a period count,
    optionally multiplied back up: ``Last 3 Months / 3`` is a 3-month average,
    and ``(Last 3 Months / 3) * 12`` is an annual run rate.

    Worth noting because it is easy to misread: the reference model model's
    ``6 Months Rate`` is ``(Last 3 Months / 3) * 6`` -- a six-month projection
    from a three-month base, not a six-month total annualised. The operators are
    taken from the expression rather than inferred from the metric name for
    exactly this reason.
    """
    m = _TRAILING_SCALAR_RE.search((expression or "").strip())
    if not m:
        return ""
    return re.sub(r"[\s)]", "", m.group(1))


def extract_relative_time_members(expression: str) -> list[str]:
    """Return the relative-time window names referenced in an expression.

    Covers both Cognos shapes: an explicit member caption after ``->[all].``,
    and a dimension rollup where the window is the dimension name itself and the
    expression ends in a bare ``->[all]``.
    """
    expr = expression or ""
    members = [m.group(1).strip() for m in _MEMBER_CAPTURE_RE.finditer(expr)]
    if members:
        return members
    return [m.group(1).strip() for m in _DIMENSION_ROLLUP_RE.finditer(expr)]


def _canon_member(member: str) -> str:
    """Lowercase a member caption and resolve spelling aliases."""
    key = re.sub(r"\s+", " ", (member or "").strip().lower())
    return _MEMBER_ALIASES.get(key, key)


def is_fiscal_member(member: str) -> bool:
    """True when a member depends on the fiscal calendar, not the Gregorian one."""
    return _canon_member(member) in _FISCAL_MEMBERS


def fiscal_requirement(member: str) -> dict[str, str] | None:
    """What a fiscal member needs from the date dimension, or None."""
    return _FISCAL_MEMBERS.get(_canon_member(member))


def window_for_member(member: str) -> tuple[str, str] | None:
    """Look up the Snowflake (start, end) window for a member caption.

    Returns None for fiscal members. That is deliberate: a fiscal window has no
    correct CURRENT_DATE() expression, so returning one would be worse than
    returning nothing. Callers should test ``is_fiscal_member`` and emit a
    predicate against the date dimension's fiscal columns instead.
    """
    key = _canon_member(member)
    win = _RELATIVE_TIME_WINDOWS.get(key)
    if win:
        return win
    # "1 Mon" / "5 Mon": the single full month N months back.
    m = _N_MON_RE.match(key)
    if m:
        return _n_month_window(int(m.group(1)))
    return None


def fiscal_windowed_sum(
    measure: str,
    fiscal_period_column: str,
    current_period_expr: str,
    offset: str,
) -> str:
    """Build a fiscal-period sum keyed off the date dimension.

    ``fiscal_period_column`` is the fiscal year or quarter id from the date
    dimension; ``current_period_expr`` resolves the period containing today.
    Arithmetic happens on the fiscal id, which the date dimension defines, rather
    than on dates.
    """
    target = (
        current_period_expr
        if offset in ("0", "", None)
        else f"({current_period_expr}) {'+' if str(offset).startswith('+') else '-'} {abs(int(offset))}"
    )
    return (
        f"SUM(CASE WHEN {fiscal_period_column} = {target} "
        f"THEN {measure} ELSE 0 END)"
    )


def windowed_sum(measure: str, date_column: str, member: str) -> str | None:
    """Build ``SUM(CASE WHEN <date> BETWEEN ... THEN <measure> ELSE 0 END)``.

    Returns None when the member caption is not a recognised relative-time
    bucket, so the caller can flag it rather than emit a wrong window.
    """
    win = window_for_member(member)
    if not win:
        return None
    start, end = win
    return (
        f"SUM(CASE WHEN {date_column} >= {start} "
        f"AND {date_column} <= {end} THEN {measure} ELSE 0 END)"
    )


def translate_relative_time_calculation(
    expression: str,
    measure: str,
    date_column: str,
    fiscal_period_columns: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Convert a Cognos relative-time calculation into a Snowflake expression.

    Handles the two shapes that account for the whole the reference model calculation
    library:

    * ``A - B``      -> a "change" metric (difference of two windows)
    * ``(A - B) / B`` -> a "growth" metric (ratio, NULLIF-guarded)

    Args:
        expression: The Cognos calculation body.
        measure: The measure column to aggregate.
        date_column: The date column the Gregorian windows filter on.
        fiscal_period_columns: Optional mapping of grain -> date-dimension column,
            e.g. ``{"year": "d.fiscal_year_id", "quarter": "d.fiscal_quarter_id"}``
            plus ``{"year_current": "...", "quarter_current": "..."}`` expressions
            resolving the current period. Required for fiscal members; without it
            they are reported as ``needs_fiscal_calendar`` rather than translated.

    Returns ``{"expr", "members", "kind", "unresolved", "fiscal_members"}``.
    ``kind`` is one of ``change``, ``growth``, ``single``,
    ``needs_fiscal_calendar``, ``unsupported``.

    Division always uses ``NULLIF(..., 0)`` rather than ``DIV0``: a growth rate
    against a zero base is genuinely undefined, and returning 0 would silently
    assert "no growth" where the correct answer is "not computable".
    """
    members = extract_relative_time_members(expression)
    fiscal = [m for m in members if is_fiscal_member(m)]
    unresolved = [
        m for m in members if window_for_member(m) is None and not is_fiscal_member(m)
    ]

    if not members:
        return {
            "expr": "",
            "members": [],
            "kind": "unsupported",
            "unresolved": [],
            "fiscal_members": [],
        }

    if unresolved:
        return {
            "expr": "",
            "members": members,
            "kind": "unsupported",
            "unresolved": unresolved,
            "fiscal_members": fiscal,
        }

    # Fiscal members need the date dimension. Without a column mapping, say so
    # rather than emitting a Gregorian approximation.
    if fiscal and not fiscal_period_columns:
        return {
            "expr": "",
            "members": members,
            "kind": "needs_fiscal_calendar",
            "unresolved": [],
            "fiscal_members": fiscal,
            "fiscal_requirements": {m: fiscal_requirement(m) for m in fiscal},
        }

    def _sum_for(member: str) -> str | None:
        if is_fiscal_member(member):
            req = fiscal_requirement(member) or {}
            grain = req.get("grain", "year")
            col = (fiscal_period_columns or {}).get(grain)
            cur = (fiscal_period_columns or {}).get(f"{grain}_current")
            if not col or not cur:
                return None
            return fiscal_windowed_sum(measure, col, cur, req.get("offset", "0"))
        return windowed_sum(measure, date_column, member)

    sums = [_sum_for(m) for m in members]
    if any(s is None for s in sums):
        return {
            "expr": "",
            "members": members,
            "kind": "needs_fiscal_calendar",
            "unresolved": [],
            "fiscal_members": fiscal,
            "fiscal_requirements": {m: fiscal_requirement(m) for m in fiscal},
        }

    # Growth: the denominator member is repeated as the third reference.
    is_growth = "/" in expression
    scalar_tail = extract_scalar_tail(expression)
    if len(members) == 1 and scalar_tail:
        # A single window with trailing arithmetic: average, run rate, or
        # projection. The operators are taken from the expression, never inferred
        # from the metric's display name.
        expr = f"({sums[0]}){scalar_tail}"
        kind = "rate"
    elif len(members) >= 3 and is_growth:
        expr = f"({sums[0]} - {sums[1]}) / NULLIF({sums[2]}, 0)"
        kind = "growth"
    elif len(members) == 2 and is_growth:
        expr = f"({sums[0]} - {sums[1]}) / NULLIF({sums[1]}, 0)"
        kind = "growth"
    elif len(members) == 2:
        expr = f"{sums[0]} - {sums[1]}"
        kind = "change"
    elif len(members) == 1:
        expr = sums[0] or ""
        kind = "single"
    else:
        return {
            "expr": "",
            "members": members,
            "kind": "unsupported",
            "unresolved": unresolved,
            "fiscal_members": fiscal,
        }

    return {
        "expr": expr,
        "members": members,
        "kind": kind,
        "unresolved": [],
        "fiscal_members": fiscal,
    }
