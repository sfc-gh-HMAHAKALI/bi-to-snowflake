"""Denodo column classification and view complexity classification.

Column classification uses a 5-priority chain:
  1. User override (explicit {col_name: 'dimension'|'metric'} map)
  2. Catalog metadata (tags + free-text description)
  3. Aggregation function context (column is output of SUM/AVG/COUNT/etc.)
  4. Naming convention patterns (DIMENSION_PATTERNS / METRIC_PATTERNS)
  5. Data type fallback (numeric types → metric; string/bool → dimension)

View complexity is classified as one of three tiers:
  - 'simple'             — direct translation to Snowflake Semantic View
  - 'needs_translation'  — automated conversion possible with function mapping
  - 'manual_required'    — requires human review; cannot be auto-converted

All public functions log entry (parameters) and exit (result summary), catch
per-item exceptions, and never let a single failure abort the remaining items.
"""

from __future__ import annotations

import re
from typing import Any

from ..common.logger import get_logger
from ..common.errors import ClassificationError, fail_step

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Classification pattern constants (exported for caller reference/override)
# ---------------------------------------------------------------------------

DIMENSION_PATTERNS: list[str] = [
    r"(?i)_id$", r"(?i)_key$", r"(?i)_code$", r"(?i)_name$", r"(?i)_type$",
    r"(?i)_status$", r"(?i)_flag$", r"(?i)_category$", r"(?i)_desc$",
    r"(?i)_region$", r"(?i)_country$", r"(?i)_city$", r"(?i)_state$",
    r"(?i)_date$", r"(?i)_year$", r"(?i)_month$", r"(?i)_quarter$",
    r"(?i)_email$", r"(?i)_phone$", r"(?i)_address$", r"(?i)_zip$",
    r"(?i)^is_", r"(?i)^has_", r"(?i)^flag_",
]

METRIC_PATTERNS: list[str] = [
    r"(?i)_amount$", r"(?i)_total$", r"(?i)_sum$", r"(?i)_count$",
    r"(?i)_qty$", r"(?i)_quantity$", r"(?i)_price$", r"(?i)_cost$",
    r"(?i)_rate$", r"(?i)_ratio$", r"(?i)_pct$", r"(?i)_percent$",
    r"(?i)_score$", r"(?i)_weight$", r"(?i)_revenue$", r"(?i)_balance$",
    r"(?i)_avg$", r"(?i)_min$", r"(?i)_max$", r"(?i)_hours$", r"(?i)_days$",
]

# Pre-compiled for performance.
_DIMENSION_RES = [re.compile(p) for p in DIMENSION_PATTERNS]
_METRIC_RES = [re.compile(p) for p in METRIC_PATTERNS]

# ---------------------------------------------------------------------------
# VQL-specific functions (not in standard SQL / Snowflake)
# ---------------------------------------------------------------------------

VQL_SPECIFIC_FUNCTIONS: frozenset[str] = frozenset(
    {
        # Date / time arithmetic
        "ADDDAY", "ADDMINUTE", "ADDSECOND", "ADDHOUR",
        "ADDMONTH", "ADDWEEK", "ADDYEAR",
        # Date / time extraction
        "DATEPARTDATE", "GETDAY", "GETDAYOFWEEK", "GETDAYOFYEAR",
        "GETDAYSBETWEEN", "GETMONTH", "GETYEAR", "GETHOUR",
        "GETMINUTE", "GETSECOND", "GETQUARTER", "GETWEEK",
        # Date / time diff
        "DIFFMONTH", "DIFFDAY", "DIFFSECOND", "DIFFMINUTE",
        "DIFFHOUR", "DIFFINBUSINESSDAYS",
        # Date formatting / truncation
        "FIRSTDAYOFMONTH", "LASTDAYOFMONTH", "FORMATDATE", "TRUNC",
        # String
        "REGEXP_REPLACE", "ILIKE",
        # Null handling
        "NVL2", "MODULENULL",
        # Session / runtime context
        "CONTEXT", "GETSESSION", "GETUSERNAME",
        # Misc / Denodo-proprietary
        "HASHCODE", "SPLITQUERY",
        # Full-text / set containment
        "ISCONTAINED", "ISNOTCONTAINED", "CONTAINSAND", "CONTAINSOR",
        # Analytic
        "PREVIOUSVALUEOF",
    }
)

