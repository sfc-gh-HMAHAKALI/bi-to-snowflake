#!/usr/bin/env python3
"""Emit a bi-modernization inventory from the extracted knowledge base.

The React generator in the bi-modernization skill takes an "enriched inventory"
dict. It was written for Tableau and Power BI, but the contract it actually reads
is source-agnostic: dashboards, dimensions, measures, facts, filters, tables,
snowflake_target. So the knowledge base can satisfy it, and the generator's tested
chart and page emission gets reused rather than reimplemented.

Two decisions worth knowing, because both are visible in the output.

Dashboards are SYNTHESISED, not extracted. Cognos Framework Manager is a modelling
layer: it declares query subjects, determinants and hierarchies, and it declares no
dashboards at all -- those live in Cognos Analytics reports, which the source did not
send. Rather than leave the generator with nothing (it falls back to a single page
called "Dashboard"), subject areas are grouped into reporting pages here. That is
our construction and is labelled as such wherever it surfaces. Presenting invented
dashboard fidelity as extracted would undermine the findings that are genuine.

Dimensions are NARROWED to entities reachable from the facts in scope. The model
declares 3,169 attributes; the six entities behind the semantic view carry 217.
Emitting all 3,169 produces a filter panel nobody can use and implies a parity
obligation for fields no report reads.

Usage:
    python3 kb_to_inventory.py [--connection NAME] [-o out/bim_inventory.json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import config

HERE = os.path.dirname(os.path.abspath(__file__))

# The six entities behind the semantic view, keyed by KB entity name. Held as a
# literal rather than discovered because "in scope" is a modelling decision, not a
# property of the model: the KB legitimately describes 195 entities and only these
# six were converted.
IN_SCOPE = {
    "FACT_SALES_SUMMARY":          ("SALES",      "fact"),
    "FACT_BOOKED_SUMMARY":         ("BOOKINGS",   "fact"),
    "DIM_TIME":                    ("CALENDAR",   "dimension"),
    "DIM_US_PROD_CUSTOMERS":       ("CUSTOMERS",  "dimension"),
    "EDW_ORA_COA_PROD_GFP_W_NORA": ("PRODUCT",    "dimension"),
    "TERRITORY_SITE_SALES_PERSON": ("TERRITORY",  "dimension"),
}

# Reporting pages, mapped to the views created by sql/45_reporting_views.sql.
# chart_type_echarts is stated explicitly rather than inferred: the generator's
# inference was built for Tableau mark types, which a Cognos model does not carry,
# so leaving it to guess produces bar charts for everything including time series.
DASHBOARDS = [
    {
        "name": "Performance Overview",
        "synthesised": True,
        "view": "RPT_KPI_SUMMARY",
        "sheets": [
            {"name": "Sales vs bookings by fiscal period", "chart_type_echarts": "line",
             "view": "RPT_SALES_BY_PERIOD",
             "x": "FISCAL_MONTH_NAME", "y": "SALES_AMOUNT_TOTAL"},
            {"name": "Sales by product group", "chart_type_echarts": "bar_h",
             "view": "RPT_SALES_BY_PRODUCT",
             "x": "GPC1", "y": "SALES_AMOUNT_TOTAL"},
        ],
    },
    {
        "name": "Territory Performance",
        "synthesised": True,
        "view": "RPT_SALES_BY_TERRITORY",
        "sheets": [
            {"name": "Sales by region", "chart_type_echarts": "bar",
             "view": "RPT_SALES_BY_TERRITORY",
             "x": "TERRITORY_LEVEL1", "y": "SALES_AMOUNT_TOTAL"},
            {"name": "Territory detail", "chart_type_echarts": "table",
             "view": "RPT_SALES_BY_TERRITORY",
             "x": "TERRITORY_LEVEL2", "y": "SALES_AMOUNT_TOTAL"},
        ],
    },
    {
        "name": "Product Analysis",
        "synthesised": True,
        "view": "RPT_SALES_BY_PRODUCT",
        "sheets": [
            {"name": "Bookings by product group", "chart_type_echarts": "bar",
             "view": "RPT_SALES_BY_PRODUCT",
             "x": "GPC1", "y": "BOOKED_AMOUNT_TOTAL"},
            {"name": "Units by product family", "chart_type_echarts": "bar_h",
             "view": "RPT_SALES_BY_PRODUCT",
             "x": "GFP_GROUP", "y": "SALES_QUANTITY_TOTAL"},
        ],
    },
    {
        "name": "Customer Analysis",
        "synthesised": True,
        "view": "RPT_SALES_BY_CUSTOMER",
        "sheets": [
            {"name": "Sales by country", "chart_type_echarts": "bar",
             "view": "RPT_SALES_BY_CUSTOMER",
             "x": "CUSTOMER_COUNTRY", "y": "SALES_AMOUNT_TOTAL"},
            {"name": "Top customers", "chart_type_echarts": "table",
             "view": "RPT_SALES_BY_CUSTOMER",
             "x": "CUSTOMER_NAME", "y": "SALES_AMOUNT_TOTAL"},
        ],
    },
]

# Filters offered in the UI. Restricted to dimensions that actually discriminate:
# a filter on a near-unique column is a search box pretending to be a filter.
FILTER_FIELDS = [
    {"name": "FISCAL_YEAR_NAME",   "data_type": "text", "table": "CALENDAR"},
    {"name": "FISCAL_QUARTER_YEAR", "data_type": "text", "table": "CALENDAR"},
    {"name": "TERRITORY_LEVEL1",   "data_type": "text", "table": "TERRITORY"},
    {"name": "TERRITORY_LEVEL2",   "data_type": "text", "table": "TERRITORY"},
    {"name": "GPC1",               "data_type": "text", "table": "PRODUCT"},
    {"name": "CUSTOMER_COUNTRY",   "data_type": "text", "table": "CUSTOMERS"},
]


def connect(connection: str):
    import snowflake.connector
    try:
        return snowflake.connector.connect(connection_name=connection)
    except TypeError:
        # Connector predates connection_name. Reading the TOML by hand needs
        # tomllib, which is 3.11+, so this path is a genuine fallback rather than
        # a preference.
        import tomllib
        cfg = os.path.expanduser("~/.snowflake/connections.toml")
        with open(cfg, "rb") as fh:
            conf = tomllib.load(fh)[connection]
        return snowflake.connector.connect(**conf)


def rows(cur, sql: str) -> list[tuple]:
    cur.execute(sql)
    return cur.fetchall()


def build(cur, naming: config.Naming) -> dict:
    kb = naming.kb
    db = naming.database
    semantic_view = naming.semantic_view
    scope_list = ", ".join(f"'{e}'" for e in IN_SCOPE)

    # Dimensions: attributes on in-scope entities. PHYSICAL_COLUMN is what the
    # reporting views actually expose, so it is the name the app must query;
    # TERM_NAME is the business label.
    dims = []
    for entity, term, phys, dtype, defn in rows(cur, f"""
        SELECT ENTITY_NAME, TERM_NAME, PHYSICAL_COLUMN, DATA_TYPE, DEFINITION
        FROM {kb}.KB_TERM
        WHERE ENTITY_NAME IN ({scope_list}) AND TERM_ROLE = 'attribute'
        ORDER BY ENTITY_NAME, TERM_NAME
    """):
        logical, _kind = IN_SCOPE[entity]
        dims.append({
            "name": phys or term,
            "label": term,
            "table": logical,
            "data_type": (dtype or "text").lower(),
            "description": defn or "",
            "source_entity": entity,
        })

    # Measures: the semantic view's metrics are the authority, because those are
    # the ones with a validated expression. KB_METRIC carries the business
    # definition and format, so the two are joined on name where possible.
    kb_meta = {
        name: {"definition": defn or "", "format": fmt or "number",
               "decimals": dec if dec is not None else 0,
               "currency": ccy or "", "certified": bool(cert)}
        for name, defn, fmt, dec, ccy, cert in rows(cur, f"""
            SELECT METRIC_NAME, DEFINITION, FORMAT_KIND, FORMAT_DECIMALS,
                   CURRENCY_CODE, IS_CERTIFIED
            FROM {kb}.KB_METRIC
        """)
    }

    measures = []
    for (name,) in rows(cur, f"""
        SELECT NAME FROM {db}.INFORMATION_SCHEMA.SEMANTIC_METRICS
        WHERE SEMANTIC_VIEW_NAME = '{semantic_view}' ORDER BY NAME
    """):
        meta = kb_meta.get(name, {})
        measures.append({
            "name": name,
            "label": name.replace("_", " ").title(),
            "aggregation": "sum",
            "description": meta.get("definition", ""),
            "format": meta.get("format", "number"),
            "decimals": meta.get("decimals", 0),
            "currency": meta.get("currency", ""),
            "certified": meta.get("certified", False),
        })

    # Drill paths, in declared level order. These are the hierarchies the Cognos
    # model declares -- the one part of the app's navigation that is genuinely
    # extracted rather than designed.
    #
    # Scoped to IN_SCOPE, like `dims` above. Unscoped, this returns every named
    # set and calculation grouping in the whole source model ("Time Cube",
    # "MTD Grouped", "Copy of UNKNOWN") -- hundreds of entries, almost none of
    # which map to a column the deployed semantic view can query. Each entry
    # also carries source_entity, so composition-rules.md's "whose root
    # dimension appears in a fact-joined table" filter can be applied
    # mechanically instead of by eye.
    hierarchies: dict[tuple[str, str, str], list[dict]] = {}
    for dim, hier, entity, ordinal, level, caption in rows(cur, f"""
        SELECT l.DIMENSION_NAME, l.HIERARCHY_NAME, l.SOURCE_ENTITY,
               l.LEVEL_ORDINAL, l.LEVEL_NAME, l.CAPTION_TERM
        FROM {kb}.KB_HIERARCHY_LEVEL l
        WHERE COALESCE(l.IS_ALL_LEVEL, FALSE) = FALSE
          AND l.SOURCE_ENTITY IN ({scope_list})
        ORDER BY l.DIMENSION_NAME, l.HIERARCHY_NAME, l.LEVEL_ORDINAL
    """):
        hierarchies.setdefault((dim, hier, entity), []).append(
            {"ordinal": int(ordinal), "level": level, "caption": caption})

    drill_paths = [
        {"dimension": d, "hierarchy": h, "source_entity": e,
         "table": IN_SCOPE[e][0], "levels": lv}
        for (d, h, e), lv in hierarchies.items() if len(lv) > 1
    ]

    tables = [
        {"name": logical, "snowflake_name": logical, "kind": kind,
         "source_entity": entity}
        for entity, (logical, kind) in IN_SCOPE.items()
    ]

    return {
        "source_type": "cognos",
        "source_model": naming.model_label,
        "snowflake_target": {"database": naming.database,
                             "schema": naming.analytics_schema},
        "semantic_view": "%s.%s" % (naming.analytics, naming.semantic_view),
        "dashboards": DASHBOARDS,
        "worksheets": [s for d in DASHBOARDS for s in d["sheets"]],
        "dimensions": dims,
        "measures": measures,
        "metrics": [],
        "facts": [],
        "filters": FILTER_FIELDS,
        "parameters": FILTER_FIELDS,
        "tables": tables,
        "drill_paths": drill_paths,
        "provenance": {
            "dashboards_synthesised": True,
            "dashboards_note": (
                "Cognos Framework Manager is a modelling layer and declares no "
                "dashboards. These reporting pages are our construction, grouped "
                "from subject areas. Dimensions, measures, hierarchies and joins "
                "are extracted from the model."
            ),
            "dimensions_narrowed_to": sorted(IN_SCOPE),
        },
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--connection", default="my-demo-account")
    config.add_arguments(ap)
    ap.add_argument("-o", "--output",
                    default=os.path.join(HERE, "out", "bim_inventory.json"))
    args = ap.parse_args(argv)
    naming = config.from_args(args)

    conn = connect(args.connection)
    try:
        cur = conn.cursor()
        inv = build(cur, naming)
    finally:
        conn.close()

    # An empty knowledge base yields a structurally valid inventory describing
    # nothing, and the generator would happily build an app around it. The failure
    # would surface as an empty dashboard several steps later, which is the same
    # trap that wasted a rebuild once already.
    if not inv["measures"]:
        print("ERROR: no measures found. The knowledge base or semantic view is "
              "empty -- refusing to emit an inventory that would generate an "
              "app with nothing in it.", file=sys.stderr)
        return 2
    if not inv["dimensions"]:
        print("ERROR: no dimensions found for the in-scope entities.",
              file=sys.stderr)
        return 2

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(inv, fh, indent=2)

    print(f"wrote {args.output}")
    print(f"  dashboards  {len(inv['dashboards'])} (synthesised)")
    print(f"  sheets      {len(inv['worksheets'])}")
    print(f"  dimensions  {len(inv['dimensions'])} "
          f"(narrowed from the full model to {len(IN_SCOPE)} entities)")
    print(f"  measures    {len(inv['measures'])}")
    print(f"  drill paths {len(inv['drill_paths'])}")
    print(f"  tables      {len(inv['tables'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
