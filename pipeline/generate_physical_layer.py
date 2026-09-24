#!/usr/bin/env python3
"""Generate the physical layer from the Knowledge Base.

The schema is not hand-written. It is emitted from ``KB_TERM`` -- every column,
type and nullability comes from what the source BI model declared. That is the
point: the KB is not documentation about the warehouse, it is enough information
to *build* the warehouse contract the BI layer expects.

It emits the six core entities the semantic view reads, and nothing beyond them:
a table that no metric or dimension resolves to is dead weight in the demo and a
question to answer on the call.

The date dimension is generated with genuine fiscal columns. the reference model's fiscal year
runs July-June, so FINC_YR_ID for a July date is the following calendar year.
Deriving that with a flat DATEADD(month, -6) offset -- as the earlier handoff did
-- is wrong at every period boundary and only at the boundaries, which is the
hardest kind of error to notice. The model's own DIM_TIME filter
(FINC_YR_ID >= 2018) is evidence these columns exist and are authoritative.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict

import config

# Cognos connection alias -> physical schema, same mapping the KB loader uses.
SCHEMAS = set(config.DEFAULT.source_schemas)

# Columns whose declared role is a measure but which are really identifiers.
# Kept as their natural NUMBER type in DDL; the point of the distinction is that
# they must not become metrics, not that they change type.
DATE_COLS = {"PROCESSED_DATE", "PERIOD_DATE", "DATA_LOAD_TIME", "DATA_UPDATE_TIME"}


def run_sql(sql: str, connection: str, label: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8") as fh:
        fh.write(sql)
        path = fh.name
    try:
        proc = subprocess.run(
            ["snow", "sql", "-f", path, "-c", connection], capture_output=True, text=True
        )
        if proc.returncode != 0:
            tail = (proc.stdout or "")[-4000:] + (proc.stderr or "")[-4000:]
            raise RuntimeError(f"{label} failed:\n{tail}")
        print(f"  {label}: ok")
    finally:
        os.unlink(path)


def ddl_from_kb(columns: list[dict], database: str) -> str:
    """Emit CREATE TABLE for each core entity using the KB-declared columns."""
    by_entity: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for r in columns:
        key = (r["PHYSICAL_SCHEMA"], r["PHYSICAL_OBJECT"], r["ENTITY_NAME"])
        by_entity[key].append(r)

    out = [f"USE DATABASE {database};", ""]
    for schema in sorted(SCHEMAS):
        out.append(f"CREATE SCHEMA IF NOT EXISTS {schema};")
    out.append("")

    for (schema, obj, entity), cols in sorted(by_entity.items()):
        seen: set[str] = set()
        lines = []
        for c in sorted(cols, key=lambda x: x["TERM_NAME"]):
            name = c["PHYSICAL_COLUMN"]
            if name in seen:
                continue
            seen.add(name)
            dt = c["DATA_TYPE"] or "VARCHAR"
            if dt == "NUMBER":
                # Amounts need scale; identifiers do not. Splitting on the
                # measure-quality verdict would be circular here, so use the
                # name, which is reliable for this one decision.
                dt = "NUMBER(38,2)" if any(
                    k in name for k in ("AMOUNT", "AMT", "QUANTITY", "RATE", "PRICE")
                ) else "NUMBER(38,0)"
            lines.append(f"    {name:34} {dt}")
        out.append(
            f"-- {entity}: {len(lines)} columns, generated from KB_TERM\n"
            f"CREATE OR REPLACE TABLE {schema}.{obj} (\n"
            + ",\n".join(lines)
            + f"\n)\nCOMMENT = 'Generated from KB_TERM for Cognos query subject {entity}';\n"
        )
    return "\n".join(out)


def data_date_dimension() -> str:
    """Populate DIM_DATE with genuine fiscal columns.

    Column names here are the *physical* names (``FISCAL_YEAR_ID``), not the
    Cognos aliases (``FINC_YR_ID``). The KB holds both, and that translation is
    precisely the mapping a semantic layer has to own: report authors type
    FINC_YR_ID, the warehouse stores FISCAL_YEAR_ID.

    the reference model's fiscal year runs July-June, so a July date belongs to the following
    calendar year's fiscal year. That is applied as a shift of the year and month
    *ordinals* only. The earlier handoff derived fiscal periods with a blanket
    ``DATEADD('month', -6, ...)`` on the date itself, which moves every quarter
    and month boundary as well and is therefore wrong at exactly the points
    period-to-date metrics are evaluated. The source model's own filter
    (``FINC_YR_ID >= 2018``) is evidence these columns already exist upstream and
    are the authority, not something to recompute.
    """
    return """
USE DATABASE {{DB}};