# Regex matching any VQL-specific function call (word boundary + open paren).
_VQL_FUNC_RE = re.compile(
    r"(?i)\b("
    + "|".join(re.escape(f) for f in sorted(VQL_SPECIFIC_FUNCTIONS))
    + r")\s*\("
)

# ---------------------------------------------------------------------------
# Complexity detection regexes
# ---------------------------------------------------------------------------

# CAST with a non-standard (non-Snowflake) target type.
_CAST_RE = re.compile(r"(?i)\bCAST\s*\(.*?AS\s+(\w+)", re.DOTALL)
_NON_STANDARD_CAST_TYPES: frozenset[str] = frozenset(
    {
        "LOCALDATE", "LOCALDATETIME", "LOCALTIME",
        "INTERVAL", "ARRAY", "STRUCT", "ROW",
        "BLOB", "CLOB", "NCLOB", "LONGNVARCHAR", "BFILE", "XMLTYPE",
    }
)

_CONNECT_BY_RE = re.compile(r"(?i)\bCONNECT\s+BY\b")
_TABLE_FUNC_RE = re.compile(r"(?i)\b(XMLTABLE|JSON_TABLE)\s*\(")
_LATERAL_RE = re.compile(r"(?i)\b(LATERAL|CROSS\s+APPLY|OUTER\s+APPLY)\b")
_JAVA_FUNC_RE = re.compile(r"(?i)\bcustom_function\b|\.java\b|\bjava:")
_MATERIALIZED_REFRESH_RE = re.compile(r"(?i)\bSCHEDULE\b|\bREFRESH\b")

# ---------------------------------------------------------------------------
# Data type sets for fallback classification (priority 5)
# ---------------------------------------------------------------------------

_NUMERIC_TYPES: frozenset[str] = frozenset(
    {
        "INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT",
        "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC", "REAL",
        "NUMBER", "MONEY", "CURRENCY",
    }
)
_STRING_TYPES: frozenset[str] = frozenset(
    {"VARCHAR", "CHAR", "NVARCHAR", "TEXT", "STRING", "BOOLEAN", "BOOL", "BIT"}
)


# ---------------------------------------------------------------------------
# Column classification
# ---------------------------------------------------------------------------


def classify_column_by_name(col_name: str) -> str:
    """Classify a column as 'dimension' or 'metric' from its name alone.

    Checks DIMENSION_PATTERNS first; on a match returns 'dimension'.
    Then checks METRIC_PATTERNS; on a match returns 'metric'.
    Defaults to 'dimension' when neither pattern matches (most columns
    are descriptive attributes).

    Args:
        col_name: Column name string.

    Returns:
        'dimension' or 'metric'.
    """
    log.debug("classify_column_by_name: col=%s", col_name)
    for pattern in _DIMENSION_RES:
        if pattern.search(col_name):
            return "dimension"
    for pattern in _METRIC_RES:
        if pattern.search(col_name):
            return "metric"
    return "dimension"


def classify_with_catalog(
    col_name: str,
    catalog_tags: dict,
    catalog_desc: str,
) -> str:
    """Classify using catalog metadata tags and free-text description.

    Scans tag keys/values and the description for known classification
    keywords, then falls back to name-based classification.

    Args:
        col_name: Column name.
        catalog_tags: Dict of {tag_name: tag_value} from the data catalog.
        catalog_desc: Free-text column description from the catalog.

    Returns:
        'dimension' or 'metric'.
    """
    log.debug(
        "classify_with_catalog: col=%s, tag_count=%d", col_name, len(catalog_tags or {})
    )

    for tag_key, tag_value in (catalog_tags or {}).items():
        combined = f"{tag_key} {tag_value}".lower()
        if any(kw in combined for kw in ("metric", "measure", "kpi", "fact")):
            return "metric"
        if any(
            kw in combined
            for kw in ("dimension", "attribute", "descriptor", "category")
        ):
            return "dimension"

    if catalog_desc:
        desc_lower = catalog_desc.lower()
        if any(
            kw in desc_lower
            for kw in ("metric", "measure", "total", "count", "sum", "kpi")
        ):
            return "metric"
        if any(
            kw in desc_lower
            for kw in ("identifier", "code", "name", "category", "type", "status")
        ):
            return "dimension"

    return classify_column_by_name(col_name)


