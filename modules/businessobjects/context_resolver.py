"""Analyze BO contexts (join graph disambiguation) to recommend Semantic View splits.

BO contexts define subsets of the join graph that avoid loops for specific
query paths. Each context makes certain joins active and others inactive.
Multiple contexts typically mean the universe spans multiple subject areas
that should become separate Snowflake Semantic Views.

All functions:
- Log entry/exit with parameters
- Handle missing/malformed context data gracefully
- Never abort on a single bad context record
"""

from __future__ import annotations

import re
from typing import Any

from ..common.errors import ParseError, fail_step
from ..common.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# extract_context_inventory
# ---------------------------------------------------------------------------

def extract_context_inventory(bo_json: dict) -> list[dict]:
    """Build a context-to-joins map from a BO universe JSON.

    Resolves which physical tables participate in each context by inspecting
    the join expressions for table references.

    Args:
        bo_json: Full universe dict as returned by :func:`load_bo_json`.

    Returns:
        List of context dicts:

        .. code-block:: python

            [{
                "context_name": str,
                "active_joins": [...],    # join expression strings
                "active_tables": [str],   # table names inferred from joins
                "join_count": int,
            }]

        If no contexts are defined, returns a single synthetic entry
        named ``"__default__"`` covering all joins.
    """
    log.info("extract_context_inventory: starting")

    data_foundation = bo_json.get("dataFoundation") or {}
    all_joins = _collect_all_joins(data_foundation)
    raw_contexts = (
        bo_json.get("contexts")
        or data_foundation.get("contexts")
        or []
    )

    if not raw_contexts:
        log.info(
            "extract_context_inventory: no contexts defined — using single default context"
        )
        return [_build_context_entry("__default__", all_joins)]

    inventory: list[dict] = []
    for raw in raw_contexts:
        try:
            entry = _parse_context_entry(raw, all_joins)
            inventory.append(entry)
        except Exception as exc:
            log.warning(
                "extract_context_inventory: skipping malformed context: %s", exc
            )

    log.info(
        "extract_context_inventory: done — %d contexts, %d total joins",
        len(inventory),
        len(all_joins),
    )
    return inventory


def _collect_all_joins(data_foundation: dict) -> list[dict]:
    """Collect all join definitions from data foundation."""
    joins = data_foundation.get("joins") or data_foundation.get("links") or []
    result: list[dict] = []
    for j in joins:
        result.append({
            "id": j.get("id") or j.get("name") or "",
            "expression": j.get("expression") or j.get("sql") or "",
            "table1": j.get("table1") or j.get("leftTable") or "",
            "table2": j.get("table2") or j.get("rightTable") or "",
        })
    return result


def _parse_context_entry(raw: dict, all_joins: list[dict]) -> dict:
    """Parse a single context record."""
    name = raw.get("name") or raw.get("contextName") or "unnamed"

    # IDs of joins included in this context
    included_ids = set(
        raw.get("includedJoins")
        or raw.get("joins")
        or []
    )
    excluded_ids = set(raw.get("excludedJoins") or [])

    # Resolve active joins
    if included_ids:
        # Explicit inclusion list
        active_joins = [
            j for j in all_joins
            if j["id"] in included_ids
        ]
    elif excluded_ids:
        # All joins minus explicitly excluded
        active_joins = [
            j for j in all_joins
            if j["id"] not in excluded_ids
        ]
    else:
        # No filtering specified — context references all joins
        active_joins = list(all_joins)

    return _build_context_entry(name, active_joins, raw.get("description", ""))


def _build_context_entry(
    name: str,
    active_joins: list[dict],
    description: str = "",
) -> dict:
    """Build a normalized context inventory entry."""
    active_tables: list[str] = []
    seen_tables: set[str] = set()
    for j in active_joins:
        for tbl in (j.get("table1", ""), j.get("table2", "")):
            tbl = tbl.strip()
            if tbl and tbl not in seen_tables:
                active_tables.append(tbl)
                seen_tables.add(tbl)

    return {
        "context_name": name,
        "description": description,
        "active_joins": [
            j.get("expression") or f"{j.get('table1','')}.* = {j.get('table2','')}.*"
            for j in active_joins
        ],
        "active_tables": active_tables,
        "join_count": len(active_joins),
    }


# ---------------------------------------------------------------------------
# recommend_semantic_view_split
# ---------------------------------------------------------------------------

