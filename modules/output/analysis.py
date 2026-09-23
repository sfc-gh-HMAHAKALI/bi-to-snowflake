"""Local semantic analysis — detect duplicates, parallel definitions, and overlaps.

Zero external dependencies.  Operates on unified inventory dicts.
"""

import re
from collections import Counter, defaultdict
from typing import Any

from ..common.logger import get_logger

log = get_logger("analysis")

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_inventory(inventory: dict) -> dict:
    """Run all local analyses on an inventory and return findings.

    Returns:
        {
            "parallel_definitions": [<group>, ...],
            "reuse_stats": {
                "total_raw": int,
                "total_unique": int,
                "reuse_instances": int,
                "reuse_pct": float,
                "top_reused": [{"name": str, "table": str, "kind": str, "count": int}, ...],
            },
            "stats": {
                "duplicates": int,
                "parallel_defs": int,
                "semantic_overlaps": int,
                "total_groups": int,
            },
        }

    Each group dict:
        {
            "group_id": int,
            "category": "duplicate" | "parallel_definition" | "semantic_overlap",
            "items": [{"kind": "dimension"|"fact"|"metric", "name": ..., "table": ..., "expr": ..., "data_type": ...}, ...],
            "name_similarity": float,
            "expression_match": bool,
            "detail": str,
        }
    """
    raw_items = _collect_all_items(inventory)
    all_items, reuse_stats = _dedup_items(raw_items)
    log.info(
        "Analyzing %d unique items (%d raw, %d reuse instances stripped — %.1f%% reuse).",
        len(all_items), reuse_stats["total_raw"], reuse_stats["reuse_instances"],
        reuse_stats["reuse_pct"],
    )

    groups: list[dict] = []
    group_id = 1

    # Pass 1 — exact expression duplicates (different names, same calc)
    expr_groups = _group_by_normalized_expression(all_items)
    for norm_expr, items in expr_groups.items():
        if len(items) < 2 or not norm_expr:
            continue
        # Sub-group by name similarity
        name_sim = _avg_name_similarity(items)
        if name_sim > 0.85:
            cat = "duplicate"
        else:
            cat = "semantic_overlap"

        # Check if this is a cross-kind group (e.g. dimension + metric with same expr).
        # In Looker, the same expression used as both a dimension (for grouping)
        # and a measure (for aggregation) is a deliberate, justified pattern.
        cross_kind = _is_cross_kind_only(items)

        g = {
            "group_id": group_id,
            "category": cat,
            "items": items,
            "name_similarity": round(name_sim, 2),
            "expression_match": True,
            "detail": f"Identical normalized expression: {norm_expr[:120]}",
            "cross_kind": cross_kind,
        }
        g["has_calculation"] = _group_has_calculation(g)
        groups.append(g)
        group_id += 1

    # Track items already grouped by expression
    expr_grouped_keys = set()
    for g in groups:
        for item in g["items"]:
            expr_grouped_keys.add(_item_key(item))

    # Pass 2 — similar names, different expressions (same-table only)
    # Cross-table name similarity is almost always noise — the same column
    # name appearing on unrelated tables in different schemas is expected,
    # not a conflict.  Only flag name-similar items when they share a table.
    name_groups = _group_by_name_tokens(all_items, threshold=0.85)
    for token_key, items in name_groups.items():
        if len(items) < 2:
            continue
        # Skip if all items already covered by expression groups
        ungrouped = [i for i in items if _item_key(i) not in expr_grouped_keys]
        if len(ungrouped) < 2:
            continue
        # Check if expressions actually differ
        norm_exprs = set(_normalize_expression(i["expr"]) for i in items)
        if len(norm_exprs) <= 1:
            continue  # Same expression — already caught in pass 1
        # Only keep items that share a table with at least one other item
        table_counts = Counter(i["table"] for i in items)
        same_table_items = [i for i in items if table_counts[i["table"]] >= 2]
        if len(same_table_items) < 2:
            continue
        # Re-check expression diversity after filtering
        st_exprs = set(_normalize_expression(i["expr"]) for i in same_table_items)
        if len(st_exprs) <= 1:
            continue
        g = {
            "group_id": group_id,
            "category": "parallel_definition",
            "items": same_table_items,
            "name_similarity": round(_avg_name_similarity(same_table_items), 2),
            "expression_match": False,
            "detail": f"Similar names ({token_key}), {len(st_exprs)} distinct expressions",
        }
        g["has_calculation"] = _group_has_calculation(g)
        groups.append(g)
        group_id += 1

    # Tally stats
    stats = {
        "duplicates": 0, "parallel_defs": 0, "semantic_overlaps": 0,
        "total_groups": len(groups),
        # Sub-counts: actionable (has calculation) vs common columns (trivial refs)
        "calculated_duplicates": 0, "trivial_duplicates": 0,
        "calculated_parallel_defs": 0, "trivial_parallel_defs": 0,
        "calculated_overlaps": 0, "trivial_overlaps": 0,
        "actionable_groups": 0,
        # Cross-kind: same expression used as both dimension and metric (justified)
        "cross_kind_overlaps": 0,
    }
    for g in groups:
        has_calc = g.get("has_calculation", False)
        is_cross_kind = g.get("cross_kind", False)

        if is_cross_kind:
            stats["cross_kind_overlaps"] += 1
            # Cross-kind groups are not actionable — they're a legitimate
            # pattern where the same expression serves as both dimension
            # (for grouping) and measure (for aggregation).
            continue

        if has_calc:
            stats["actionable_groups"] += 1
        if g["category"] == "duplicate":
            stats["duplicates"] += 1
            if has_calc:
                stats["calculated_duplicates"] += 1
            else:
                stats["trivial_duplicates"] += 1
        elif g["category"] == "parallel_definition":
            stats["parallel_defs"] += 1
            if has_calc:
                stats["calculated_parallel_defs"] += 1
            else:
                stats["trivial_parallel_defs"] += 1
        elif g["category"] == "semantic_overlap":
            stats["semantic_overlaps"] += 1
            if has_calc:
                stats["calculated_overlaps"] += 1
            else:
                stats["trivial_overlaps"] += 1

    log.info(
        "Analysis complete: %d groups (%d actionable). Duplicates: %d (%d calculated). "
        "Parallel defs: %d (%d calculated). Overlaps: %d.",
        stats["total_groups"], stats["actionable_groups"],
        stats["duplicates"], stats["calculated_duplicates"],
        stats["parallel_defs"], stats["calculated_parallel_defs"],
        stats["semantic_overlaps"],
    )

    return {"parallel_definitions": groups, "reuse_stats": reuse_stats, "stats": stats}


# ---------------------------------------------------------------------------
# Item collection
# ---------------------------------------------------------------------------

def _collect_all_items(inventory: dict) -> list[dict]:
    """Flatten dims/facts/metrics into a uniform list with a 'kind' field."""
    items = []
    for kind, key in [("dimension", "dimensions"), ("fact", "facts"), ("metric", "metrics")]:
        for item in inventory.get(key, []):
            items.append({
                "kind": kind,
                "name": item.get("name", ""),
                "table": item.get("table", ""),
                "expr": item.get("expr", ""),
                "data_type": item.get("data_type", ""),
                "description": item.get("description", "") or "",
                "complexity": item.get("complexity", "simple"),
                "source_view": item.get("source_view", ""),
                "dashboard_name": item.get("dashboard_name", ""),
                "page_name": item.get("page_name", ""),
                "widget_name": item.get("widget_name", ""),
                "source_file": item.get("source_file", ""),
            })
    return items


