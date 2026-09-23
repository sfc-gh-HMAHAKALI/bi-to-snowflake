"""Resolve LookML references: ${TABLE}, field refs, extends inheritance, Liquid.

All functions are stateless transforms over parsed LookML structures.
"""

import re
from collections import deque
from typing import Any

from ..common.errors import ParseError, fail_step
from ..common.logger import get_logger

log = get_logger("looker.resolver")

# Matches ${view_name.field_name} — captures view and field separately
_FIELD_REF_RE = re.compile(r"\$\{(\w+)\.(\w+)\}")
# Matches ${TABLE}
_TABLE_REF_RE = re.compile(r"\$\{TABLE\}", re.IGNORECASE)
# Matches Liquid template tags
_LIQUID_RE = re.compile(r"\{%|\{\{")


def resolve_table_ref(sql_expr: str, sql_table_name: str) -> str:
    """Replace all ${TABLE} occurrences with the actual table name.

    Args:
        sql_expr: SQL expression that may contain ${TABLE}.
        sql_table_name: The concrete table name to substitute.

    Returns:
        SQL expression with ${TABLE} replaced.
    """
    log.debug(
        "resolve_table_ref: entering — table=%s, expr_len=%d",
        sql_table_name, len(sql_expr),
    )
    if not sql_expr:
        return sql_expr
    result = _TABLE_REF_RE.sub(sql_table_name, sql_expr)
    log.debug("resolve_table_ref: done — substitutions made=%s", result != sql_expr)
    return result


def resolve_field_ref(sql_expr: str, views_dict: dict[str, dict]) -> str:
    """Resolve ${view_name.field_name} references to TABLE.COLUMN.

    Looks up each view in views_dict (keyed by view name), finds the field,
    and replaces the reference with <sql_table_name>.<field_sql_column>.
    Unresolvable references are left in-place and logged as warnings.

    Args:
        sql_expr: SQL expression containing ${view.field} refs.
        views_dict: Dict of view_name -> parsed view dict (from parse_view()).

    Returns:
        SQL expression with resolved field references.
    """
    log.debug("resolve_field_ref: entering — expr_len=%d", len(sql_expr) if sql_expr else 0)

    if not sql_expr:
        return sql_expr

    def _replace(match: re.Match) -> str:
        view_name = match.group(1)
        field_name = match.group(2)

        view = views_dict.get(view_name)
        if view is None:
            log.warning(
                "resolve_field_ref: view '%s' not found — leaving ref in place",
                view_name,
            )
            return match.group(0)

        # Search dimensions, measures, dimension_groups
        field = _find_field(view, field_name)
        if field is None:
            log.warning(
                "resolve_field_ref: field '%s.%s' not found in view — leaving in place",
                view_name, field_name,
            )
            return match.group(0)

        # Use the field's own sql, or fall back to table.column_name
        field_sql = field.get("sql", "")
        table = view.get("sql_table_name", view_name)

        if field_sql:
            # Recursively resolve ${TABLE} within the field sql
            resolved = resolve_table_ref(field_sql, table)
            # Strip outer ${...} if it's a simple column ref
            if resolved.startswith("${") and resolved.endswith("}"):
                resolved = resolved[2:-1]
            return resolved

        return f"{table}.{field_name}"

    result = _FIELD_REF_RE.sub(_replace, sql_expr)
    log.debug("resolve_field_ref: done")
    return result


def _find_field(view: dict, field_name: str) -> dict | None:
    """Find a field by name across dimensions, measures, and dimension_groups."""
    for bucket in ("dimensions", "measures", "parameters"):
        for f in view.get(bucket, []):
            if f.get("name") == field_name:
                return f
    # dimension_groups generate sub-fields like group_name_timeframe
    for dg in view.get("dimension_groups", []):
        dg_name = dg.get("name", "")
        for tf in dg.get("timeframes", []):
            if f"{dg_name}_{tf}" == field_name:
                return dg
        if dg_name == field_name:
            return dg
    return None