TRUNCATE TABLE IF EXISTS COMMON_ANALYTICS.DIM_DATE;

INSERT INTO COMMON_ANALYTICS.DIM_DATE (
    DAY_DATE, DAY_NUMBER, DAY_WORK_NUMBER, BUSINESS_DAY_FLAG,
    CALENDAR_MONTH_ID, CALENDAR_MONTH_NAME,
    CALENDAR_QUARTER_ID, CALENDAR_QUARTER_NAME,
    CALENDAR_YEAR_ID, CALENDAR_YEAR_NAME,
    FISCAL_YEAR_ID, FISCAL_YEAR_NAME,
    FISCAL_QUARTER_ID, FISCAL_QUARTER_NAME, FISCAL_QUARTER_YEAR,
    FISCAL_MONTH_ID, FISCAL_MONTH_NAME,
    MONTH_YEAR, WEEK_NUMBER_OF_YEAR, START_OF_WEEK,
    START_WEEK_NAME, END_WEEK_NAME,
    DATA_LOAD_TIME, DATA_UPDATE_TIME
)
WITH d AS (
    SELECT DATEADD('day', SEQ4(), DATE '2018-01-01') AS dt
    FROM TABLE(GENERATOR(ROWCOUNT => 3652))
), f AS (
    SELECT
        dt,
        -- Fiscal year starts 1 July: Jul-Dec rolls into the next fiscal year.
        IFF(MONTH(dt) >= 7, YEAR(dt) + 1, YEAR(dt))       AS fy,
        -- Fiscal month 1 = July. Shifting the ordinal, not the date.
        IFF(MONTH(dt) >= 7, MONTH(dt) - 6, MONTH(dt) + 6)  AS fm
    FROM d
)
SELECT
    dt                                                         AS DAY_DATE,
    DAYOFMONTH(dt)                                             AS DAY_NUMBER,
    -- Working-day ordinal within the fiscal month, for business-day reporting.
    SUM(IFF(DAYOFWEEK(dt) BETWEEN 1 AND 5, 1, 0))
        OVER (PARTITION BY fy, fm ORDER BY dt)                 AS DAY_WORK_NUMBER,
    IFF(DAYOFWEEK(dt) BETWEEN 1 AND 5, 1, 0)                   AS BUSINESS_DAY_FLAG,
    YEAR(dt) * 100 + MONTH(dt)                                 AS CALENDAR_MONTH_ID,
    MONTHNAME(dt)                                              AS CALENDAR_MONTH_NAME,
    YEAR(dt) * 10 + QUARTER(dt)                                AS CALENDAR_QUARTER_ID,
    'CY' || YEAR(dt) || ' Q' || QUARTER(dt)                     AS CALENDAR_QUARTER_NAME,
    YEAR(dt)                                                   AS CALENDAR_YEAR_ID,
    'CY' || YEAR(dt)                                           AS CALENDAR_YEAR_NAME,
    fy                                                         AS FISCAL_YEAR_ID,
    'FY' || fy                                                 AS FISCAL_YEAR_NAME,
    fy * 10 + CEIL(fm / 3.0)                                   AS FISCAL_QUARTER_ID,
    'Q' || CEIL(fm / 3.0)                                      AS FISCAL_QUARTER_NAME,
    'FY' || fy || ' Q' || CEIL(fm / 3.0)                        AS FISCAL_QUARTER_YEAR,
    fy * 100 + fm                                              AS FISCAL_MONTH_ID,
    'FY' || fy || ' P' || LPAD(fm::VARCHAR, 2, '0')              AS FISCAL_MONTH_NAME,
    MONTHNAME(dt) || '-' || YEAR(dt)                            AS MONTH_YEAR,
    WEEKOFYEAR(dt)                                             AS WEEK_NUMBER_OF_YEAR,
    DATE_TRUNC('week', dt)                                     AS START_OF_WEEK,
    TO_CHAR(DATE_TRUNC('week', dt), 'DD-MON-YY')               AS START_WEEK_NAME,
    TO_CHAR(DATEADD('day', 6, DATE_TRUNC('week', dt)), 'DD-MON-YY') AS END_WEEK_NAME,
    CURRENT_DATE()                                             AS DATA_LOAD_TIME,
    CURRENT_DATE()                                             AS DATA_UPDATE_TIME
