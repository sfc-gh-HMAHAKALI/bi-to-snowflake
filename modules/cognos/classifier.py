"""Cognos expression classification.

Three tiers, matching the other adapters:

* ``simple`` -- direct translation to a Snowflake Semantic View expression.
* ``needs_translation`` -- convertible with function mapping or a rewrite.
* ``manual_required`` -- carries semantics Snowflake has no direct equivalent
  for; needs a human decision.

Cognos is classified differently from the SQL-dialect sources (Denodo,
BusinessObjects) because most of its hard cases are not function-level at all.
They are *dimensional*: a calculation that references an OLAP member
(``->[all].[Week To Date]``) or a relative-time dimension is not expressible as
a scalar SQL expression, however the functions inside it are mapped. Those are
the expressions that drive the real conversion work, so they are detected
structurally rather than by scanning a function blacklist.
"""

from __future__ import annotations

import re
from typing import Any

from ..common.logger import get_logger

log = get_logger(__name__)

SIMPLE = "simple"
NEEDS_TRANSLATION = "needs_translation"
MANUAL_REQUIRED = "manual_required"

# ---------------------------------------------------------------------------
# Structural markers
# ---------------------------------------------------------------------------

# OLAP member reference: "->[all].[Week To Date]". This is a member of a
# dimension, not a column, so no scalar rewrite exists. It must become either a
# metric filter or a time-window CASE in the target semantic view.
_MEMBER_REF_RE = re.compile(r"->\s*\[")

# Cognos OLAP / dimensional functions with no scalar SQL equivalent.
_OLAP_FUNCTIONS: frozenset[str] = frozenset(
    {
        "aggregate", "currentmember", "completetuple", "tuple", "member",
        "memberset", "children", "descendants", "ancestor", "ancestors",
        "parent", "firstchild", "lastchild", "firstsibling", "lastsibling",
        "nextmember", "prevmember", "lag", "lead", "closingperiod",
        "openingperiod", "parallelperiod", "periodstodate", "ytd", "qtd",
        "mtd", "wtd", "hierarchize", "except", "intersect", "union",
        "filter", "order", "topcount", "bottomcount", "head", "tail",
        "item", "level", "levels", "linkmember", "roleValue", "rolevalue",
    }
)

# Cognos scalar functions that map onto a Snowflake equivalent. Presence of one
# of these is what makes an expression needs_translation rather than simple.
_TRANSLATABLE_FUNCTIONS: frozenset[str] = frozenset(
    {
        "to_char", "to_date", "to_number", "cast", "substring", "substr",
        "trim", "ltrim", "rtrim", "upper", "lower", "length", "char_length",
        "position", "concat", "coalesce", "nullif", "case", "decode",
        "abs", "ceil", "ceiling", "floor", "round", "trunc", "mod", "power",
        "sqrt", "exp", "ln", "log", "sign",
        "extract", "_add_days", "_add_months", "_add_years", "_days_between",
        "_months_between", "_years_between", "_day_of_week", "_day_of_year",
        "_week_of_year", "_first_of_month", "_last_of_month", "_make_timestamp",
        "current_date", "current_timestamp", "sysdate",
        "sum", "avg", "count", "min", "max", "stddev", "variance",
        "rank", "row_number", "dense_rank", "percentile",
    }
)

# Cognos macro syntax: #prompt('x')#, #sq($var)#. These resolve at runtime
# against a prompt or a session parameter, so they cannot be converted without
# knowing the intended binding.
_MACRO_RE = re.compile(r"#[^#]+#")
_PROMPT_RE = re.compile(r"\b(prompt|promptmany)\s*\(", re.IGNORECASE)

_FUNC_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _functions_used(expression: str) -> set[str]:
    """Lowercased function names called in an expression."""
    return {m.group(1).lower() for m in _FUNC_CALL_RE.finditer(expression or "")}


def classify_cognos_expression(expression: str) -> str:
    """Classify a single Cognos expression body.

    Empty expressions are ``simple``: a query item with no expression is a plain
    physical column passthrough, which is the easiest case, not an unknown one.
    """
    expr = (expression or "").strip()
    if not expr:
        return SIMPLE

    # Runtime-bound macros and prompts cannot be resolved statically.
    if _MACRO_RE.search(expr) or _PROMPT_RE.search(expr):
        return MANUAL_REQUIRED

    # Dimensional member references have no scalar equivalent.
    if _MEMBER_REF_RE.search(expr):
        return MANUAL_REQUIRED

    funcs = _functions_used(expr)
    if funcs & _OLAP_FUNCTIONS:
        return MANUAL_REQUIRED

    if funcs & _TRANSLATABLE_FUNCTIONS:
        return NEEDS_TRANSLATION

    # A bare arithmetic combination of column references converts directly.
    if funcs:
        # Unrecognised function: safer to flag than to assume it maps.
        return NEEDS_TRANSLATION

    return SIMPLE


def classify_cognos_complexity(item: Any) -> str:
    """Classify a parsed item dict (or a raw expression string).

    Accepts the same shapes the other adapters' classifiers accept, so the CLI
    router can call it uniformly.
    """
    if isinstance(item, str):
        return classify_cognos_expression(item)
    if isinstance(item, dict):
        return classify_cognos_expression(
            item.get("expression", "") or item.get("formula", "")
        )
    return SIMPLE


def classify_query_subject(qs: dict) -> str:
    """Classify a whole query subject by how cleanly it converts.

    A ``SELECT * FROM <table>`` passthrough is simple regardless of what its
    items do -- the items are classified separately. Embedded SQL logic in the
    query subject itself is what makes the entity hard, because the
    transformation lives in the BI layer and has to be rebuilt.
    """
    if qs.get("kind") == "db" and not qs.get("is_passthrough"):
        sql = (qs.get("sql") or "").upper()
        if any(k in sql for k in (" JOIN ", " UNION ", "CASE ", " WITH ")):
            return MANUAL_REQUIRED
        return NEEDS_TRANSLATION
    if qs.get("kind") == "model" and not qs.get("items"):
        return NEEDS_TRANSLATION
    return SIMPLE


def summarize(items: list[dict]) -> dict[str, int]:
    """Tally classifications across a list of parsed items."""
    out = {SIMPLE: 0, NEEDS_TRANSLATION: 0, MANUAL_REQUIRED: 0}
    for i in items:
        out[classify_cognos_complexity(i)] += 1
    return out
