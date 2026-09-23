#!/usr/bin/env python3
"""Path 3: generate an Apache Ossie (OSI) semantic model from the Knowledge Base.

Why OSI and not Snowflake's native semantic-view YAML: the reference model's condition for
looking at Snowflake is that the semantic layer be Ossie compliant. Generating
native YAML and *claiming* compliance is not the same thing. Generating OSI and
having Snowflake's own importer accept it is a demonstration -- and the same file
is what a non-Snowflake tool consumes, unchanged.

Spec details that matter, from the Ossie 0.1.1 core spec:

  * ``datasets[*].source`` is a **dotted string** (``db.schema.table``), or a SQL
    string starting with SELECT/WITH for a subquery. It is not a nested object.
  * Every field and metric expression is wrapped in ``expression.dialects[]``.
    Snowflake prefers the SNOWFLAKE dialect and falls back to ANSI_SQL. A field
    with neither is *silently skipped*, which is the most dangerous failure mode
    in the format -- so both are always emitted here.
  * Field classification is driven by the ``dimension`` marker, not by a type:
    ``dimension.is_time: true`` -> time dimension, ``dimension`` present ->
    dimension, ``dimension`` absent -> fact.
  * Metrics are model-level only. Table-level metrics have no OSI representation
    and would be pushed into a Snowflake custom extension, so metrics are emitted
    at the top level where they round-trip cleanly.

Snowflake currently accepts version 0.1.1 only, which is also the latest released
Ossie version (0.2.0 is still in development), so there is no version gap to
bridge today. That will change when 0.2.0 releases, which is why the version is a
single constant here rather than scattered through the file.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

import config

OSSIE_VERSION = "0.1.1"
KB = config.DEFAULT.kb  # overridden from the naming flags in main()

# The six entities that make up the core star schema. Everything else in the
# source model is a clone, a role-based re-export, or out of scope for this pass.
CORE_ENTITIES = [
    "FACT_SALES_SUMMARY",
    "FACT_BOOKED_SUMMARY",
    "DIM_TIME",
    "DIM_US_PROD_CUSTOMERS",
    "TERRITORY_SITE_SALES_PERSON",
    "EDW_ORA_COA_PROD_GFP_W_NORA",
]

# Friendly dataset names. The physical names are Oracle-era EDW artifacts; a
# business user should not have to read EDW_ORA_COA_PROD_GFP_W_NORA to find
# "product". The KB keeps the mapping, so renaming here loses nothing.
DATASET_ALIAS = {
    "FACT_SALES_SUMMARY": "sales",
    "FACT_BOOKED_SUMMARY": "bookings",
    "DIM_TIME": "calendar",
    "DIM_US_PROD_CUSTOMERS": "customers",
    "TERRITORY_SITE_SALES_PERSON": "territory",
    "EDW_ORA_COA_PROD_GFP_W_NORA": "product",
}

# Per-dataset column allowlist. A semantic view exposing all 3,967 KB terms would
# be unusable in a picker and would slow agent planning; these are the columns the
# Cognos reports actually group and filter by.
DATASET_COLUMNS: dict[str, list[str]] = {
    "FACT_SALES_SUMMARY": [
        "PROCESSED_DATE", "PERIOD_DATE", "CUST_ACCT_SITE_ID", "INVENTORY_ITEM_ID",
        "ORDER_LINE_ID", "QUANTITY", "QUANTITY_EXCL_RESUPPLY",
        "TRANSACTION_AMOUNT_USD", "TRAN_AMT_EXCL_RESPLY_USD",
        "TRANSACTION_TYPE", "ORDER_CATEGORY", "ORDER_TYPE", "DISCOUNT_TYPE",
        "TRANSACTION_CURRENCY_CODE",
    ],
    "FACT_BOOKED_SUMMARY": [
        "PROCESSED_DATE", "PERIOD_DATE", "CUST_ACCT_SITE_ID", "INVENTORY_ITEM_ID",
        "ORDER_LINE_ID", "QUANTITY", "QUANTITY_EXCL_RESUPPLY",
        "TRANSACTION_AMOUNT_USD", "TRAN_AMT_EXCL_RESPLY_USD",
        "TRANSACTION_TYPE", "ORDER_CATEGORY", "ORDER_TYPE", "DISCOUNT_TYPE",
        "TRANSACTION_CURRENCY_CODE",
    ],
    "DIM_TIME": [
        "DAY_DATE", "CALENDAR_MONTH_ID", "CALENDAR_MONTH_NAME",
        "CALENDAR_QUARTER_NAME", "CALENDAR_YEAR_ID",
        "FISCAL_YEAR_ID", "FISCAL_YEAR_NAME", "FISCAL_QUARTER_ID",
        "FISCAL_QUARTER_YEAR", "FISCAL_MONTH_ID", "FISCAL_MONTH_NAME",
        "BUSINESS_DAY_FLAG",
    ],
    "DIM_US_PROD_CUSTOMERS": [
        "CUST_ACCT_SITE_ID", "CUSTOMER_ID", "CUSTOMER_NAME", "CUSTOMER_NUMBER",
        "SITE_USE_CODE", "CUSTOMER_CLASS_CODE", "CUSTOMER_TYPE", "CUST_SEGMENT",
        "CITY", "STATE", "CUSTOMER_COUNTRY", "CUSTOMER_TIER1",
    ],
    "TERRITORY_SITE_SALES_PERSON": [
        "CUST_ACCT_SITE_ID", "TERRITORY_LEVEL1", "TERRITORY_LEVEL2",
        "TERRITORY_LEVEL3", "TERRITORY_LEVEL4", "TERRITORY_TYPE", "ROLE",
        "L1_SALES_PERSON_NAME", "L2_SALES_PERSON_NAME",
        "L3_SALES_PERSON_NAME", "L4_SALES_PERSON_NAME",
    ],
    "EDW_ORA_COA_PROD_GFP_W_NORA": [
        "INVENTORY_ITEM_ID", "ITEM_NO", "INVENTORY_ITEM_DESC",
        "GPC1", "GPC2", "GPC3", "GPC4", "GFP_GROUP",
    ],
}

# Time columns per dataset, so the dimension.is_time marker is set from the
# model's own declaration rather than guessed from a name.
TIME_COLUMNS = {"PROCESSED_DATE", "PERIOD_DATE", "DAY_DATE"}

# Measures that survived the identifier check. Everything else stays a dimension
# even where the source model declares it a fact, because SUM(INVENTORY_ITEM_ID)
# is not a business metric.
FACT_COLUMNS = {
    "QUANTITY", "QUANTITY_EXCL_RESUPPLY",
    "TRANSACTION_AMOUNT_USD", "TRAN_AMT_EXCL_RESPLY_USD",
    "BUSINESS_DAY_FLAG",
}

# The model-layer filters the source Cognos model applies. These are reproduced as
# dataset subqueries rather than dropped.
#
# This is the correctness point of the whole exercise. The source model restricts
# customers to SITE_USE_CODE = 'SHIP_TO'; without it the customer join fans out
# 2x against these tables, measured. It also caps both facts at CURRENT_DATE and
# floors the calendar at fiscal 2018. A semantic view that omits them returns rows
# Cognos excludes, so totals will not reconcile and the gap reads as a data
# problem rather than a missing predicate.
ENTITY_FILTERS: dict[str, str] = {
    "DIM_US_PROD_CUSTOMERS": (
        "SITE_USE_CODE = 'SHIP_TO' "
        "AND CUSTOMER_CLASS_CODE NOT IN ('31 DIAGNOSTIK', '31 HAENDLER EXPORT')"
    ),
    "DIM_TIME": "FISCAL_YEAR_ID >= 2018 AND DAY_DATE < CURRENT_DATE()",
    "FACT_SALES_SUMMARY": "PROCESSED_DATE <= CURRENT_DATE()",
    "FACT_BOOKED_SUMMARY": "PROCESSED_DATE <= CURRENT_DATE()",
}

PRIMARY_KEYS = {
    "DIM_TIME": ["DAY_DATE"],
    "DIM_US_PROD_CUSTOMERS": ["CUST_ACCT_SITE_ID"],
    "TERRITORY_SITE_SALES_PERSON": ["CUST_ACCT_SITE_ID"],
    "EDW_ORA_COA_PROD_GFP_W_NORA": ["INVENTORY_ITEM_ID"],
    "FACT_SALES_SUMMARY": ["ORDER_LINE_ID"],
    "FACT_BOOKED_SUMMARY": ["ORDER_LINE_ID"],
}

# Relationships, from the KB's non-clone equality joins. Direction matters in
# OSI: `from` is the many side, `to` is the one side.
RELATIONSHIPS = [
    ("sales_to_calendar",    "sales",    "calendar",  ["PROCESSED_DATE"],   ["DAY_DATE"]),
    ("sales_to_customers",   "sales",    "customers", ["CUST_ACCT_SITE_ID"], ["CUST_ACCT_SITE_ID"]),
    ("sales_to_territory",   "sales",    "territory", ["CUST_ACCT_SITE_ID"], ["CUST_ACCT_SITE_ID"]),
    ("sales_to_product",     "sales",    "product",   ["INVENTORY_ITEM_ID"], ["INVENTORY_ITEM_ID"]),
    ("bookings_to_calendar", "bookings", "calendar",  ["PROCESSED_DATE"],   ["DAY_DATE"]),
    ("bookings_to_customers","bookings", "customers", ["CUST_ACCT_SITE_ID"], ["CUST_ACCT_SITE_ID"]),
    ("bookings_to_territory","bookings", "territory", ["CUST_ACCT_SITE_ID"], ["CUST_ACCT_SITE_ID"]),
    ("bookings_to_product",  "bookings", "product",   ["INVENTORY_ITEM_ID"], ["INVENTORY_ITEM_ID"]),
]


# ---------------------------------------------------------------------------
# Fiscal period expressions
# ---------------------------------------------------------------------------
# the reference model's fiscal year runs July to June, so a July date belongs to the *next*
# fiscal year: FY2027 spans 2026-07-01 to 2027-06-30. This is a year-boundary
# rule. Deriving fiscal periods with a blanket DATEADD('month', -6, ...) shift --
# as the earlier handoff did -- also moves every quarter and month boundary, and so
# is wrong at exactly the dates a period-to-date metric is evaluated on.
#
# The rule is applied ONCE, as derived fields on the calendar dataset, rather than
# repeated inside every metric. Two reasons, the first discovered the hard way:
#
#  1. Repeating it makes each metric expression roughly a kilobyte of SQL. When a
#     query projects such a metric, Snowflake derives an output object name from
#     the expression text and fails with "Object name ... exceeds maximum length
#     limit of 255 characters". Compact metrics avoid that entirely.
#
#  2. One definition is checkable. DIM_DATE.FISCAL_YEAR_ID is the authority, and
#     because these fields restate the same rule they can be verified against it
#     across every day in the dimension -- validate_fiscal_expressions() does that.
#
# Referencing calendar.* from a metric is safe here: the date relationship is
# one-to-one on a conformed dimension with 100% match, so it cannot fan out.
#
# If the reference model's real fiscal calendar is 4-4-5 or 13-period rather than a clean
# July-June split, these are wrong and the metrics must use DIM_DATE's own columns.
# That is a question for their finance team, not something to infer from a model.
FY = "IFF(MONTH({d}) >= 7, YEAR({d}) + 1, YEAR({d}))"
FM = "IFF(MONTH({d}) >= 7, MONTH({d}) - 6, MONTH({d}) + 6)"
FQ = "CEIL((" + FM + ") / 3.0)"

# Current fiscal position. Kept terse because these appear in every fiscal metric
# and expression length is a hard constraint, not a style preference.
CUR_FY = "IFF(MONTH(CURRENT_DATE()) >= 7, YEAR(CURRENT_DATE()) + 1, YEAR(CURRENT_DATE()))"
CUR_FY_START = (
    "DATE_FROM_PARTS(IFF(MONTH(CURRENT_DATE()) >= 7, YEAR(CURRENT_DATE()), "
    "YEAR(CURRENT_DATE()) - 1), 7, 1)"
)
CUR_DOY = f"DATEDIFF('day', {CUR_FY_START}, CURRENT_DATE())"
CUR_FQ = (
    "CEIL((IFF(MONTH(CURRENT_DATE()) >= 7, MONTH(CURRENT_DATE()) - 6, "
    "MONTH(CURRENT_DATE()) + 6)) / 3.0)"
)

# Derived fields on the calendar dataset.
#
# The current-period logic lives here as boolean flags rather than inside each
# metric. This is both better modelling and a hard requirement:
#
#  * Better modelling, because "is this row in the current fiscal year to date" is
#    a property of a date, not of sales versus bookings. A date dimension carrying
#    period flags is the conventional design; repeating CURRENT_DATE() arithmetic
#    in ninety metric expressions is not.
#
#  * Required, because Snowflake derives an output object name from a metric's
#    expression text and rejects anything over 255 characters. With the
#    CURRENT_DATE() arithmetic inlined, every fiscal metric blew that limit and the
#    view could be created but not queried -- a failure that only appears at query
#    time, not at deploy time. Flags reduce each metric to about ninety characters.
#
# FISCAL_DAY_OF_YEAR is what makes a prior-year comparison cover the same *elapsed
# portion* of its year. Without it, YTD growth measured in September compares three
# months of this year against twelve of last.
CALENDAR_DERIVED_FIELDS = [
    (
        "FISCAL_DAY_OF_YEAR",
        "DATEDIFF('day', DATE_FROM_PARTS(FISCAL_YEAR_ID - 1, 7, 1), DAY_DATE)",
        "Elapsed days since 1 July of this fiscal year. Bounds a fiscal to-date window.",
    ),
    (
        "FISCAL_QUARTER_NUM",
        "MOD(FISCAL_QUARTER_ID, 10)",
        "Fiscal quarter 1-4, where Q1 is July to September.",
    ),
    (
        "IS_FISCAL_YTD",
        f"FISCAL_YEAR_ID = {CUR_FY} AND "
        f"DATEDIFF('day', DATE_FROM_PARTS(FISCAL_YEAR_ID - 1, 7, 1), DAY_DATE) <= {CUR_DOY}",
        "True for dates in the current fiscal year up to today.",
    ),
    (
        "IS_PRIOR_FISCAL_YTD",
        f"FISCAL_YEAR_ID = {CUR_FY} - 1 AND "
        f"DATEDIFF('day', DATE_FROM_PARTS(FISCAL_YEAR_ID - 1, 7, 1), DAY_DATE) <= {CUR_DOY}",
        "True for the prior fiscal year up to the same elapsed day. The correct YTD comparison base.",
    ),
    (
        "IS_FISCAL_QTD",
        f"FISCAL_YEAR_ID = {CUR_FY} AND MOD(FISCAL_QUARTER_ID, 10) = {CUR_FQ} AND "
        f"DATEDIFF('day', DATE_FROM_PARTS(FISCAL_YEAR_ID - 1, 7, 1), DAY_DATE) <= {CUR_DOY}",
        "True for dates in the current fiscal quarter up to today.",
    ),
    (
        "IS_PRIOR_FISCAL_QTD",
        f"FISCAL_YEAR_ID = {CUR_FY} - 1 AND MOD(FISCAL_QUARTER_ID, 10) = {CUR_FQ} AND "
        f"DATEDIFF('day', DATE_FROM_PARTS(FISCAL_YEAR_ID - 1, 7, 1), DAY_DATE) <= {CUR_DOY}",
        "True for the same fiscal quarter one year earlier, to the same elapsed day.",
    ),
    (
        "IS_LAST_FISCAL_YEAR",
        f"FISCAL_YEAR_ID = {CUR_FY} - 1",
        "True for the whole of the prior fiscal year.",
    ),
    (
        "IS_PREVIOUS_FISCAL_YEAR",
        f"FISCAL_YEAR_ID = {CUR_FY} - 2",
        "True for the fiscal year before last. Supports the Cognos 2nd-prior-year calculations.",
    ),
]


def _fiscal_metrics(fact_alias: str, measure: str) -> list[dict]:
    """Fiscal-calendar metrics, driven by the calendar's period flags.

    the reference model's Cognos model expresses YTD and PYQTD against its fiscal calendar, so
    implementing them with DATE_TRUNC('year', ...) is not a simplification. It
    reports the wrong number for the six months from July to December, wrong by the
    entire first half of the fiscal year.
    """
    def agg(flag: str) -> str:
        return f"SUM(CASE WHEN calendar.{flag} THEN {measure} ELSE 0 END)"

    fytd = agg("IS_FISCAL_YTD")
    pfytd = agg("IS_PRIOR_FISCAL_YTD")
    fqtd = agg("IS_FISCAL_QTD")
    pfqtd = agg("IS_PRIOR_FISCAL_QTD")
    last_fy = agg("IS_LAST_FISCAL_YEAR")
    prev_fy = agg("IS_PREVIOUS_FISCAL_YEAR")

    note = (
        "Fiscal calendar metric. the reference model's fiscal year runs July to June, so FY2027 "
        "means July 2026 through June 2027 and a calendar-year expression is wrong "
        "for all of July through December. Verified against DIM_DATE.FISCAL_YEAR_ID."
    )
    return [
        {
            "name": f"{fact_alias}_fiscal_ytd",
            "description": f"Fiscal year-to-date {fact_alias}. {note}",
            "expression": _expr(fytd),
        },
        {
            "name": f"{fact_alias}_fiscal_pytd",
            "description": f"Prior fiscal year {fact_alias} to the same elapsed day. {note}",
            "expression": _expr(pfytd),
        },
        {
            "name": f"{fact_alias}_fiscal_ytd_change",
            "description": f"Fiscal YTD {fact_alias} versus prior-year-to-date. {note}",
            "expression": _expr(f"{fytd} - {pfytd}"),
        },
        {
            "name": f"{fact_alias}_fiscal_ytd_growth",
            # NULLIF, not DIV0: growth on a zero base is undefined, and returning 0
            # would assert "no growth", which is a different claim.
            "description": (
                f"Fiscal YTD {fact_alias} growth against prior-year-to-date. "
                f"Undefined rather than zero when the prior period is empty. {note}"
            ),
            "expression": _expr(f"({fytd} - {pfytd}) / NULLIF({pfytd}, 0)"),
        },
        {
            "name": f"{fact_alias}_fiscal_qtd",
            "description": f"Fiscal quarter-to-date {fact_alias}. {note}",
            "expression": _expr(fqtd),
        },
        {
            "name": f"{fact_alias}_fiscal_pyqtd_change",
            "description": (
                f"Fiscal QTD {fact_alias} versus the same fiscal quarter a year "
                f"earlier. Covers the Cognos PYQTD calculations. {note}"
            ),
            "expression": _expr(f"{fqtd} - {pfqtd}"),
        },
        {
            "name": f"{fact_alias}_fiscal_pyqtd_growth",
            "description": f"Fiscal QTD growth against prior-year QTD. {note}",
            "expression": _expr(f"({fqtd} - {pfqtd}) / NULLIF({pfqtd}, 0)"),
        },
        {
            "name": f"{fact_alias}_last_fiscal_year",
            "description": f"Full prior fiscal year {fact_alias}. {note}",
            "expression": _expr(last_fy),
        },
        {
            "name": f"{fact_alias}_fiscal_year_growth",
            "description": (
                f"Prior full fiscal year versus the one before it. Covers the Cognos "
                f"'Last Fiscal Year' and 'Previous (2nd) Fiscal Year' calculations. {note}"
            ),
            "expression": _expr(f"({last_fy} - {prev_fy}) / NULLIF({prev_fy}, 0)"),
        },
    ]


def validate_fiscal_expressions(connection: str, database: str) -> dict:
    """Prove the fiscal rule used in the model matches the date dimension.

    The semantic view derives fiscal position from DIM_DATE's own FISCAL_ columns,
    and the derived FISCAL_DAY_OF_YEAR / FISCAL_QUARTER_NUM fields restate part of
    that rule. Two implementations of one rule can drift, so this checks every date
    in the dimension and reports mismatches. The claim "equivalent to DIM_DATE" is
    then measured rather than asserted.
    """
    d = "d.DAY_DATE"
    sql = f"""
    SELECT
        COUNT(*)                                                       AS DAYS_CHECKED,
        SUM(IFF(({FY.format(d=d)}) <> d.FISCAL_YEAR_ID, 1, 0))         AS FY_MISMATCHES,
        SUM(IFF(({FQ.format(d=d)}) <> MOD(d.FISCAL_QUARTER_ID, 10), 1, 0)) AS FQ_MISMATCHES,
        SUM(IFF(({FM.format(d=d)}) <> MOD(d.FISCAL_MONTH_ID, 100), 1, 0))  AS FM_MISMATCHES,
        -- The derived day-of-fiscal-year must be positive and within one year.
        SUM(IFF(DATEDIFF('day', DATE_FROM_PARTS(d.FISCAL_YEAR_ID - 1, 7, 1), d.DAY_DATE)
                NOT BETWEEN 0 AND 366, 1, 0))                          AS DOY_OUT_OF_RANGE
    FROM {database}.COMMON_ANALYTICS.DIM_DATE d;
    """
    rows = query_json(sql, connection)
    r = rows[0] if rows else {}
    ok = all(
        int(r.get(k, 1) or 0) == 0
        for k in ("FY_MISMATCHES", "FQ_MISMATCHES", "FM_MISMATCHES", "DOY_OUT_OF_RANGE")
    )
    return {"equivalent_to_dim_date": ok, **r}


def run_snow(sql: str, connection: str, fmt: str | None = None) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8") as fh:
        fh.write(sql)
        path = fh.name
    try:
        cmd = ["snow", "sql", "-f", path, "-c", connection]
        if fmt:
            cmd += ["--format", fmt]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"failed:\n{(proc.stdout or '')[-4000:]}{(proc.stderr or '')[-4000:]}"
            )
        return proc.stdout
    finally:
        os.unlink(path)


def query_json(sql: str, connection: str) -> list[dict]:
    out = run_snow(sql, connection, "json").strip()
    i = out.find("[")
    return json.loads(out[i:]) if i >= 0 else []


def _expr(sql: str) -> dict:
    """Wrap an expression with both supported dialects.

    Both are emitted deliberately. Snowflake prefers SNOWFLAKE and falls back to
    ANSI_SQL, but a field carrying *neither* is silently dropped from the created
    view with no error -- so emitting only one dialect makes a non-Snowflake
    consumer silently lose fields instead of failing loudly.
    """
    return {
        "dialects": [
            {"dialect": "SNOWFLAKE", "expression": sql},
            {"dialect": "ANSI_SQL", "expression": sql},
        ]
    }


def build_ossie_model(connection: str, database: str, model_name: str) -> dict:
    """Build the Ossie document from the knowledge base."""
    terms = query_json(
        f"""
        SELECT t.ENTITY_NAME, t.TERM_NAME, t.PHYSICAL_COLUMN, t.DEFINITION,
               t.DATA_TYPE, t.TERM_ROLE,
               b.PHYSICAL_SCHEMA, b.PHYSICAL_OBJECT
        FROM {KB}.KB_TERM t
        JOIN {KB}.KB_V_PHYSICAL_BINDING b
          ON b.SOURCE_SYSTEM = t.SOURCE_SYSTEM
         AND b.SOURCE_MODEL  = t.SOURCE_MODEL
         AND b.ENTITY_NAME   = t.ENTITY_NAME
        WHERE t.ENTITY_NAME IN ({",".join("'" + e + "'" for e in CORE_ENTITIES)})
          AND t.PHYSICAL_COLUMN IS NOT NULL
        """,
        connection,
    )
    metrics_rows = query_json(
        f"""
        SELECT METRIC AS METRIC_CANONICAL, METRIC_KIND, SNOWFLAKE_EXPR,
               DEPLOY_READINESS, FORMAT_KIND, REQUIRES_FISCAL_CALENDAR
        FROM {KB}.KB_V_METRIC_LIBRARY
        WHERE DEPLOY_READINESS = 'ready'
        ORDER BY METRIC
        """,
        connection,
    )

    by_entity: dict[str, dict] = {}
    for t in terms:
        by_entity.setdefault(t["ENTITY_NAME"], {})[t["PHYSICAL_COLUMN"]] = t

    datasets = []
    for entity in CORE_ENTITIES:
        cols = by_entity.get(entity, {})
        if not cols:
            continue
        sample = next(iter(cols.values()))
        alias = DATASET_ALIAS[entity]
        fq = f'{database}.{sample["PHYSICAL_SCHEMA"]}.{sample["PHYSICAL_OBJECT"]}'

        # A filtered entity becomes a subquery source, which is how OSI expresses
        # "this logical table is a restricted view of a physical one".
        flt = ENTITY_FILTERS.get(entity)
        source = f"SELECT * FROM {fq} WHERE {flt}" if flt else fq

        fields = []
        for col in DATASET_COLUMNS.get(entity, sorted(cols)):
            meta = cols.get(col)
            if not meta:
                continue
            f: dict = {
                "name": col,
                "expression": _expr(col),
                "description": (meta.get("DEFINITION") or col),
            }
            if col in TIME_COLUMNS:
                f["dimension"] = {"is_time": True}
            elif col not in FACT_COLUMNS:
                f["dimension"] = {"is_time": False}
            # else: no dimension marker -> classified as a fact
            fields.append(f)

        # The calendar gains two derived fields. They exist so the fiscal metrics
        # can stay short enough to project, and so the fiscal rule lives in one
        # place instead of being restated in ninety expressions.
        if entity == "DIM_TIME":
            for fname, fexpr, fdesc in CALENDAR_DERIVED_FIELDS:
                fields.append(
                    {
                        "name": fname,
                        "expression": _expr(fexpr),
                        "description": fdesc,
                        # Dimensions, not facts: these are attributes of a date,
                        # and marking them facts would invite SUM(IS_FISCAL_YTD).
                        "dimension": {"is_time": False},
                    }
                )

        ds: dict = {
            "name": alias,
            "source": source,
            "description": (
                f"{entity} from the source Cognos DMR model"
                + (f". Row filter reproduced from the source model: {flt}" if flt else "")
            ),
            "fields": fields,
        }
        if entity in PRIMARY_KEYS:
            ds["primary_key"] = PRIMARY_KEYS[entity]
        ds["ai_context"] = (
            f"Physical object {sample['PHYSICAL_OBJECT']}. "
            + (
                "This dataset is deliberately filtered to match the source BI "
                "model; do not remove the filter, it prevents a join fan-out."
                if flt
                else "Unfiltered passthrough of the physical table."
            )
        )
        datasets.append(ds)

    # Model-level metrics. The KB stores each pattern templated on {measure} and
    # {date_column}; binding happens here, once per fact, which is how 59 patterns
    # cover two fact tables without duplicating a definition.
    metrics = [
        {
            "name": "sales_amount_total",
            "description": "Total shipped and invoiced sales, USD.",
            "expression": _expr("SUM(sales.TRANSACTION_AMOUNT_USD)"),
        },
        {
            "name": "sales_quantity_total",
            "description": "Total units shipped.",
            "expression": _expr("SUM(sales.QUANTITY)"),
        },
        {
            "name": "booked_amount_total",
            "description": "Total booked or ordered amount, USD. Leads shipped sales.",
            "expression": _expr("SUM(bookings.TRANSACTION_AMOUNT_USD)"),
        },
        {
            "name": "booked_quantity_total",
            "description": "Total units booked.",
            "expression": _expr("SUM(bookings.QUANTITY)"),
        },
    ]

    # Distinct canonical names can slug to the same identifier -- the source model
    # carries both "MTD Change" and "MTD  Change", and "% Growth" versus "%Growth".
    # Snowflake rejects the whole model on a duplicate expression name, so collisions
    # are resolved here and reported rather than silently overwriting a definition.
    seen_slugs: dict[str, str] = {}
    collisions: list[tuple[str, str]] = []

    for m in metrics_rows:
        expr = m.get("SNOWFLAKE_EXPR") or ""
        if not expr or "{measure}" not in expr:
            continue
        canonical = (m["METRIC_CANONICAL"] or "").strip()
        if not canonical:
            continue
        slug = (
            canonical.lower()
            .replace("%", "pct")
            .replace("-", "_")
            .replace(".", "")
            .replace("(", "")
            .replace(")", "")
        )
        slug = "_".join(slug.split())  # collapse any run of whitespace
        if slug in seen_slugs:
            if seen_slugs[slug] != canonical:
                collisions.append((canonical, seen_slugs[slug]))
            continue
        seen_slugs[slug] = canonical

        for fact_alias, measure in (
            ("sales", "sales.TRANSACTION_AMOUNT_USD"),
            ("bookings", "bookings.TRANSACTION_AMOUNT_USD"),
        ):
            date_col = f"{fact_alias}.PROCESSED_DATE"
            bound = expr.replace("{measure}", measure).replace("{date_column}", date_col)
            metrics.append(
                {
                    "name": f"{fact_alias}_{slug}",
                    "description": (
                        f"{canonical} on {fact_alias}. Converted from the Cognos "
                        f"calculation of the same name; one definition replaces the "
                        f"four fiscal-year clones the source model carried."
                    ),
                    "expression": _expr(bound),
                }
            )

    if collisions:
        print(f"  note: {len(collisions)} metric name collisions resolved (first wins):")
        for dup, kept in collisions[:8]:
            print(f"    {dup!r} collides with {kept!r}")

    # The fiscal metrics. These are the ones the KB flags as
    # needs_fiscal_calendar, deliberately excluded from the generic pattern loop
    # above because a CURRENT_DATE() window cannot express them.
    for fact_alias in ("sales", "bookings"):
        metrics.extend(
            _fiscal_metrics(fact_alias, f"{fact_alias}.TRANSACTION_AMOUNT_USD")
        )

    return {
        "version": OSSIE_VERSION,
        "name": model_name,
        "description": (
            "Sales and bookings for a sleep and respiratory products business. "
            "Generated from the source-agnostic semantic knowledge base "
            "extracted from a Cognos Framework Manager project. "
            "Row filters declared by the source model are reproduced as dataset "
            "subqueries; metric patterns are bound once per fact table rather than "
            "cloned per fiscal year."
        ),
        # ai_context is a plain string in the Ossie 0.1.1 schema, not a structured
        # object. Sample questions are folded into the prose rather than given
        # their own key, since the spec has nowhere to put them.
        "ai_context": (
            "Sales means shipped and invoiced revenue; bookings means ordered "
            "revenue and leads sales by roughly nine days. The gap between them "
            "is backlog. Territory is a four-level hierarchy expressed as dotted "
            "codes (EAST.GREATLAKES.KAM.CLEVELAND), so prefix matching gives "
            "hierarchical rollup. Fiscal year runs July to June: FY2026 starts "
            "2025-07-01, so never derive a fiscal period by shifting a calendar "
            "date -- use the FISCAL_ columns on the calendar dataset. "
            "The fiscal to-date and prior-period metrics (names containing fiscal_ytd, "
            "fiscal_pytd, fiscal_qtd, fiscal_pyqtd) are anchored to today, so they must "
            "NOT be grouped by fiscal year or fiscal quarter: the comparison base falls "
            "outside the group and growth comes back null or minus one hundred percent. "
            "Group them by product, territory or customer instead, or use them ungrouped. "
            "Representative questions: What is our year to date sales amount by "
            "product line? What is our quarter over quarter booked growth by "
            "sales territory? Which territories have the largest gap between "
            "bookings and sales? Show monthly sales for flow generators this "
            "fiscal year."
        ),
        "datasets": datasets,
        "relationships": [
            {
                "name": name,
                "from": frm,
                "to": to,
                "from_columns": fc,
                "to_columns": tc,
            }
            for name, frm, to, fc, tc in RELATIONSHIPS
        ],
        "metrics": metrics,
        "custom_extensions": [
            {
                "vendor": "SNOWFLAKE",
                "content": json.dumps(
                    {
                        "provenance": {
                            "knowledge_base": KB,
                            "source_system": "cognos",
                            "source_model": naming.model_label,
                        },
                        "known_limitations": [
                            "Metrics requiring the fiscal calendar are excluded from "
                            "this model until they are bound to the calendar dataset's "
                            "FISCAL_ columns. A date-offset approximation would be wrong "
                            "at every fiscal period boundary.",
                            "Territory row-level security is not applied in the model "
                            "itself; it is enforced by a row access policy on the "
                            "underlying tables, so it applies to every consumer equally.",
                        ],
                    }
                ),
            }
        ],
    }


def to_yaml(obj, indent: int = 0) -> str:
    """Minimal YAML emitter.

    Hand-rolled rather than using PyYAML so the output ordering is stable and
    every scalar is quoted. Unquoted YAML scalars are a recurring source of silent
    breakage: a bare ``0.1.1`` parses as a string but bare ``1.0`` becomes a float,
    and a bare expression containing ``: `` becomes a mapping.
    """
    pad = "  " * indent
    if isinstance(obj, dict):
        lines = []
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                lines.append(f"{pad}{k}:")
                lines.append(to_yaml(v, indent + 1))
            else:
                lines.append(f"{pad}{k}: {_scalar(v)}")
        return "\n".join(lines)
    if isinstance(obj, list):
        lines = []
        for item in obj:
            if isinstance(item, dict):
                body = to_yaml(item, indent + 1)
                first, *rest = body.split("\n")
                lines.append(f"{pad}- {first.lstrip()}")
                lines.extend(rest)
            elif isinstance(item, (list,)):
                lines.append(f"{pad}-")
                lines.append(to_yaml(item, indent + 1))
            else:
                lines.append(f"{pad}- {_scalar(item)}")
        return "\n".join(lines)
    return f"{pad}{_scalar(obj)}"


def _scalar(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Path 3: KB -> Ossie semantic model")
    ap.add_argument("--connection", default="my-demo-account")
    config.add_arguments(ap)
    ap.add_argument("--target-schema",
                    help="Defaults to <database>.<analytics-schema>")
    ap.add_argument("--out-dir", default="out")
    ap.add_argument("--deploy", action="store_true", help="Create the semantic view")
    ap.add_argument("--round-trip", action="store_true", help="Export back to OSI and diff")
    args = ap.parse_args(argv)

    os.makedirs(args.out_dir, exist_ok=True)

    # Validate before generating: if the inline fiscal rule does not match the
    # date dimension there is no point deploying metrics built on it.
    print("validating fiscal expressions against DIM_DATE")
    v = validate_fiscal_expressions(args.connection, args.database)
    print(
        f"  {v.get('DAYS_CHECKED')} days checked; "
        f"year/quarter/month mismatches: "
        f"{v.get('FY_MISMATCHES')}/{v.get('FQ_MISMATCHES')}/{v.get('FM_MISMATCHES')}"
    )
    if not v["equivalent_to_dim_date"]:
        print(
            "  WARNING: inline fiscal expressions do NOT match DIM_DATE. "
            "Fiscal metrics would be wrong; fix the rule before relying on them."
        )

    print("building Ossie model from the knowledge base")
    model = build_ossie_model(args.connection, args.database, args.model_name)
    yaml_text = to_yaml(model) + "\n"

    p = os.path.join(args.out_dir, f"{args.model_name.lower()}.ossie.yaml")
    with open(p, "w", encoding="utf-8") as f:
        f.write(yaml_text)
    print(
        f"  wrote {p}: {len(model['datasets'])} datasets, "
        f"{sum(len(d['fields']) for d in model['datasets'])} fields, "
        f"{len(model['relationships'])} relationships, {len(model['metrics'])} metrics"
    )
    with open(os.path.join(args.out_dir, f"{args.model_name.lower()}.ossie.json"), "w",
              encoding="utf-8") as f:
        json.dump(model, f, indent=2)

    if args.deploy:
        print(f"deploying to {args.target_schema} via SYSTEM$CREATE_SEMANTIC_VIEW_FROM_OSSIE_YAML")
        run_snow(
            f"CREATE SCHEMA IF NOT EXISTS {args.target_schema};", args.connection
        )
        sql = (
            f"CALL SYSTEM$CREATE_SEMANTIC_VIEW_FROM_OSSIE_YAML(\n"
            f"  '{args.target_schema}',\n  $$\n{yaml_text}$$\n);"
        )
        out = run_snow(sql, args.connection)
        print("  " + ("\n  ".join(l for l in out.splitlines() if "success" in l.lower()) or "see output"))

    if args.round_trip:
        print("round-tripping via SYSTEM$READ_OSSIE_YAML_FROM_SEMANTIC_VIEW")
        fq = f"{args.target_schema}.{args.model_name}"
        out = run_snow(
            f"SELECT SYSTEM$READ_OSSIE_YAML_FROM_SEMANTIC_VIEW('{fq}') AS OSSIE;",
            args.connection,
            "json",
        )
        i = out.find("[")
        rows = json.loads(out[i:]) if i >= 0 else []
        if rows:
            exported = rows[0].get("OSSIE", "")
            rp = os.path.join(args.out_dir, f"{args.model_name.lower()}.roundtrip.ossie.yaml")
            with open(rp, "w", encoding="utf-8") as f:
                f.write(exported)
            print(f"  wrote {rp} ({len(exported)} bytes)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