def recommend_semantic_view_split(
    context_inventory: list[dict],
) -> list[dict]:
    """Recommend how to split BO contexts into Snowflake Semantic Views.

    Strategy:
    - **Single/no context**: One semantic view.
    - **Multiple contexts with disjoint tables**: One semantic view per context.
    - **Multiple contexts with shared tables**: One SV per context, shared
      dimensions tagged for reuse.
    - **Heavily overlapping contexts** (>80% table overlap): Flag for manual
      review — may indicate the contexts are not true subject-area splits.

    Args:
        context_inventory: Output of :func:`extract_context_inventory`.

    Returns:
        List of recommendation dicts:

        .. code-block:: python

            [{
                "context_name": str,
                "recommended_sv_name": str,
                "strategy": str,          # 'one_to_one' | 'shared_dims' | 'review'
                "shared_tables": [str],   # tables shared with other contexts
                "unique_tables": [str],   # tables unique to this context
                "notes": str,
            }]
    """
    log.info(
        "recommend_semantic_view_split: analyzing %d contexts",
        len(context_inventory),
    )

    if not context_inventory:
        log.warning("recommend_semantic_view_split: empty inventory")
        return []

    # Single default context
    if len(context_inventory) == 1:
        ctx = context_inventory[0]
        sv_name = _normalize_sv_name(ctx["context_name"])
        log.info("recommend_semantic_view_split: single context → 1 semantic view")
        return [{
            "context_name": ctx["context_name"],
            "recommended_sv_name": sv_name,
            "strategy": "one_to_one",
            "shared_tables": [],
            "unique_tables": ctx["active_tables"],
            "notes": (
                "Single context — map directly to one Snowflake Semantic View."
                if ctx["context_name"] != "__default__"
                else "No contexts defined — treat entire universe as one semantic view."
            ),
        }]

    # Build table → contexts map
    table_to_contexts: dict[str, list[str]] = {}
    for ctx in context_inventory:
        for tbl in ctx["active_tables"]:
            table_to_contexts.setdefault(tbl, []).append(ctx["context_name"])

    shared_globally = {
        tbl for tbl, ctxs in table_to_contexts.items()
        if len(ctxs) > 1
    }
    log.debug(
        "recommend_semantic_view_split: %d shared tables across contexts",
        len(shared_globally),
    )

    recommendations: list[dict] = []
    for ctx in context_inventory:
        ctx_tables = set(ctx["active_tables"])
        shared = sorted(ctx_tables & shared_globally)
        unique = sorted(ctx_tables - shared_globally)

        # Compute overlap ratio with other contexts
        if ctx_tables:
            overlap_ratio = len(shared) / len(ctx_tables)
        else:
            overlap_ratio = 0.0

        if overlap_ratio > 0.80:
            strategy = "review"
            notes = (
                f"Context '{ctx['context_name']}' shares {overlap_ratio:.0%} of its "
                "tables with other contexts. This may indicate overlapping subject areas "
                "rather than independent fact domains. Review before splitting."
            )
        elif shared:
            strategy = "shared_dims"
            notes = (
                f"Context '{ctx['context_name']}' has {len(shared)} shared dimension "
                f"table(s). Create one Semantic View for this context; shared tables "
                "become shared dimension joins or should be extracted to a separate "
                "conformed dimensions view."
            )
        else:
            strategy = "one_to_one"
            notes = (
                f"Context '{ctx['context_name']}' has fully disjoint tables. "
                "Map directly to one Snowflake Semantic View."
            )

        recommendations.append({
            "context_name": ctx["context_name"],
            "recommended_sv_name": _normalize_sv_name(ctx["context_name"]),
            "strategy": strategy,
            "shared_tables": shared,
            "unique_tables": unique,
            "notes": notes,
        })

    review_count = sum(1 for r in recommendations if r["strategy"] == "review")
    log.info(
        "recommend_semantic_view_split: done — %d views, %d need review",
        len(recommendations),
        review_count,
    )
    return recommendations


def _normalize_sv_name(context_name: str) -> str:
    """Convert a BO context name to a Snowflake-compatible identifier."""
    name = re.sub(r"[^a-zA-Z0-9_]", "_", context_name.strip())
    name = re.sub(r"_+", "_", name).strip("_")
    if name == "__default__" or not name:
        name = "UNIVERSE_SV"
    else:
        name = name.upper() + "_SV"
    return name


# ---------------------------------------------------------------------------
# print_context_summary
# ---------------------------------------------------------------------------

