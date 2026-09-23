"""Classify SAP Business Objects expressions by migration complexity.

Three complexity tiers:
- **simple**: Direct column refs, standard aggregations, Snowflake-compatible
  scalar functions — can be used as-is in a Snowflake Semantic View.
- **needs_translation**: Single-level @Functions, Oracle-specific syntax with
  known equivalents, BO-specific date functions — can be auto-translated with
  moderate confidence.
- **manual_required**: @Derived_Table, @Script, @Aggregate_Aware, deeply nested
  @Select, platform-specific constructs without direct Snowflake equivalents —
  require human review.

All compiled regex patterns are module-level singletons for performance.
"""

import re
from typing import Any

from ..common.errors import ClassificationError, fail_step
from ..common.logger import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Compiled regex patterns (module-level singletons)
# ---------------------------------------------------------------------------

# Nested @Select: @Select containing another @Select before the closing paren
BO_AT_NESTED = re.compile(
    r"@Select\s*\(.*?@Select",
    re.IGNORECASE | re.DOTALL,
)

# Any single-level @Function usage
BO_AT_SIMPLE = re.compile(
    r"@(Select|Where|Variable|Prompt|Aggregate_Aware|Derived_Table|Script)\s*\(",
    re.IGNORECASE,
)

# Standard ANSI/Snowflake aggregations — no @Functions required
BO_STANDARD_AGG = re.compile(
    r"\b(SUM|COUNT|AVG|MIN|MAX|COUNT\s*DISTINCT)\s*\(",
    re.IGNORECASE,
)