def apply_override(col_name: str, overrides: dict) -> str | None:
    """Look up *col_name* in the user-supplied override dict.

    Comparison is case-insensitive.

    Args:
        col_name: Column name.
        overrides: Dict mapping column names to 'dimension' or 'metric'.

    Returns:
        The override string, or None if the column is not in *overrides*.
    """
    if not overrides:
        return None
    lower_name = col_name.lower()
    for key, value in overrides.items():
        if key.lower() == lower_name:
            log.debug("apply_override: %s → %s", col_name, value)
            return value
    return None


def classify_column(
    col_name: str,
    overrides: dict | None = None,
    catalog_meta: dict | None = None,
    agg_context: bool = False,
    data_type: str | None = None,
) -> str:
    """Classify a column using a 5-priority chain.

    Priority order (highest to lowest):
      1. User override  — explicit {col_name: 'dimension'|'metric'} map.
      2. Catalog metadata — tags + description from the source catalog.
      3. Aggregation context — column is the output of SUM/AVG/COUNT/etc.
      4. Naming conventions — DIMENSION_PATTERNS / METRIC_PATTERNS.
      5. Data type fallback — numeric → metric; string/bool → dimension.

    Args:
        col_name: Column name.
        overrides: Optional dict of explicit classifications.
        catalog_meta: Optional dict with keys 'tags' (dict) and
            'description' (str).
        agg_context: True when this column is produced by an aggregation
            function (always a metric regardless of name).
        data_type: Optional SQL type string (e.g. 'INTEGER', 'VARCHAR(255)').

    Returns:
        'dimension' or 'metric'.
    """
    log.debug(
        "classify_column: col=%s, agg_context=%s, data_type=%s",
        col_name, agg_context, data_type,
    )

    # Priority 1: user override
    if overrides:
        result = apply_override(col_name, overrides)
        if result is not None:
            return result

    # Priority 2: catalog metadata
    if catalog_meta:
        tags = catalog_meta.get("tags", {})
        desc = catalog_meta.get("description", "")
        return classify_with_catalog(col_name, tags, desc)

    # Priority 3: aggregation function context
    if agg_context:
        return "metric"

    # Priority 4: naming conventions
    name_result = classify_column_by_name(col_name)

    # Priority 5: data type fallback (only when naming defaults to 'dimension')
    if data_type:
        dtype_upper = data_type.upper().split("(")[0].strip()
        if dtype_upper in _NUMERIC_TYPES:
            # Trust numeric type unless name is explicitly a dimension key.
            dim_hit = any(p.search(col_name) for p in _DIMENSION_RES)
            if not dim_hit:
                return "metric"
        elif dtype_upper in _STRING_TYPES:
            return "dimension"

    return name_result


# ---------------------------------------------------------------------------
# View complexity classification
# ---------------------------------------------------------------------------


def _count_case_nesting_depth(sql: str) -> int:
    """Return the maximum CASE...END nesting depth in *sql*.

    Skips occurrences inside single- and double-quoted strings.
    """
    depth = max_depth = 0
    in_single = in_double = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            upper4 = sql[i : i + 4].upper()
            upper3 = sql[i : i + 3].upper()
            pre_ok = i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_")
            if upper4 == "CASE" and pre_ok:
                post = sql[i + 4] if i + 4 < len(sql) else " "
                if not (post.isalnum() or post == "_"):
                    depth += 1
                    max_depth = max(max_depth, depth)
                    i += 4
                    continue
            elif upper3 == "END" and pre_ok:
                post = sql[i + 3] if i + 3 < len(sql) else " "
                if not (post.isalnum() or post == "_"):
                    depth = max(0, depth - 1)
                    i += 3
                    continue
        i += 1
    return max_depth


def _count_subquery_depth(sql: str) -> int:
    """Estimate the maximum depth of nested subqueries (SELECT inside parens)."""
    depth = sq_depth = max_sq = 0
    in_single = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'" and not in_single:
            in_single = True
        elif ch == "'" and in_single:
            in_single = False
        elif not in_single:
            if ch == "(":
                depth += 1
                # Peek ahead for SELECT keyword (skipping whitespace).
                rest = sql[i + 1 :].lstrip()
                if rest[:6].upper() == "SELECT":
                    sq_depth += 1
                    max_sq = max(max_sq, sq_depth)
            elif ch == ")":
                depth = max(0, depth - 1)
                if sq_depth > 0 and depth < sq_depth:
                    sq_depth -= 1
        i += 1
    return max_sq


