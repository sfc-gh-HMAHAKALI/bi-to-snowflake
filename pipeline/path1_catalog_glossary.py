#!/usr/bin/env python3
"""Path 1: populate the Horizon catalog, a business glossary, and tags from the KB.

This path deliberately requires **no semantic view and no agent**. It is the
answer to "we have not decided whether we want a Snowflake semantic layer": the
knowledge base already carries enough to make the warehouse self-describing and
governed, and every artifact here is standard Snowflake metadata that any tool
can read.

Four outputs, in increasing order of commitment:

  1. Object and column comments   -- Horizon descriptions, searchable in Snowsight
  2. Tag taxonomy                 -- subject area, conversion tier, stewardship
  3. BUSINESS_GLOSSARY table      -- a governed term list with lineage
  4. Ontology handoff             -- entity/relationship proposal for the
                                     ontology-stack-builder skill

Terms whose definition was auto-generated from the column name are written with
that caveat intact. A glossary full of unmarked machine text is worse than an
empty one, because it looks reviewed when it is not.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

import config

# Rebound in main() from the naming flags. Every {KB} below is an f-string
# evaluated at call time, so it picks up the rebind -- but a module-level
# f-string constant would not, which is why the DDL blocks are functions.
KB = config.DEFAULT.kb


def run_sql(sql: str, connection: str, label: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8") as fh:
        fh.write(sql)
        path = fh.name
    try:
        proc = subprocess.run(
            ["snow", "sql", "-f", path, "-c", connection], capture_output=True, text=True
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"{label} failed:\n{(proc.stdout or '')[-4000:]}{(proc.stderr or '')[-4000:]}"
            )
        print(f"  {label}: ok")
        return proc.stdout
    finally:
        os.unlink(path)


def query_json(sql: str, connection: str) -> list[dict]:
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8") as fh:
        fh.write(sql)
        path = fh.name
    try:
        proc = subprocess.run(
            ["snow", "sql", "-f", path, "-c", connection, "--format", "json"],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"query failed:\n{proc.stderr[-3000:]}")
        text = proc.stdout.strip()
        start = text.find("[")
        return json.loads(text[start:]) if start >= 0 else []
    finally:
        os.unlink(path)


def _lit(s: str | None) -> str:
    if s is None:
        return "''"
    return "'" + str(s).replace("\\", "\\\\").replace("'", "''") + "'"


def existing_tables(connection: str, database: str) -> set[tuple[str, str]]:
    """Physical tables that actually exist in the target database.

    The KB describes 195 entities from the source model, but only the subset that
    has been materialised can carry a comment or a tag. Attempting the rest fails
    the batch on the first missing object, so the catalog operations are
    intersected with reality rather than assuming the KB and the warehouse are in
    step. The difference between the two counts is itself useful: it is the
    backlog of entities the model describes but the warehouse does not yet expose.
    """
    rows = query_json(
        f"""
        SELECT TABLE_SCHEMA, TABLE_NAME
        FROM {database}.INFORMATION_SCHEMA.TABLES
        WHERE TABLE_TYPE = 'BASE TABLE'
        """,
        connection,
    )
    return {(r["TABLE_SCHEMA"], r["TABLE_NAME"]) for r in rows}


def existing_columns(connection: str, database: str) -> set[tuple[str, str, str]]:
    """Columns that actually exist, for the same reason."""
    rows = query_json(
        f"""
        SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME
        FROM {database}.INFORMATION_SCHEMA.COLUMNS
        """,
        connection,
    )
    return {(r["TABLE_SCHEMA"], r["TABLE_NAME"], r["COLUMN_NAME"]) for r in rows}


# ---------------------------------------------------------------------------
# 1 + 2. Comments and tags
# ---------------------------------------------------------------------------

def tag_ddl() -> str:
    return f"""
USE SCHEMA {KB};