def _item_key(item: dict) -> str:
    return f"{item['kind']}::{item['table']}::{item['name']}"


def _dedup_items(items: list[dict]) -> tuple[list[dict], dict]:
    """Remove items that are exact reuse (same kind+name+table+expr).

    In Looker, the same view dimension can appear in many explores/dashboards.
    In Power BI, the same measure can appear in many reports.  These are *reuse*,
    not duplication — they share a single canonical definition.

    Also strips ``+`` prefixes from table names in legacy inventories that
    were generated before the parser-level refinement merge fix.

    Returns:
        (deduped_items, reuse_stats)
    """
    from collections import OrderedDict

    seen: OrderedDict[tuple, dict] = OrderedDict()
    reuse_counter: Counter = Counter()

    for item in items:
        # Strip leading '+' from table names — legacy inventories may still
        # contain +VIEW_NAME entries from before the parser-level refinement fix.
        table = item["table"]
        expr = item["expr"]
        if table.startswith("+"):
            bare = table.lstrip("+")
            expr = expr.replace(table, bare)
            table = bare
            item = {**item, "table": table, "expr": expr}

        key = (item["kind"], item["name"], table, expr)
        if key in seen:
            reuse_counter[key] += 1
        else:
            seen[key] = item

    reuse_instances = sum(reuse_counter.values())
    total_raw = len(items)
    total_unique = len(seen)

    # Top reused items (most referenced across explores/reports)
    top_reused = []
    for (kind, name, table, _expr), count in reuse_counter.most_common(20):
        top_reused.append({
            "name": name,
            "table": table,
            "kind": kind,
            "count": count + 1,  # +1 for the canonical instance
        })

    # ── Semantic metrics ──
    deduped = list(seen.values())
    distinct_names: set[str] = set()
    calculated_count = 0
    name_to_exprs: dict[str, set[str]] = {}
    for item in deduped:
        n = item["name"]
        distinct_names.add(n)
        if _is_calculated_expression(item.get("expr", "")):
            calculated_count += 1
        name_to_exprs.setdefault(n, set()).add(item.get("expr", ""))

    inconsistent_count = sum(1 for exprs in name_to_exprs.values() if len(exprs) > 1)

    reuse_stats = {
        "total_raw": total_raw,
        "total_unique": total_unique,
        "reuse_instances": reuse_instances,
        "reuse_pct": round(reuse_instances / total_raw * 100, 1) if total_raw else 0.0,
        "top_reused": top_reused,
        "distinct_field_names": len(distinct_names),
        "calculated_definitions": calculated_count,
        "inconsistent_names": inconsistent_count,
    }

    return deduped, reuse_stats


# ---------------------------------------------------------------------------
# Expression normalization and grouping
# ---------------------------------------------------------------------------

_TABLE_PREFIX_RE = re.compile(r'(?<!\$\{)\b[A-Za-z_][A-Za-z0-9_]*\.(?![^}]*\})')
_LOOKML_REF_RE = re.compile(r'\$\{[^}]+\}')
_WHITESPACE_RE = re.compile(r'\s+')


def _normalize_expression(expr: str) -> str:
    """Normalize an expression for comparison.

    - Lowercase
    - Collapse whitespace
    - Remove SQL table prefixes (e.g. Sales.Amount → Amount)
      but preserve LookML field refs (${view.field} stays intact)
    - Strip quotes
    """
    if not expr:
        return ""
    s = expr.lower().strip()
    s = _WHITESPACE_RE.sub(" ", s)
    # Protect LookML refs from table-prefix stripping by replacing them
    # with placeholders, stripping prefixes, then restoring.
    refs: list[str] = []
    def _save_ref(m):
        refs.append(m.group(0))
        return f"\x00REF{len(refs)-1}\x00"
    s = _LOOKML_REF_RE.sub(_save_ref, s)
    s = _TABLE_PREFIX_RE.sub("", s)
    for i, ref in enumerate(refs):
        s = s.replace(f"\x00REF{i}\x00", ref)
    s = s.replace('"', '').replace("'", "").replace("`", "")
    return s