def _has_vql_functions(sql: str) -> bool:
    """Return True if *sql* contains any VQL-specific function calls."""
    return bool(_VQL_FUNC_RE.search(sql))


def _has_non_standard_cast(sql: str) -> bool:
    """Return True if *sql* contains CAST with a non-Snowflake-native type."""
    for m in _CAST_RE.finditer(sql):
        if m.group(1).upper() in _NON_STANDARD_CAST_TYPES:
            return True
    return False


def classify_denodo_complexity(view: dict) -> str:
    """Classify a Denodo view dict into a migration complexity tier.

    Evaluation order (first match wins):

    **manual_required**
      - MATERIALIZED views with SCHEDULE/REFRESH options
      - 5 or more JOIN clauses
      - Subquery nesting depth >= 3
      - Java / custom function references
      - CONNECT BY hierarchical queries
      - XMLTABLE or JSON_TABLE
      - LATERAL / CROSS APPLY / OUTER APPLY

    **needs_translation**
      - VQL-specific date/string/session functions (35+)
      - CASE expressions with nesting depth >= 3
      - CAST with non-standard (non-Snowflake) target types

    **simple** (default)
      - INTERFACE views (always simple — they are contracts, not SQL)
      - Standard SQL SELECTs with direct column refs, aggregations,
        basic WHERE / GROUP BY

    Args:
        view: View dict as produced by :func:`~.vql_parser.parse_vql_file`.

    Returns:
        'simple', 'needs_translation', or 'manual_required'.
    """
    view_name = view.get("name", "?")
    log.debug("classify_denodo_complexity: view=%s", view_name)

    sql = view.get("full_sql", "")
    view_type = view.get("type", "DERIVED")
    joins = view.get("joins", [])

    # INTERFACE views are pure contracts — always translatable as-is.
    if view_type == "INTERFACE":
        log.debug("classify_denodo_complexity: %s → simple (INTERFACE)", view_name)
        return "simple"

    # --- manual_required checks ---

    if view_type == "MATERIALIZED" and _MATERIALIZED_REFRESH_RE.search(sql):
        log.debug(
            "classify_denodo_complexity: %s → manual_required (MATERIALIZED+refresh)",
            view_name,
        )
        return "manual_required"

    if len(joins) >= 5:
        log.debug(
            "classify_denodo_complexity: %s → manual_required (%d joins)",
            view_name, len(joins),
        )
        return "manual_required"

    if _count_subquery_depth(sql) >= 3:
        log.debug(
            "classify_denodo_complexity: %s → manual_required (subquery depth >= 3)",
            view_name,
        )
        return "manual_required"

    if _JAVA_FUNC_RE.search(sql):
        log.debug(
            "classify_denodo_complexity: %s → manual_required (Java/custom function)",
            view_name,
        )
        return "manual_required"

    if _CONNECT_BY_RE.search(sql):
        log.debug(
            "classify_denodo_complexity: %s → manual_required (CONNECT BY)",
            view_name,
        )
        return "manual_required"

    if _TABLE_FUNC_RE.search(sql):
        log.debug(
            "classify_denodo_complexity: %s → manual_required (XMLTABLE/JSON_TABLE)",
            view_name,
        )
        return "manual_required"

    if _LATERAL_RE.search(sql):
        log.debug(
            "classify_denodo_complexity: %s → manual_required (LATERAL/CROSS APPLY)",
            view_name,
        )
        return "manual_required"

    # --- needs_translation checks ---

    if _has_vql_functions(sql):
        log.debug(
            "classify_denodo_complexity: %s → needs_translation (VQL-specific functions)",
            view_name,
        )
        return "needs_translation"

    if _count_case_nesting_depth(sql) >= 3:
        log.debug(
            "classify_denodo_complexity: %s → needs_translation (CASE nesting >= 3)",
            view_name,
        )
        return "needs_translation"

    if _has_non_standard_cast(sql):
        log.debug(
            "classify_denodo_complexity: %s → needs_translation (non-standard CAST)",
            view_name,
        )
        return "needs_translation"

    log.debug("classify_denodo_complexity: %s → simple", view_name)
    return "simple"


# ---------------------------------------------------------------------------
# Inventory builder
# ---------------------------------------------------------------------------


