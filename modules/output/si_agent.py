"""Snowflake Intelligence agent artifact generator.

Takes a unified inventory and produces deployment-ready artifacts for
Snowflake Intelligence:
  1. Domain-grouped Semantic View DDL
  2. Cortex Agent specification JSON
  3. Deployment SQL script (semantic views + agent + grants)

Architecture follows SI best practices:
  - One agent per BI dashboard/source
  - Domain-grouped semantic views (not per-page or per-table)
  - 5-10 tools per agent (cortex_analyst_text_to_sql)
  - Rich tool descriptions auto-generated from inventory metadata
"""

import json
import re
from collections import defaultdict
from typing import Any

from ..common.logger import get_logger

log = get_logger("si_agent")

# Max columns per semantic view for optimal Cortex Analyst performance
_MAX_COLS_PER_VIEW = 100


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def assess_for_si(inventory: dict) -> dict:
    """Assess inventory readiness for SI agent generation.

    Returns domain grouping, column counts, and feasibility info.
    """
    domains = group_tables_by_domain(inventory)
    table_columns = _build_table_columns(inventory)

    domain_details = []
    for domain_name, tables in sorted(domains.items()):
        col_count = sum(len(table_columns.get(t, {})) for t in tables)
        metric_count = sum(
            1 for m in inventory.get("metrics", [])
            if m.get("table", "").upper() in tables
        )
        dim_count = sum(
            1 for d in inventory.get("dimensions", [])
            if d.get("table", "").upper() in tables
        )
        fact_count = sum(
            1 for f in inventory.get("facts", [])
            if f.get("table", "").upper() in tables
        )
        domain_details.append({
            "domain": domain_name,
            "tables": sorted(tables),
            "table_count": len(tables),
            "columns": col_count,
            "dimensions": dim_count,
            "facts": fact_count,
            "metrics": metric_count,
            "exceeds_limit": col_count > _MAX_COLS_PER_VIEW,
        })

    total_cols = sum(d["columns"] for d in domain_details)
    return {
        "source_type": inventory.get("source_type", "unknown"),
        "total_tables": len(table_columns),
        "total_columns": total_cols,
        "domain_count": len(domains),
        "domains": domain_details,
        "feasible": len(domains) <= 10,
        "warnings": _assess_warnings(domain_details),
    }


