"""Snowflake Semantic View YAML generator.

Takes a unified inventory and produces one or more YAML files
conforming to the Semantic View spec.
"""

import os
import re
from typing import Any

from ..common.logger import get_logger
from ..common.errors import ValidationError, fail_step

log = get_logger("yaml_generator")

# Max recommended columns per semantic view for Cortex Analyst
MAX_COLUMNS_PER_VIEW = 100


def generate_semantic_view_yaml(
    inventory: dict,
    view_name: str | None = None,
    include_sample_values: bool = True,
    include_flagged: bool = False,
    verified_queries: list[dict] | None = None,
) -> str:
    """Generate a Semantic View YAML string from a unified inventory.

    Args:
        inventory: Unified inventory dict from inventory.py.
        view_name: Name for the semantic view. Auto-generated if None.
        include_sample_values: Add sample_values placeholders.
        include_flagged: Include manual_required items as commented-out entries.

    Returns:
        YAML string ready for deployment.
    """
    log.info("Generating Semantic View YAML for '%s'.", view_name or "auto")

    sf = inventory.get("snowflake_target", {})
    db = sf.get("database", "TARGET_DB")
    schema = sf.get("schema", "PUBLIC")

    if view_name is None:
        source = inventory.get("source_type", "EXTRACTED")
        view_name = f"{source.upper()}_SEMANTIC_VIEW"

    view_name = _sanitize_name(view_name)

    lines: list[str] = []
    lines.append(f"name: {view_name}")
    lines.append("")

    # --- Tables ---
    tables = inventory.get("tables", [])
    if tables:
        lines.append("tables:")
        for table in tables:
            tname = _sanitize_name(table.get("name", "UNKNOWN"))
            physical = table.get("physical_table", tname)
            pk = _find_primary_key(tname, inventory)

            lines.append(f"  - name: {tname}")
            lines.append(f"    base_table:")
            lines.append(f"      database: {db}")
            lines.append(f"      schema: {schema}")
            lines.append(f"      table: {_sanitize_name(physical)}")
            if pk:
                lines.append(f"    primary_key: {pk}")
        lines.append("")

    # --- Relationships ---
    rels = inventory.get("relationships", [])
    active_rels = [r for r in rels if r.get("is_active", True)]
    if active_rels:
        lines.append("relationships:")
        for i, rel in enumerate(active_rels):
            left = _sanitize_name(rel.get("left_table", ""))
            right = _sanitize_name(rel.get("right_table", ""))
            if not left or not right:
                continue

            rel_name = f"{left.lower()}_to_{right.lower()}"
            left_col = rel.get("left_column", "")
            right_col = rel.get("right_column", "")
            rel_type = _map_relationship_type(rel.get("relationship_type", "many_to_one"))
            join_type = _map_join_type(rel.get("join_type", "left_outer"))

            lines.append(f"  - name: {rel_name}")
            lines.append(f"    left_table: {left}")
            lines.append(f"    right_table: {right}")
            if left_col and right_col:
                lines.append(f"    relationship_columns:")
                lines.append(f"      - left_column: {left_col}")
                lines.append(f"        right_column: {right_col}")
            else:
                lines.append(f"    # TODO: Specify relationship_columns from join condition:")
                lines.append(f"    # {rel.get('condition', 'unknown')}")
                lines.append(f"    relationship_columns:")
                lines.append(f"      - left_column: TODO")
                lines.append(f"        right_column: TODO")
            lines.append(f"    relationship_type: {rel_type}")
            lines.append(f"    join_type: {join_type}")
        lines.append("")

    # --- Dimensions ---
    dims = inventory.get("dimensions", [])
    eligible_dims = [
        d for d in dims
        if (d.get("complexity") != "manual_required" or include_flagged)
        and _sanitize_name(d.get("name", "")) not in ("", "UNKNOWN")  # skip unnamed proxy fields
    ]
    if eligible_dims:
        lines.append("dimensions:")
        seen_dim_names: set[str] = set()
        for dim in eligible_dims:
            is_manual = dim.get("complexity") == "manual_required"
            prefix = "  # " if is_manual else "  "

            name = _sanitize_name(dim.get("name", "UNKNOWN"))
            if name in seen_dim_names:
                continue  # deduplicate fields with identical sanitized names
            seen_dim_names.add(name)
            expr = dim.get("expr", "")
            dt = dim.get("data_type", "VARCHAR")
            desc = dim.get("description", "")
            syns = dim.get("synonyms", [])

            lines.append(f"{prefix}- name: {name}")
            if syns:
                syn_str = ", ".join(f'"{s}"' for s in syns[:5])
                lines.append(f"{prefix}  synonyms: [{syn_str}]")
            lines.append(f"{prefix}  expr: {_format_expr(expr, dim.get('table', ''), name)}")
            lines.append(f"{prefix}  data_type: {dt}")
            if desc:
                lines.append(f"{prefix}  description: \"{_escape_yaml_string(desc)}\"")
            if include_sample_values and dt == "VARCHAR":
                lines.append(f"{prefix}  sample_values:")
                lines.append(f"{prefix}    - \"TODO\"")
            if is_manual:
                lines.append(f"{prefix}  # MANUAL REVIEW: complexity={dim.get('complexity')}")
        lines.append("")

    # --- Facts ---
    facts = inventory.get("facts", [])
    eligible_facts = [
        f for f in facts
        if (f.get("complexity") != "manual_required" or include_flagged)
        and _sanitize_name(f.get("name", "")) not in ("", "UNKNOWN")  # skip unnamed proxy fields
    ]
    if eligible_facts:
        lines.append("facts:")
        seen_fact_names: set[str] = set()
        for fact in eligible_facts:
            is_manual = fact.get("complexity") == "manual_required"
            prefix = "  # " if is_manual else "  "

            name = _sanitize_name(fact.get("name", "UNKNOWN"))
            if name in seen_fact_names:
                continue  # deduplicate fields with identical sanitized names
            seen_fact_names.add(name)
            expr = fact.get("expr", "")
            dt = fact.get("data_type", "NUMBER")
            desc = fact.get("description", "")
            syns = fact.get("synonyms", [])

            lines.append(f"{prefix}- name: {name}")
            if syns:
                syn_str = ", ".join(f'"{s}"' for s in syns[:5])
                lines.append(f"{prefix}  synonyms: [{syn_str}]")
            lines.append(f"{prefix}  expr: {_format_expr(expr, fact.get('table', ''), name)}")
            lines.append(f"{prefix}  data_type: {dt}")
            if desc:
                lines.append(f"{prefix}  description: \"{_escape_yaml_string(desc)}\"")
        lines.append("")

    # --- Metrics ---
    metrics = inventory.get("metrics", [])
    eligible_metrics = [m for m in metrics if m.get("complexity") != "manual_required" or include_flagged]
    if eligible_metrics:
        lines.append("metrics:")
        for metric in eligible_metrics:
            is_manual = metric.get("complexity") == "manual_required"
            prefix = "  # " if is_manual else "  "

            name = _sanitize_name(metric.get("name", "UNKNOWN"))
            expr = metric.get("expr", "")
            dt = metric.get("data_type", "NUMBER")
            desc = metric.get("description", "")
            syns = metric.get("synonyms", [])

            lines.append(f"{prefix}- name: {name}")
            if syns:
                syn_str = ", ".join(f'"{s}"' for s in syns[:5])
                lines.append(f"{prefix}  synonyms: [{syn_str}]")
            lines.append(f"{prefix}  expr: {_format_expr(expr, metric.get('table', ''), name)}")
            lines.append(f"{prefix}  data_type: {dt}")
            if desc:
                lines.append(f"{prefix}  description: \"{_escape_yaml_string(desc)}\"")
            if is_manual:
                lines.append(f"{prefix}  # MANUAL REVIEW: original expression needs translation")
        lines.append("")

    # --- Filters ---
    filters = inventory.get("filters", [])
    eligible_filters = [f for f in filters if f.get("complexity") != "manual_required"]
    if eligible_filters:
        lines.append("filters:")
        for filt in eligible_filters:
            name = _sanitize_name(filt.get("name", "UNKNOWN"))
            expr = filt.get("expr", "")
            desc = filt.get("description", "")
            syns = filt.get("synonyms", [])

            lines.append(f"  - name: {name}")
            if syns:
                syn_str = ", ".join(f'"{s}"' for s in syns[:5])
                lines.append(f"    synonyms: [{syn_str}]")
            lines.append(f"    expr: \"{_escape_yaml_string(expr)}\"")
            if desc:
                lines.append(f"    description: \"{_escape_yaml_string(desc)}\"")
        lines.append("")

    # --- Verified Queries ---
    if verified_queries:
        lines.append("verified_queries:")
        for vq in verified_queries:
            vq_name = vq.get("name", "question")
            vq_question = vq.get("question", "")
            vq_sql = vq.get("sql", "")
            vq_verified_at = vq.get("verified_at", "")
            vq_verified_by = vq.get("verified_by", "semantic-extraction-utility")
            vq_onboarding = vq.get("use_as_onboarding_question", False)

            lines.append(f"  - name: {vq_name}")
            lines.append(f"    question: \"{_escape_yaml_string(vq_question)}\"")
            # SQL may be multi-line; indent continuation lines
            sql_lines = vq_sql.strip().split("\n")
            if len(sql_lines) == 1:
                lines.append(f"    sql: \"{_escape_yaml_string(vq_sql)}\"")
            else:
                lines.append("    sql: |")
                for sl in sql_lines:
                    lines.append(f"      {sl}")
            if vq_verified_at:
                lines.append(f"    verified_at: \"{vq_verified_at}\"")
            lines.append(f"    verified_by: \"{_escape_yaml_string(vq_verified_by)}\"")
            if vq_onboarding:
                lines.append("    use_as_onboarding_question: true")
        lines.append("")

    yaml_str = "\n".join(lines)

    # Warn if too many columns
    total_cols = len(eligible_dims) + len(eligible_facts) + len(eligible_metrics)
    if total_cols > MAX_COLUMNS_PER_VIEW:
        log.warning(
            "Semantic view '%s' has %d columns (recommended max: %d). "
            "Consider splitting into domain-specific views.",
            view_name, total_cols, MAX_COLUMNS_PER_VIEW,
        )

    log.info(
        "YAML generated: %d tables, %d dimensions, %d facts, %d metrics, %d filters.",
        len(tables), len(eligible_dims), len(eligible_facts),
        len(eligible_metrics), len(eligible_filters),
    )

    return yaml_str