# Snowflake-compatible scalar functions that need no translation
BO_SNOWFLAKE_COMPAT = re.compile(
    r"\b(COALESCE|NVL|UPPER|LOWER|TRIM|SUBSTRING|CAST|CONVERT|ROUND|ABS|CEIL"
    r"|FLOOR|CONCAT|LENGTH|REPLACE|LEFT|RIGHT|LPAD|RPAD|CASE|DECODE|TO_DATE"
    r"|TO_CHAR|TO_NUMBER)\s*\(",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# manual_required patterns
# ---------------------------------------------------------------------------

# @Aggregate_Aware — multi-source aggregation, needs human choice
_RE_AGGREGATE_AWARE = re.compile(r"@Aggregate_Aware\s*\(", re.IGNORECASE)

# @Derived_Table — inline derived table reference
_RE_DERIVED_TABLE = re.compile(r"@Derived_Table\s*\(", re.IGNORECASE)

# @Script — custom JavaScript/VBA
_RE_SCRIPT = re.compile(r"@Script\s*\(", re.IGNORECASE)

# Oracle DECODE with 7+ branches (3 pairs + 1 default = 7 args)
_RE_DECODE_HEAVY = re.compile(r"\bDECODE\s*\(", re.IGNORECASE)

# Subquery depth: second SELECT inside a subquery (2+ levels)
_RE_NESTED_SUBQUERY = re.compile(
    r"\bSELECT\b.*?\bSELECT\b",
    re.IGNORECASE | re.DOTALL,
)

# Hierarchical queries
_RE_CONNECT_BY = re.compile(r"\bCONNECT\s+BY\b|\bPRIOR\b", re.IGNORECASE)

# Aggregation / XML functions with no Snowflake equivalent or complex semantics
_RE_SPECIAL_AGG = re.compile(
    r"\b(LISTAGG|XMLAGG|WM_CONCAT|COLLECT|CORR_K|CORR_S)\s*\(",
    re.IGNORECASE,
)

# PIVOT / UNPIVOT
_RE_PIVOT = re.compile(r"\b(PIVOT|UNPIVOT)\s*\(", re.IGNORECASE)

# Oracle-specific builtins with no clean Snowflake mapping
_RE_ORACLE_PLATFORM = re.compile(
    r"\b(ROWNUM|ROWID|SYSDATE\b|SYSTIMESTAMP\b|DBMS_\w+|UTL_\w+|USERENV\s*\(|SYS_CONTEXT\s*\()",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# needs_translation patterns
# ---------------------------------------------------------------------------

# NVL2 — Snowflake does not have NVL2 natively (use IFF/CASE)
_RE_NVL2 = re.compile(r"\bNVL2\s*\(", re.IGNORECASE)

# CASE nesting 3+ levels (approximate: 3+ WHEN inside a CASE)
_RE_DEEP_CASE = re.compile(
    r"\bCASE\b(?:(?!\bEND\b).)*\bCASE\b(?:(?!\bEND\b).)*\bCASE\b",
    re.IGNORECASE | re.DOTALL,
)

# BO-specific date functions
_RE_BO_DATE_FUNCS = re.compile(
    r"\b(RelativeDate|ToDate|DatesBetween|DaysBetween|LastDayOfMonth"
    r"|MonthsBetween|YearsBetween|WeekNumberOfYear|DayOfWeek)\s*\(",
    re.IGNORECASE,
)

# TRUNC with a date format string (second arg) — different from TRUNC(number)
_RE_TRUNC_DATE = re.compile(r"\bTRUNC\s*\([^,)]+,\s*'[^']+'\s*\)", re.IGNORECASE)

# TO_CHAR / TO_DATE with Oracle format strings (non-Snowflake format masks)
_RE_ORACLE_FORMAT = re.compile(
    r"\b(TO_CHAR|TO_DATE)\s*\([^,)]+,\s*'[^']*(?:DD|MM|YYYY|HH24|MI|SS)[^']*'\s*\)",
    re.IGNORECASE,
)

# Oracle outer join (+) syntax
_RE_ORACLE_OUTER_JOIN = re.compile(r"\(\s*\+\s*\)")

# SUBSTR — Oracle uses SUBSTR; Snowflake prefers SUBSTRING but supports both
_RE_SUBSTR = re.compile(r"\bSUBSTR\s*\(", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Helper: count DECODE arguments
# ---------------------------------------------------------------------------

def _count_decode_args(expression: str) -> int:
    """Count the number of arguments in the first DECODE call."""
    m = _RE_DECODE_HEAVY.search(expression)
    if not m:
        return 0
    start = m.end()
    depth = 1
    idx = start
    args = 1
    while idx < len(expression) and depth > 0:
        ch = expression[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        elif ch == "," and depth == 1:
            args += 1
        idx += 1
    return args


# ---------------------------------------------------------------------------
# classify_bo_complexity
# ---------------------------------------------------------------------------

def classify_bo_complexity(
    expression: str,
    select_expr: str | None = None,
) -> str:
    """Classify a BO object expression's migration complexity.

    Args:
        expression: The WHERE expression (or combined expression).
        select_expr: The @Select expression, if separate. If None, ``expression``
                     is treated as the full expression.

    Returns:
        ``'simple'``, ``'needs_translation'``, or ``'manual_required'``.
    """
    log.debug("classify_bo_complexity: expression=%.100s", expression)

    combined = expression
    if select_expr:
        combined = f"{select_expr} {expression}"

    # ---- manual_required checks (short-circuit on first match) --------------

    # @Aggregate_Aware
    if _RE_AGGREGATE_AWARE.search(combined):
        log.debug("classify_bo_complexity: manual_required — @Aggregate_Aware")
        return "manual_required"

    # @Derived_Table
    if _RE_DERIVED_TABLE.search(combined):
        log.debug("classify_bo_complexity: manual_required — @Derived_Table")
        return "manual_required"

    # @Script
    if _RE_SCRIPT.search(combined):
        log.debug("classify_bo_complexity: manual_required — @Script")
        return "manual_required"

    # Nested @Select (2+ levels)
    if BO_AT_NESTED.search(combined):
        log.debug("classify_bo_complexity: manual_required — nested @Select")
        return "manual_required"

    # DECODE with 7+ args
    decode_args = _count_decode_args(combined)
    if decode_args >= 7:
        log.debug(
            "classify_bo_complexity: manual_required — DECODE with %d args",
            decode_args,
        )
        return "manual_required"

    # Nested subqueries (2+ SELECT levels)
    if _RE_NESTED_SUBQUERY.search(combined):
        log.debug("classify_bo_complexity: manual_required — nested subquery")
        return "manual_required"

    # Hierarchical queries
    if _RE_CONNECT_BY.search(combined):
        log.debug("classify_bo_complexity: manual_required — CONNECT BY / PRIOR")
        return "manual_required"

    # LISTAGG, XMLAGG, etc.
    if _RE_SPECIAL_AGG.search(combined):
        log.debug("classify_bo_complexity: manual_required — special aggregation")
        return "manual_required"

    # PIVOT / UNPIVOT
    if _RE_PIVOT.search(combined):
        log.debug("classify_bo_complexity: manual_required — PIVOT/UNPIVOT")
        return "manual_required"

    # Oracle platform-specific
    if _RE_ORACLE_PLATFORM.search(combined):
        log.debug("classify_bo_complexity: manual_required — Oracle platform builtin")
        return "manual_required"

    # ---- needs_translation checks -------------------------------------------

    # Simple @Select / @Where / @Variable (single level — nested already caught)
    if BO_AT_SIMPLE.search(combined):
        log.debug("classify_bo_complexity: needs_translation — @Function present")
        return "needs_translation"

    # NVL2
    if _RE_NVL2.search(combined):
        log.debug("classify_bo_complexity: needs_translation — NVL2")
        return "needs_translation"

    # CASE 3+ nesting
    if _RE_DEEP_CASE.search(combined):
        log.debug("classify_bo_complexity: needs_translation — deep CASE nesting")
        return "needs_translation"

    # BO date functions
    if _RE_BO_DATE_FUNCS.search(combined):
        log.debug("classify_bo_complexity: needs_translation — BO date function")
        return "needs_translation"

    # TRUNC with format string
    if _RE_TRUNC_DATE.search(combined):
        log.debug("classify_bo_complexity: needs_translation — TRUNC date format")
        return "needs_translation"

    # TO_CHAR/TO_DATE with Oracle format masks
    if _RE_ORACLE_FORMAT.search(combined):
        log.debug("classify_bo_complexity: needs_translation — Oracle format mask")
        return "needs_translation"

    # Oracle outer join (+)
    if _RE_ORACLE_OUTER_JOIN.search(combined):
        log.debug("classify_bo_complexity: needs_translation — Oracle outer join (+)")
        return "needs_translation"

    # SUBSTR → SUBSTRING rename
    if _RE_SUBSTR.search(combined):
        log.debug("classify_bo_complexity: needs_translation — SUBSTR")
        return "needs_translation"

    # ---- simple -------------------------------------------------------------
    log.debug("classify_bo_complexity: simple")
    return "simple"


# ---------------------------------------------------------------------------
# classify_all_objects
# ---------------------------------------------------------------------------

def classify_all_objects(objects: list[dict]) -> dict[str, Any]:
    """Classify every BO object and return a summary with counts.

    Args:
        objects: List of normalized object dicts (from :func:`extract_bo_inventory`).

    Returns:
        .. code-block:: python

            {
                "objects": [
                    {**original_object, "complexity": str}
                    ...
                ],
                "counts": {
                    "simple": int,
                    "needs_translation": int,
                    "manual_required": int,
                    "total": int,
                },
                "errors": [...]
            }
    """
    log.info("classify_all_objects: classifying %d objects", len(objects))

    counts: dict[str, int] = {
        "simple": 0,
        "needs_translation": 0,
        "manual_required": 0,
    }
    classified: list[dict] = []
    errors: list[dict] = []

    for obj in objects:
        try:
            complexity = classify_bo_complexity(
                expression=obj.get("where_expression") or "",
                select_expr=obj.get("select_expression") or "",
            )
            entry = dict(obj)
            entry["complexity"] = complexity
            classified.append(entry)
            counts[complexity] = counts.get(complexity, 0) + 1
        except Exception as exc:
            errors.append(
                fail_step(
                    "classify_object",
                    exc,
                    partial_results=obj.get("name"),
                )
            )
            # Still include the object, marked unknown
            entry = dict(obj)
            entry["complexity"] = "unknown"
            classified.append(entry)

    counts["total"] = sum(counts.values())

    log.info(
        "classify_all_objects: done — simple=%d needs_translation=%d "
        "manual_required=%d total=%d errors=%d",
        counts["simple"],
        counts["needs_translation"],
        counts["manual_required"],
        counts["total"],
        len(errors),
    )

    return {
        "objects": classified,
        "counts": counts,
        "errors": errors,
    }