def print_context_summary(context_inventory: list[dict]) -> str:
    """Format context inventory as a human-readable decision table.

    Args:
        context_inventory: Output of :func:`extract_context_inventory`.

    Returns:
        Multi-line string suitable for printing or including in a report.
    """
    log.debug("print_context_summary: formatting %d contexts", len(context_inventory))

    if not context_inventory:
        return "No contexts found in universe."

    lines: list[str] = []
    lines.append("BO Context Inventory")
    lines.append("=" * 72)
    lines.append(
        f"{'Context Name':<30} {'Tables':>6} {'Joins':>6}  Tables"
    )
    lines.append("-" * 72)

    for ctx in context_inventory:
        name = ctx.get("context_name", "unnamed")
        tables = ctx.get("active_tables", [])
        join_count = ctx.get("join_count", 0)
        table_preview = ", ".join(tables[:5])
        if len(tables) > 5:
            table_preview += f" (+{len(tables) - 5} more)"
        lines.append(
            f"{name[:30]:<30} {len(tables):>6} {join_count:>6}  {table_preview}"
        )

    lines.append("-" * 72)
    lines.append(f"Total contexts: {len(context_inventory)}")

    result = "\n".join(lines)
    log.debug("print_context_summary: done")
    return result


# ---------------------------------------------------------------------------
# map_objects_to_contexts
# ---------------------------------------------------------------------------

def map_objects_to_contexts(
    objects: list[dict],
    context_inventory: list[dict],
) -> dict[str, Any]:
    """Determine which BO objects are valid in which contexts.

    Matching is done by checking whether the tables referenced in an object's
    SELECT expression appear in the context's active table list.

    Objects that reference only shared tables (or reference no specific tables)
    are marked as ``all_contexts`` — they should appear in every generated
    semantic view.

    Args:
        objects: List of normalized BO objects from :func:`extract_bo_inventory`.
        context_inventory: Output of :func:`extract_context_inventory`.

    Returns:
        .. code-block:: python

            {
                "by_context": {
                    "<context_name>": [object_name, ...],
                },
                "all_contexts": [object_name, ...],  # valid in every context
                "unmapped": [object_name, ...],       # couldn't determine context
            }
    """
    log.info(
        "map_objects_to_contexts: %d objects × %d contexts",
        len(objects),
        len(context_inventory),
    )

    # Build sets for fast lookup
    context_table_sets: dict[str, set[str]] = {
        ctx["context_name"]: set(t.upper() for t in ctx["active_tables"])
        for ctx in context_inventory
    }

    by_context: dict[str, list[str]] = {
        ctx["context_name"]: [] for ctx in context_inventory
    }
    all_context_objects: list[str] = []
    unmapped: list[str] = []

    # Tables common to ALL contexts
    if context_table_sets:
        shared_tables = set.intersection(*context_table_sets.values())
    else:
        shared_tables = set()

    for obj in objects:
        try:
            obj_name = obj.get("name", "<unnamed>")
            select_expr = obj.get("select_expression") or ""
            where_expr = obj.get("where_expression") or ""
            combined = f"{select_expr} {where_expr}".upper()

            # Extract table references (simple heuristic: word before a dot)
            referenced = set(
                re.findall(r"\b([A-Z_][A-Z0-9_$#]*)\s*\.", combined)
            )

            if not referenced:
                # No table reference found — treat as available in all contexts
                all_context_objects.append(obj_name)
                continue

            # Check which contexts contain ALL referenced tables
            valid_contexts = [
                ctx_name
                for ctx_name, ctx_tables in context_table_sets.items()
                if referenced.issubset(ctx_tables)
            ]

            if len(valid_contexts) == len(context_inventory):
                # Valid in every context — shared object
                all_context_objects.append(obj_name)
            elif valid_contexts:
                for ctx_name in valid_contexts:
                    by_context[ctx_name].append(obj_name)
            else:
                unmapped.append(obj_name)
                log.debug(
                    "map_objects_to_contexts: '%s' references tables not in any "
                    "context: %s",
                    obj_name,
                    referenced - set.union(*context_table_sets.values()),
                )
        except Exception as exc:
            log.warning(
                "map_objects_to_contexts: error mapping '%s': %s",
                obj.get("name", "?"),
                exc,
            )
            unmapped.append(obj.get("name", "?"))

    log.info(
        "map_objects_to_contexts: done — all_contexts=%d unmapped=%d",
        len(all_context_objects),
        len(unmapped),
    )

    return {
        "by_context": by_context,
        "all_contexts": all_context_objects,
        "unmapped": unmapped,
    }
