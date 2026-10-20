"""Inspect declared Cognos security rules without inferring executable grants.

Column patterns and dotted values are descriptive hints only. They cannot prove
principal inheritance, predicate grouping, or which Snowflake policies are needed.
Retain original expressions for a separately reviewed security translation.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from ..common.logger import get_logger

log = get_logger(__name__)

# Dotted values are descriptive only; they do not imply inherited entitlement.
_DOTTED_RE = re.compile(r"^[A-Z0-9_]+(?:\.[A-Z0-9_]+)+$", re.IGNORECASE)

_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]+")
# Trailing level number, with or without a separator: LEVEL_4 / LEVEL4.
_LEVEL_SEP_RE = re.compile(r"_(\d+)$")
# Descriptive numbered columns, independent of business domain.
_TERRITORY_LEVEL_RE = re.compile(r"^(.+_LEVEL)(\d+)$")


def normalize_column(name: str) -> str:
    """Canonical display name for descriptive grouping, never authorization."""
    canonical = _NON_ALNUM_RE.sub("_", (name or "").upper()).strip("_")
    return _LEVEL_SEP_RE.sub(r"\1", canonical)


def generalize_column(name: str) -> str:
    """Group numbered level labels for display, not policy equivalence."""
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
                # Preserve source group paths verbatim; do not infer inheritance.
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
    """Summarize column shapes without inferring security equivalence."""
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

    # Retain this inspection hint for compatibility; never use it for prefix access.
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
        # Descriptive label counts are not policy counts.
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
        # Policy count cannot be established from column shapes.
        "policies_required": None,
        "collapses_to_single_policy": None,
        "hierarchy_warning": (
            "Column/value summaries do not preserve complete Boolean semantics or "
            "principal inheritance. Review the full source expressions and explicit "
            "principal mappings before generating security controls."
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
    security_role: str | None = None,
) -> str:
    """Retired unsafe generator; descriptive summaries cannot authorize access."""
    raise ValueError(
        'Automatic policy generation from column-shape summaries is unsafe. '
        'Preserve complete predicates and explicitly map principals before policy design. '
        'Dotted values do not establish hierarchical entitlement; no bypass role is inferred.'
    )