def generate_all_yamls(
    inventory: dict,
    output_dir: str,
    split_threshold: int = MAX_COLUMNS_PER_VIEW,
    verified_queries: list[dict] | None = None,
) -> list[str]:
    """Generate YAML files, splitting if inventory exceeds threshold.

    Args:
        inventory: Unified inventory.
        output_dir: Directory to write YAML files.
        split_threshold: Max columns per view before splitting.

    Returns:
        List of written file paths.
    """
    log.info("Generating YAML files to %s", output_dir)
    os.makedirs(output_dir, exist_ok=True)

    total = (
        len(inventory.get("dimensions", []))
        + len(inventory.get("facts", []))
        + len(inventory.get("metrics", []))
    )

    if total <= split_threshold:
        yaml_str = generate_semantic_view_yaml(inventory, verified_queries=verified_queries)
        name = inventory.get("source_type", "extracted").upper()
        path = os.path.join(output_dir, f"{name}_SEMANTIC_VIEW.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(yaml_str)
        log.info("Wrote single YAML: %s", path)
        return [path]

    # Split by table — each table gets its own semantic view
    log.info(
        "Total columns (%d) exceeds threshold (%d). Splitting by table.",
        total, split_threshold,
    )
    paths = []
    for table in inventory.get("tables", []):
        tname = table.get("name", "UNKNOWN")
        sub_inv = _filter_inventory_by_table(inventory, tname)
        sub_cols = (
            len(sub_inv.get("dimensions", []))
            + len(sub_inv.get("facts", []))
            + len(sub_inv.get("metrics", []))
        )
        if sub_cols == 0:
            continue

        yaml_str = generate_semantic_view_yaml(
            sub_inv, view_name=f"{tname}_SEMANTIC_VIEW",
            verified_queries=[vq for vq in (verified_queries or []) if tname in vq.get("tables", [])],
        )
        path = os.path.join(output_dir, f"{tname}_SEMANTIC_VIEW.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(yaml_str)
        paths.append(path)

    log.info("Wrote %d YAML files.", len(paths))
    return paths


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize_name(name: str) -> str:
    """Convert a name to a valid Snowflake identifier."""
    if not name:
        return "UNKNOWN"
    # Strip common wrappers
    name = name.strip("[]`\"' ")
    # Replace non-alphanumeric with underscore
    name = re.sub(r'[^A-Za-z0-9_]', '_', name)
    # Collapse multiple underscores
    name = re.sub(r'_+', '_', name)
    # Strip leading/trailing underscores
    name = name.strip('_')
    return name.upper() or "UNKNOWN"


def _escape_yaml_string(s: str) -> str:
    """Escape special characters for YAML double-quoted strings."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_expr(expr: str, table: str, name: str = "") -> str:
    """Format an expression, ensuring table prefix if simple column ref.

    When ``expr`` is empty (e.g. Tableau sqlproxy / Published Data Sources
    where physical column references are not embedded in the .twb), fall back
    to the sanitized field ``name`` as the column reference rather than the
    generic placeholder ``UNKNOWN``.  This produces a usable YAML that maps
    naturally when Snowflake column names match the Tableau field captions.
    """
    if not expr:
        if name:
            col = _sanitize_name(name)
            return f"{table}.{col}" if table else col
        return f"{table}.UNKNOWN" if table else "UNKNOWN"
    # If it's just a column name, prefix with table
    if re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', expr.strip()) and table:
        return f"{table}.{expr.strip().upper()}"
    return expr


def _find_primary_key(table_name: str, inventory: dict) -> str | None:
    """Find the primary key for a table from dimensions."""
    for dim in inventory.get("dimensions", []):
        if dim.get("table", "").upper() == table_name.upper():
            if dim.get("primary_key"):
                return _sanitize_name(dim.get("name", ""))
            # Heuristic: column ending in _ID with table name prefix
            name = dim.get("name", "").upper()
            if name.endswith("_ID") and table_name.upper().rstrip("S") in name:
                return _sanitize_name(name)
    return None


def _map_relationship_type(rel_type: str) -> str:
    """Map source relationship type to Snowflake semantic view type."""
    mapping = {
        "many_to_one": "many_to_one",
        "many-to-one": "many_to_one",
        "one_to_many": "one_to_many",
        "one-to-many": "one_to_many",
        "one_to_one": "one_to_one",
        "one-to-one": "one_to_one",
        "many_to_many": "many_to_many",
        "many-to-many": "many_to_many",
    }
    return mapping.get(rel_type.lower(), "many_to_one")


def _map_join_type(join_type: str) -> str:
    """Map source join type to Snowflake semantic view join type."""
    jt = join_type.lower().replace("_", " ").replace("-", " ")
    if "inner" in jt:
        return "inner"
    if "full" in jt:
        return "full_outer"
    return "left_outer"


def _filter_inventory_by_table(inventory: dict, table_name: str) -> dict:
    """Create a sub-inventory containing only items for a specific table."""
    tname_upper = table_name.upper()
    return {
        **inventory,
        "tables": [t for t in inventory.get("tables", [])
                    if t.get("name", "").upper() == tname_upper],
        "relationships": [r for r in inventory.get("relationships", [])
                          if r.get("left_table", "").upper() == tname_upper
                          or r.get("right_table", "").upper() == tname_upper],
        "dimensions": [d for d in inventory.get("dimensions", [])
                       if d.get("table", "").upper() == tname_upper],
        "facts": [f for f in inventory.get("facts", [])
                  if f.get("table", "").upper() == tname_upper],
        "metrics": [m for m in inventory.get("metrics", [])
                    if m.get("table", "").upper() == tname_upper],
        "filters": [f for f in inventory.get("filters", [])
                    if f.get("table", "").upper() == tname_upper],
        "flagged": [f for f in inventory.get("flagged", [])
                    if f.get("source", "").upper() == tname_upper
                    or f.get("item", "").upper().startswith(tname_upper)],
    }