def group_tables_by_domain(
    inventory: dict,
    explicit_domains: dict[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    """Group inventory tables into logical domains.

    Grouping strategy (in priority order):
    1. Explicit override via ``explicit_domains``
    2. Page-based clustering (tables that co-occur on pages)
    3. Table name prefix heuristic
    4. Fallback: single domain with all tables

    Returns:
        Dict mapping domain name -> list of table names (uppercased).
    """
    all_tables = {t.get("name", "").upper() for t in inventory.get("tables", [])}
    all_tables.discard("")

    if not all_tables:
        return {}

    # --- 1. Explicit override ---
    if explicit_domains:
        domains: dict[str, list[str]] = {}
        for dname, tbls in explicit_domains.items():
            domains[dname] = [t.upper() for t in tbls if t.upper() in all_tables]
        # Capture any unassigned tables
        assigned = {t for tbls in domains.values() for t in tbls}
        unassigned = all_tables - assigned
        if unassigned:
            domains["Other"] = sorted(unassigned)
        return {k: v for k, v in domains.items() if v}

    # --- 2. Page-based clustering ---
    domains: dict[str, list[str]] | None = None
    page_tables = _page_table_clusters(inventory)
    if page_tables and len(page_tables) > 1:
        merged = _merge_overlapping_clusters(page_tables, all_tables)
        if 1 < len(merged) <= 10:
            domains = merged

    # --- 3. Table name prefix heuristic ---
    if domains is None:
        prefix_domains = _prefix_based_grouping(all_tables)
        if 1 < len(prefix_domains) <= 10:
            domains = prefix_domains

    # --- 4. Fallback: single domain ---
    if domains is None:
        domain_name = _infer_domain_name(inventory)
        domains = {domain_name: sorted(all_tables)}

    # --- 5. Cross-domain bridge detection ---
    # Runs after any of the above strategies.  Detects FK paths between
    # separate domains and appends combined "A × B" views as additional
    # tools so the agent can answer cross-cutting questions.
    if len(domains) > 1:
        bridges = _detect_cross_domain_bridges(domains, inventory)
        if bridges:
            cross = _build_cross_domain_views(bridges, domains, inventory)
            domains.update(cross)

    return domains


def generate_agent_spec(
    inventory: dict,
    *,
    agent_name: str = "BI_ANALYTICS_AGENT",
    database: str = "TARGET_DB",
    schema: str = "PUBLIC",
    domains: dict[str, list[str]] | None = None,
) -> dict:
    """Generate a Cortex Agent specification JSON.

    Args:
        inventory: Unified inventory dict.
        agent_name: Name for the agent.
        database: Target Snowflake database.
        schema: Target Snowflake schema.
        domains: Explicit domain→tables mapping. Auto-grouped if None.

    Returns:
        Agent specification dict ready for ``CREATE AGENT FROM SPECIFICATION``.
    """
    if domains is None:
        domains = group_tables_by_domain(inventory)

    sf = inventory.get("snowflake_target", {})
    db = sf.get("database", database)
    sch = sf.get("schema", schema)
    source_type = inventory.get("source_type", "BI tool")

    tools = []
    tool_resources = {}

    for domain_name, tables in sorted(domains.items()):
        tool_name = _domain_to_tool_name(domain_name)
        view_name = _domain_to_view_name(domain_name)
        fqn = f"{db}.{sch}.{view_name}"

        description = _generate_tool_description(domain_name, tables, inventory)

        tools.append({
            "tool_spec": {
                "type": "cortex_analyst_text_to_sql",
                "name": tool_name,
                "description": description,
            }
        })
        tool_resources[tool_name] = {
            "execution_environment": {
                "query_timeout": 299,
                "type": "warehouse",
                "warehouse": "",
            },
            "semantic_view": fqn,
        }

    # Build orchestration instructions
    orchestration = _generate_orchestration_instructions(
        agent_name, source_type, domains, inventory
    )
    response = _generate_response_instructions(source_type)

    spec = {
        "models": {"orchestration": "auto"},
        "instructions": {
            "orchestration": orchestration,
            "response": response,
        },
        "tools": tools,
        "tool_resources": tool_resources,
    }

    return spec


def generate_deployment_sql(
    inventory: dict,
    *,
    agent_name: str = "BI_ANALYTICS_AGENT",
    database: str = "TARGET_DB",
    schema: str = "PUBLIC",
    domains: dict[str, list[str]] | None = None,
    role: str | None = None,
) -> list[str]:
    """Generate the full deployment SQL script.

    Returns a list of SQL statement strings.
    """
    if domains is None:
        domains = group_tables_by_domain(inventory)

    sf = inventory.get("snowflake_target", {})
    db = sf.get("database", database)
    sch = sf.get("schema", schema)
    source_type = inventory.get("source_type", "BI tool")

    statements: list[str] = []

    # Header
    statements.append(
        f"-- Snowflake Intelligence deployment script\n"
        f"-- Source: {source_type} inventory\n"
        f"-- Agent: {db}.{sch}.{agent_name}\n"
        f"-- Domains: {len(domains)}\n"
        f"-- Generated by semantic-extraction utility\n"
    )

    # Role setup (commented out for safety)
    role_name = role or f"SI_{agent_name}_ROLE"
    statements.append(
        f"-- === Role & Privilege Setup (uncomment and customize) ===\n"
        f"-- CREATE ROLE IF NOT EXISTS {role_name};\n"
        f"-- GRANT USAGE ON DATABASE {db} TO ROLE {role_name};\n"
        f"-- GRANT USAGE ON SCHEMA {db}.{sch} TO ROLE {role_name};\n"
        f"-- GRANT CREATE SEMANTIC VIEW ON SCHEMA {db}.{sch} TO ROLE {role_name};\n"
        f"-- GRANT CREATE AGENT ON SCHEMA {db}.{sch} TO ROLE {role_name};\n"
        f"-- GRANT SELECT ON FUTURE TABLES IN SCHEMA {db}.{sch} TO ROLE {role_name};\n"
        f"-- GRANT SELECT ON FUTURE SEMANTIC VIEWS IN SCHEMA {db}.{sch} TO ROLE {role_name};\n"
    )

    # Semantic View DDL for each domain
    statements.append("-- === Semantic View Definitions ===\n")
    for domain_name, tables in sorted(domains.items()):
        view_name = _domain_to_view_name(domain_name)
        ddl = _generate_semantic_view_ddl(
            domain_name, tables, inventory, db, sch, view_name
        )
        statements.append(ddl)

    # Agent creation
    spec = generate_agent_spec(
        inventory,
        agent_name=agent_name,
        database=database,
        schema=schema,
        domains=domains,
    )
    spec_json = json.dumps(spec, indent=2)

    statements.append("-- === Cortex Agent Definition ===\n")
    statements.append(
        f"CREATE OR REPLACE AGENT {db}.{sch}.{agent_name}\n"
        f"  COMMENT = 'SI agent replicating {source_type} dashboard experience. "
        f"Generated by semantic-extraction utility.'\n"
        f"FROM SPECIFICATION $spec$\n"
        f"{spec_json}\n"
        f"$spec$;\n"
    )

    # Grant access (commented)
    statements.append(
        f"-- === Grant Agent Access (uncomment and customize) ===\n"
        f"-- GRANT USAGE ON AGENT {db}.{sch}.{agent_name} TO ROLE {role_name};\n"
    )

    return statements


def format_si_assessment(assessment: dict) -> str:
    """Format SI assessment as human-readable text."""
    lines: list[str] = []
    lines.append(
        f"{assessment['source_type']} inventory — "
        f"{assessment['total_tables']} tables, "
        f"{assessment['total_columns']} columns"
    )
    lines.append(
        f"Proposed: {assessment['domain_count']} domain(s) → "
        f"{assessment['domain_count']} semantic view(s) → 1 agent"
    )
    lines.append("")

    # Domain table
    lines.append(f"{'Domain':<30} {'Tables':>6} {'Cols':>6} "
                 f"{'Dims':>6} {'Facts':>6} {'Metrics':>7} {'Status':>10}")
    lines.append("-" * 80)
    for d in assessment["domains"]:
        status = "OVER LIMIT" if d["exceeds_limit"] else "OK"
        lines.append(
            f"{d['domain']:<30} {d['table_count']:>6} {d['columns']:>6} "
            f"{d['dimensions']:>6} {d['facts']:>6} {d['metrics']:>7} "
            f"{status:>10}"
        )

    if assessment["warnings"]:
        lines.append("")
        lines.append("Warnings:")
        for w in assessment["warnings"]:
            lines.append(f"  - {w}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Domain grouping internals
# ---------------------------------------------------------------------------

def _page_table_clusters(inventory: dict) -> dict[str, set[str]]:
    """Extract page→tables mapping from inventory items."""
    clusters: dict[str, set[str]] = {}
    for cat in ("dimensions", "facts", "metrics"):
        for item in inventory.get(cat, []):
            page_str = item.get("page_name", "")
            tbl = item.get("table", "").upper()
            if not page_str or not tbl:
                continue
            for page in page_str.split(", "):
                page = page.strip()
                if page:
                    clusters.setdefault(page, set()).add(tbl)
    return clusters


def _merge_overlapping_clusters(
    page_tables: dict[str, set[str]],
    all_tables: set[str],
) -> dict[str, list[str]]:
    """Merge pages that share tables into unified domains.

    Uses union-find to group pages with overlapping table sets,
    then names the domain after the page with the most tables.
    """
    # Build table → pages reverse index
    table_pages: dict[str, set[str]] = defaultdict(set)
    for page, tables in page_tables.items():
        for t in tables:
            table_pages[t].add(page)

    # Union-find on pages
    parent: dict[str, str] = {p: p for p in page_tables}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Merge pages that share any table
    for tables_set in table_pages.values():
        pages_list = list(tables_set)
        for i in range(1, len(pages_list)):
            union(pages_list[0], pages_list[i])

    # Collect groups
    groups: dict[str, set[str]] = defaultdict(set)
    group_pages: dict[str, list[str]] = defaultdict(list)
    for page, tables in page_tables.items():
        root = find(page)
        groups[root].update(tables)
        group_pages[root].append(page)

    # Name each domain after the most representative page
    domains: dict[str, list[str]] = {}
    for root, tables in groups.items():
        pages = group_pages[root]
        # Pick the page with the most tables as domain name
        best_page = max(pages, key=lambda p: len(page_tables.get(p, set())))
        domain_name = _clean_domain_name(best_page)
        domains[domain_name] = sorted(tables)

    # Add unassigned tables
    assigned = {t for tbls in domains.values() for t in tbls}
    unassigned = all_tables - assigned
    if unassigned:
        domains["Other"] = sorted(unassigned)

    return domains


def _build_fk_graph(inventory: dict) -> dict[str, set[str]]:
    """Build directed FK adjacency: child_table -> {parent_tables}."""
    graph: dict[str, set[str]] = {}
    for r in inventory.get("relationships", []):
        lt = r.get("left_table", "").upper()
        rt = r.get("right_table", "").upper()
        if not lt or not rt or lt == "NAN" or rt == "NAN":
            continue
        graph.setdefault(lt, set()).add(rt)
    return graph


def _detect_cross_domain_bridges(
    domains: dict[str, list[str]],
    inventory: dict,
) -> list[dict]:
    """Detect FK bridges between separate domains.

    Two domains are bridged when a table in domain A has a direct FK
    to a table in domain B (or both FK into a shared table).

    Returns a list of bridge specs::

        {
            "domains": (domain_a_name, domain_b_name),
            "bridge_table": "FACT PLACEMENT",      # table both connect through
            "tables_a": [...],                      # tables from domain A to include
            "tables_b": [...],                      # tables from domain B to include
            "shared_dims": [...],                   # shared dimension tables
            "link_rels": [...],                     # relationship dicts that cross
        }
    """
    fk_graph = _build_fk_graph(inventory)

    # Reverse map: table -> domain
    table_domain: dict[str, str] = {}
    for dname, tables in domains.items():
        for t in tables:
            table_domain[t] = dname

    # Build reverse FK graph too: parent -> {children}
    reverse_fk: dict[str, set[str]] = {}
    for child, parents in fk_graph.items():
        for p in parents:
            reverse_fk.setdefault(p, set()).add(child)

    bridges: list[dict] = []
    domain_names = sorted(domains.keys())
    seen_pairs: set[tuple[str, str]] = set()

    for i, da in enumerate(domain_names):
        for db in domain_names[i + 1:]:
            pair = (da, db)
            if pair in seen_pairs:
                continue

            tables_a = set(domains[da])
            tables_b = set(domains[db])

            # Check: does any table in A FK directly into a table in B?
            bridge_table = None
            link_rels: list[dict] = []

            for r in inventory.get("relationships", []):
                lt = r.get("left_table", "").upper()
                rt = r.get("right_table", "").upper()
                if not lt or not rt or lt == "NAN" or rt == "NAN":
                    continue
                # A -> B
                if lt in tables_a and rt in tables_b:
                    link_rels.append(r)
                    bridge_table = rt
                # B -> A
                elif lt in tables_b and rt in tables_a:
                    link_rels.append(r)
                    bridge_table = rt

            if not link_rels:
                continue

            seen_pairs.add(pair)

            # Find shared dimensions: tables that both domain A and B
            # have FK paths to (through the bridge table or directly)
            dims_a = set()
            for t in tables_a:
                dims_a.update(fk_graph.get(t, set()))
            dims_b = set()
            for t in tables_b:
                dims_b.update(fk_graph.get(t, set()))
            # Also include dims reachable through the bridge table
            if bridge_table:
                bt_dims = fk_graph.get(bridge_table, set())
                dims_a.update(bt_dims)
                dims_b.update(bt_dims)
            shared_dims = sorted(dims_a & dims_b)

            bridges.append({
                "domains": pair,
                "bridge_table": bridge_table,
                "tables_a": sorted(tables_a),
                "tables_b": sorted(tables_b),
                "shared_dims": shared_dims,
                "link_rels": link_rels,
            })
            log.info(
                "Cross-domain bridge: %s <-> %s via %s (%d shared dims)",
                da, db, bridge_table, len(shared_dims),
            )

    return bridges


def _build_cross_domain_views(
    bridges: list[dict],
    domains: dict[str, list[str]],
    inventory: dict,
) -> dict[str, list[str]]:
    """Build cross-domain table sets from detected bridges.

    Returns new domain entries keyed ``"A × B"`` with the combined table
    list, pruned to stay within ``_MAX_COLS_PER_VIEW``.
    """
    if not bridges:
        return {}

    # Pre-compute column counts per table (dims + facts + metrics)
    table_col_count: dict[str, int] = defaultdict(int)
    for cat in ("dimensions", "facts", "metrics"):
        for item in inventory.get(cat, []):
            tbl = item.get("table", "").upper()
            if tbl:
                table_col_count[tbl] += 1

    fk_graph = _build_fk_graph(inventory)
    cross_domains: dict[str, list[str]] = {}

    for bridge in bridges:
        da, db = bridge["domains"]
        bridge_tbl = bridge["bridge_table"]
        link_rels = bridge["link_rels"]

        # Collect the fact tables that participate in the cross-domain link.
        # These are the tables on the "left" side of link_rels (the FK source).
        cross_facts: set[str] = set()
        for r in link_rels:
            lt = r.get("left_table", "").upper()
            cross_facts.add(lt)
        if bridge_tbl:
            cross_facts.add(bridge_tbl)

        # Shared dims are the key join enablers — always include
        shared_dims = set(bridge["shared_dims"])

        # Start with the essential tables: bridge + cross-referencing facts
        # + shared dimensions
        essential = cross_facts | shared_dims
        budget_used = sum(table_col_count.get(t, 0) for t in essential)

        # Add remaining tables from both source domains that aren't already
        # included, plus local dimension tables reachable via FK from any
        # included fact table.  Sort by column count (smallest first) to
        # maximise table coverage within the budget.
        candidate_extras: list[tuple[int, str]] = []

        # Source domain tables not yet included
        all_source_tables = set(bridge["tables_a"]) | set(bridge["tables_b"])
        for t in all_source_tables:
            if t not in essential:
                candidate_extras.append(
                    (table_col_count.get(t, 0), t)
                )

        # Local dimensions reachable from included fact tables
        for fact_t in cross_facts:
            for dim_t in fk_graph.get(fact_t, set()):
                if dim_t not in essential and dim_t not in all_source_tables:
                    candidate_extras.append(
                        (table_col_count.get(dim_t, 0), dim_t)
                    )
        candidate_extras.sort()  # smallest first to maximise coverage

        included = set(essential)
        for cols, tbl in candidate_extras:
            if budget_used + cols <= _MAX_COLS_PER_VIEW:
                included.add(tbl)
                budget_used += cols
            else:
                log.info(
                    "Cross-domain %s × %s: dropped %s (%d cols) — budget %d/%d",
                    da, db, tbl, cols, budget_used, _MAX_COLS_PER_VIEW,
                )

        if len(included) < 3:
            # Not enough substance for a cross-domain view
            continue

        # Build domain name
        # Shorten domain labels: strip " (FACT)" / " (DIM)" suffixes
        short_a = re.sub(r'\s*\([^)]*\)\s*$', '', da).strip()
        short_b = re.sub(r'\s*\([^)]*\)\s*$', '', db).strip()
        if short_a == short_b:
            # Same stem (e.g. "Timesheet (DIM)" × "Timesheet (FACT)")
            # — use the original names to disambiguate
            cross_name = f"{da} × {db}"
        else:
            cross_name = f"{short_a} × {short_b}"

        cross_domains[cross_name] = sorted(included)
        log.info(
            "Cross-domain view '%s': %d tables, ~%d columns",
            cross_name, len(included), budget_used,
        )

    return cross_domains


def _prefix_based_grouping(all_tables: set[str]) -> dict[str, list[str]]:
    """Group tables by shared name prefixes (DIM_, FACT_, etc.)."""
    prefix_groups: dict[str, list[str]] = defaultdict(list)

    for tbl in sorted(all_tables):
        # Try common prefixes: DIM_X, FACT_X, VW_X, RPT_X
        match = re.match(r'^(DIM|FACT|VW|RPT|STG|AGG|REF|LKP)[\s_]+(.+)', tbl, re.I)
        if match:
            prefix = match.group(1).upper()
            stem = match.group(2)
            # Group by the first meaningful word after prefix
            word = re.split(r'[_\s]+', stem)[0].title()
            domain_name = f"{word} ({prefix})"
            prefix_groups[domain_name].append(tbl)
        else:
            # Try grouping by first word
            parts = re.split(r'[_\s]+', tbl)
            if len(parts) > 1:
                prefix_groups[parts[0].title()].append(tbl)
            else:
                prefix_groups["General"].append(tbl)

    # Merge groups that are too small (< 2 tables) into "General"
    final: dict[str, list[str]] = {}
    general: list[str] = []
    for name, tables in prefix_groups.items():
        if len(tables) < 2:
            general.extend(tables)
        else:
            final[name] = tables

    if general:
        final.setdefault("General", []).extend(general)

    return final


def _clean_domain_name(page_name: str) -> str:
    """Clean a page name into a domain label."""
    # Remove common prefixes/suffixes
    name = re.sub(r'(?i)^(page|tab|sheet|dashboard)\s*[-:_]?\s*', '', page_name)
    name = re.sub(r'(?i)\s*(page|tab|sheet|dashboard)$', '', name)
    name = name.strip()
    return name or page_name


def _infer_domain_name(inventory: dict) -> str:
    """Derive a meaningful single-domain name from inventory metadata.

    Priority:
    1. Datasource caption (e.g. ``"BoD Plan+ (Multiple Connections)"`` → ``"Bod Plan"``)
    2. Most common dashboard-name prefix word(s) shared across dashboards
    3. source_type title-cased (last resort — avoids the generic "Tableau" label)
    """
    # 1. Try datasource caption from tables metadata
    for table in inventory.get("tables", []):
        caption = (table.get("connection") or {}).get("caption", "") or table.get("caption", "")
        if caption and caption.strip():
            # Strip parenthetical suffixes like "(Multiple Connections)"
            clean = re.sub(r'\s*\([^)]*\)', '', caption).strip()
            # Convert to title-case identifier (max 3 words)
            words = re.findall(r'[A-Za-z]+', clean)[:3]
            if words:
                return " ".join(w.title() for w in words)

    # 2. Try to find a common prefix across dashboard names
    dash_names = [d.get("name", "") for d in inventory.get("dashboards", []) if d.get("name")]
    if dash_names:
        # Split each name into words, find words common to >50% of dashboards
        from collections import Counter
        word_counts: Counter = Counter()
        for dn in dash_names:
            for w in re.findall(r'[A-Za-z]{3,}', dn):
                word_counts[w.title()] += 1
        threshold = max(2, len(dash_names) // 2)
        common = [w for w, c in word_counts.most_common(2) if c >= threshold]
        # Filter out generic terms
        generic = {"Dashboard", "Bookings", "Summary", "Sales", "Report", "Data"}
        meaningful = [w for w in common if w not in generic]
        if meaningful:
            return " ".join(meaningful)

    # 3. Fallback to source_type (e.g. "powerbi" → "Power Bi")
    source = inventory.get("source_type", "Analytics")
    return source.replace("_", " ").title()



def _is_cross_domain(domain_name: str) -> bool:
    """Return True if *domain_name* is a cross-domain combined view."""
    return "×" in domain_name


def _generate_tool_description(
    domain_name: str,
    tables: list[str],
    inventory: dict,
) -> str:
    """Generate a rich tool description from inventory metadata."""
    parts: list[str] = []

    if _is_cross_domain(domain_name):
        halves = [h.strip() for h in domain_name.split("×")]
        parts.append(
            f"Cross-domain query tool spanning {' and '.join(halves)} data. "
            f"Use this for questions that require joining across these domains. "
            f"Powered by a semantic model covering {len(tables)} table(s)."
        )
    else:
        parts.append(
            f"Query {domain_name} data using natural language. "
            f"Powered by a semantic model covering {len(tables)} table(s)."
        )

    # Tables summary
    table_names = ", ".join(tables[:8])
    if len(tables) > 8:
        table_names += f", ... ({len(tables)} total)"
    parts.append(f"\nTables: {table_names}")

    # Key metrics
    metrics = [
        m.get("name", "")
        for m in inventory.get("metrics", [])
        if m.get("table", "").upper() in tables
    ]
    if metrics:
        metric_sample = ", ".join(metrics[:10])
        if len(metrics) > 10:
            metric_sample += f" ({len(metrics)} total)"
        parts.append(f"Key Metrics: {metric_sample}")

    # Key dimensions
    dims = [
        d.get("name", "")
        for d in inventory.get("dimensions", [])
        if d.get("table", "").upper() in tables
    ]
    if dims:
        dim_sample = ", ".join(dims[:10])
        if len(dims) > 10:
            dim_sample += f" ({len(dims)} total)"
        parts.append(f"Common Filters/Dimensions: {dim_sample}")

    # When to use / not use
    parts.append(f"\nWhen to Use:")
    if _is_cross_domain(domain_name):
        halves = [h.strip() for h in domain_name.split("×")]
        parts.append(
            f"- Questions that span {' and '.join(halves)} "
            f"(e.g. breakdowns of one domain's measures by another domain's dimensions)"
        )
    else:
        parts.append(f"- Questions about {domain_name.lower()} data, trends, or breakdowns")
    if metrics:
        parts.append(f"- Metric calculations: {', '.join(metrics[:5])}")

    parts.append(f"\nWhen NOT to Use:")
    other_domain_hint = "other domain-specific tools"
    parts.append(
        f"- Questions unrelated to {domain_name.lower()} "
        f"(use {other_domain_hint} instead)"
    )

    return "\n".join(parts)


def _generate_orchestration_instructions(
    agent_name: str,
    source_type: str,
    domains: dict[str, list[str]],
    inventory: dict,
) -> str:
    """Generate orchestration instructions for the agent."""
    domain_list = ", ".join(sorted(domains.keys()))
    total_tables = sum(len(t) for t in domains.values())

    lines: list[str] = []
    lines.append(
        f"Your Role: You are an analytics assistant that answers questions "
        f"about data originally managed in a {source_type} environment. "
        f"You have access to {len(domains)} data domain(s): {domain_list}."
    )
    lines.append("")
    lines.append("Tool Selection Guidelines:")
    for dname in sorted(domains.keys()):
        tool_name = _domain_to_tool_name(dname)
        lines.append(
            f"- For questions about {dname.lower()}: "
            f"Use \"{tool_name}\""
        )
    lines.append("")
    lines.append(
        "If a question spans multiple domains, use the most relevant tool first, "
        "then supplement with additional tools if needed."
    )
    lines.append("")
    lines.append(
        "Limitations:\n"
        "- Data is sourced from the underlying tables and may have refresh delays.\n"
        "- Be specific about time ranges when querying date-based data.\n"
        "- If a query returns no results, suggest alternative phrasings or filters."
    )

    return "\n".join(lines)


def _generate_response_instructions(source_type: str) -> str:
    """Generate response formatting instructions."""
    return (
        "Response Style:\n"
        "- Be concise and direct. Lead with the answer, then supporting detail.\n"
        "- Use tables for multi-row data (>3 items).\n"
        "- Use charts for trends, comparisons, and rankings.\n"
        "- Always include units (counts, percentages, currency) with numbers.\n"
        "- For single values, state them directly without a table."
    )


# ---------------------------------------------------------------------------
# Semantic View DDL generation
# ---------------------------------------------------------------------------

def _generate_semantic_view_ddl(
    domain_name: str,
    tables: list[str],
    inventory: dict,
    database: str,
    schema: str,
    view_name: str,
) -> str:
    """Generate CREATE SEMANTIC VIEW DDL for a domain group."""
    lines: list[str] = []
    if _is_cross_domain(domain_name):
        lines.append(f"-- Cross-Domain Semantic View: {domain_name}")
    else:
        lines.append(f"-- Semantic View: {domain_name}")
    lines.append(
        f"CREATE OR REPLACE SEMANTIC VIEW {database}.{schema}.{view_name}"
    )

    # TABLES clause
    table_clauses: list[str] = []
    table_defs = {t.get("name", "").upper(): t for t in inventory.get("tables", [])}

    for tbl in sorted(tables):
        tdef = table_defs.get(tbl, {})
        physical = _sanitize_name(tdef.get("physical_table", tbl))
        safe_tbl = _sanitize_name(tbl)
        pk = _find_pk_for_table(tbl, inventory)
        pk_clause = f' PRIMARY KEY ("{pk}")' if pk else ""
        table_clauses.append(
            f"  {safe_tbl} AS {database}.{schema}.{physical}{pk_clause}"
        )

    if table_clauses:
        lines.append("TABLES (")
        lines.append(",\n".join(table_clauses))
        lines.append(")")

    # RELATIONSHIPS clause
    rels = inventory.get("relationships", [])
    table_set = set(tables)
    active_rels = [
        r for r in rels
        if r.get("is_active", True)
        and r.get("left_table", "").upper() in table_set
        and r.get("right_table", "").upper() in table_set
    ]
    if active_rels:
        rel_clauses: list[str] = []
        for rel in active_rels:
            lt = _sanitize_name(rel.get("left_table", ""))
            rt = _sanitize_name(rel.get("right_table", ""))
            lc = rel.get("left_column", "")
            rc = rel.get("right_column", "")
            if lt and rt and lc and rc:
                rel_clauses.append(
                    f"  {lt}.{lc} REFERENCES {rt}.{rc}"
                )
        if rel_clauses:
            lines.append("RELATIONSHIPS (")
            lines.append(",\n".join(rel_clauses))
            lines.append(")")

    # DIMENSIONS clause
    dims = [
        d for d in inventory.get("dimensions", [])
        if d.get("table", "").upper() in table_set
        and d.get("complexity") != "manual_required"
    ]
    if dims:
        dim_clauses: list[str] = []
        for d in dims:
            tbl = _sanitize_name(d.get("table", ""))
            name = _sanitize_name(d.get("name", ""))
            dim_clauses.append(
                f"  {tbl}.{name} AS \"{_sanitize_name(d.get('name', ''))}\""
            )
        if dim_clauses:
            lines.append("DIMENSIONS (")
            lines.append(",\n".join(dim_clauses))
            lines.append(")")

    # FACTS clause (numeric columns that aren't metrics)
    facts = [
        f for f in inventory.get("facts", [])
        if f.get("table", "").upper() in table_set
        and f.get("complexity") != "manual_required"
    ]
    if facts:
        fact_clauses: list[str] = []
        for f in facts:
            tbl = _sanitize_name(f.get("table", ""))
            name = _sanitize_name(f.get("name", ""))
            fact_clauses.append(
                f"  {tbl}.{name} AS \"{_sanitize_name(f.get('name', ''))}\""
            )
        if fact_clauses:
            lines.append("FACTS (")
            lines.append(",\n".join(fact_clauses))
            lines.append(")")

    # METRICS clause
    metrics = [
        m for m in inventory.get("metrics", [])
        if m.get("table", "").upper() in table_set
        and m.get("complexity") != "manual_required"
    ]
    if metrics:
        metric_clauses: list[str] = []
        for m in metrics:
            tbl = _sanitize_name(m.get("table", ""))
            name = _sanitize_name(m.get("name", ""))
            expr = m.get("expr", "")
            # Use original expr if it's an aggregate; otherwise default
            if expr and re.search(r'\b(SUM|COUNT|AVG|MIN|MAX)\s*\(', expr, re.I):
                metric_clauses.append(f"  {tbl}.{name} AS {expr}")
            else:
                metric_clauses.append(
                    f"  {tbl}.{name} AS SUM(\"{name}\")"
                )
        if metric_clauses:
            lines.append("METRICS (")
            lines.append(",\n".join(metric_clauses))
            lines.append(")")

    # Comment
    source_type = inventory.get("source_type", "BI tool")
    lines.append(
        f"COMMENT = 'Semantic view for {domain_name} domain. "
        f"Generated from {source_type} inventory by semantic-extraction utility.';"
    )
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_table_columns(inventory: dict) -> dict[str, dict[str, dict]]:
    """Build table→columns map from inventory items."""
    table_columns: dict[str, dict[str, dict]] = {}
    for cat in ("dimensions", "facts", "metrics"):
        for item in inventory.get(cat, []):
            tbl = item.get("table", "").upper()
            if tbl:
                table_columns.setdefault(tbl, {})[item["name"]] = item
    return table_columns


def _find_pk_for_table(table_name: str, inventory: dict) -> str | None:
    """Find PK column for a table from inventory."""
    tbl_upper = table_name.upper()
    for dim in inventory.get("dimensions", []):
        if dim.get("table", "").upper() == tbl_upper:
            if dim.get("primary_key"):
                return dim.get("name", "")
            name = dim.get("name", "").upper()
            stem = re.sub(r'^(DIM|FACT|VW)[\s_]*', '', tbl_upper, flags=re.I)
            if name.endswith("_ID") and stem in name:
                return dim.get("name", "")
    return None


def _domain_to_tool_name(domain_name: str) -> str:
    """Convert domain name to a Cortex Agent tool name."""
    # Replace × with "And" for readability in cross-domain names
    name = domain_name.replace("×", "And")
    clean = re.sub(r'[^A-Za-z0-9\s]', '', name).strip()
    # CamelCase with "Query" prefix
    words = clean.split()
    return "Query" + "".join(w.title() for w in words) + "Data"


def _domain_to_view_name(domain_name: str) -> str:
    """Convert domain name to a semantic view object name."""
    # Replace × with X for cross-domain view names
    name = domain_name.replace("×", "X")
    clean = re.sub(r'[^A-Za-z0-9_\s]', '', name).strip()
    name = re.sub(r'\s+', '_', clean).upper()
    return f"{name}_SEMANTIC_VIEW"


def _sanitize_name(name: str) -> str:
    """Sanitize a name for use in SQL identifiers."""
    if not name:
        return "UNKNOWN"
    name = name.strip("[]`\"' ")
    name = re.sub(r'[^A-Za-z0-9_]', '_', name)
    name = re.sub(r'_+', '_', name)
    name = name.strip('_')
    result = name.upper() or "UNKNOWN"
    if result[0].isdigit():
        result = f"_{result}"
    return result


def _assess_warnings(domain_details: list[dict]) -> list[str]:
    """Generate assessment warnings."""
    warnings = []
    over_limit = [d for d in domain_details if d["exceeds_limit"]]
    if over_limit:
        for d in over_limit:
            warnings.append(
                f"Domain '{d['domain']}' has {d['columns']} columns "
                f"(recommended max: {_MAX_COLS_PER_VIEW}). "
                f"Consider splitting into sub-domains."
            )
    no_metrics = [d for d in domain_details if d["metrics"] == 0]
    if no_metrics:
        for d in no_metrics:
            warnings.append(
                f"Domain '{d['domain']}' has no metrics. "
                f"The semantic view will have limited analytical value."
            )
    if len(domain_details) > 10:
        warnings.append(
            f"Too many domains ({len(domain_details)}). "
            f"Best practice is 5-10 tools per agent. "
            f"Consider consolidating domains."
        )
    return warnings