def _group_by_normalized_expression(items: list[dict]) -> dict[str, list[dict]]:
    """Group items by their normalized expression."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        norm = _normalize_expression(item["expr"])
        if norm:
            groups[norm].append(item)
    return dict(groups)


# ---------------------------------------------------------------------------
# Name tokenization and similarity
# ---------------------------------------------------------------------------

_NAME_SPLIT_RE = re.compile(r'[_\s\-]+')


def _tokenize_name(name: str) -> set[str]:
    """Split a name into lowercase tokens.

    Keeps numeric tokens (e.g. '1', '7', '30') so that companion columns
    like PAGE_VIEW_1_DAY and PAGE_VIEW_7_DAY are not collapsed into
    identical token sets.
    """
    # Also split camelCase
    spaced = re.sub(r'([a-z])([A-Z])', r'\1 \2', name)
    parts = _NAME_SPLIT_RE.split(spaced.lower())
    return {p for p in parts if p}


def _jaccard_similarity(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _avg_name_similarity(items: list[dict]) -> float:
    """Average pairwise name similarity across items."""
    if len(items) < 2:
        return 1.0
    token_sets = [_tokenize_name(i["name"]) for i in items]
    total = 0.0
    count = 0
    for i in range(len(token_sets)):
        for j in range(i + 1, len(token_sets)):
            total += _jaccard_similarity(token_sets[i], token_sets[j])
            count += 1
    return total / count if count else 0.0


def _group_by_name_tokens(items: list[dict], threshold: float = 0.7) -> dict[str, list[dict]]:
    """Group items whose tokenized names are similar.

    Uses a greedy clustering approach: for each item, find or create a cluster
    whose representative has Jaccard similarity >= threshold.
    """
    clusters: list[tuple[str, set[str], list[dict]]] = []

    for item in items:
        tokens = _tokenize_name(item["name"])
        if not tokens:
            continue
        placed = False
        for i, (key, rep_tokens, members) in enumerate(clusters):
            if _jaccard_similarity(tokens, rep_tokens) >= threshold:
                members.append(item)
                placed = True
                break
        if not placed:
            key = " ".join(sorted(tokens))
            clusters.append((key, tokens, [item]))

    return {key: members for key, _, members in clusters if len(members) >= 2}


# ---------------------------------------------------------------------------
# Calculated vs trivial expression classification
# ---------------------------------------------------------------------------

_TRIVIAL_EXPR_RE = re.compile(r'^[\+\"\w\.\s\[\]]+$')


def _is_calculated_expression(expr: str) -> bool:
    """Return True if an expression contains logic beyond a simple column reference.

    Trivial: ``TABLE."COLUMN"``, ``+VIEW."COL"``, ``"SCHEMA"."TABLE"."COL"``
    Calculated: anything with functions, CASE, operators, aggregations, ${refs}, etc.
    """
    if not expr or not expr.strip():
        return False
    return not _TRIVIAL_EXPR_RE.match(expr.strip())


def _group_has_calculation(group: dict) -> bool:
    """Return True if any item in the group has a non-trivial expression."""
    return any(_is_calculated_expression(item.get("expr", "")) for item in group["items"])


def _is_cross_kind_only(items: list[dict]) -> bool:
    """Return True if the group's items differ only by kind (dimension vs metric).

    In Looker, the same expression is commonly used as both a dimension
    (for grouping/filtering) and a measure (for aggregation).  This is a
    deliberate design pattern, not a semantic overlap that needs resolution.
    """
    kinds = set(i.get("kind", "") for i in items)
    return len(kinds) > 1


# ---------------------------------------------------------------------------
# AI-assisted rationalization via Snowflake Cortex
# ---------------------------------------------------------------------------

def ai_rationalize_groups(
    groups: list[dict],
    *,
    connection: str | None = None,
    model: str = "llama3.1-70b",
    max_groups: int = 100,
) -> dict[int, str]:
    """Call Snowflake Cortex AI_COMPLETE to produce rationalization advice.

    For each actionable group (duplicate / parallel def / overlap), sends a
    prompt describing the definitions and asks the model to recommend how to
    consolidate them into a single governed definition.

    Args:
        groups: The ``parallel_definitions`` list from ``analyze_inventory()``.
        connection: Optional Snowflake connection name.
        model: Cortex LLM model identifier.
        max_groups: Cap on how many groups to send to the LLM (cost control).

    Returns:
        Dict mapping ``group_id`` → AI recommendation text.
        Groups that fail or are skipped will not appear in the dict.
    """
    actionable = [g for g in groups
                  if g.get("has_calculation") and not g.get("cross_kind")][:max_groups]

    if not actionable:
        log.info("ai_rationalize_groups: no actionable groups — skipping.")
        return {}

    log.info("ai_rationalize_groups: sending %d groups to Cortex (%s)…",
             len(actionable), model)

    try:
        import snowflake.connector
    except ImportError:
        log.warning("snowflake-connector-python not installed — AI analysis unavailable.")
        return {}

    try:
        conn = _get_snowflake_connection(connection)
    except Exception as e:
        log.warning("Could not establish Snowflake connection for AI analysis: %s", e)
        return {}

    recommendations: dict[int, str] = {}
    cur = conn.cursor()

    for g in actionable:
        prompt = _build_rationalization_prompt(g)
        try:
            cur.execute(
                "SELECT SNOWFLAKE.CORTEX.COMPLETE(%s, %s) AS recommendation",
                (model, prompt),
            )
            row = cur.fetchone()
            if row and row[0]:
                rec = row[0].strip()
                # Truncate excessively long responses
                if len(rec) > 500:
                    rec = rec[:497] + "…"
                recommendations[g["group_id"]] = rec
        except Exception as e:
            log.warning("AI_COMPLETE failed for group %d: %s", g["group_id"], e)
            continue

    cur.close()
    conn.close()
    log.info("ai_rationalize_groups: got %d/%d recommendations.",
             len(recommendations), len(actionable))
    return recommendations


def _build_rationalization_prompt(group: dict) -> str:
    """Build a concise prompt for one group of overlapping definitions."""
    cat = group["category"].replace("_", " ")
    items = group["items"]

    lines = [
        f"You are a data governance analyst. Below is a group of {cat} "
        f"definitions found in a BI semantic layer. Each item defines a "
        f"metric or dimension that overlaps with the others in this group.",
        "",
        "Definitions:",
    ]
    for i, item in enumerate(items[:8], 1):
        lines.append(
            f"  {i}. {item['kind']} \"{item['name']}\" on table {item['table']}"
            f"  →  {item['expr'][:200]}"
        )

    lines.extend([
        "",
        "In 1-2 sentences, recommend which definition to keep as the canonical "
        "governed version and why. If the expressions are identical, say so and "
        "recommend the clearest name. If they differ, explain the semantic difference "
        "and which is more complete. Be specific and concise.",
    ])

    return "\n".join(lines)


def _get_snowflake_connection(connection_name: str | None = None):
    """Open a Snowflake connection using snowflake-connector-python.

    Tries, in order:
    1. Named connection from ~/.snowflake/connections.toml (if connection_name given)
    2. Default connection from SNOWFLAKE_DEFAULT_CONNECTION_NAME env var
    3. Bare ``snowflake.connector.connect()`` which reads default config
    """
    import snowflake.connector

    if connection_name:
        return snowflake.connector.connect(connection_name=connection_name)

    import os
    default = os.environ.get("SNOWFLAKE_DEFAULT_CONNECTION_NAME")
    if default:
        return snowflake.connector.connect(connection_name=default)

    return snowflake.connector.connect()


# ---------------------------------------------------------------------------
# Expression lineage extraction (for future AI enrichment)
# ---------------------------------------------------------------------------

_AGG_RE = re.compile(r'\b(SUM|AVG|COUNT|COUNT_DISTINCT|MIN|MAX|CALCULATE|SUMX|AVERAGEX)\b', re.IGNORECASE)
_COL_REF_RE = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b')


def extract_expression_lineage(expr: str) -> dict:
    """Extract lineage metadata from an expression.

    Returns:
        {
            "aggregations": ["SUM", ...],
            "column_refs": [("Table", "Column"), ...],
            "has_filter": bool,
        }
    """
    if not expr:
        return {"aggregations": [], "column_refs": [], "has_filter": False}

    aggs = list({m.group(1).upper() for m in _AGG_RE.finditer(expr)})
    refs = list({(m.group(1), m.group(2)) for m in _COL_REF_RE.finditer(expr)})
    has_filter = bool(re.search(r'\b(WHERE|FILTER|CALCULATE)\b', expr, re.IGNORECASE))

    return {"aggregations": sorted(aggs), "column_refs": sorted(refs), "has_filter": has_filter}


# ---------------------------------------------------------------------------
# Cross-dashboard coverage analysis
# ---------------------------------------------------------------------------

def analyze_dashboard_coverage(inventory: dict) -> dict:
    """Analyse field overlap and uniqueness across dashboards/pages.

    Groups every dimension/fact/metric by its ``dashboard_name`` (falling
    back to ``source_file`` then ``source_view``).  For each pair of
    dashboards computes Jaccard similarity of their field-name sets.

    Returns::

        {
            "dashboards": [
                {
                    "name": str,
                    "total_fields": int,
                    "unique_fields": int,       # fields used ONLY in this dashboard
                    "shared_fields": int,        # fields also in >= 1 other dashboard
                    "dimensions": int,
                    "facts": int,
                    "metrics": int,
                    "field_names": [str, ...],   # sorted list for downstream use
                },
                ...
            ],
            "similarity_matrix": [
                {
                    "dashboard_a": str,
                    "dashboard_b": str,
                    "jaccard": float,            # 0-1
                    "shared_count": int,
                    "shared_fields": [str, ...], # the actual shared field names
                },
                ...
            ],
            "stats": {
                "total_dashboards": int,
                "avg_jaccard": float,
                "max_jaccard": float,
                "most_similar_pair": [str, str] | None,
                "total_unique_fields": int,      # fields that appear in exactly 1 dashboard
                "total_shared_fields": int,      # fields that appear in 2+ dashboards
            },
        }
    """
    raw_items = _collect_all_items(inventory)

    # -- Group fields by dashboard label ------------------------------------
    dash_fields: dict[str, dict[str, set]] = defaultdict(lambda: {"dimension": set(), "fact": set(), "metric": set()})

    for item in raw_items:
        fname = f"{item['table']}.{item['name']}"
        kind = item["kind"]

        # Determine grouping labels.  Prefer page_name (dashboard pages) over
        # dashboard_name over source_file — source_view is the *table* name
        # and must NOT be used as a dashboard label (it would create one
        # "dashboard" per table, which is meaningless).
        page_name = item.get("page_name", "")
        dash_name = item.get("dashboard_name", "")
        src_file = item.get("source_file", "")

        if page_name:
            # page_name may be comma-separated ("Page A, Page B") when a
            # field is used on multiple dashboard pages — fan out.
            labels = [p.strip() for p in page_name.split(",") if p.strip()]
        elif dash_name:
            labels = [dash_name]
        elif src_file:
            labels = [src_file]
        else:
            labels = ["(model-only)"]

        for label in labels:
            dash_fields[label][kind].add(fname)

    if len(dash_fields) < 2:
        # Nothing meaningful to compare
        single = list(dash_fields.keys())
        return {
            "dashboards": [{
                "name": single[0] if single else "(none)",
                "total_fields": sum(len(s) for s in dash_fields.get(single[0], {}).values()) if single else 0,
                "unique_fields": sum(len(s) for s in dash_fields.get(single[0], {}).values()) if single else 0,
                "shared_fields": 0,
                "dimensions": len(dash_fields.get(single[0], {}).get("dimension", set())) if single else 0,
                "facts": len(dash_fields.get(single[0], {}).get("fact", set())) if single else 0,
                "metrics": len(dash_fields.get(single[0], {}).get("metric", set())) if single else 0,
                "field_names": sorted(set().union(*(dash_fields.get(single[0], {}).values()))) if single else [],
            }] if single else [],
            "similarity_matrix": [],
            "stats": {
                "total_dashboards": len(single),
                "avg_jaccard": 0.0,
                "max_jaccard": 0.0,
                "most_similar_pair": None,
                "total_unique_fields": sum(len(s) for s in dash_fields.get(single[0], {}).values()) if single else 0,
                "total_shared_fields": 0,
            },
        }

    # -- Build per-dashboard all-fields sets --------------------------------
    all_sets: dict[str, set[str]] = {}
    for label, kinds in dash_fields.items():
        all_sets[label] = kinds["dimension"] | kinds["fact"] | kinds["metric"]

    # Determine which fields are globally unique vs shared
    field_occurrence: Counter = Counter()
    for fset in all_sets.values():
        for f in fset:
            field_occurrence[f] += 1

    global_unique = {f for f, c in field_occurrence.items() if c == 1}
    global_shared = {f for f, c in field_occurrence.items() if c >= 2}

    # -- Dashboard summaries ------------------------------------------------
    dashboards = []
    for label in sorted(all_sets):
        fset = all_sets[label]
        kinds = dash_fields[label]
        unique = fset & global_unique
        shared = fset & global_shared
        dashboards.append({
            "name": label,
            "total_fields": len(fset),
            "unique_fields": len(unique),
            "shared_fields": len(shared),
            "dimensions": len(kinds["dimension"]),
            "facts": len(kinds["fact"]),
            "metrics": len(kinds["metric"]),
            "field_names": sorted(fset),
        })

    # -- Pairwise Jaccard similarity ----------------------------------------
    labels_sorted = sorted(all_sets)
    similarity_matrix = []
    max_jaccard = 0.0
    max_pair = None
    jaccard_sum = 0.0
    pair_count = 0

    for i, a in enumerate(labels_sorted):
        for b in labels_sorted[i + 1:]:
            sa, sb = all_sets[a], all_sets[b]
            intersection = sa & sb
            union = sa | sb
            jac = len(intersection) / len(union) if union else 0.0
            similarity_matrix.append({
                "dashboard_a": a,
                "dashboard_b": b,
                "jaccard": round(jac, 4),
                "shared_count": len(intersection),
                "shared_fields": sorted(intersection),
            })
            jaccard_sum += jac
            pair_count += 1
            if jac > max_jaccard:
                max_jaccard = jac
                max_pair = [a, b]

    avg_jaccard = round(jaccard_sum / pair_count, 4) if pair_count else 0.0

    return {
        "dashboards": dashboards,
        "similarity_matrix": similarity_matrix,
        "stats": {
            "total_dashboards": len(dashboards),
            "avg_jaccard": avg_jaccard,
            "max_jaccard": round(max_jaccard, 4),
            "most_similar_pair": max_pair,
            "total_unique_fields": len(global_unique),
            "total_shared_fields": len(global_shared),
        },
    }


# ---------------------------------------------------------------------------
# Report narrative intelligence
# ---------------------------------------------------------------------------

# Generic domain keywords for purpose inference — ordered by specificity.
# These are universal business patterns, not customer-specific terms.
# Each tuple: (keyword_to_match, topic_noun, question_verb)
# The question_verb helps construct "What is our <topic>?" or "How are we <verb>ing?"
_PURPOSE_KEYWORDS: list[tuple[str, str, str]] = [
    ("REVENUE", "revenue", "earning"),
    ("PROFIT", "profitability", "performing"),
    ("MARGIN", "margins", "performing"),
    ("COST", "costs", "spending"),
    ("EXPENSE", "expenses", "spending"),
    ("BUDGET", "budget", "tracking against budget"),
    ("FORECAST", "forecast", "projecting"),
    ("INVENTORY", "inventory levels", "managing inventory"),
    ("PIPELINE", "pipeline", "tracking pipeline"),
    ("RETENTION", "retention", "retaining"),
    ("CHURN", "churn", "losing customers"),
    ("ATTRITION", "attrition", "losing"),
    ("CONVERSION", "conversion rates", "converting"),
    ("UTILIZATION", "utilization", "utilizing resources"),
    ("COMPLIANCE", "compliance", "meeting compliance"),
    ("SLA", "SLA performance", "meeting SLAs"),
    ("CANCEL", "cancellations", "tracking cancellations"),
    ("SUBMIT", "submissions", "processing submissions"),
    ("CONFIRM", "confirmations", "confirming"),
    ("APPROVAL", "approvals", "approving"),
    ("SCHEDULE", "scheduling", "scheduling"),
    ("ROSTER", "roster composition", "staffing"),
    ("AUDIT", "audit trail", "reconciling"),
    ("TRANSPORT", "transport activity", "moving"),
    ("TIMEKEEP", "time entries", "tracking time"),
    ("HOURS", "hours worked", "tracking hours"),
    ("OVERTIME", "overtime", "tracking overtime"),
    ("DEPLOY", "deployments", "deploying"),
    ("FILL", "fill rates", "filling positions"),
    ("ORDER", "orders", "processing orders"),
    ("SALES", "sales", "selling"),
    ("CUSTOMER", "customers", "serving customers"),
    ("PATIENT", "patients", "tracking patients"),
    ("CLAIM", "claims", "processing claims"),
    ("PAYMENT", "payments", "processing payments"),
    ("INVOICE", "invoices", "billing"),
    ("SHIPMENT", "shipments", "shipping"),
    ("DELIVERY", "deliveries", "delivering"),
    ("TICKET", "tickets", "resolving tickets"),
    ("INCIDENT", "incidents", "managing incidents"),
    ("HEADCOUNT", "headcount", "staffing"),
    ("HIRE", "hiring", "hiring"),
    ("TURNOVER", "turnover", "managing turnover"),
    ("BACKLOG", "backlog", "managing backlog"),
    ("CAPACITY", "capacity", "managing capacity"),
    ("QUALITY", "quality", "measuring quality"),
    ("DEFECT", "defects", "tracking defects"),
    ("PERFORMANCE", "performance", "measuring performance"),
]

# Dimension-like keywords that suggest a GROUP BY axis / breakdown
_DIMENSION_HINTS: list[tuple[str, str]] = [
    ("REGION", "region"),
    ("TERRITORY", "territory"),
    ("DEPARTMENT", "department"),
    ("DEPT", "department"),
    ("DIVISION", "division"),
    ("LOCATION", "location"),
    ("FACILITY", "facility"),
    ("SITE", "site"),
    ("BRANCH", "branch"),
    ("OFFICE", "office"),
    ("CATEGORY", "category"),
    ("SEGMENT", "segment"),
    ("CHANNEL", "channel"),
    ("PRODUCT", "product"),
    ("SERVICE", "service"),
    ("CLIENT", "client"),
    ("VENDOR", "vendor"),
    ("SUPPLIER", "supplier"),
    ("EMPLOYEE", "employee"),
    ("WORKER", "worker"),
    ("NURSE", "nurse"),
    ("PHYSICIAN", "physician"),
    ("PROVIDER", "provider"),
    ("SPECIALTY", "specialty"),
    ("PERIOD", "time period"),
    ("QUARTER", "quarter"),
    ("MONTH", "month"),
    ("WEEK", "week"),
    ("YEAR", "year"),
    ("STATUS", "status"),
    ("PRIORITY", "priority"),
    ("TYPE", "type"),
]


def _infer_page_purpose(page_name: str, metric_names: list[str],
                         field_names: list[str] | None = None,
                         table_names: list[str] | None = None) -> str:
    """Infer a question-oriented purpose from the data on the page.

    Aims to produce output like:
      "Tracks fill rates by department and facility"
      "Monitors cancellations by region"
      "Analyzes revenue across 4 tables"

    Never returns the page name verbatim.
    """
    # Build a combined text corpus from all available signals
    all_tokens = list(metric_names or [])
    if field_names:
        all_tokens.extend(field_names)
    if table_names:
        all_tokens.extend(table_names)
    combined = " ".join(all_tokens).upper()

    # Strip table prefixes from field names to get bare column names
    bare_fields_upper: list[str] = []
    for fn in (field_names or []):
        # field_names are "TABLE.COLUMN" — take the column part
        bare = fn.split(".")[-1] if "." in fn else fn
        bare_fields_upper.append(bare.upper())
    bare_combined = " ".join(bare_fields_upper)

    # 1) Find matching topics from the data (collect ALL matches, not just first)
    matched_topics: list[str] = []
    for keyword, topic_noun, _ in _PURPOSE_KEYWORDS:
        if keyword in combined:
            matched_topics.append(topic_noun)
        if len(matched_topics) >= 3:
            break

    # 2) Find dimension/breakdown axes from field names
    matched_dims: list[str] = []
    for keyword, dim_label in _DIMENSION_HINTS:
        if keyword in bare_combined:
            matched_dims.append(dim_label)
        if len(matched_dims) >= 2:
            break

    # 3) Construct the purpose string
    if matched_topics:
        # Primary topic
        primary = matched_topics[0]
        # Build the sentence
        if matched_dims:
            dim_str = " and ".join(matched_dims[:2])
            purpose = f"Tracks {primary} by {dim_str}"
        else:
            t_count = len(table_names) if table_names else 0
            if t_count > 1:
                purpose = f"Tracks {primary} across {t_count} tables"
            else:
                purpose = f"Tracks {primary}"

        # Append secondary topics if distinct
        if len(matched_topics) > 1:
            extras = [t for t in matched_topics[1:] if t != primary]
            if extras:
                purpose += f"; also covers {', '.join(extras)}"
        return purpose

    # 4) No topic keyword match — try to describe from composition
    m_count = len(metric_names or [])
    t_count = len(table_names) if table_names else 0
    f_count = len(field_names) if field_names else 0

    if matched_dims:
        dim_str = " and ".join(matched_dims[:2])
        if m_count > 0:
            return f"Analyzes {m_count} metrics by {dim_str}"
        else:
            return f"Views data by {dim_str}"

    if m_count > 0 and t_count > 0:
        return f"Analyzes {m_count} metrics across {t_count} tables"
    elif f_count > 0 and t_count > 0:
        return f"Displays {f_count} fields across {t_count} tables"
    elif m_count > 0:
        return f"Tracks {m_count} metrics"
    else:
        return "General data view"


def analyze_report_narrative(inventory: dict, analysis: dict | None = None) -> dict:
    """Build interpretive narrative data for report intelligence section.

    Returns a dict with page_profiles, active_vs_model, reuse_distribution,
    consolidation_opportunities, conflict_coverage, and
    semantic_model_recommendation.
    """
    raw_items = _collect_all_items(inventory)
    flagged_groups = (analysis or {}).get("parallel_definitions", [])

    # Source-aware terminology — "report pages" is wrong for dashboards
    source_type = inventory.get("source_type", "unknown").lower()
    if source_type in ("powerbi", "tableau"):
        page_label = "dashboard pages"
    elif source_type == "looker":
        page_label = "explores"
    else:
        page_label = "pages"

    # ── Page profiles ────────────────────────────────────────────────────
    page_data: dict[str, dict] = defaultdict(lambda: {
        "facts": set(), "metrics": set(), "metric_names": [],
        "widgets": set(), "tables": set(), "field_names": set(),
        "dashboard_names": set(),
    })

    for item in raw_items:
        pn = item.get("page_name", "")
        wn = item.get("widget_name", "")
        dn = item.get("dashboard_name", "")
        if not pn:
            continue
        fname = f"{item['table']}.{item['name']}"
        for page in pn.split(", "):
            page = page.strip()
            if not page:
                continue
            pd = page_data[page]
            pd["field_names"].add(fname)
            pd["tables"].add(item["table"])
            if dn:
                for d in dn.split(", "):
                    d = d.strip()
                    if d:
                        pd["dashboard_names"].add(d)
            if item["kind"] == "metric":
                pd["metrics"].add(fname)
                pd["metric_names"].append(item["name"])
            else:
                pd["facts"].add(fname)
            if wn:
                for w in wn.split(", "):
                    w = w.strip()
                    if w and w != "unknown":
                        pd["widgets"].add(w)

    page_profiles = []
    for pname in sorted(page_data):
        pd = page_data[pname]
        purpose = _infer_page_purpose(
            pname, pd["metric_names"],
            field_names=list(pd["field_names"]),
            table_names=sorted(pd["tables"]),
        )
        page_profiles.append({
            "name": pname,
            "purpose": purpose,
            "total_fields": len(pd["field_names"]),
            "fact_count": len(pd["facts"]),
            "metric_count": len(pd["metrics"]),
            "table_count": len(pd["tables"]),
            "tables": sorted(pd["tables"]),
            "widgets": sorted(pd["widgets"]),
            "dashboard_names": sorted(pd["dashboard_names"]),
        })

    # ── Active vs model-only ─────────────────────────────────────────────
    active_fields: set[str] = set()
    for pd in page_data.values():
        active_fields |= pd["field_names"]

    all_fields: set[str] = set()
    for item in raw_items:
        all_fields.add(f"{item['table']}.{item['name']}")

    model_only = all_fields - active_fields
    mo_by_table: Counter = Counter()
    for f in model_only:
        tbl = f.split(".")[0]
        mo_by_table[tbl] += 1

    active_vs_model = {
        "total": len(all_fields),
        "active_count": len(active_fields),
        "active_pct": round(100 * len(active_fields) / len(all_fields), 1) if all_fields else 0,
        "model_only_count": len(model_only),
        "model_only_pct": round(100 * len(model_only) / len(all_fields), 1) if all_fields else 0,
        "model_only_by_table": [
            {"table": t, "unused_count": c}
            for t, c in mo_by_table.most_common(10)
        ],
    }

    # ── Reuse distribution ───────────────────────────────────────────────
    field_page_count: dict[str, set[str]] = defaultdict(set)
    for pname, pd in page_data.items():
        for fname in pd["field_names"]:
            field_page_count[fname].add(pname)

    dist: Counter = Counter()
    for pages in field_page_count.values():
        dist[len(pages)] += 1

    core_fields = []
    for fname, pages in sorted(field_page_count.items(), key=lambda x: -len(x[1])):
        if len(pages) >= 5:
            core_fields.append({
                "name": fname,
                "page_count": len(pages),
                "pages": sorted(pages),
            })

    reuse_distribution = {
        "single_page": dist.get(1, 0),
        "multi_page": sum(c for n, c in dist.items() if n >= 2),
        "core_field_count": len(core_fields),
        "core_fields": core_fields[:20],
        "distribution": dict(sorted(dist.items())),
    }

    # ── Consolidation opportunities ──────────────────────────────────────
    # Use dashboard coverage data for Jaccard
    dash_cov = analyze_dashboard_coverage(inventory)
    consolidation = []
    for pair in dash_cov.get("similarity_matrix", []):
        if pair["jaccard"] >= 0.80:
            a, b = pair["dashboard_a"], pair["dashboard_b"]
            # Skip (model-only) bucket
            if "(model-only)" in (a, b):
                continue
            # Find what differs
            a_set = set(next((d["field_names"] for d in dash_cov["dashboards"] if d["name"] == a), []))
            b_set = set(next((d["field_names"] for d in dash_cov["dashboards"] if d["name"] == b), []))
            only_a = sorted(f.split(".")[-1] for f in (a_set - b_set))
            only_b = sorted(f.split(".")[-1] for f in (b_set - a_set))
            consolidation.append({
                "page_a": a,
                "page_b": b,
                "jaccard": pair["jaccard"],
                "shared_count": pair["shared_count"],
                "only_in_a": only_a[:10],
                "only_in_b": only_b[:10],
            })

    # ── Conflict coverage ────────────────────────────────────────────────
    # Build per-group detail with page mapping for active conflicts
    active_conflict_groups: list[dict] = []
    model_only_conflict_count = 0
    conflicted_field_names: set[str] = set()   # Table.Name of ALL fields in active conflicts

    for group in flagged_groups:
        items = group.get("items", [])
        item_names = {f"{i.get('table', '')}.{i.get('name', '')}" for i in items}
        active_in_group = item_names & active_fields
        if active_in_group:
            # Collect pages affected by this conflict
            pages_affected: set[str] = set()
            for i in items:
                pn = i.get("page_name", "")
                if pn:
                    for pg in pn.split(", "):
                        pg = pg.strip()
                        if pg:
                            pages_affected.add(pg)
            active_conflict_groups.append({
                "category": group.get("category", "unknown"),
                "has_calculation": group.get("has_calculation", False),
                "fields": [f"{i.get('table', '')}.{i.get('name', '')}" for i in items],
                "pages_affected": sorted(pages_affected),
                "detail": group.get("detail", ""),
            })
            conflicted_field_names |= item_names
        else:
            model_only_conflict_count += 1

    conflict_coverage = {
        "total": len(flagged_groups),
        "active_conflicts": len(active_conflict_groups),
        "model_only_conflicts": model_only_conflict_count,
        "active_conflict_groups": active_conflict_groups,
        "conflicted_field_names": sorted(conflicted_field_names),
        "active_conflict_ratio": round(
            len(active_conflict_groups) / len(active_fields) * 100, 1
        ) if active_fields else 0,
    }

    # ── Semantic model recommendation ────────────────────────────────────
    # P1: core grain fields that are NOT involved in any conflict
    # P1-blocked: core fields that ARE in a conflict group
    # P2: remaining active fields (not core)
    # P3: model-only fields
    core_names = {f["name"] for f in core_fields}
    p1_clean = [f for f in core_fields if f["name"] not in conflicted_field_names]
    p1_blocked = [f for f in core_fields if f["name"] in conflicted_field_names]
    p2_count = len(active_fields) - len(core_fields)
    p3_count = len(model_only)

    semantic_model_recommendation = {
        "priority_1": {"label": "Core Fields", "count": len(p1_clean),
                        "description": f"Used across 5+ {page_label}, conflict-free. Include immediately.",
                        "fields": [f["name"] for f in p1_clean[:20]]},
        "priority_1_blocked": {"label": "Core — Blocked by Conflicts", "count": len(p1_blocked),
                                "description": "Core fields involved in definition conflicts. Resolve conflicts to promote.",
                                "fields": [f["name"] for f in p1_blocked[:20]]},
        "priority_2": {"label": "Page-Specific Fields", "count": p2_count,
                        "description": f"Active on 1-4 {page_label}. Include based on business need."},
        "priority_3": {"label": "Model-Only Fields", "count": p3_count,
                        "description": f"Not used on any {page_label.rstrip('s') if page_label.endswith('s') else page_label}. Triage at table level."},
    }

    # ── Scoped readiness (per-dashboard or per-page) ──────────────────────
    # Collect all unique dashboard names across page profiles
    all_dash_names: set[str] = set()
    for pp in page_profiles:
        all_dash_names.update(pp.get("dashboard_names", []))

    multi_dashboard = len(all_dash_names) > 1

    def _score_readiness(scope_fields: set, scope_conflict_count: int,
                         scope_core_count: int) -> tuple[str, str]:
        """Return (label, color_css) using the same 4-tier logic."""
        ratio = (scope_conflict_count / len(scope_fields) * 100) if scope_fields else 0
        if scope_core_count >= 5 and ratio < 1:
            return "High", "var(--green)"
        elif scope_core_count >= 1 and ratio < 5:
            return "Good", "var(--green)"
        elif ratio <= 15 or (scope_core_count < 5 and scope_conflict_count > 0):
            return "Moderate", "var(--amber)"
        else:
            return "Low", "var(--red)"

    scoped_readiness: list[dict] = []

    if multi_dashboard:
        # Group pages by dashboard name, compute per-dashboard readiness
        dash_page_map: dict[str, list[str]] = defaultdict(list)
        for pp in page_profiles:
            for dn in pp.get("dashboard_names", []):
                dash_page_map[dn].append(pp["name"])

        for dname in sorted(dash_page_map):
            d_pages = set(dash_page_map[dname])
            d_fields: set[str] = set()
            for pp in page_profiles:
                if pp["name"] in d_pages:
                    # Reconstruct field set from page_data
                    d_fields |= page_data.get(pp["name"], {}).get("field_names", set())
            # Count conflicts touching this dashboard's pages
            d_conflicts = 0
            for cg in active_conflict_groups:
                if d_pages & set(cg.get("pages_affected", [])):
                    d_conflicts += 1
            # Count core fields in this dashboard (fields used on 5+ pages globally that are on this dashboard's pages)
            d_core = sum(1 for cf in core_fields if d_pages & set(cf.get("pages", [])))
            label, color = _score_readiness(d_fields, d_conflicts, d_core)
            scoped_readiness.append({
                "scope_name": dname,
                "scope_type": "dashboard",
                "page_count": len(d_pages),
                "field_count": len(d_fields),
                "conflict_count": d_conflicts,
                "core_field_count": d_core,
                "readiness_label": label,
                "readiness_color": color,
            })
    else:
        # Single dashboard (or no dashboard info) — per-page readiness
        for pp in page_profiles:
            p_fields = page_data.get(pp["name"], {}).get("field_names", set())
            p_conflicts = 0
            for cg in active_conflict_groups:
                if pp["name"] in cg.get("pages_affected", []):
                    p_conflicts += 1
            p_core = sum(1 for cf in core_fields if pp["name"] in cf.get("pages", []))
            label, color = _score_readiness(p_fields, p_conflicts, p_core)
            scoped_readiness.append({
                "scope_name": pp["name"],
                "scope_type": "page",
                "page_count": 1,
                "field_count": len(p_fields),
                "conflict_count": p_conflicts,
                "core_field_count": p_core,
                "readiness_label": label,
                "readiness_color": color,
            })

    return {
        "page_label": page_label,
        "page_profiles": page_profiles,
        "active_vs_model": active_vs_model,
        "reuse_distribution": reuse_distribution,
        "consolidation_opportunities": consolidation,
        "conflict_coverage": conflict_coverage,
        "semantic_model_recommendation": semantic_model_recommendation,
        "dashboard_coverage": dash_cov,
        "scoped_readiness": scoped_readiness,
    }


# ---------------------------------------------------------------------------
# Business Question Generation (Eval Framework)
# ---------------------------------------------------------------------------

_QUESTION_TEMPLATES: list[dict[str, str]] = [
    # metric + dimension → aggregation question
    {
        "pattern": "metric_by_dim",
        "template": "What is the total {metric} by {dimension}?",
        "sql": "SELECT __{table}.{dimension}, SUM(__{table}.{metric}) AS total_{metric_safe} FROM __{table} GROUP BY __{table}.{dimension}",
    },
    # metric + time dimension → trend question
    {
        "pattern": "metric_over_time",
        "template": "How has {metric} changed over {time_dim}?",
        "sql": "SELECT __{table}.{time_dim}, SUM(__{table}.{metric}) AS total_{metric_safe} FROM __{table} GROUP BY __{table}.{time_dim} ORDER BY __{table}.{time_dim}",
    },
    # metric + dimension → ranking question
    {
        "pattern": "metric_ranking",
        "template": "Which {dimension} has the highest {metric}?",
        "sql": "SELECT __{table}.{dimension}, SUM(__{table}.{metric}) AS total_{metric_safe} FROM __{table} GROUP BY __{table}.{dimension} ORDER BY total_{metric_safe} DESC LIMIT 10",
    },
    # metric only → total question
    {
        "pattern": "metric_total",
        "template": "What is the overall {metric}?",
        "sql": "SELECT SUM(__{table}.{metric}) AS total_{metric_safe} FROM __{table}",
    },
]

# Time-related dimension keywords for detecting trend axes
_TIME_KEYWORDS = {"DATE", "MONTH", "QUARTER", "YEAR", "WEEK", "DAY", "PERIOD", "TIME"}


def generate_business_questions(
    page_profiles: list[dict],
    inventory: dict,
    *,
    connection: str | None = None,
    model: str = "claude-sonnet-4-6",
) -> list[dict]:
    """Generate business questions from page profiles for verified_queries.

    Primary path uses Cortex Complete (LLM) for natural, high-quality questions.
    Falls back to deterministic templates if Snowflake connection is unavailable.

    Args:
        page_profiles: List of page profile dicts from analyze_report_narrative().
        inventory: The unified inventory dict.
        connection: Optional Snowflake connection name for Cortex Complete.
        model: Cortex LLM model identifier (default: claude-sonnet-4-6).

    Returns:
        List of verified_query dicts with keys:
            name, question, sql, page_name, dashboard_name, tables,
            verified_at, verified_by
    """
    if not page_profiles:
        log.info("generate_business_questions: no page profiles — skipping.")
        return []

    # Build a lookup of logical column names from the inventory
    col_lookup = _build_column_lookup(inventory)

    # Try Cortex Complete first
    questions = _generate_questions_cortex(
        page_profiles, inventory, col_lookup,
        connection=connection, model=model,
    )

    if questions:
        log.info("generate_business_questions: Cortex Complete produced %d questions.", len(questions))
        return questions

    # Fallback to deterministic templates
    log.info("generate_business_questions: falling back to template-based generation.")
    questions = _generate_questions_templates(page_profiles, inventory, col_lookup)
    log.info("generate_business_questions: templates produced %d questions.", len(questions))
    return questions


def _build_column_lookup(inventory: dict) -> dict[str, dict]:
    """Build a lookup of table → {metrics: [...], dimensions: [...], facts: [...]}
    using the logical (semantic model) column names."""
    lookup: dict[str, dict[str, list[str]]] = defaultdict(
        lambda: {"metrics": [], "dimensions": [], "facts": [], "time_dims": []}
    )

    for dim in inventory.get("dimensions", []):
        table = dim.get("table", "")
        name = dim.get("name", "")
        if table and name:
            lookup[table]["dimensions"].append(name)
            # Detect time dimensions
            name_upper = name.upper()
            if any(tk in name_upper for tk in _TIME_KEYWORDS):
                lookup[table]["time_dims"].append(name)

    for fact in inventory.get("facts", []):
        table = fact.get("table", "")
        name = fact.get("name", "")
        if table and name:
            lookup[table]["facts"].append(name)

    for metric in inventory.get("metrics", []):
        table = metric.get("table", "")
        name = metric.get("name", "")
        if table and name:
            lookup[table]["metrics"].append(name)

    return dict(lookup)


def _generate_questions_cortex(
    page_profiles: list[dict],
    inventory: dict,
    col_lookup: dict,
    *,
    connection: str | None = None,
    model: str = "claude-sonnet-4-6",
) -> list[dict]:
    """Generate business questions using Snowflake Cortex Complete."""
    try:
        import snowflake.connector  # noqa: F401
    except ImportError:
        log.info("snowflake-connector-python not installed — skipping Cortex path.")
        return []

    try:
        conn = _get_snowflake_connection(connection)
    except Exception as e:
        log.warning("Could not connect to Snowflake for question generation: %s", e)
        return []

    from datetime import datetime
    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    all_questions: list[dict] = []
    cur = conn.cursor()

    # Process pages in batches to manage prompt size
    for pp in page_profiles:
        prompt = _build_question_prompt(pp, inventory, col_lookup)
        try:
            cur.execute(
                "SELECT SNOWFLAKE.CORTEX.COMPLETE(%s, %s) AS questions",
                (model, prompt),
            )
            row = cur.fetchone()
            if row and row[0]:
                parsed = _parse_cortex_questions(
                    row[0], pp, now_iso,
                )
                all_questions.extend(parsed)
        except Exception as e:
            log.warning("Cortex Complete failed for page '%s': %s", pp.get("name", "?"), e)
            continue

    cur.close()
    conn.close()
    return all_questions


def _build_question_prompt(
    page_profile: dict,
    inventory: dict,
    col_lookup: dict,
) -> str:
    """Build the LLM prompt for generating business questions from a page profile."""
    page_name = page_profile.get("name", "Unknown")
    dashboards = page_profile.get("dashboard_names", [])
    purpose = page_profile.get("purpose", "General data view")
    tables = page_profile.get("tables", [])
    metric_count = page_profile.get("metric_count", 0)
    fact_count = page_profile.get("fact_count", 0)

    # Collect the logical column names available on this page's tables
    available_metrics: list[str] = []
    available_dims: list[str] = []
    available_facts: list[str] = []
    available_time_dims: list[str] = []

    for table in tables:
        tl = col_lookup.get(table, {})
        available_metrics.extend(f"{table}.{m}" for m in tl.get("metrics", []))
        available_dims.extend(f"{table}.{d}" for d in tl.get("dimensions", []))
        available_facts.extend(f"{table}.{f}" for f in tl.get("facts", []))
        available_time_dims.extend(f"{table}.{t}" for t in tl.get("time_dims", []))

    lines = [
        "You are a business intelligence analyst generating verified queries for a "
        "Snowflake Semantic View. Given a dashboard page and its available semantic "
        "model columns, generate 2-4 natural business questions that this page answers.",
        "",
        "IMPORTANT RULES FOR SQL:",
        "- Use ONLY the logical column names listed below (not physical column names).",
        "- Reference tables with double-underscore prefix: __TABLE_NAME.COLUMN_NAME",
        "- SQL must be valid Snowflake SQL using these logical references.",
        "- Each question should be a natural language question a business user would ask.",
        "",
        f"Page: {page_name}",
        f"Dashboard(s): {', '.join(dashboards) if dashboards else 'N/A'}",
        f"Purpose: {purpose}",
        f"Tables: {', '.join(tables)}",
        "",
        "Available columns:",
    ]

    if available_metrics:
        lines.append(f"  Metrics: {', '.join(available_metrics[:20])}")
    if available_dims:
        lines.append(f"  Dimensions: {', '.join(available_dims[:20])}")
    if available_facts:
        lines.append(f"  Facts: {', '.join(available_facts[:20])}")
    if available_time_dims:
        lines.append(f"  Time dimensions: {', '.join(available_time_dims[:10])}")

    lines.extend([
        "",
        "Respond with a JSON array of objects, each with:",
        '  {"name": "short_snake_case_name", "question": "Natural language question?", "sql": "SELECT ..."}',
        "",
        "Generate 2-4 questions. Return ONLY the JSON array, no other text.",
    ])

    return "\n".join(lines)


def _parse_cortex_questions(
    raw_response: str,
    page_profile: dict,
    now_iso: str,
) -> list[dict]:
    """Parse the LLM response into verified_query dicts."""
    import json

    page_name = page_profile.get("name", "")
    dashboards = page_profile.get("dashboard_names", [])
    tables = page_profile.get("tables", [])
    dash_str = dashboards[0] if dashboards else ""

    # Try to extract JSON array from the response
    raw = raw_response.strip()

    # Handle markdown code fences
    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("["):
                raw = part
                break

    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        # Try to find a JSON array in the response
        start = raw.find("[")
        end = raw.rfind("]")
        if start >= 0 and end > start:
            try:
                items = json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                log.warning("Could not parse Cortex response as JSON for page '%s'.", page_name)
                return []
        else:
            log.warning("No JSON array found in Cortex response for page '%s'.", page_name)
            return []

    if not isinstance(items, list):
        return []

    questions = []
    for item in items[:4]:  # Cap at 4 per page
        if not isinstance(item, dict):
            continue
        name = item.get("name", "")
        question = item.get("question", "")
        sql = item.get("sql", "")
        if not (name and question and sql):
            continue

        questions.append({
            "name": _sanitize_question_name(name, page_name),
            "question": question,
            "sql": sql,
            "page_name": page_name,
            "dashboard_name": dash_str,
            "tables": tables,
            "verified_at": now_iso,
            "verified_by": "semantic-extraction-utility",
            "use_as_onboarding_question": len(questions) == 0,  # First question per page
        })

    return questions


def _sanitize_question_name(name: str, page_name: str) -> str:
    """Ensure a question name is a valid snake_case identifier."""
    # Strip non-alphanumeric, lowercase, collapse underscores
    clean = re.sub(r"[^a-zA-Z0-9_]", "_", name.lower()).strip("_")
    clean = re.sub(r"_+", "_", clean)
    if not clean:
        clean = re.sub(r"[^a-zA-Z0-9_]", "_", page_name.lower()).strip("_")
        clean = re.sub(r"_+", "_", clean) or "question"
    return clean[:60]


def _generate_questions_templates(
    page_profiles: list[dict],
    inventory: dict,
    col_lookup: dict,
) -> list[dict]:
    """Generate business questions using deterministic templates (fallback)."""
    from datetime import datetime
    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    all_questions: list[dict] = []
    q_counter = 0

    for pp in page_profiles:
        page_name = pp.get("name", "Unknown")
        dashboards = pp.get("dashboard_names", [])
        tables = pp.get("tables", [])
        dash_str = dashboards[0] if dashboards else ""

        page_questions: list[dict] = []

        for table in tables:
            tl = col_lookup.get(table, {})
            metrics = tl.get("metrics", [])
            dims = tl.get("dimensions", [])
            time_dims = tl.get("time_dims", [])

            if not metrics:
                continue

            for metric in metrics[:3]:  # Cap metrics per table
                metric_safe = re.sub(r"[^a-zA-Z0-9_]", "_", metric.lower())

                # Metric + dimension → aggregation
                if dims and len(page_questions) < 4:
                    dim = dims[0]
                    page_questions.append(_apply_template(
                        _QUESTION_TEMPLATES[0], metric, dim, table,
                        metric_safe, page_name, dash_str, tables, now_iso,
                    ))

                # Metric + time → trend
                if time_dims and len(page_questions) < 4:
                    tdim = time_dims[0]
                    page_questions.append(_apply_template(
                        _QUESTION_TEMPLATES[1], metric, tdim, table,
                        metric_safe, page_name, dash_str, tables, now_iso,
                        is_time=True,
                    ))

                # Metric + dimension → ranking
                if dims and len(page_questions) < 4:
                    dim = dims[0]
                    page_questions.append(_apply_template(
                        _QUESTION_TEMPLATES[2], metric, dim, table,
                        metric_safe, page_name, dash_str, tables, now_iso,
                    ))

                if len(page_questions) >= 4:
                    break

            if len(page_questions) >= 4:
                break

        # If no metrics found, generate a simple total question from facts
        if not page_questions:
            for table in tables:
                tl = col_lookup.get(table, {})
                facts = tl.get("facts", [])
                if facts:
                    fact = facts[0]
                    fact_safe = re.sub(r"[^a-zA-Z0-9_]", "_", fact.lower())
                    page_questions.append({
                        "name": _sanitize_question_name(f"{fact}_total", page_name),
                        "question": f"What is the overall {_humanize(fact)}?",
                        "sql": f"SELECT SUM(__{table}.{fact}) AS total_{fact_safe} FROM __{table}",
                        "page_name": page_name,
                        "dashboard_name": dash_str,
                        "tables": tables,
                        "verified_at": now_iso,
                        "verified_by": "semantic-extraction-utility",
                        "use_as_onboarding_question": q_counter == 0,
                    })
                    break

        # Mark first overall question as onboarding
        for i, pq in enumerate(page_questions):
            pq["use_as_onboarding_question"] = (q_counter + i) == 0

        q_counter += len(page_questions)
        all_questions.extend(page_questions)

    return all_questions


def _apply_template(
    template: dict,
    metric: str,
    dim: str,
    table: str,
    metric_safe: str,
    page_name: str,
    dash_str: str,
    tables: list[str],
    now_iso: str,
    *,
    is_time: bool = False,
) -> dict:
    """Apply a question template with the given field names."""
    dim_key = "time_dim" if is_time else "dimension"
    question = template["template"].format(
        metric=_humanize(metric), **{dim_key: _humanize(dim)},
    )
    sql = template["sql"].format(
        table=table, metric=metric, metric_safe=metric_safe,
        **{dim_key: dim},
    )
    name_stem = f"{metric}_{template['pattern']}"

    return {
        "name": _sanitize_question_name(name_stem, page_name),
        "question": question,
        "sql": sql,
        "page_name": page_name,
        "dashboard_name": dash_str,
        "tables": tables,
        "verified_at": now_iso,
        "verified_by": "semantic-extraction-utility",
        "use_as_onboarding_question": False,
    }


def _humanize(col_name: str) -> str:
    """Convert a COLUMN_NAME to 'column name' for natural language."""
    return col_name.replace("_", " ").strip().lower()