-- Tags live in the KB schema, not on the data, so the taxonomy is versioned
-- alongside the metadata that populates it.
CREATE TAG IF NOT EXISTS SUBJECT_AREA
    COMMENT = 'Business subject area this object belongs to, from the source BI model';
CREATE TAG IF NOT EXISTS SOURCE_BI_MODEL
    COMMENT = 'BI model the definition was extracted from';
CREATE TAG IF NOT EXISTS CONVERSION_TIER
    ALLOWED_VALUES 'simple', 'needs_translation', 'manual_required'
    COMMENT = 'How cleanly this object converts from its BI source';
CREATE TAG IF NOT EXISTS DATA_STEWARD
    COMMENT = 'Accountable steward. Unset until assigned by governance';
CREATE TAG IF NOT EXISTS TERM_ROLE
    ALLOWED_VALUES 'measure', 'identifier', 'attribute'
    COMMENT = 'Role the source BI model declares for this column';
CREATE TAG IF NOT EXISTS SEMANTIC_RISK
    ALLOWED_VALUES 'none', 'misdeclared_measure', 'semi_additive', 'grain_sensitive', 'filter_dependent'
    COMMENT = 'Known semantic hazard that makes naive aggregation wrong';
"""


def generate_comments(connection: str, database: str) -> str:
    """Emit COMMENT ON for every materialised table and column the KB describes."""
    have_tables = existing_tables(connection, database)
    have_cols = existing_columns(connection, database)
    rows = query_json(
        f"""
        SELECT b.PHYSICAL_SCHEMA, b.PHYSICAL_OBJECT, b.ENTITY_NAME,
               b.TERM_COUNT, b.MEASURE_COUNT, b.KEY_COUNT, b.SOURCE_CONNECTION,
               b.IS_PASSTHROUGH, b.CONVERSION_TIER
        FROM {KB}.KB_V_PHYSICAL_BINDING b
        WHERE b.PHYSICAL_SCHEMA IS NOT NULL
        ORDER BY 1, 2
        """,
        connection,
    )
    col_rows = query_json(
        f"""
        SELECT b.PHYSICAL_SCHEMA, b.PHYSICAL_OBJECT, t.PHYSICAL_COLUMN,
               t.TERM_NAME, t.DEFINITION, t.TERM_ROLE
        FROM {KB}.KB_TERM t
        JOIN {KB}.KB_V_PHYSICAL_BINDING b
          ON b.SOURCE_SYSTEM = t.SOURCE_SYSTEM
         AND b.SOURCE_MODEL  = t.SOURCE_MODEL
         AND b.ENTITY_NAME   = t.ENTITY_NAME
        WHERE t.PHYSICAL_COLUMN IS NOT NULL
          AND b.PHYSICAL_SCHEMA IS NOT NULL
        ORDER BY 1, 2, 3
        """,
        connection,
    )

    described = {(r["PHYSICAL_SCHEMA"], r["PHYSICAL_OBJECT"]) for r in rows}
    existing = described & have_tables
    skipped = len(described - have_tables)
    out = [
        f"USE DATABASE {database};",
        f"-- {len(existing)} of {len(described)} KB-described entities are materialised here.",
        f"-- {skipped} are described by the source model but not yet built in the warehouse.",
        "",
    ]

    for r in rows:
        if (r["PHYSICAL_SCHEMA"], r["PHYSICAL_OBJECT"]) not in existing:
            continue
        fq = f'{database}.{r["PHYSICAL_SCHEMA"]}.{r["PHYSICAL_OBJECT"]}'
        desc = (
            f'{r["ENTITY_NAME"]} -- {r["TERM_COUNT"]} business terms '
            f'({r["MEASURE_COUNT"]} measures, {r["KEY_COUNT"]} keys). '
            f'Defined in the source Cognos DMR model; extracted to {KB}.'
        )
        out.append(f"COMMENT ON TABLE {fq} IS {_lit(desc)};")

    out.append("")
    # Column comments are the bulk of the value: this is what makes a warehouse
    # searchable by business vocabulary rather than by column name.
    seen: set[tuple] = set()
    for c in col_rows:
        key = (c["PHYSICAL_SCHEMA"], c["PHYSICAL_OBJECT"], c["PHYSICAL_COLUMN"])
        if key in seen or key not in have_cols:
            continue
        seen.add(key)
        fq = f'{database}.{c["PHYSICAL_SCHEMA"]}.{c["PHYSICAL_OBJECT"]}'
        defn = c.get("DEFINITION") or c["TERM_NAME"]
        # Carry the Cognos-facing name: report authors know FINC_YR_ID, the
        # warehouse stores FISCAL_YEAR_ID, and the mapping is the useful part.
        note = f'[{c["TERM_ROLE"]}] {defn}'
        if c["TERM_NAME"] != c["PHYSICAL_COLUMN"]:
            note += f' (BI name: {c["TERM_NAME"]})'
        out.append(
            f'COMMENT ON COLUMN {fq}."{c["PHYSICAL_COLUMN"]}" IS {_lit(note)};'
        )
    return "\n".join(out)


def generate_tags(connection: str, database: str) -> str:
    """Emit tag assignments for materialised objects, including semantic risk."""
    have_tables = existing_tables(connection, database)
    have_cols = existing_columns(connection, database)
    rows = query_json(
        f"""
        WITH risk AS (
            SELECT SOURCE_SYSTEM, SOURCE_MODEL, ENTITY_NAME, TERM_NAME,
                   'misdeclared_measure' AS RISK
            FROM {KB}.KB_V_MEASURE_QUALITY
            WHERE NOT SAFE_TO_EXPOSE_AS_METRIC
            UNION ALL
            SELECT SOURCE_SYSTEM, SOURCE_MODEL, ENTITY_NAME, TERM_NAME,
                   'semi_additive'
            FROM {KB}.KB_AGGREGATION_RULE
            WHERE IS_SEMI_ADDITIVE
        )
        SELECT b.PHYSICAL_SCHEMA, b.PHYSICAL_OBJECT, t.PHYSICAL_COLUMN,
               t.TERM_ROLE, r.RISK
        FROM {KB}.KB_TERM t
        JOIN {KB}.KB_V_PHYSICAL_BINDING b
          ON b.SOURCE_SYSTEM = t.SOURCE_SYSTEM
         AND b.SOURCE_MODEL  = t.SOURCE_MODEL
         AND b.ENTITY_NAME   = t.ENTITY_NAME
        LEFT JOIN risk r
          ON r.SOURCE_SYSTEM = t.SOURCE_SYSTEM
         AND r.SOURCE_MODEL  = t.SOURCE_MODEL
         AND r.ENTITY_NAME   = t.ENTITY_NAME
         AND r.TERM_NAME     = t.TERM_NAME
        WHERE t.PHYSICAL_COLUMN IS NOT NULL AND b.PHYSICAL_SCHEMA IS NOT NULL
        ORDER BY 1,2,3
        """,
        connection,
    )
    tables = query_json(
        f"""
        SELECT DISTINCT b.PHYSICAL_SCHEMA, b.PHYSICAL_OBJECT, b.ENTITY_NAME,
               b.CONVERSION_TIER, b.SOURCE_MODEL,
               COALESCE(h.DIMENSION_NAME, 'Sales & Bookings') AS SUBJECT_AREA
        FROM {KB}.KB_V_PHYSICAL_BINDING b
        LEFT JOIN {KB}.KB_HIERARCHY_LEVEL h
               ON h.SOURCE_ENTITY = b.ENTITY_NAME
        WHERE b.PHYSICAL_SCHEMA IS NOT NULL
        ORDER BY 1,2
        """,
        connection,
    )

    out = [f"USE DATABASE {database};", ""]
    seen_t: set[tuple] = set()
    for t in tables:
        key = (t["PHYSICAL_SCHEMA"], t["PHYSICAL_OBJECT"])
        if key in seen_t or key not in have_tables:
            continue
        seen_t.add(key)
        fq = f'{database}.{t["PHYSICAL_SCHEMA"]}.{t["PHYSICAL_OBJECT"]}'
        out.append(
            f'ALTER TABLE {fq} SET TAG {KB}.SUBJECT_AREA = {_lit(t["SUBJECT_AREA"])}, '
            f'{KB}.SOURCE_BI_MODEL = {_lit(t["SOURCE_MODEL"])}, '
            f'{KB}.CONVERSION_TIER = {_lit(t.get("CONVERSION_TIER") or "simple")};'
        )

    out.append("")
    seen_c: set[tuple] = set()
    for c in rows:
        key = (c["PHYSICAL_SCHEMA"], c["PHYSICAL_OBJECT"], c["PHYSICAL_COLUMN"])
        if key in seen_c or key not in have_cols:
            continue
        seen_c.add(key)
        fq = f'{database}.{c["PHYSICAL_SCHEMA"]}.{c["PHYSICAL_OBJECT"]}'
        parts = [f'{KB}.TERM_ROLE = {_lit(c["TERM_ROLE"])}']
        if c.get("RISK"):
            parts.append(f'{KB}.SEMANTIC_RISK = {_lit(c["RISK"])}')
        out.append(
            f'ALTER TABLE {fq} MODIFY COLUMN "{c["PHYSICAL_COLUMN"]}" '
            f'SET TAG {", ".join(parts)};'
        )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 3. Business glossary
# ---------------------------------------------------------------------------

def glossary_ddl() -> str:
    return f"""
