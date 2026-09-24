#!/usr/bin/env python3
"""Load a unified inventory into the Semantic Knowledge Base.

Reads the JSON produced by ``semantic-extraction``'s ``parse`` command and writes
it into the knowledge base schema. Deliberately source-agnostic: it consumes
the unified inventory contract, never a source tool's native structures, so a
Tableau or Power BI inventory loads through the same path as a Cognos one.

Usage
-----
    python3 kb_loader.py --inventory out/cognos_inventory.json \\
        --source-system cognos --connection my-demo-account

    # Multiple models into the same KB:
    python3 kb_loader.py --inventory out/model_a.json --source-system cognos
    python3 kb_loader.py --inventory out/model_b.json --source-system powerbi

Idempotence
-----------
Every write is a MERGE on the natural key, so re-running against a revised model
updates in place. LOAD_ID is recorded on every row, which means a load can be
audited or isolated after the fact without the loader having to delete anything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

import config

log = logging.getLogger("kb_loader")

# Defaults only. The effective values come from the naming flags in config,
# which build.py forwards to every child script.
KB_DATABASE = config.DEFAULT.kb_database
KB_SCHEMA = config.DEFAULT.kb_schema

# Cognos connection alias -> physical Snowflake schema. The source model names a
# JDBC connection, not a schema, so the mapping has to be supplied rather than
# inferred. Passed in via --schema-map for other models.
DEFAULT_SCHEMA_MAP = {
    "RAPID_Snowflake_Heavy": "SALES_ANALYTICS",
    "RAPID_Snowflake_Heavy1": "COMMON_ANALYTICS",
}


def _hash(*parts: Any) -> str:
    """Stable short id from the given parts.

    Used for surrogate keys on tables whose natural key would otherwise be a very
    long composite (security rules, lineage edges). Deterministic across runs so
    a MERGE matches the same row again rather than inserting a duplicate.
    """
    joined = "\u0001".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:24]


def _sql_str(v: Any) -> str:
    """Render a Python value as a SQL literal."""
    if v is None or v == "":
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("\\", "\\\\").replace("'", "''")
    # Snowflake's VARCHAR default is 16MB, but a single embedded SQL blob or a
    # pathological expression can still be unhelpfully long in a catalog. Truncate
    # with a visible marker rather than silently.
    if len(s) > 60000:
        s = s[:60000] + " ...[truncated by kb_loader]"
    return f"'{s}'"


def _sql_variant(v: Any) -> str:
    """Render a Python object as a PARSE_JSON literal."""
    if v is None:
        return "NULL"
    return f"PARSE_JSON({_sql_str(json.dumps(v, default=str))})"


def _sql_array(v: Iterable[Any] | None) -> str:
    """Render a list as an ARRAY literal."""
    if not v:
        return "ARRAY_CONSTRUCT()"
    return f"PARSE_JSON({_sql_str(json.dumps(list(v), default=str))})::ARRAY"


class SnowSqlRunner:
    """Executes SQL through the Snowflake CLI, one subprocess per call.

    Kept as the fallback path. Shelling out to the CLI means the loader runs with
    whatever connection and authentication the operator already has configured,
    including browser and PAT auth, without this script holding credentials --
    which is a good property and the reason this was the original design.

    Its cost is that every call is a fresh process and a fresh session. Measured
    at roughly 4s of process spawn plus auth before any SQL runs, about 14 times
    across a full load, so ~56s of the knowledge-base phase was startup.
    ``ConnectorRunner`` below removes that while reading the same credentials.

    Statements are written to a temp file and executed as a batch. A failure
    surfaces the CLI's own error output rather than a wrapped exception, because
    the CLI reports which statement failed and a wrapper usually loses that.
    """

    def __init__(self, connection: str | None):
        self.connection = connection

    def run_file(self, sql: str, label: str) -> None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".sql", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(sql)
            path = fh.name
        cmd = ["snow", "sql", "-f", path]
        if self.connection:
            cmd += ["-c", self.connection]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                tail = (proc.stdout or "")[-3000:] + (proc.stderr or "")[-3000:]
                raise RuntimeError(f"{label} failed:\n{tail}")
            log.info("%s ok", label)
        finally:
            os.unlink(path)

    def run_statements(self, statements: list[str], label: str) -> None:
        """Run a batch as one file. The CLI splits it; that is its job."""
        self.run_file("\n\n".join(statements), label)

    def close(self) -> None:
        """No persistent state to release. Present so callers need not branch."""


class ConnectorRunner:
    """Executes SQL over one persistent connector session.

    Same credentials as the CLI path: ``connection_name`` reads the same
    ``~/.snowflake/connections.toml`` that ``snow -c`` reads, so this does not
    introduce a second place to configure auth or ask the operator for secrets.
    What it removes is paying process spawn, TLS and login once per chunk.

    Failure reporting is deliberately kept as good as the CLI's. The reason the
    CLI was chosen was that it names the failing statement; a naive wrapper loses
    that, so ``run_statements`` reports the exact statement that raised, truncated,
    alongside Snowflake's own message.

    Note it takes a *list* of statements rather than a joined string. Splitting a
    joined string on ";\n" looked equivalent and is not: ``_sql_str`` escapes
    backslashes and quotes but leaves newlines intact, so any Cognos expression
    containing a semicolon at the end of a line -- and this model has 25,398
    expressions -- would be cut in half and sent as invalid SQL. Never parsing the
    SQL back apart removes that class of bug entirely.
    """

    def __init__(self, connection: str | None):
        import snowflake.connector

        try:
            self.conn = snowflake.connector.connect(connection_name=connection)
        except TypeError:
            # Connector predates connection_name. Reading the TOML by hand needs
            # tomllib (3.11+), so this is a genuine fallback, not a preference.
            import tomllib

            cfg = os.path.expanduser("~/.snowflake/connections.toml")
            with open(cfg, "rb") as fh:
                conf = tomllib.load(fh)[connection]
            self.conn = snowflake.connector.connect(**conf)
        self.cur = self.conn.cursor()

    def run_statements(self, statements: list[str], label: str) -> None:
        for stmt in statements:
            stmt = stmt.strip().rstrip(";")
            if not stmt:
                continue
            try:
                self.cur.execute(stmt)
            except Exception as exc:  # noqa: BLE001 -- re-raised with context
                raise RuntimeError(
                    f"{label} failed on this statement:\n{stmt[:600]}\n...\n{exc}"
                ) from exc
        log.info("%s ok", label)

    def run_file(self, sql: str, label: str) -> None:
        """For a caller holding one blob. Uses the connector's own SQL splitter.

        Not used by the KB load itself, which passes statements as a list -- see
        the class docstring on why splitting a joined string is unsafe here.
        """
        for cur in self.conn.execute_string(sql):
            if cur.description:
                cur.fetchall()
        log.info("%s ok", label)

    def close(self) -> None:
        try:
            self.cur.close()
        finally:
            self.conn.close()


def make_runner(connection: str | None, prefer_cli: bool = False):
    """One persistent session when the connector is available, else the CLI.

    Falling back rather than requiring the connector keeps the loader working on a
    machine that only has the CLI, which is the environment the original design
    was written for.
    """
    if prefer_cli:
        return SnowSqlRunner(connection)
    try:
        runner = ConnectorRunner(connection)
        log.info("using one persistent connector session")
        return runner
    except Exception as exc:  # noqa: BLE001 -- any failure means use the CLI
        log.info("connector unavailable (%s); falling back to the snow CLI", exc)
        return SnowSqlRunner(connection)


def _batched(rows: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


class KnowledgeBaseLoader:
    """Writes a unified inventory into the KB tables."""

    # Rows per MERGE. Batching matters: 19,921 security rules as individual
    # INSERTs is tens of minutes of round trips.
    BATCH = 500
    # Statements per runner call. This mattered a great deal when every call was a
    # fresh `snow sql` subprocess -- it cut invocations from ~90 to ~13. With the
    # connector runner holding one session it is only the granularity of the
    # progress log, and it still bounds the CLI fallback's invocation count.
    STATEMENTS_PER_CALL = 40

    def __init__(
        self,
        runner: SnowSqlRunner | ConnectorRunner,
        source_system: str,
        source_model: str,
        load_id: str,
        schema_map: dict[str, str],
        kb_database: str,
    ):
        self.runner = runner
        self.system = source_system
        self.model = source_model
        self.load_id = load_id
        self.schema_map = schema_map
        # The knowledge base, not the analytics layer -- 01_kb_tables.sql creates
        # KNOWLEDGE_BASE in {{KB_DB}}, and the two are commonly different databases.
        self.target_database = kb_database
        self.stats: dict[str, int] = {}

    # -- helpers ------------------------------------------------------------

    def _merge(
        self,
        table: str,
        key_columns: list[str],
        all_columns: list[str],
        rows: list[list[str]],
        label: str,
    ) -> None:
        """MERGE a batch of literal rows into a KB table."""
        if not rows:
            log.info("%s: nothing to load", label)
            self.stats[table] = 0
            return

        col_list = ", ".join(all_columns)
        on_clause = " AND ".join(f"t.{c} = s.{c}" for c in key_columns)
        update_cols = [c for c in all_columns if c not in key_columns]
        set_clause = ", ".join(f"t.{c} = s.{c}" for c in update_cols)
        insert_vals = ", ".join("s." + c for c in all_columns)

        statements: list[str] = []
        for batch in _batched(rows, self.BATCH):
            # One VALUES row-constructor list, not 500 SELECTs joined by UNION ALL.
            #
            # This is a compile-time fix, not an execution one. Measured on the
            # same 500 rows into the same table: the UNION ALL form spends 1.87s
            # compiling and 0.57s executing; the VALUES form spends 0.43s and
            # 0.30s. Compile is 77% of the cost and it scales with the length of
            # the SQL text rather than the row count, so the phase was mostly
            # waiting on Snowflake parsing a ~97KB statement, 88 times over.
            #
            # Everything else here is unchanged on purpose: same batch size, same
            # natural-key ON clause, same MERGE semantics. Re-running still
            # converges rather than duplicating.
            values = ",\n    ".join("(" + ", ".join(r) + ")" for r in batch)
            statements.append(
                f"MERGE INTO {self.target_database}.{KB_SCHEMA}.{table} t\n"
                f"USING (SELECT * FROM VALUES\n    {values}\n) s ({col_list})\n"
                f"ON {on_clause}\n"
                f"WHEN MATCHED THEN UPDATE SET {set_clause}\n"
                f"WHEN NOT MATCHED THEN INSERT ({col_list}) VALUES ({insert_vals});"
            )

        total = 0
        for i in range(0, len(statements), self.STATEMENTS_PER_CALL):
            chunk = statements[i : i + self.STATEMENTS_PER_CALL]
            self.runner.run_statements(
                chunk, f"{label} ({len(chunk)} merge stmts)"
            )
        total = len(rows)
        self.stats[table] = total
        log.info("%s: %d rows", label, total)

    def _base(self) -> list[str]:
        return [_sql_str(self.system), _sql_str(self.model)]

    def _physical_schema(self, alias: str) -> str | None:
        return self.schema_map.get(alias)

    # -- loaders ------------------------------------------------------------

    def load_source_model(self, inv: dict, analysis: dict, inventory_path: str) -> None:
        counts = {
            "entities": len(inv.get("tables", [])),
            "terms": len(inv.get("dimensions", [])) + len(inv.get("facts", [])),
            "metrics": len(inv.get("metrics", [])),
            "relationships": len(inv.get("relationships", [])),
            "hierarchies": len(inv.get("hierarchies", [])),
            "grain_declarations": len(inv.get("grain_declarations", [])),
            "aggregation_rules": len(inv.get("aggregation_rules", [])),
            "security_rules": len(inv.get("security_rules", [])),
            "flagged": len(inv.get("flagged", [])),
            "complexity": inv.get("complexity_summary", {}),
        }
        src_file = ""
        for t in inv.get("dimensions", []) + inv.get("metrics", []):
            if t.get("source_file"):
                src_file = t["source_file"]
                break
        src_file = src_file or inventory_path

        cols = [
            "SOURCE_SYSTEM", "SOURCE_MODEL", "SOURCE_FILE", "SOURCE_FILE_BYTES",
            "MODEL_OWNER", "ELEMENT_COUNTS", "EXTRACTION_SUMMARY", "LOAD_ID",
        ]
        row = self._base() + [
            _sql_str(src_file),
            _sql_str(os.path.getsize(src_file) if os.path.isfile(src_file) else None),
            _sql_str(None),
            _sql_variant(analysis.get("element_counts")),
            _sql_variant(counts),
            _sql_str(self.load_id),
        ]
        self._merge(
            "KB_SOURCE_MODEL",
            ["SOURCE_SYSTEM", "SOURCE_MODEL"],
            cols,
            [row],
            "KB_SOURCE_MODEL",
        )

    def load_entities(self, inv: dict, analysis: dict) -> None:
        platform_by_alias = {
            s["name"]: s.get("platform", "unknown")
            for s in (analysis.get("data_sources", {}).get("sources") or [])
        }
        cols = [
            "SOURCE_SYSTEM", "SOURCE_MODEL", "ENTITY_NAME", "ENTITY_CANONICAL",
            "CLONE_VARIANT", "ENTITY_KIND", "SOURCE_LAYER", "PHYSICAL_DATABASE",
            "PHYSICAL_SCHEMA", "PHYSICAL_OBJECT", "SOURCE_CONNECTION",
            "SOURCE_PLATFORM", "IS_PASSTHROUGH", "EMBEDDED_SQL", "SOURCE_STATUS",
            "CONVERSION_TIER", "DESCRIPTION", "LOAD_ID",
        ]
        rows = []
        for t in inv.get("tables", []):
            name = t.get("name", "")
            if not name:
                continue
            canonical, variant = _canonical_name(name)
            alias = t.get("source_alias", "")
            rows.append(
                self._base()
                + [
                    _sql_str(name),
                    _sql_str(canonical),
                    _sql_str(variant),
                    _sql_str(t.get("table_type") or ("view" if t.get("is_passthrough") else "model_layer")),
                    _sql_str(t.get("source_view")),
                    _sql_str(t.get("database")),
                    _sql_str(self._physical_schema(alias)),
                    _sql_str(t.get("physical_name") or name),
                    _sql_str(alias),
                    _sql_str(platform_by_alias.get(alias, "unknown")),
                    _sql_str(t.get("is_passthrough")),
                    _sql_str(t.get("sql") if not t.get("is_passthrough") else None),
                    _sql_str(t.get("status")),
                    _sql_str(t.get("complexity")),
                    _sql_str(t.get("description")),
                    _sql_str(self.load_id),
                ]
            )
        self._merge(
            "KB_ENTITY",
            ["SOURCE_SYSTEM", "SOURCE_MODEL", "ENTITY_NAME"],
            cols,
            rows,
            "KB_ENTITY",
        )

    def load_terms(self, inv: dict) -> None:
        cols = [
            "SOURCE_SYSTEM", "SOURCE_MODEL", "ENTITY_NAME", "TERM_NAME",
            "PHYSICAL_COLUMN", "TERM_ROLE", "DATA_TYPE", "SOURCE_DATA_TYPE",
            "IS_NULLABLE", "IS_CALCULATED", "EXPRESSION", "CONVERSION_TIER",
            "DEFINITION", "SYNONYMS", "SOURCE_LAYER", "LOAD_ID",
        ]
        rows = []
        seen: set[tuple[str, str]] = set()
        for role, items in (("measure", inv.get("facts", [])), (None, inv.get("dimensions", []))):
            for it in items:
                entity = it.get("table", "")
                name = it.get("name", "")
                if not name or (entity, name) in seen:
                    continue
                seen.add((entity, name))
                orig = it.get("original", {}) or {}
                term_role = role or orig.get("usage") or "attribute"
                rows.append(
                    self._base()
                    + [
                        _sql_str(entity),
                        _sql_str(name),
                        _sql_str(orig.get("external_name")),
                        _sql_str(term_role),
                        _sql_str(it.get("data_type")),
                        _sql_str(orig.get("datatype_raw")),
                        _sql_str(orig.get("nullable")),
                        _sql_str(orig.get("is_calculated")),
                        _sql_str(orig.get("expression")),
                        _sql_str(it.get("complexity")),
                        _sql_str(it.get("description") or _humanize(name)),
                        _sql_array(it.get("synonyms")),
                        _sql_str(it.get("source_view")),
                        _sql_str(self.load_id),
                    ]
                )
        self._merge(
            "KB_TERM",
            ["SOURCE_SYSTEM", "SOURCE_MODEL", "ENTITY_NAME", "TERM_NAME"],
            cols,
            rows,
            "KB_TERM",
        )

    def load_metrics(self, inv: dict) -> None:
        cols = [
            "SOURCE_SYSTEM", "SOURCE_MODEL", "METRIC_NAME", "METRIC_CANONICAL",
            "CLONE_VARIANT", "METRIC_KIND", "EXPRESSION", "TRANSLATED_EXPR",
            "TIME_WINDOWS", "REQUIRES_FISCAL_CALENDAR", "FORMAT_KIND",
            "FORMAT_DECIMALS", "CURRENCY_CODE", "CONVERSION_TIER", "DEFINITION",
            "SYNONYMS", "LOAD_ID",
        ]
        rows = []
        seen: set[str] = set()
        for m in inv.get("metrics", []):
            orig = m.get("original", {}) or {}
            tr = orig.get("translation", {}) or {}
            # The canonical name is the metric identity in the KB: the clone
            # family collapses to one row, which is the whole point of tracking
            # canonical names at parse time.
            name = orig.get("canonical_name") or m.get("name", "")
            if not name or name in seen:
                continue
            seen.add(name)
            rows.append(
                self._base()
                + [
                    _sql_str(name),
                    _sql_str(orig.get("canonical_name") or name),
                    _sql_str(orig.get("fiscal_year_clone")),
                    _sql_str(tr.get("kind") or "total"),
                    _sql_str(orig.get("expression")),
                    _sql_str(m.get("expr")),
                    _sql_array(tr.get("members")),
                    _sql_str(bool(tr.get("fiscal_members"))),
                    _sql_str(orig.get("format_kind")),
                    _sql_str(orig.get("format_decimals")),
                    _sql_str(orig.get("currency")),
                    _sql_str(m.get("complexity")),
                    _sql_str(m.get("description")),
                    _sql_array(m.get("synonyms")),
                    _sql_str(self.load_id),
                ]
            )
        self._merge(
            "KB_METRIC",
            ["SOURCE_SYSTEM", "SOURCE_MODEL", "METRIC_NAME"],
            cols,
            rows,
            "KB_METRIC",
        )

    def load_hierarchies(self, inv: dict) -> None:
        h_cols = [
            "SOURCE_SYSTEM", "SOURCE_MODEL", "DIMENSION_NAME", "HIERARCHY_NAME",
            "DIMENSION_CANONICAL", "CLONE_VARIANT", "IS_DEFAULT", "ROOT_CAPTION",
            "LEVEL_COUNT", "SOURCE_LAYER", "LOAD_ID",
        ]
        l_cols = [
            "SOURCE_SYSTEM", "SOURCE_MODEL", "DIMENSION_NAME", "HIERARCHY_NAME",
            "LEVEL_ORDINAL", "LEVEL_NAME", "IS_ALL_LEVEL", "CAPTION_TERM",
            "BUSINESS_KEY_TERM", "SOURCE_ENTITY", "COLUMNS", "LOAD_ID",
        ]
        h_rows, l_rows = [], []
        seen_h: set[tuple[str, str]] = set()
        for h in inv.get("hierarchies", []):
            dim, hname = h.get("dimension", ""), h.get("name", "")
            if not dim or not hname or (dim, hname) in seen_h:
                continue
            seen_h.add((dim, hname))
            h_rows.append(
                self._base()
                + [
                    _sql_str(dim), _sql_str(hname),
                    _sql_str(h.get("dimension_base")),
                    _sql_str(h.get("fiscal_year_clone")),
                    _sql_str(h.get("is_default")),
                    _sql_str(h.get("root_caption")),
                    _sql_str(h.get("depth")),
                    _sql_str(h.get("namespace")),
                    _sql_str(self.load_id),
                ]
            )
            seen_l: set[int] = set()
            for lv in h.get("levels", []):
                ordinal = lv.get("ordinal", 0)
                if ordinal in seen_l:
                    continue
                seen_l.add(ordinal)
                src_entity = ""
                for c in lv.get("columns", []):
                    if c.get("source_entity"):
                        src_entity = c["source_entity"]
                        break
                l_rows.append(
                    self._base()
                    + [
                        _sql_str(dim), _sql_str(hname), _sql_str(ordinal),
                        _sql_str(lv.get("name")),
                        _sql_str(lv.get("is_all_level")),
                        _sql_str(lv.get("caption_item")),
                        _sql_str(lv.get("business_key_item")),
                        _sql_str(src_entity),
                        _sql_variant(lv.get("columns")),
                        _sql_str(self.load_id),
                    ]
                )
        self._merge(
            "KB_HIERARCHY",
            ["SOURCE_SYSTEM", "SOURCE_MODEL", "DIMENSION_NAME", "HIERARCHY_NAME"],
            h_cols, h_rows, "KB_HIERARCHY",
        )
        self._merge(
            "KB_HIERARCHY_LEVEL",
            ["SOURCE_SYSTEM", "SOURCE_MODEL", "DIMENSION_NAME", "HIERARCHY_NAME", "LEVEL_ORDINAL"],
            l_cols, l_rows, "KB_HIERARCHY_LEVEL",
        )

    def load_grain(self, inv: dict) -> None:
        cols = [
            "SOURCE_SYSTEM", "SOURCE_MODEL", "ENTITY_NAME", "GRAIN_NAME",
            "KEY_COLUMNS", "ATTRIBUTE_COLUMNS", "IS_ROW_GRAIN", "IS_GROUPABLE",
            "DOUBLE_COUNT_RISK", "RISK_REASON", "LOAD_ID",
        ]
        rows = []
        seen: set[tuple[str, str]] = set()
        for g in inv.get("grain_declarations", []):
            entity, gname = g.get("entity", ""), g.get("name", "")
            if not entity or not gname or (entity, gname) in seen:
                continue
            seen.add((entity, gname))
            # Two independent double-count mechanisms, both worth naming rather
            # than collapsing to a boolean with no explanation.
            reasons = []
            if g.get("groupable") and not g.get("is_row_grain"):
                reasons.append(
                    "groupable coarser level: SUM repeats values unless grouped to the determinant key"
                )
            if g.get("is_row_grain") and len(g.get("key_columns") or []) > 1:
                reasons.append("composite row grain: single-column joins may fan out")
            rows.append(
                self._base()
                + [
                    _sql_str(entity), _sql_str(gname),
                    _sql_array(g.get("key_columns")),
                    _sql_array(g.get("attribute_columns")),
                    _sql_str(g.get("is_row_grain")),
                    _sql_str(g.get("groupable")),
                    _sql_str(bool(reasons)),
                    _sql_str("; ".join(reasons) if reasons else None),
                    _sql_str(self.load_id),
                ]
            )
        self._merge(
            "KB_GRAIN",
            ["SOURCE_SYSTEM", "SOURCE_MODEL", "ENTITY_NAME", "GRAIN_NAME"],
            cols, rows, "KB_GRAIN",
        )

    def load_aggregation_rules(self, inv: dict) -> None:
        cols = [
            "SOURCE_SYSTEM", "SOURCE_MODEL", "ENTITY_NAME", "TERM_NAME",
            "PHYSICAL_COLUMN", "TERM_ROLE", "REGULAR_AGGREGATE",
            "ROLLUP_AGGREGATE", "REGULAR_SQL", "ROLLUP_SQL",
            "IS_SEMI_ADDITIVE", "IS_MEASURE", "LOAD_ID",
        ]
        rows = []
        seen: set[tuple[str, str]] = set()
        for a in inv.get("aggregation_rules", []):
            entity, term = a.get("entity", ""), a.get("term", "")
            if not entity or not term or (entity, term) in seen:
                continue
            seen.add((entity, term))
            rows.append(
                self._base()
                + [
                    _sql_str(entity), _sql_str(term),
                    _sql_str(a.get("physical_column")),
                    _sql_str(a.get("usage")),
                    _sql_str(a.get("regular_aggregate")),
                    _sql_str(a.get("rollup_aggregate")),
                    _sql_str(a.get("regular_sql")),
                    _sql_str(a.get("rollup_sql")),
                    _sql_str(a.get("is_semi_additive")),
                    _sql_str(a.get("is_measure")),
                    _sql_str(self.load_id),
                ]
            )
        self._merge(
            "KB_AGGREGATION_RULE",
            ["SOURCE_SYSTEM", "SOURCE_MODEL", "ENTITY_NAME", "TERM_NAME"],
            cols, rows, "KB_AGGREGATION_RULE",
        )

    def load_relationships(self, inv: dict) -> None:
        cols = [
            "SOURCE_SYSTEM", "SOURCE_MODEL", "RELATIONSHIP_NAME", "LEFT_ENTITY",
            "RIGHT_ENTITY", "LEFT_COLUMN", "RIGHT_COLUMN", "JOIN_PREDICATE",
            "CARDINALITY", "IS_SIMPLE_EQUALITY", "SOURCE_STATUS",
            "IS_CLONE_RELATED", "LOAD_ID",
        ]
        rows = []
        seen: set[tuple[str, str, str]] = set()
        for r in inv.get("relationships", []):
            name = r.get("name", "")
            le, re_ = r.get("left_table", ""), r.get("right_table", "")
            if not name or (name, le, re_) in seen:
                continue
            seen.add((name, le, re_))
            clone = bool(
                _canonical_name(le)[1]
                or _canonical_name(re_)[1]
                or name.lower().startswith("copy")
            )
            rows.append(
                self._base()
                + [
                    _sql_str(name), _sql_str(le), _sql_str(re_),
                    _sql_str(r.get("left_column")),
                    _sql_str(r.get("right_column")),
                    _sql_str(r.get("condition")),
                    _sql_str(r.get("cardinality")),
                    _sql_str(r.get("is_simple_equality")),
                    _sql_str(r.get("source_status")),
                    _sql_str(clone),
                    _sql_str(self.load_id),
                ]
            )
        self._merge(
            "KB_RELATIONSHIP",
            ["SOURCE_SYSTEM", "SOURCE_MODEL", "RELATIONSHIP_NAME", "LEFT_ENTITY", "RIGHT_ENTITY"],
            cols, rows, "KB_RELATIONSHIP",
        )

    def load_security(self, inv: dict, mapping_rows: list[dict]) -> None:
        cols = [
            "SOURCE_SYSTEM", "SOURCE_MODEL", "RULE_ID", "PRINCIPAL",
            "PRINCIPAL_TYPE", "PRINCIPAL_PATH", "ROLE_GROUP",
            "PROTECTED_ENTITIES", "FILTER_COLUMNS", "PREDICATES",
            "FILTER_EXPRESSION", "DISPLAY_NAME", "LOAD_ID",
        ]
        rows = []
        seen: set[str] = set()
        for s in inv.get("security_rules", []):
            rid = _hash(s.get("principal"), s.get("expression"))
            if rid in seen:
                continue
            seen.add(rid)
            rows.append(
                self._base()
                + [
                    _sql_str(rid),
                    _sql_str(s.get("principal")),
                    _sql_str(s.get("principal_type")),
                    _sql_str(s.get("principal_path") or "/".join(s.get("principal_groups") or [])),
                    _sql_str(s.get("role_group")),
                    _sql_array(s.get("entities")),
                    _sql_array(s.get("columns")),
                    _sql_variant(s.get("predicates")),
                    _sql_str(s.get("expression")),
                    _sql_str(s.get("display_name")),
                    _sql_str(self.load_id),
                ]
            )
        self._merge(
            "KB_SECURITY_RULE",
            ["SOURCE_SYSTEM", "SOURCE_MODEL", "RULE_ID"],
            cols, rows, "KB_SECURITY_RULE",
        )

        m_cols = [
            "SOURCE_SYSTEM", "SOURCE_MODEL", "PRINCIPAL", "ROLE_GROUP",
            "COLUMN_NAME", "COLUMN_VALUE", "ENTITY_NAME", "LOAD_ID",
        ]
        m_rows = []
        seen_m: set[tuple[str, str, str]] = set()
        for r in mapping_rows:
            key = (r["principal"], r["column_name"], r["column_value"])
            if key in seen_m:
                continue
            seen_m.add(key)
            m_rows.append(
                self._base()
                + [
                    _sql_str(r["principal"]),
                    _sql_str(r.get("role_group")),
                    _sql_str(r["column_name"]),
                    _sql_str(r["column_value"]),
                    _sql_str(r.get("entity")),
                    _sql_str(self.load_id),
                ]
            )
        self._merge(
            "KB_SECURITY_MAPPING",
            ["SOURCE_SYSTEM", "SOURCE_MODEL", "PRINCIPAL", "COLUMN_NAME", "COLUMN_VALUE"],
            m_cols, m_rows, "KB_SECURITY_MAPPING",
        )

    def load_lineage(self, inv: dict) -> None:
        cols = [
            "SOURCE_SYSTEM", "SOURCE_MODEL", "EDGE_ID", "FROM_KIND", "FROM_NAME",
            "FROM_ENTITY", "TO_KIND", "TO_NAME", "TO_ENTITY", "EDGE_TYPE", "LOAD_ID",
        ]
        rows = []
        seen: set[str] = set()

        def add(fk, fn, fe, tk, tn, te, et):
            eid = _hash(fk, fn, fe, tk, tn, te, et)
            if eid in seen:
                return
            seen.add(eid)
            rows.append(
                self._base()
                + [
                    _sql_str(eid), _sql_str(fk), _sql_str(fn), _sql_str(fe),
                    _sql_str(tk), _sql_str(tn), _sql_str(te), _sql_str(et),
                    _sql_str(self.load_id),
                ]
            )

        # Entity -> physical object.
        for t in inv.get("tables", []):
            if t.get("physical_name"):
                add("entity", t.get("name"), t.get("name"), "physical_object",
                    t["physical_name"], "", "binds_to")

        # Term -> physical column, and term -> referenced entity for calculated
        # terms. Both directions matter: the first answers "where does this come
        # from", the second answers "what breaks if I change this".
        for it in inv.get("dimensions", []) + inv.get("facts", []):
            orig = it.get("original", {}) or {}
            if orig.get("external_name"):
                add("term", it.get("name"), it.get("table"), "physical_column",
                    orig["external_name"], it.get("table"), "binds_to")
            for ref in orig.get("expression_refs") or []:
                if ref.get("item"):
                    add("term", it.get("name"), it.get("table"), "term",
                        ref["item"], ref.get("object", ""), "derives_from")

        # Metric -> referenced terms.
        for m in inv.get("metrics", []):
            orig = m.get("original", {}) or {}
            for ref in orig.get("expression_refs") or []:
                if ref.get("item"):
                    add("metric", m.get("name"), "", "term",
                        ref["item"], ref.get("object", ""), "aggregates")

        self._merge(
            "KB_LINEAGE",
            ["SOURCE_SYSTEM", "SOURCE_MODEL", "EDGE_ID"],
            cols, rows, "KB_LINEAGE",
        )

    def load_issues(self, inv: dict, analysis: dict, extra: list[dict] | None = None) -> None:
        """Write the issue ledger.

        Populated on every load, including clean ones. An extraction report that
        lists only successes cannot be reviewed, because the reviewer cannot see
        what was skipped or assumed.
        """
        cols = [
            "SOURCE_SYSTEM", "SOURCE_MODEL", "ISSUE_ID", "SEVERITY", "CATEGORY",
            "OBJECT_KIND", "OBJECT_NAME", "ISSUE", "IMPLICATION", "DETAIL", "LOAD_ID",
        ]
        issues: list[dict] = list(extra or [])

        for f in inv.get("flagged", []):
            reason = f.get("reason", "")
            if "empty expression" in reason:
                issues.append({
                    "severity": "risk",
                    "category": "source_model_defect",
                    "kind": "metric",
                    "name": f.get("name"),
                    "issue": "Metric has no expression in the source model",
                    "implication": "Nothing to convert. The metric is already non-functional in the source tool, so any report using it is returning nothing or erroring.",
                    "detail": reason,
                })
            elif "fiscal calendar" in reason:
                issues.append({
                    "severity": "blocker",
                    "category": "needs_decision",
                    "kind": "metric",
                    "name": f.get("name"),
                    "issue": "Metric depends on the fiscal calendar",
                    "implication": "Cannot be derived with a date offset. Deriving it from a flat month shift is wrong at every fiscal period boundary, and only at the boundaries. Must bind to fiscal columns in the date dimension.",
                    "detail": f.get("expression", "")[:2000],
                })
            else:
                issues.append({
                    "severity": "risk",
                    "category": "unconvertible",
                    "kind": "term",
                    "name": f.get("name"),
                    "issue": reason,
                    "implication": "Needs a human decision before this term can be exposed.",
                    "detail": f.get("expression", "")[:2000],
                })

        # Stale source metadata: relationships the source tool flags as broken.
        stale = [
            r for r in inv.get("relationships", [])
            if r.get("source_status") not in (None, "", "valid")
        ]
        if stale:
            sound = [r for r in stale if r.get("is_simple_equality")]
            issues.append({
                "severity": "risk",
                "category": "stale_metadata",
                "kind": "relationship",
                "name": f"{len(stale)} relationships",
                "issue": f"{len(stale)} of {len(inv.get('relationships', []))} relationships are not flagged valid by the source tool",
                "implication": (
                    f"{len(sound)} of them are single-column equality joins, which points to stale "
                    "bookkeeping in the source tool rather than broken predicates. That is not proof "
                    "the joins are sound: referential integrity still has to be confirmed against "
                    "real data at production volume."
                ),
                "detail": "; ".join(
                    f"{r.get('left_table')}->{r.get('right_table')} [{r.get('source_status')}]"
                    for r in stale[:40]
                ),
            })

        clones = analysis.get("clones") or {}
        if clones.get("calculation_clone_ratio", 0) > 1:
            issues.append({
                "severity": "note",
                "category": "source_model_defect",
                "kind": "model",
                "name": self.model,
                "issue": (
                    f"{clones.get('calculation_total')} metrics collapse to "
                    f"{clones.get('calculation_canonical_patterns')} distinct patterns "
                    f"({clones.get('calculation_clone_ratio')}x duplication)"
                ),
                "implication": (
                    "Each clone must be maintained separately in the source tool and re-created every "
                    "fiscal year. A date-window-driven metric needs one definition regardless of year."
                ),
                "detail": f"{clones.get('fiscal_year_cloned_objects')} cloned objects; "
                          f"{clones.get('package_role_based')} role-based package exports",
            })

        rows = []
        for i in issues:
            iid = _hash(i.get("category"), i.get("kind"), i.get("name"), i.get("issue"))
            rows.append(
                self._base()
                + [
                    _sql_str(iid),
                    _sql_str(i.get("severity")),
                    _sql_str(i.get("category")),
                    _sql_str(i.get("kind")),
                    _sql_str(i.get("name")),
                    _sql_str(i.get("issue")),
                    _sql_str(i.get("implication")),
                    _sql_str(i.get("detail")),
                    _sql_str(self.load_id),
                ]
            )
        self._merge(
            "KB_ISSUE",
            ["SOURCE_SYSTEM", "SOURCE_MODEL", "ISSUE_ID"],
            cols, rows, "KB_ISSUE",
        )


def _canonical_name(name: str) -> tuple[str, str | None]:
    """Strip a trailing fiscal-year clone suffix."""
    import re

    m = re.match(r"^(?P<base>.+?)[_\s]*FY\s?(?P<year>\d{2,4})$", (name or "").strip(), re.I)
    if not m:
        return (name or "").strip(), None
    return m.group("base").rstrip("_ "), f"FY{m.group('year')}"


def _humanize(name: str) -> str:
    """Fallback definition from a column name.

    Marked as generated so a steward can tell a real definition from a
    placeholder. A glossary full of unmarked auto-text is worse than an empty
    one, because it looks reviewed.
    """
    words = (name or "").replace("_", " ").title()
    return f"{words} (auto-generated from the source name; not steward-reviewed)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Load a unified inventory into the Semantic Knowledge Base")
    ap.add_argument("--inventory", required=True, help="Unified inventory JSON from semantic-extraction")
    ap.add_argument("--source-system", required=True,
                    choices=["cognos", "tableau", "powerbi", "looker", "denodo", "businessobjects"])
    ap.add_argument("--source-model", help="Override the model name (defaults to the name in the inventory)")
    ap.add_argument("--connection", help="snow CLI connection name")
    config.add_arguments(ap)
    ap.add_argument("--schema-map", help='JSON: {"connection_alias": "SCHEMA"}')
    ap.add_argument("--security-mapping", help="JSON array of flattened security mapping rows")
    ap.add_argument("--load-id", help="Override the generated load id")
    ap.add_argument("--use-cli", action="store_true",
                    help="Execute through one `snow sql` subprocess per chunk instead "
                         "of one persistent connector session. Slower (a fresh "
                         "process and login per chunk) but needs no Python connector")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )

    with open(args.inventory, encoding="utf-8") as f:
        inv = json.load(f)

    analysis = inv.get("source_analysis", {}) or {}
    model = args.source_model or analysis.get("model_name") or os.path.splitext(
        os.path.basename(args.inventory)
    )[0]
    load_id = args.load_id or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    schema_map = dict(DEFAULT_SCHEMA_MAP)
    if args.schema_map:
        schema_map.update(json.loads(args.schema_map))

    mapping_rows: list[dict] = []
    if args.security_mapping:
        with open(args.security_mapping, encoding="utf-8") as f:
            mapping_rows = json.load(f)

    log.info("Loading %s model %r as load_id=%s", args.source_system, model, load_id)

    runner = make_runner(args.connection, prefer_cli=args.use_cli)
    loader = KnowledgeBaseLoader(
        runner,
        args.source_system,
        model,
        load_id,
        schema_map,
        args.kb_database,
    )

    try:
        loader.load_source_model(inv, analysis, args.inventory)
        loader.load_entities(inv, analysis)
        loader.load_terms(inv)
        loader.load_metrics(inv)
        loader.load_hierarchies(inv)
        loader.load_grain(inv)
        loader.load_aggregation_rules(inv)
        loader.load_relationships(inv)
        loader.load_security(inv, mapping_rows)
        loader.load_lineage(inv)
        loader.load_issues(inv, analysis)
    finally:
        # Release the session even on failure, so a failed load does not leave a
        # connection open for the rest of the build.
        runner.close()

    print(json.dumps({
        "status": "ok",
        "load_id": load_id,
        "source_system": args.source_system,
        "source_model": model,
        "rows_by_table": loader.stats,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