def resolve_refinements(views: list[dict]) -> list[dict]:
    """Merge LookML refinement views (+view_name) into their base views.

    Refinements override/extend the base view's fields, labels, and SQL.
    After merging, the refinement entry is dropped from the list and the
    base view contains the merged result.

    Must be called *before* resolve_extends() so that refinements are
    applied before inheritance chains are evaluated.

    Args:
        views: List of normalized view dicts (from parse_view()).

    Returns:
        New list of views with refinements merged into base views.
    """
    log.info("resolve_refinements: entering — views=%d", len(views))

    base_views: dict[str, dict] = {}
    refinements: list[dict] = []

    for v in views:
        name = v.get("name", "")
        if name.startswith("+"):
            refinements.append(v)
        else:
            # Last base view wins if duplicates exist (same as LookML behavior)
            base_views[name] = v

    if not refinements:
        log.info("resolve_refinements: no refinements found, returning unchanged")
        return views

    merged_count = 0
    unmatched = []

    for ref in refinements:
        bare_name = ref["name"].lstrip("+")
        base = base_views.get(bare_name)
        if base is None:
            log.warning(
                "resolve_refinements: no base view '%s' for refinement '%s' — "
                "stripping '+' and keeping as standalone view",
                bare_name, ref["name"],
            )
            stripped = dict(ref)
            stripped["name"] = bare_name
            unmatched.append(stripped)
            continue

        # Refinement fields override base fields (child overrides parent)
        base_views[bare_name] = _merge_views(base, {**ref, "name": bare_name})
        merged_count += 1
        log.debug("resolve_refinements: merged '%s' into base '%s'", ref["name"], bare_name)

    # Rebuild list preserving original order of base views, appending unmatched
    result = [base_views[v["name"]] for v in views
              if not v["name"].startswith("+") and v["name"] in base_views]
    result.extend(unmatched)

    log.info(
        "resolve_refinements: done — merged=%d, unmatched=%d, output views=%d",
        merged_count, len(unmatched), len(result),
    )
    return result


def resolve_extends(views: list[dict]) -> list[dict]:
    """Process view inheritance declared via extends:.

    Performs a topological sort so base views are merged before child views.
    Child fields override parent fields with the same name.
    Cycles are detected and logged; views in a cycle are left unmodified.

    Args:
        views: List of normalized view dicts (from parse_view()).

    Returns:
        New list of views with inherited fields merged in.
    """
    log.info("resolve_extends: entering — views=%d", len(views))

    by_name: dict[str, dict] = {v["name"]: v for v in views}

    # Build dependency graph: name -> list of names it extends
    deps: dict[str, list[str]] = {}
    for v in views:
        ext = v.get("extends", [])
        # lkml sometimes returns nested lists: [["base"]] instead of ["base"]
        flat = []
        for e in ext:
            if isinstance(e, list):
                flat.extend(e)
            else:
                flat.append(e)
        deps[v["name"]] = [e for e in flat if e in by_name]

    # Topological sort (Kahn's algorithm)
    try:
        order = _topo_sort(deps)
    except Exception as exc:
        fail_step("resolve_extends:topo_sort", exc)
        log.warning("resolve_extends: topo sort failed, returning views unmodified")
        return views

    resolved: dict[str, dict] = {}

    for name in order:
        view = by_name.get(name)
        if view is None:
            continue

        parent_names = deps.get(name, [])
        if not parent_names:
            resolved[name] = view
            continue

        merged = _deep_copy_view(view)
        for parent_name in parent_names:
            parent = resolved.get(parent_name) or by_name.get(parent_name)
            if parent is None:
                log.warning(
                    "resolve_extends: parent '%s' for view '%s' not found — skipping",
                    parent_name, name,
                )
                continue
            merged = _merge_views(parent, merged)

        resolved[name] = merged
        log.debug("resolve_extends: merged '%s' from parents %s", name, parent_names)

    # Views not in the topo order (e.g. cycle members) are returned as-is
    result = []
    for v in views:
        result.append(resolved.get(v["name"], v))

    log.info("resolve_extends: done — resolved=%d views", len(result))
    return result