USE SCHEMA {KB};

-- Shape follows the existing BUSINESS_GLOSSARY already in this account
-- (LAB_EFFICIENCY_DATA_PRODUCT.METADATA_REGISTRY) so a steward sees one
-- consistent structure rather than a second competing glossary format.
CREATE OR REPLACE TABLE BUSINESS_GLOSSARY (
    TERM_ID              VARCHAR       COMMENT 'Stable id: source system + model + term',
    BUSINESS_TERM        VARCHAR       NOT NULL,
    TERM_KIND            VARCHAR       COMMENT 'TERM | METRIC',
    SUBJECT_AREA         VARCHAR,
    CLASSIFICATION       VARCHAR       COMMENT 'measure | identifier | attribute, or metric kind',
    DEFINITION           VARCHAR,
    -- Explicit rather than inferred. A steward must be able to see at a glance
    -- which definitions are machine-generated placeholders.
    DEFINITION_SOURCE    VARCHAR       COMMENT 'source_model | auto_generated | steward_authored',
    CALCULATION_LOGIC    VARCHAR       COMMENT 'Snowflake expression, for metrics',
    SYNONYMS             ARRAY,
    PHYSICAL_BINDING     VARCHAR       COMMENT 'Fully qualified column, where one exists',
    SOURCE_BI_MODEL      VARCHAR,
    SOURCE_BI_SYSTEM     VARCHAR,
    SEMANTIC_RISK        VARCHAR       COMMENT 'Known hazard that makes naive use wrong',
    CONVERSION_TIER      VARCHAR,
    STEWARD              VARCHAR,
    CERTIFICATION_STATUS VARCHAR       DEFAULT 'DRAFT'
                                       COMMENT 'DRAFT until a steward reviews. Never set by automation',
    LAST_UPDATED         TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'Governed business glossary, generated from the semantic knowledge base.';
"""

def glossary_load() -> str:
    return f"""
USE SCHEMA {KB};

INSERT INTO BUSINESS_GLOSSARY
    (TERM_ID, BUSINESS_TERM, TERM_KIND, SUBJECT_AREA, CLASSIFICATION, DEFINITION,
     DEFINITION_SOURCE, CALCULATION_LOGIC, SYNONYMS, PHYSICAL_BINDING,
     SOURCE_BI_MODEL, SOURCE_BI_SYSTEM, SEMANTIC_RISK, CONVERSION_TIER, STEWARD)
WITH risk AS (
    SELECT SOURCE_SYSTEM, SOURCE_MODEL, ENTITY_NAME, TERM_NAME,
           'misdeclared_measure' AS RISK
    FROM KB_V_MEASURE_QUALITY WHERE NOT SAFE_TO_EXPOSE_AS_METRIC
    UNION ALL
    SELECT SOURCE_SYSTEM, SOURCE_MODEL, ENTITY_NAME, TERM_NAME, 'semi_additive'
    FROM KB_AGGREGATION_RULE WHERE IS_SEMI_ADDITIVE
)
SELECT
    g.SOURCE_SYSTEM || ':' || g.SOURCE_MODEL || ':' || g.SUBJECT_AREA || ':' || g.BUSINESS_TERM,
    g.BUSINESS_TERM,
    g.TERM_KIND,
    g.SUBJECT_AREA,
    g.CLASSIFICATION,
    g.DEFINITION,
    -- The auto-generated marker is written into the definition text by the
    -- loader, so detecting it here keeps one source of truth for that decision.
    IFF(g.DEFINITION ILIKE '%auto-generated from the source name%',
        'auto_generated', 'source_model')                       AS DEFINITION_SOURCE,
    -- Joined rather than a correlated scalar subquery: Snowflake cannot evaluate
    -- a subquery correlated on several outer columns in a projection.
    m.TRANSLATED_EXPR                                            AS CALCULATION_LOGIC,
    g.SYNONYMS,
    CASE WHEN g.PHYSICAL_OBJECT IS NOT NULL
         THEN g.PHYSICAL_DATABASE || '.' || g.PHYSICAL_SCHEMA || '.'
              || g.PHYSICAL_OBJECT || '.' || g.PHYSICAL_COLUMN
    END                                                          AS PHYSICAL_BINDING,
    g.SOURCE_MODEL,
    g.SOURCE_SYSTEM,
    COALESCE(r.RISK, 'none')                                     AS SEMANTIC_RISK,
    g.CONVERSION_TIER,
    g.STEWARD
FROM KB_V_GLOSSARY g
LEFT JOIN KB_METRIC m
       ON g.TERM_KIND       = 'METRIC'
      AND m.SOURCE_SYSTEM    = g.SOURCE_SYSTEM
      AND m.SOURCE_MODEL     = g.SOURCE_MODEL
      AND m.METRIC_CANONICAL = g.BUSINESS_TERM
LEFT JOIN risk r
       ON r.SOURCE_SYSTEM = g.SOURCE_SYSTEM
      AND r.SOURCE_MODEL  = g.SOURCE_MODEL
      AND r.ENTITY_NAME   = g.SUBJECT_AREA
      AND r.TERM_NAME     = g.BUSINESS_TERM;
"""


# ---------------------------------------------------------------------------
# 4. Ontology handoff
# ---------------------------------------------------------------------------


def generate_ontology_handoff(connection: str) -> dict:
    """Build an entity/relationship proposal for the ontology-stack-builder skill.

    That skill normally has to *infer* entities and relationships by introspecting
    a schema. Here they are declared: the BI model already states which objects
    are entities, which columns join them, and what the drill hierarchies are. So
    the handoff is a pre-built proposal rather than a discovery task, which is the
    difference between guessing a data model and reading one.
    """
    entities = query_json(
        f"""
        SELECT ENTITY_NAME, PHYSICAL_DATABASE, PHYSICAL_SCHEMA, PHYSICAL_OBJECT,
               TERM_COUNT, MEASURE_COUNT, KEY_COUNT,
               IFF(MEASURE_COUNT > 0 AND KEY_COUNT > 0, 'fact', 'dimension') AS CLASS_KIND
        FROM {KB}.KB_V_PHYSICAL_BINDING
        WHERE PHYSICAL_SCHEMA IS NOT NULL
        ORDER BY ENTITY_NAME
        """,
        connection,
    )
    rels = query_json(
        f"""
        SELECT RELATIONSHIP_NAME, LEFT_ENTITY, RIGHT_ENTITY, LEFT_COLUMN,
               RIGHT_COLUMN, CARDINALITY, IS_SIMPLE_EQUALITY, SOURCE_STATUS
        FROM {KB}.KB_RELATIONSHIP
        WHERE NOT IS_CLONE_RELATED AND IS_SIMPLE_EQUALITY
        ORDER BY LEFT_ENTITY, RIGHT_ENTITY
        """,
        connection,
    )
    hier = query_json(
        f"""
        SELECT DIMENSION_NAME, HIERARCHY_NAME, LEVEL_ORDINAL, LEVEL_NAME,
               CAPTION_TERM, SOURCE_ENTITY
        FROM {KB}.KB_V_HIERARCHY_TREE
        WHERE NOT IS_ALL_LEVEL AND LEVEL_COUNT >= 2
        ORDER BY DIMENSION_NAME, HIERARCHY_NAME, LEVEL_ORDINAL
        """,
        connection,
    )
    return {
        "source": KB,
        "note": (
            "Entities, relationships and hierarchies are declared by the source BI "
            "model, not inferred from schema introspection. Relationships are "
            "filtered to non-clone, single-column equality joins; SOURCE_STATUS "
            "records what the source tool asserts about validity, which is stale "
            "on the base model in this case and should not be used to exclude a join."
        ),
        "classes": entities,
        "relationships": rels,
        "hierarchies": hier,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Path 1: Horizon catalog, glossary, ontology")
    ap.add_argument("--connection", default="my-demo-account")
    config.add_arguments(ap)
    # Generated DDL goes to out/ (git-ignored), never to sql/ (tracked). These
    # files are an audit artefact -- the SQL that actually runs is passed to
    # run_sql() from memory below, so nothing reads them back. Writing them over
    # the tracked templates left the working tree dirty after every build, with
    # the customer's database, schema and table names in 552 added lines, one
    # `git add -A` away from being published.
    ap.add_argument("--out-dir", default="out")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args(argv)

    # Bind the knowledge-base location from the naming flags before any DDL
    # is built or any query runs. Without this the whole script silently
    # targeted the default KB database and ignored --kb-database.
    global KB
    KB = config.from_args(args).kb

    os.makedirs(args.out_dir, exist_ok=True)
    os.makedirs("out", exist_ok=True)

    print("building Path 1 artifacts from the knowledge base")
    comments = generate_comments(args.connection, args.database)
    tags = generate_tags(args.connection, args.database)
    ontology = generate_ontology_handoff(args.connection)

    files = {
        "20_tag_taxonomy.sql": tag_ddl(),
        "21_object_comments.sql": comments,
        "22_tag_assignments.sql": tags,
        "23_business_glossary.sql": glossary_ddl() + glossary_load(),
    }
    for name, sql in files.items():
        p = os.path.join(args.out_dir, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(sql)
        print(f"  wrote {p} ({sql.count(';')} statements)")

    with open("out/ontology_handoff.json", "w", encoding="utf-8") as f:
        json.dump(ontology, f, indent=2, default=str)
    print(
        f"  wrote out/ontology_handoff.json "
        f"({len(ontology['classes'])} classes, {len(ontology['relationships'])} relationships, "
        f"{len(ontology['hierarchies'])} hierarchy levels)"
    )

    if args.execute:
        print("executing:")
        run_sql(tag_ddl(), args.connection, "tag taxonomy")
        run_sql(comments, args.connection, "object + column comments")
        run_sql(tags, args.connection, "tag assignments")
        run_sql(glossary_ddl() + glossary_load(), args.connection, "business glossary")

    return 0


if __name__ == "__main__":
    sys.exit(main())