FROM f;
"""


def fetch_core_columns(kb_database: str, kb_schema: str, connection: str) -> list[dict]:
    """Query the KB for the columns of the six core entities.

    ``main()`` used to simply assume ``out/core_columns.json`` already existed,
    which meant an undocumented manual query stood between a loaded KB and a
    buildable physical layer. Nothing in the repo produced that file.

    ``CORE_ENTITIES`` holds *canonical* (unsuffixed) entity names, so a model
    with fiscal-year clone families still resolves -- the extractor emits one
    canonical ``KB_ENTITY`` row per entity with an empty ``CLONE_VARIANT``
    alongside the suffixed clones. A model whose clone family does not produce
    that canonical row would come back empty, which is why this raises rather
    than writing an empty file.
    """
    from path3_ossie_semantic_view import CORE_ENTITIES

    names = ", ".join("'" + e + "'" for e in CORE_ENTITIES)
    sql = f"""
        SELECT t.ENTITY_NAME, t.TERM_NAME, t.PHYSICAL_COLUMN, t.DATA_TYPE,
               t.TERM_ROLE, b.PHYSICAL_SCHEMA, b.PHYSICAL_OBJECT
        FROM {kb_database}.{kb_schema}.KB_TERM t
        JOIN {kb_database}.{kb_schema}.KB_V_PHYSICAL_BINDING b
          ON b.SOURCE_SYSTEM = t.SOURCE_SYSTEM
         AND b.SOURCE_MODEL  = t.SOURCE_MODEL
         AND b.ENTITY_NAME   = t.ENTITY_NAME
        WHERE t.ENTITY_NAME IN ({names})
          AND t.PHYSICAL_COLUMN IS NOT NULL
        ORDER BY b.PHYSICAL_SCHEMA, b.PHYSICAL_OBJECT, t.PHYSICAL_COLUMN
    """
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False,
                                    encoding="utf-8") as fh:
        fh.write(sql)
        path = fh.name
    try:
        proc = subprocess.run(
            ["snow", "sql", "-f", path, "-c", connection, "--format", "json"],
            capture_output=True, text=True,
        )
    finally:
        os.unlink(path)
    if proc.returncode != 0:
        raise RuntimeError(
            "core column query failed:\n"
            + (proc.stdout or "")[-4000:] + (proc.stderr or "")[-4000:]
        )
    i = proc.stdout.find("[")
    columns = json.loads(proc.stdout[i:]) if i >= 0 else []
    if not columns:
        raise RuntimeError(
            f"no core columns found in {kb_database}.{kb_schema}.KB_TERM for "
            f"{', '.join(CORE_ENTITIES)}. Load the knowledge base first, and "
            "check that each core entity has a canonical (CLONE_VARIANT = '') "
            "row in KB_ENTITY."
        )
    return columns


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate the physical layer from the KB")
    ap.add_argument("--columns", default="out/core_columns.json")
    config.add_arguments(ap)
    ap.add_argument("--connection", default="my-demo-account")
    ap.add_argument("--out-dir", default="sql")
    ap.add_argument("--execute", action="store_true", help="Run the generated SQL")
    args = ap.parse_args(argv)
    naming = config.from_args(args)

    if not os.path.exists(args.columns):
        print(f"{args.columns} absent: querying the knowledge base for it")
        columns = fetch_core_columns(naming.kb_database, naming.kb_schema,
                                     args.connection)
        os.makedirs(os.path.dirname(args.columns) or ".", exist_ok=True)
        with open(args.columns, "w", encoding="utf-8") as f:
            json.dump(columns, f, indent=2)
        print(f"  wrote {args.columns}: {len(columns)} columns")

    with open(args.columns, encoding="utf-8") as f:
        columns = json.load(f)

    core = ddl_from_kb(columns, args.database)

    header = (
        f"-- Generated by generate_physical_layer.py from KB_TERM\n"
        f"-- {len(columns)} column definitions across "
        f"{len({(r['PHYSICAL_SCHEMA'], r['PHYSICAL_OBJECT']) for r in columns})} tables.\n"
        f"-- Do not hand-edit: re-run the generator after reloading the KB.\n\n"
        f"CREATE DATABASE IF NOT EXISTS {args.database}\n"
        f"  COMMENT = 'Physical layer, schema generated from the semantic knowledge base';\n"
    )

    # This script builds and runs its own SQL instead of going through
    # build.py's run_sql_file(), which renders placeholders automatically -- so
    # the date template has to be rendered explicitly here. Forgetting it sends
    # a literal USE DATABASE {{DB}} to Snowflake.
    date_sql = config.render_sql(data_date_dimension(), naming)

    path = os.path.join(args.out_dir, "10_physical_layer.sql")
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + core)
    print(f"wrote {path}")

    date_path = os.path.join(args.out_dir, "11_date_dimension.sql")
    with open(date_path, "w", encoding="utf-8") as f:
        f.write(date_sql)
    print(f"wrote {date_path}")

    if args.execute:
        print("executing:")
        run_sql(header + core, args.connection, "physical layer DDL")
        run_sql(date_sql, args.connection, "date dimension")

    return 0


if __name__ == "__main__":
    sys.exit(main())