def _topo_sort(deps: dict[str, list[str]]) -> list[str]:
    """Kahn's topological sort. Raises ValueError on cycle detection."""
    in_degree = {n: len(parents) for n, parents in deps.items()}
    queue = deque(n for n, d in in_degree.items() if d == 0)
    order = []

    # Reverse adjacency: parent -> children
    children: dict[str, list[str]] = {n: [] for n in deps}
    for n, parents in deps.items():
        for p in parents:
            if p in children:
                children[p].append(n)

    while queue:
        node = queue.popleft()
        order.append(node)
        for child in children.get(node, []):
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    if len(order) != len(deps):
        cycle_nodes = [n for n in deps if n not in order]
        log.warning("resolve_extends: cycle detected among views: %s", cycle_nodes)
        # Append cycle members at the end so they're still processed
        order.extend(cycle_nodes)

    return order


def _merge_views(parent: dict, child: dict) -> dict:
    """Merge parent fields into child. Child fields take precedence."""
    merged = _deep_copy_view(parent)
    merged["name"] = child["name"]
    merged["_source_file"] = child.get("_source_file")

    # Override scalar metadata from child
    for key in ("label", "description", "sql_table_name", "derived_table"):
        if child.get(key) is not None:
            merged[key] = child[key]

    # Merge field lists: child overrides parent on same name
    for bucket in ("dimensions", "dimension_groups", "measures", "parameters", "sets"):
        parent_fields = {f["name"]: f for f in merged.get(bucket, [])}
        for f in child.get(bucket, []):
            parent_fields[f["name"]] = f
        merged[bucket] = list(parent_fields.values())

    return merged


def _deep_copy_view(view: dict) -> dict:
    """Shallow-copy a view dict with independent field lists."""
    copy = dict(view)
    for bucket in ("dimensions", "dimension_groups", "measures", "parameters", "sets"):
        copy[bucket] = list(view.get(bucket, []))
    return copy


def flag_liquid_templates(sql: str) -> bool:
    """Return True if the SQL expression contains Liquid template tags.

    Liquid tags ({%, {{) cannot be statically analyzed or auto-converted.

    Args:
        sql: SQL expression to check.

    Returns:
        True if Liquid syntax is detected, False otherwise.
    """
    if not sql:
        return False
    found = bool(_LIQUID_RE.search(sql))
    if found:
        log.debug("flag_liquid_templates: Liquid detected in expr (len=%d)", len(sql))
    return found


def resolve_duration_dimensions(dim_group: dict) -> list[dict]:
    """Generate one synthetic dimension per interval for a duration-type group.

    Uses SQL DATEDIFF(<interval>, sql_start, sql_end) semantics.

    Args:
        dim_group: A normalized dimension_group dict with type='duration'.

    Returns:
        List of dimension dicts, one per interval, ready to emit as measures/dims.
    """
    name = dim_group.get("name", "")
    log.debug("resolve_duration_dimensions: entering — name=%s", name)

    sql_start = dim_group.get("sql_start", "")
    sql_end = dim_group.get("sql_end", "")
    intervals = dim_group.get("intervals") or [
        "second", "minute", "hour", "day", "week", "month", "quarter", "year"
    ]

    results = []
    for interval in intervals:
        dim_name = f"{name}_{interval}s"
        sql = f"DATEDIFF({interval}, {sql_start}, {sql_end})"
        results.append({
            "name": dim_name,
            "type": "number",
            "sql": sql,
            "label": f"{dim_group.get('label', name).rstrip()} {interval.capitalize()}s",
            "description": dim_group.get("description"),
            "hidden": dim_group.get("hidden", False),
            "_from_duration_group": name,
        })

    log.debug(
        "resolve_duration_dimensions: done — name=%s, generated=%d",
        name, len(results),
    )
    return results
