"""Denodo VQL export file parser.

Parses CREATE OR REPLACE VIEW statements from Denodo Virtual DataPort (VDP)
VQL export files into structured dicts for downstream semantic view conversion.

All public functions:
  - Log entry (parameters) and exit (result summary).
  - Catch per-item exceptions via fail_step so one bad block never aborts others.
  - Return partial results rather than raising when any single item fails.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..common.logger import get_logger
from ..common.errors import ParseError, fail_step

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Compiled regexes
# ---------------------------------------------------------------------------

# Top-level VIEW block splitter.
# Groups: (1) prefix, (2) optional type modifier, (3) view name, (4) AS body.
# Stops at the next CREATE OR REPLACE or end-of-string.
_CREATE_VIEW_RE = re.compile(
    r"(CREATE\s+OR\s+REPLACE\s+)(INTERFACE\s+|MATERIALIZED\s+)?VIEW\s+(\S+)\s+AS\s+"
    r"(.*?)(?=CREATE\s+OR\s+REPLACE|\Z)",
    re.DOTALL | re.IGNORECASE,
)

# JOIN header: captures type, right-side table, and optional alias.
_JOIN_HEADER_RE = re.compile(
    r"(?i)\b(INNER\s+JOIN|LEFT\s+OUTER\s+JOIN|RIGHT\s+OUTER\s+JOIN"
    r"|FULL\s+OUTER\s+JOIN|LEFT\s+JOIN|RIGHT\s+JOIN|CROSS\s+JOIN|JOIN)\s+"
    r"(\w+(?:\.\w+)*)"          # table name (possibly schema.table)
    r"(?:\s+(?:AS\s+)?(\w+))?", # optional alias
)

# Keywords that terminate an ON condition (lookahead anchors).
_NEXT_CLAUSE_RE = re.compile(
    r"(?i)\b(?:INNER\s+JOIN|LEFT\s+OUTER\s+JOIN|LEFT\s+JOIN|RIGHT\s+OUTER\s+JOIN"
    r"|RIGHT\s+JOIN|CROSS\s+JOIN|FULL\s+OUTER\s+JOIN|JOIN"
    r"|WHERE|GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT)\b"
)

# GROUP BY body — stops at HAVING / ORDER BY / LIMIT / set ops / end.
_GROUP_BY_RE = re.compile(
    r"(?i)\bGROUP\s+BY\s+(.*?)"
    r"(?=\s*(?:\bHAVING\b|\bORDER\s+BY\b|\bLIMIT\b|\bUNION\b|\bEXCEPT\b|\bINTERSECT\b|\Z))",
    re.DOTALL,
)

# ORDER BY body.
_ORDER_BY_RE = re.compile(
    r"(?i)\bORDER\s+BY\s+(.*?)"
    r"(?=\s*(?:\bLIMIT\b|\bUNION\b|\bEXCEPT\b|\bINTERSECT\b|\Z))",
    re.DOTALL,
)

# WHERE body — stops at GROUP BY / ORDER BY / HAVING / LIMIT / set ops / end.
_WHERE_RE = re.compile(
    r"(?i)\bWHERE\s+(.*?)"
    r"(?=\s*(?:\bGROUP\s+BY\b|\bORDER\s+BY\b|\bHAVING\b|\bLIMIT\b|\bUNION\b|\Z))",
    re.DOTALL,
)

# Aggregation functions: SUM/AVG/MIN/MAX/COUNT with optional DISTINCT.
_AGG_FUNC_RE = re.compile(
    r"(?i)\b(SUM|AVG|MIN|MAX|COUNT)\s*\(\s*(DISTINCT\s+)?(.*?)\s*\)"
    r"(?:\s+AS\s+(\w+))?",
)

# Explicit AS alias at the end of a column expression.
_EXPLICIT_ALIAS_RE = re.compile(r"(?i)\bAS\s+(\w+)\s*$")

# Qualified column reference: [schema.]table.col
_QUALIFIED_COL_RE = re.compile(r"^(?:\w+\.)*?(\w+)\.(\w+)$")


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _split_on_top_level_commas(text: str) -> list[str]:
    """Split *text* on commas that are not inside parentheses or quotes."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    in_single = False
    in_double = False

    for ch in text:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            elif ch == "," and depth == 0:
                part = "".join(current).strip()
                if part:
                    parts.append(part)
                current = []
                continue
        current.append(ch)

    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _find_keyword_boundary(sql: str, keyword: str, start: int = 0) -> int:
    """Return the index of *keyword* at paren/quote depth 0, or -1.

    Skips occurrences inside single-quoted strings, double-quoted identifiers,
    and parenthesised expressions (subqueries / function calls).
    """
    kw_upper = keyword.upper()
    kw_len = len(keyword)
    depth = 0
    in_single = False
    in_double = False
    i = start

    while i < len(sql):
        ch = sql[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            elif depth == 0 and sql[i : i + kw_len].upper() == kw_upper:
                pre = sql[i - 1] if i > 0 else " "
                post = sql[i + kw_len] if i + kw_len < len(sql) else " "
                if not (pre.isalnum() or pre == "_") and not (
                    post.isalnum() or post == "_"
                ):
                    return i
        i += 1
    return -1


def _extract_select_body(sql: str) -> str | None:
    """Return the text between the top-level SELECT and the top-level FROM."""
    sel_pos = _find_keyword_boundary(sql, "SELECT")
    if sel_pos == -1:
        return None
    from_pos = _find_keyword_boundary(sql, "FROM", sel_pos + 6)
    if from_pos == -1:
        return None
    return sql[sel_pos + 6 : from_pos].strip()


def _infer_alias(expression: str) -> str:
    """Best-effort alias when no AS clause is present."""
    expr = expression.strip()
    # Simple identifier or qualified name: [table.]col → col
    m = re.match(r"^(?:\w+\.)*(\w+)$", expr)
    if m:
        return m.group(1)
    # Function call: FUNC(...) → lowercase function name
    m = re.match(r"^(\w+)\s*\(", expr)
    if m:
        return m.group(1).lower()
    # Expression fallback: sanitise and truncate
    clean = re.sub(r"\W+", "_", expr).strip("_")
    return clean[:40] or "col"


def _parse_one_column(raw: str) -> dict:
    """Parse a single SELECT-list entry into {alias, expression, source_table}."""
    raw = raw.strip()

    as_match = _EXPLICIT_ALIAS_RE.search(raw)
    if as_match:
        alias = as_match.group(1)
        expression = raw[: as_match.start()].strip()
    else:
        alias = _infer_alias(raw)
        expression = raw

    source_table: str | None = None
    qual_match = _QUALIFIED_COL_RE.match(expression)
    if qual_match:
        source_table = qual_match.group(1)

    return {"alias": alias, "expression": expression, "source_table": source_table}


def _find_main_from_tables(sql: str) -> list[str]:
    """Extract table names from the FROM clause (before any JOIN keywords)."""
    from_pos = _find_keyword_boundary(sql, "FROM")
    if from_pos == -1:
        return []

    after_from = sql[from_pos + 4 :]
    stop = re.search(
        r"(?i)\b(?:INNER\s+JOIN|LEFT\s+(?:OUTER\s+)?JOIN|RIGHT\s+(?:OUTER\s+)?JOIN"
        r"|CROSS\s+JOIN|FULL\s+(?:OUTER\s+)?JOIN|JOIN"
        r"|WHERE|GROUP\s+BY|ORDER\s+BY|HAVING)\b",
        after_from,
    )
    table_text = after_from[: stop.start()].strip() if stop else after_from.strip()

    tables: list[str] = []
    for part in re.split(r",", table_text):
        m = re.match(r"\s*(\w+(?:\.\w+)*)", part)
        if m:
            tables.append(m.group(1))
    return tables


def _determine_view_type(modifier: str | None) -> str:
    """Map the CREATE ... VIEW modifier to a canonical type string."""
    if modifier is None:
        return "DERIVED"
    mod = modifier.strip().upper()
    if "INTERFACE" in mod:
        return "INTERFACE"
    if "MATERIALIZED" in mod:
        return "MATERIALIZED"
    return "DERIVED"


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def extract_select_columns(sql: str) -> list[dict]:
    """Parse the SELECT clause into a list of column descriptors.

    Handles nested function calls, CASE expressions, and quoted identifiers
    by tracking parenthesis depth rather than splitting on raw commas.

    Args:
        sql: Full SQL string containing a SELECT...FROM block.

    Returns:
        List of dicts, each with keys:
          alias (str), expression (str), source_table (str | None).
    """
    log.debug("extract_select_columns: entering, sql_len=%d", len(sql))
    columns: list[dict] = []

    select_body = _extract_select_body(sql)
    if select_body is None:
        log.warning("extract_select_columns: no SELECT...FROM found")
        return columns

    raw_parts = _split_on_top_level_commas(select_body)
    log.debug("extract_select_columns: %d raw column parts found", len(raw_parts))

    for raw in raw_parts:
        if not raw.strip():
            continue
        try:
            columns.append(_parse_one_column(raw))
        except Exception as exc:  # noqa: BLE001
            record = fail_step("extract_select_columns._parse_one_column", exc)
            log.warning(
                "extract_select_columns: skipping '%s…': %s",
                raw[:60],
                record["error_message"],
            )

    log.debug("extract_select_columns: returning %d columns", len(columns))
    return columns


def extract_joins_from_sql(sql: str) -> list[dict]:
    """Parse JOIN clauses from a SQL string.

    Handles INNER JOIN, LEFT [OUTER] JOIN, RIGHT [OUTER] JOIN, FULL [OUTER] JOIN,
    and CROSS JOIN.  The ON condition is extracted up to the next recognisable
    clause keyword.

    Args:
        sql: Full SQL string.

    Returns:
        List of dicts with keys: type (str), left (str), right (str),
        condition (str).
    """
    log.debug("extract_joins_from_sql: entering, sql_len=%d", len(sql))
    joins: list[dict] = []

    main_tables = _find_main_from_tables(sql)
    left_default = main_tables[0] if main_tables else ""

    for match in _JOIN_HEADER_RE.finditer(sql):
        try:
            raw_type = re.sub(r"\s+", " ", match.group(1).strip().upper())
            right_table = match.group(2)

            # ON condition: text after the JOIN header until the next clause.
            after_join = sql[match.end() :]
            on_match = re.match(r"(?i)\s*ON\s+(.*)", after_join, re.DOTALL)
            condition: str = ""
            if on_match:
                cond_text = on_match.group(1)
                stop = _NEXT_CLAUSE_RE.search(cond_text)
                condition = (
                    cond_text[: stop.start()].strip() if stop else cond_text.strip()
                )

            # Left table: previous join's right side, or the initial FROM table.
            left_table = joins[-1]["right"] if joins else left_default

            joins.append(
                {
                    "type": raw_type,
                    "left": left_table,
                    "right": right_table,
                    "condition": condition,
                }
            )
        except Exception as exc:  # noqa: BLE001
            record = fail_step("extract_joins_from_sql", exc)
            log.warning(
                "extract_joins_from_sql: skipping join at offset %d: %s",
                match.start(),
                record["error_message"],
            )

    log.debug("extract_joins_from_sql: returning %d joins", len(joins))
    return joins


def extract_group_by_cols(sql: str) -> list[str]:
    """Extract column references from the GROUP BY clause.

    Args:
        sql: Full SQL string.

    Returns:
        List of stripped column/expression strings.
    """
    log.debug("extract_group_by_cols: entering")
    cols: list[str] = []

    m = _GROUP_BY_RE.search(sql)
    if not m:
        return cols

    for part in _split_on_top_level_commas(m.group(1)):
        part = part.strip()
        if part:
            cols.append(part)

    log.debug("extract_group_by_cols: returning %d columns", len(cols))
    return cols


def extract_aggregations(sql: str) -> list[dict]:
    """Find aggregation function calls in *sql*.

    Detects SUM, AVG, MIN, MAX, COUNT, and COUNT DISTINCT.

    Args:
        sql: Full SQL string.

    Returns:
        List of dicts with keys: function (str), column (str), alias (str).
    """
    log.debug("extract_aggregations: entering")
    aggs: list[dict] = []

    for m in _AGG_FUNC_RE.finditer(sql):
        try:
            func_name = m.group(1).upper()
            has_distinct = bool(m.group(2))
            col_arg = m.group(3).strip()
            alias = m.group(4) or ""

            if func_name == "COUNT" and has_distinct:
                func_name = "COUNT DISTINCT"

            aggs.append({"function": func_name, "column": col_arg, "alias": alias})
        except Exception as exc:  # noqa: BLE001
            record = fail_step("extract_aggregations", exc)
            log.warning(
                "extract_aggregations: skipping match: %s", record["error_message"]
            )

    log.debug("extract_aggregations: returning %d aggregations", len(aggs))
    return aggs


def parse_vql_export(vql_path: str) -> list[dict]:
    """Parse a Denodo VQL export file and return a list of view dicts.

    Splits the file on ``CREATE OR REPLACE [INTERFACE|MATERIALIZED] VIEW``
    blocks and extracts basic clause information for each view.  Sub-parser
    enrichment (joins, columns, etc.) is handled by :func:`parse_vql_file`.

    Args:
        vql_path: Path to the ``.vql`` export file.

    Returns:
        List of view dicts, each containing:
          name, type, full_sql, select_columns, from_tables, joins,
          where_clause, group_by_cols, aggregations, order_by, errors.

    Raises:
        ParseError: If the file cannot be read.
    """
    log.info("parse_vql_export: entering, path=%s", vql_path)
    views: list[dict] = []

    try:
        content = Path(vql_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ParseError(
            f"Cannot read VQL file: {exc}", context={"path": str(vql_path)}
        ) from exc

    log.debug(
        "parse_vql_export: read %d chars from %s", len(content), vql_path
    )

    matches = list(_CREATE_VIEW_RE.finditer(content))
    if not matches:
        log.warning(
            "parse_vql_export: no CREATE OR REPLACE VIEW statements found in %s",
            vql_path,
        )
        return views

    log.info("parse_vql_export: found %d VIEW blocks", len(matches))

    for m in matches:
        view_name = m.group(3).strip().strip('"').strip("`")
        view_type = _determine_view_type(m.group(2))
        body_sql = m.group(4).rstrip(";").strip()

        view: dict = {
            "name": view_name,
            "type": view_type,
            "full_sql": m.group(0).strip(),
            "select_columns": [],
            "from_tables": [],
            "joins": [],
            "where_clause": None,
            "group_by_cols": [],
            "aggregations": [],
            "order_by": None,
            "errors": [],
        }

        try:
            view["from_tables"] = _find_main_from_tables(body_sql)
        except Exception as exc:  # noqa: BLE001
            record = fail_step("parse_vql_export.from_tables", exc, partial_results=view)
            view["errors"].append(record["error_message"])

        try:
            where_m = _WHERE_RE.search(body_sql)
            if where_m:
                view["where_clause"] = where_m.group(1).strip()
        except Exception as exc:  # noqa: BLE001
            record = fail_step("parse_vql_export.where_clause", exc, partial_results=view)
            view["errors"].append(record["error_message"])

        try:
            order_m = _ORDER_BY_RE.search(body_sql)
            if order_m:
                view["order_by"] = order_m.group(1).strip()
        except Exception as exc:  # noqa: BLE001
            record = fail_step("parse_vql_export.order_by", exc, partial_results=view)
            view["errors"].append(record["error_message"])

        views.append(view)

    log.info("parse_vql_export: returning %d views from %s", len(views), vql_path)
    return views


def parse_vql_file(path: str) -> dict:
    """Main entry point: read a VQL file and return a fully enriched inventory.

    Calls :func:`parse_vql_export` for initial block extraction, then enriches
    each view with :func:`extract_select_columns`, :func:`extract_joins_from_sql`,
    :func:`extract_group_by_cols`, and :func:`extract_aggregations`.

    Failures in any sub-parser are captured per-view in ``view["errors"]``
    without aborting processing of remaining views.

    Args:
        path: Path to the ``.vql`` export file.

    Returns:
        Dict with keys: file_path (str), view_count (int), views (list),
        errors (list).
    """
    log.info("parse_vql_file: entering, path=%s", path)
    result: dict = {
        "file_path": str(path),
        "view_count": 0,
        "views": [],
        "errors": [],
    }

    try:
        views = parse_vql_export(path)
    except ParseError as exc:
        record = fail_step("parse_vql_file.parse_vql_export", exc)
        result["errors"].append(record)
        log.error("parse_vql_file: aborting — could not read file: %s", exc)
        return result

    for view in views:
        body_sql = view.get("full_sql", "")

        try:
            view["select_columns"] = extract_select_columns(body_sql)
        except Exception as exc:  # noqa: BLE001
            record = fail_step("parse_vql_file.extract_select_columns", exc)
            view["errors"].append(record["error_message"])

        try:
            view["joins"] = extract_joins_from_sql(body_sql)
        except Exception as exc:  # noqa: BLE001
            record = fail_step("parse_vql_file.extract_joins_from_sql", exc)
            view["errors"].append(record["error_message"])

        try:
            view["group_by_cols"] = extract_group_by_cols(body_sql)
        except Exception as exc:  # noqa: BLE001
            record = fail_step("parse_vql_file.extract_group_by_cols", exc)
            view["errors"].append(record["error_message"])

        try:
            view["aggregations"] = extract_aggregations(body_sql)
        except Exception as exc:  # noqa: BLE001
            record = fail_step("parse_vql_file.extract_aggregations", exc)
            view["errors"].append(record["error_message"])

    result["views"] = views
    result["view_count"] = len(views)
    log.info(
        "parse_vql_file: complete — %d views, %d file-level errors",
        result["view_count"],
        len(result["errors"]),
    )
    return result
