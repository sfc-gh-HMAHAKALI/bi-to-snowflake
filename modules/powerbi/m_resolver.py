"""M (Power Query) expression resolver — extracts source table info from partitions."""

import re
from typing import Any

from ..common.errors import ParseError, fail_step
from ..common.logger import get_logger

log = get_logger("powerbi.m_resolver")

# ---------------------------------------------------------------------------
# Compiled patterns for M expression source detection
# ---------------------------------------------------------------------------

# Pattern 1a: Snowflake.Databases("account.snowflakecomputing.com", ...)
_SNOWFLAKE_CONN_RE = re.compile(
    r'Snowflake\.Databases\s*\(\s*"([^"]+)"\s*,\s*"([^"]+)"',
    re.IGNORECASE,
)

# Pattern 1b: Navigation to database/schema/table via {[Name="..."]}[Data] chain
# Captures up to 3 levels: database, schema, table
_NAV_STEP_RE = re.compile(
    r'\{\s*\[Name\s*=\s*"([^"]+)"\s*\]\s*\}\s*\[Data\]',
    re.IGNORECASE,
)

# Pattern 2: Value.NativeQuery(..., "SELECT ...")
_NATIVE_QUERY_RE = re.compile(
    r'Value\.NativeQuery\s*\([^,]+,\s*"((?:[^"\\]|\\.)+)"',
    re.IGNORECASE | re.DOTALL,
)

# Pattern 3: Sql.Database / Sql.Databases for generic SQL sources
_SQL_DB_RE = re.compile(
    r'(?:Sql\.Database|Sql\.Databases)\s*\(\s*"([^"]+)"\s*,\s*"([^"]+)"',
    re.IGNORECASE,
)


def extract_source_from_m(m_expression: str) -> dict | None:
    """Extract source table metadata from an M / Power Query expression.

    Recognises two primary patterns:
    - **Pattern 1**: Snowflake.Databases connector with navigation chain
      → returns {type, account, warehouse, database, schema, table}
    - **Pattern 2**: Value.NativeQuery with inline SQL
      → returns {type, native_query}

    Falls back to None for unrecognised expressions and logs a warning.

    Args:
        m_expression: Raw M expression string from a partition source.

    Returns:
        Source metadata dict, or None if the expression is unrecognised.
    """
    log.debug(
        "extract_source_from_m: entry — expr_len=%d",
        len(m_expression) if m_expression else 0,
    )

    if not m_expression or not m_expression.strip():
        log.debug("extract_source_from_m: empty expression, returning None")
        return None

    try:
        # ---- Pattern 2: Value.NativeQuery (check before Snowflake connector
        #      because native queries can be nested inside the connector) ----
        native_match = _NATIVE_QUERY_RE.search(m_expression)
        if native_match:
            sql = native_match.group(1).replace('\\"', '"').strip()
            log.debug("extract_source_from_m: matched native_query")
            return {"type": "native_query", "native_query": sql}

        # ---- Pattern 1: Snowflake.Databases ----
        snow_match = _SNOWFLAKE_CONN_RE.search(m_expression)
        if snow_match:
            account = snow_match.group(1)
            warehouse = snow_match.group(2)

            # Collect all {[Name="..."]}[Data] navigation steps
            nav_steps = _NAV_STEP_RE.findall(m_expression)
            log.debug(
                "extract_source_from_m: snowflake connector — account=%s, "
                "nav_steps=%s",
                account,
                nav_steps,
            )

            result: dict[str, Any] = {
                "type": "snowflake",
                "account": account,
                "warehouse": warehouse,
            }
            if len(nav_steps) >= 1:
                result["database"] = nav_steps[0]
            if len(nav_steps) >= 2:
                result["schema"] = nav_steps[1]
            if len(nav_steps) >= 3:
                result["table"] = nav_steps[2]

            log.debug("extract_source_from_m: exit — result=%s", result)
            return result

        # ---- Pattern 3: Generic SQL connector ----
        sql_match = _SQL_DB_RE.search(m_expression)
        if sql_match:
            server = sql_match.group(1)
            database = sql_match.group(2)
            nav_steps = _NAV_STEP_RE.findall(m_expression)
            result = {
                "type": "sql_server",
                "server": server,
                "database": database,
            }
            if len(nav_steps) >= 1:
                result["schema"] = nav_steps[0]
            if len(nav_steps) >= 2:
                result["table"] = nav_steps[1]
            log.debug("extract_source_from_m: sql_server — result=%s", result)
            return result

        # Unrecognised expression
        log.warning(
            "extract_source_from_m: unrecognised M expression pattern — "
            "first 120 chars: %s",
            m_expression[:120].replace("\n", " "),
        )
        return None

    except Exception as exc:
        fail_step("extract_source_from_m", exc)
        return None


def resolve_all_sources(tables: list[dict]) -> list[dict]:
    """Apply M source resolution to all tables that have a source_query.

    Mutates each table dict in-place by adding a ``source_info`` key.
    Never raises — tables that fail resolution receive ``source_info=None``.

    Args:
        tables: List of table dicts as produced by extract_semantics(), each
                with optional ``source_query`` string.

    Returns:
        The same list with ``source_info`` added to every table.
    """
    log.info("resolve_all_sources: entry — tables=%d", len(tables))

    resolved = 0
    unresolved = 0

    for tbl in tables:
        tbl_name = tbl.get("name", "")
        source_query = tbl.get("source_query")

        if not source_query:
            tbl["source_info"] = None
            continue

        try:
            info = extract_source_from_m(source_query)
            tbl["source_info"] = info
            if info is not None:
                resolved += 1
            else:
                unresolved += 1
        except Exception as exc:
            err = fail_step(f"resolve_source:{tbl_name}", exc)
            tbl["source_info"] = None
            tbl.setdefault("errors", []).append(err)
            unresolved += 1

    log.info(
        "resolve_all_sources: exit — resolved=%d, unresolved=%d",
        resolved,
        unresolved,
    )
    return tables