def build_view_inventory(
    views: list[dict],
    overrides: dict | None = None,
) -> dict:
    """Build a full inventory of views with column classifications and complexity.

    Per-view and per-column errors are captured in place; failures never abort
    processing of remaining views or columns.

    Args:
        views: List of view dicts from :func:`~.vql_parser.parse_vql_file`.
        overrides: Optional {col_name: 'dimension'|'metric'} classification map.

    Returns:
        Dict with keys:
          total_views (int), simple_count (int),
          needs_translation_count (int), manual_required_count (int),
          views (list) — each view enriched with 'complexity' (str) and
          'classified_columns' (list of col dicts with 'classification' key).
    """
    log.info("build_view_inventory: entering, view_count=%d", len(views))

    inventory: dict = {
        "total_views": len(views),
        "simple_count": 0,
        "needs_translation_count": 0,
        "manual_required_count": 0,
        "views": [],
    }

    for view in views:
        view_name = view.get("name", "unknown")
        enriched = dict(view)

        # Complexity classification
        try:
            complexity = classify_denodo_complexity(view)
        except Exception as exc:  # noqa: BLE001
            record = fail_step("build_view_inventory.classify_denodo_complexity", exc)
            complexity = "manual_required"  # safe fallback
            enriched.setdefault("errors", []).append(record["error_message"])

        enriched["complexity"] = complexity
        count_key = f"{complexity}_count"
        inventory[count_key] = inventory.get(count_key, 0) + 1

        # Build a set of aliases that are aggregation outputs for fast lookup.
        agg_aliases: set[str] = {
            a["alias"]
            for a in view.get("aggregations", [])
            if a.get("alias")
        }

        # Column classification
        classified: list[dict] = []
        for col in view.get("select_columns", []):
            col_alias = col.get("alias") or col.get("expression", "unknown")
            agg_ctx = col_alias in agg_aliases
            try:
                classification = classify_column(
                    col_alias,
                    overrides=overrides,
                    agg_context=agg_ctx,
                )
            except Exception as exc:  # noqa: BLE001
                record = fail_step("build_view_inventory.classify_column", exc)
                classification = "dimension"  # safe fallback
                enriched.setdefault("errors", []).append(record["error_message"])

            classified.append({**col, "classification": classification})

        enriched["classified_columns"] = classified
        inventory["views"].append(enriched)

        log.debug(
            "build_view_inventory: %s → complexity=%s, cols=%d",
            view_name, complexity, len(classified),
        )

    log.info(
        "build_view_inventory: complete — simple=%d, needs_translation=%d, manual=%d",
        inventory["simple_count"],
        inventory["needs_translation_count"],
        inventory["manual_required_count"],
    )
    return inventory


def print_inventory_summary(inventory: dict) -> str:
    """Format a view inventory as a human-readable summary string.

    Args:
        inventory: Dict as returned by :func:`build_view_inventory`.

    Returns:
        Multi-line summary string ready to print or log.
    """
    log.debug("print_inventory_summary: entering")

    total = inventory.get("total_views", 0)
    simple = inventory.get("simple_count", 0)
    needs_tx = inventory.get("needs_translation_count", 0)
    manual = inventory.get("manual_required_count", 0)

    lines = [
        "=" * 60,
        "Denodo VQL View Inventory Summary",
        "=" * 60,
        f"Total views:          {total}",
        f"  Simple:             {simple}  ({_pct(simple, total)}%)",
        f"  Needs translation:  {needs_tx}  ({_pct(needs_tx, total)}%)",
        f"  Manual required:    {manual}  ({_pct(manual, total)}%)",
        "",
    ]

    if inventory.get("views"):
        lines.append("Views by complexity:")
        for view in inventory["views"]:
            name = view.get("name", "unknown")
            complexity = view.get("complexity", "unknown")
            n_cols = len(view.get("classified_columns", []))
            n_joins = len(view.get("joins", []))
            n_errors = len(view.get("errors", []))
            error_flag = "  [ERRORS]" if n_errors else ""
            lines.append(
                f"  [{complexity:<20}] {name}"
                f"  (cols={n_cols}, joins={n_joins}){error_flag}"
            )

    lines.append("=" * 60)
    summary = "\n".join(lines)
    log.debug("print_inventory_summary: %d chars", len(summary))
    return summary


def _pct(part: int, total: int) -> str:
    """Format *part*/*total* as a percentage string."""
    if total == 0:
        return "0.0"
    return f"{100.0 * part / total:.1f}"
