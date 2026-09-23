"""Cognos security filters to Snowflake row access policy design.

The reference model carries 19,921 ``securityFilterDefinition`` entries, which
collapse to 19,051 once duplicate principal/filter pairs are removed. Each
binds one Cognos account to a row filter, almost always of the form::

    AREA.ROLE = 'AREA-ROLE' AND AREA.TERRITORY_LEVEL4 = 'EAST.GREATLAKES.AREA.CLEVELAND'

That is not 19,921 distinct security rules. It is one rule -- *a principal sees
rows matching their role and their territory* -- instantiated per principal. The
useful output is therefore a **mapping table plus a single policy**, not a
generated policy per filter.

This module derives that mapping. It deliberately does not claim to reproduce
the reference model's security model: hierarchical inheritance (an L2 manager seeing their L1
reports' rows) is not expressible in these flat per-account filters, so if it
exists in Cognos it lives somewhere else and has to be designed separately. The
``hierarchy_warning`` field says so explicitly rather than leaving the reader to
assume the mapping is complete.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from ..common.logger import get_logger

log = get_logger(__name__)

# Territory codes are dotted paths: EAST.GREATLAKES.KAM.CLEVELAND
_DOTTED_RE = re.compile(r"^[A-Z0-9_]+(?:\.[A-Z0-9_]+)+$", re.IGNORECASE)

_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]+")
# Trailing level number, with or without a separator: LEVEL_4 / LEVEL4.
_LEVEL_SEP_RE = re.compile(r"_(\d+)$")
# Any territory level column, whatever its level number.
_TERRITORY_LEVEL_RE = re.compile(r"^(TERRITORY_LEVEL)(\d+)$")


def normalize_column(name: str) -> str:
    """Canonical form of a column name for grouping.

    The same logical column appears under several spellings across the Cognos
    namespaces -- ``TERRITORY_LEVEL4``, ``Territory Level 4``, ``Territory
    Level4`` -- because the Import View uses physical names while the Model and
    DMR views use presentation names. Grouping on the raw string reports ten
    distinct security shapes where there are two, which would wrongly suggest the
    population cannot collapse to a single policy.

    The trailing separator before a level number is also dropped, so
    ``TERRITORY_LEVEL_4`` and ``TERRITORY_LEVEL4`` are one column.
    """
    canonical = _NON_ALNUM_RE.sub("_", (name or "").upper()).strip("_")
    return _LEVEL_SEP_RE.sub(r"\1", canonical)


def generalize_column(name: str) -> str:
    """Abstract a territory level number away: TERRITORY_LEVEL4 -> TERRITORY_LEVEL#.

    Used to answer the question that actually matters for policy design: the
    filters constrain *a* territory level, and which level differs per role. That
    is one policy taking the level as a parameter, not four policies.
    """
    return _TERRITORY_LEVEL_RE.sub(r"\1#", normalize_column(name))


def derive_security_rules(parsed: dict) -> list[dict[str, Any]]:
    """Normalise security filters into source-agnostic rule records."""
    out: list[dict[str, Any]] = []
    for sf in parsed.get("security_filters", []):
        preds = sf.get("predicates", [])
        entities = sorted({p["entity"] for p in preds if p.get("entity")})
        out.append(
            {
                "principal": sf.get("principal", ""),
                "principal_type": sf.get("principal_type", ""),
                # The Cognos group path is the role hierarchy, e.g. SALES:REGION:AREA-ROLE
                "principal_groups": sf.get("principal_groups", []),
                "role_group": (sf.get("principal_groups") or [""])[-1],
                "entities": entities,
                "columns": sf.get("columns", []),
                "predicates": preds,
                "expression": sf.get("expression", ""),
                "display_name": sf.get("display_name", ""),
            }
        )
    log.info("Derived %d security rules.", len(out))
    return out


def summarize_security(parsed: dict) -> dict[str, Any]:
    """Collapse the filter population into its distinct shapes.

    A "shape" is the set of columns a filter constrains, ignoring the literal
    values. If the whole population reduces to one or two shapes, a single row
    access policy covers it.
    """
    rules = derive_security_rules(parsed)

    shapes: dict[tuple[str, ...], int] = defaultdict(int)
    patterns: dict[tuple[str, ...], int] = defaultdict(int)
    by_role: dict[str, int] = defaultdict(int)
    entities: dict[str, int] = defaultdict(int)
    values_by_column: dict[str, set[str]] = defaultdict(set)
    levels_by_role: dict[str, set[str]] = defaultdict(set)

    for r in rules:
        cols = {normalize_column(c) for c in r["columns"]}
        shapes[tuple(sorted(cols))] += 1
        patterns[tuple(sorted({generalize_column(c) for c in cols}))] += 1
        by_role[r["role_group"] or "(none)"] += 1
        for e in r["entities"]:
            entities[e] += 1
        for p in r["predicates"]:
            if p.get("column") and p.get("value"):
                col = normalize_column(p["column"])
                values_by_column[col].add(p["value"])
                if _TERRITORY_LEVEL_RE.match(col):
                    levels_by_role[r["role_group"] or "(none)"].add(col)

    shape_list = sorted(
        ({"columns": list(k), "filter_count": v} for k, v in shapes.items()),
        key=lambda s: -s["filter_count"],
    )
    pattern_list = sorted(
        ({"columns": list(k), "filter_count": v} for k, v in patterns.items()),
        key=lambda s: -s["filter_count"],
    )

    # A column whose values are dotted paths is a hierarchy encoded as a string,
    # which matters: prefix matching on it gives hierarchical access without a
    # recursive mapping table.
    hierarchical_columns = [
        col
        for col, vals in values_by_column.items()
        if vals and sum(1 for v in vals if _DOTTED_RE.match(v)) / len(vals) > 0.8
    ]

    return {
        "filter_total": len(rules),
        "distinct_principals": len({r["principal"] for r in rules}),
        "distinct_shapes": len(shapes),
        "shapes": shape_list,
        # Shapes with the territory level number abstracted away. This is the
        # number that decides how many policies are actually needed.
        "distinct_patterns": len(patterns),
        "patterns": pattern_list,
        "territory_levels_used": sorted(
            {lv for lvs in levels_by_role.values() for lv in lvs}
        ),
        "levels_by_role_group": {k: sorted(v) for k, v in sorted(levels_by_role.items())},
        "by_role_group": dict(sorted(by_role.items(), key=lambda x: -x[1])),
        "protected_entities": dict(sorted(entities.items(), key=lambda x: -x[1])),
        "distinct_values_per_column": {
            c: len(v) for c, v in sorted(values_by_column.items())
        },
        "hierarchical_columns": hierarchical_columns,
        # One policy per generalized pattern, not per raw shape. The level column
        # becomes a parameter of the mapping table rather than a separate policy.
        "policies_required": len(patterns),
        "collapses_to_single_policy": len(patterns) <= 2,
        "hierarchy_warning": (
            "These filters are flat per-principal equality predicates. If the reference model "
            "requires hierarchical inheritance (an L2 manager seeing their L1 "
            "reports' rows), it is not expressed in this model and must be "
            "designed separately -- the mapping below reproduces direct "
            "assignment only."
        ),
    }


def build_mapping_rows(parsed: dict) -> list[dict[str, str]]:
    """Flatten filters into mapping-table rows.

    One row per (principal, column, value). Deduplicated, because the same
    principal appears in many packages with an identical filter.
    """
    seen: set[tuple[str, str, str, str]] = set()
    rows: list[dict[str, str]] = []
    for r in derive_security_rules(parsed):
        for p in r["predicates"]:
            if not p.get("column") or not p.get("value"):
                continue
            key = (r["principal"], r["role_group"], normalize_column(p["column"]), p["value"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "principal": r["principal"],
                    "role_group": r["role_group"],
                    "column_name": normalize_column(p["column"]),
                    "column_value": p["value"],
                    "entity": p.get("entity", ""),
                }
            )
    log.info("Built %d distinct security mapping rows.", len(rows))
    return rows


def generate_row_access_policy(
    summary: dict,
    policy_name: str,
    mapping_table: str,
    territory_column: str,
    role_column: str = "ROLE",
    security_role: str = "BI_SECURITY_ADMIN",
) -> str:
    """Emit a Snowflake row access policy replacing the whole filter population.

    Uses prefix matching (``LIKE value || '%'``) on the dotted territory path
    rather than strict equality, because the dotted codes are a hierarchy encoded
    as a string: a principal mapped to ``EAST.GREATLAKES`` should see
    ``EAST.GREATLAKES.KAM.CLEVELAND``. Strict equality would give a manager
    access to nothing but their own exact node.

    Note the deliberate absence of a fail-open branch. A principal with no
    mapping row sees no rows. Adding ``OR NOT EXISTS (...)`` to let unmapped
    users through is how row access policies silently stop protecting anything.
    """
    hierarchical = territory_column in (summary.get("hierarchical_columns") or [])
    match = (
        f"{territory_column} LIKE m.column_value || '%'"
        if hierarchical
        else f"{territory_column} = m.column_value"
    )
    note = (
        "-- Prefix match: territory codes are dotted hierarchy paths, so a\n"
        "-- principal mapped to a parent node sees all descendants.\n"
        if hierarchical
        else "-- Exact match: territory values are not hierarchical paths.\n"
    )

    return f"""CREATE OR REPLACE ROW ACCESS POLICY {policy_name}
  AS ({territory_column} VARCHAR) RETURNS BOOLEAN ->
{note}  EXISTS (
    SELECT 1
    FROM {mapping_table} m
    WHERE m.principal = CURRENT_USER()
      AND m.column_name = '{territory_column}'
      AND {match}
  )
  -- No fail-open branch: a principal with no mapping row sees no rows.
  OR IS_ROLE_IN_SESSION('{security_role}');
"""
