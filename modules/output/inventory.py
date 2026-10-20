"""Unified inventory builder that normalizes parsed output from any source.

Takes source-specific parsed data and produces a standardized inventory
structure that the YAML generator consumes.
"""

import json
import os
from typing import Any

from ..common.logger import get_logger
from ..common.errors import ValidationError, fail_step

log = get_logger("inventory")


def build_unified_inventory(
    parsed_data: dict,
    source_type: str,
    snowflake_target: dict | None = None,
) -> dict:
    """Convert source-specific parsed data into a unified inventory.

    Args:
        parsed_data: Output from a source-specific parser (tableau, looker, etc.)
        source_type: One of 'tableau', 'looker', 'powerbi', 'denodo', 'businessobjects'
        snowflake_target: Optional dict with {database, schema} for target mapping.

    Returns:
        Unified inventory dict.
    """
    log.info("Building unified inventory from %s source.", source_type)

    sf_db = (snowflake_target or {}).get("database", "TARGET_DB")
    sf_schema = (snowflake_target or {}).get("schema", "PUBLIC")

    inventory = {
        "source_type": source_type,
        "snowflake_target": {"database": sf_db, "schema": sf_schema},
        "tables": [],
        "relationships": [],
        "dimensions": [],
        "facts": [],
        "metrics": [],
        "filters": [],
        "flagged": [],
        # Four source-agnostic modelling concepts that the original five
        # adapters had nowhere to put. They are additive: an adapter that does
        # not populate them leaves them empty, so nothing downstream breaks.
        #
        # grain_declarations  - Cognos determinants, Power BI table grain,
        #                       Tableau LOD context. Needed to know whether a
        #                       SUM can double-count across a join.
        # aggregation_rules   - the (regular, rollup) pair. A rollup that differs
        #                       from the regular aggregate is semi-additive and
        #                       is not expressible as one SQL aggregate.
        # hierarchies         - named drill paths with ordered levels, so a
        #                       front end can present a tree without a developer
        #                       hand-coding it.
        # security_rules      - row-filter intent plus the principal it binds to.
        "grain_declarations": [],
        "aggregation_rules": [],
        "hierarchies": [],
        "security_rules": [],
        "complexity_summary": {
            "simple": 0,
            "needs_translation": 0,
            "manual_required": 0,
        },
        "errors": parsed_data.get("errors", []),
    }

    try:
        router = {
            "tableau": _from_tableau,
            "looker": _from_looker,
            "powerbi": _from_powerbi,
            "denodo": _from_denodo,
            "businessobjects": _from_businessobjects,
            "cognos": _from_cognos,
        }
        builder = router.get(source_type)
        if builder is None:
            raise ValidationError(
                f"Unknown source_type: {source_type}",
                context={"source_type": source_type},
            )
        builder(parsed_data, inventory)
    except Exception as e:
        record = fail_step("build_unified_inventory", e, partial_results=inventory)
        inventory["errors"].append(record)

    # Retain unnormalized source metadata for translation/security review. A parser
    # capability gap is not evidence that the source has no restrictions.
    inventory["source_metadata"] = parsed_data

    # Tally complexity
    for item_list in [inventory["dimensions"], inventory["facts"], inventory["metrics"]]:
        for item in item_list:
            c = item.get("complexity", "simple")
            if c in inventory["complexity_summary"]:
                inventory["complexity_summary"][c] += 1

    # Preserve dashboard and worksheet structure from the parser so downstream
    # consumers (enrich-charts, generate-streamlit, generate-react, build-agent)
    # can use the full dashboard/worksheet topology.  The builder functions above
    # only populate dimensions/facts/metrics — they never write these keys.
    inventory["dashboards"] = parsed_data.get("dashboards", [])
    inventory["worksheets"] = parsed_data.get("worksheets", [])

    log.info(
        "Unified inventory built: %d tables, %d dimensions, %d facts, %d metrics, "
        "%d flagged, %d dashboards, %d worksheets, complexity=%s",
        len(inventory["tables"]),
        len(inventory["dimensions"]),
        len(inventory["facts"]),
        len(inventory["metrics"]),
        len(inventory["flagged"]),
        len(inventory["dashboards"]),
        len(inventory["worksheets"]),
        inventory["complexity_summary"],
    )

    return inventory


def merge_inventories(inventories: list[dict]) -> dict:
    """Merge multiple inventories (e.g., from multiple source files) into one.

    Tables are deduped by name. Dimensions/facts/metrics are appended.
    Flagged items and errors are merged.
    """
    log.info("Merging %d inventories.", len(inventories))

    merged = {
        "source_type": "merged",
        "snowflake_target": inventories[0].get("snowflake_target", {}) if inventories else {},
        "tables": [],
        "relationships": [],
        "dimensions": [],
        "facts": [],
        "metrics": [],
        "filters": [],
        "flagged": [],
        "grain_declarations": [],
        "aggregation_rules": [],
        "hierarchies": [],
        "security_rules": [],
        "complexity_summary": {"simple": 0, "needs_translation": 0, "manual_required": 0},
        "errors": [],
    }

    seen_tables: set[str] = set()
    # Track which source each merged inventory came from, so a multi-tool merge
    # (a Cognos model plus a Power BI model) stays attributable downstream.
    merged["source_types"] = [inv.get("source_type", "unknown") for inv in inventories]
    merged["source_metadata"] = {"inputs": [inv.get("source_metadata", {}) for inv in inventories]}

    for inv in inventories:
        for table in inv.get("tables", []):
            tname = table.get("name", "")
            if tname and tname not in seen_tables:
                merged["tables"].append(table)
                seen_tables.add(tname)
            elif tname:
                merged["errors"].append({"message": "Duplicate entity in merged sources; assign explicit distinct aliases", "entity": tname})

        merged["relationships"].extend(inv.get("relationships", []))
        merged["dimensions"].extend(inv.get("dimensions", []))
        merged["facts"].extend(inv.get("facts", []))
        merged["metrics"].extend(inv.get("metrics", []))
        merged["filters"].extend(inv.get("filters", []))
        merged["flagged"].extend(inv.get("flagged", []))
        merged["errors"].extend(inv.get("errors", []))
        for key in ("grain_declarations", "aggregation_rules", "hierarchies", "security_rules"):
            merged[key].extend(inv.get(key, []))

        for k in ("simple", "needs_translation", "manual_required"):
            merged["complexity_summary"][k] += inv.get("complexity_summary", {}).get(k, 0)

    log.info(
        "Merged result: %d tables, %d dimensions, %d facts, %d metrics.",
        len(merged["tables"]),
        len(merged["dimensions"]),
        len(merged["facts"]),
        len(merged["metrics"]),
    )

    return merged


def save_inventory(inventory: dict, output_path: str) -> str:
    """Save inventory to JSON file.

    Creates the parent directory. Without this, the documented first command of a
    guided run -- `modules.cli parse ... -o /tmp/b2s/inventory.json` -- failed with
    FileNotFoundError on any machine where /tmp/b2s did not already exist, which is
    every machine on its first run. The failure arrives as a parse error report, so it
    reads as "your model could not be parsed" when the parse in fact succeeded and
    only the write failed. build.py never hit it because run_extract makes the
    directory first; only the documented standalone command did.
    """
    log.info("Saving inventory to %s", output_path)
    parent = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(parent, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(inventory, f, indent=2, default=str)
    log.info("Inventory saved: %s", output_path)
    return output_path


def load_inventory(input_path: str) -> dict:
    """Load inventory from JSON file."""
    log.info("Loading inventory from %s", input_path)
    with open(input_path, "r", encoding="utf-8") as f:
        inventory = json.load(f)
    log.info(
        "Inventory loaded: %d tables, %d dimensions, %d facts, %d metrics.",
        len(inventory.get("tables", [])),
        len(inventory.get("dimensions", [])),
        len(inventory.get("facts", [])),
        len(inventory.get("metrics", [])),
    )
    return inventory


# ---------------------------------------------------------------------------
# Source-specific normalizers
# ---------------------------------------------------------------------------

def _normalize_item(
    name: str,
    expr: str,
    data_type: str = "VARCHAR",
    table: str = "",
    description: str = "",
    synonyms: list[str] | None = None,
    complexity: str = "simple",
    original: dict | None = None,
    source_view: str = "",
    dashboard_name: str = "",
    page_name: str = "",
    widget_name: str = "",
    source_file: str = "",
) -> dict:
    """Build a normalized dimension/fact/metric dict."""
    return {
        "name": name.upper().replace(" ", "_"),
        "expr": expr,
        "data_type": data_type,
        "table": table,
        "description": description,
        "synonyms": synonyms or [],
        "complexity": complexity,
        "original": original or {},
        "source_view": source_view,
        "dashboard_name": dashboard_name,
        "page_name": page_name,
        "widget_name": widget_name,
        "source_file": source_file,
    }


def _from_tableau(parsed: dict, inv: dict) -> None:
    """Normalize Tableau parsed data into unified inventory."""

    # Build reverse provenance index: (datasource, field_name) → [(dashboard, worksheet)]
    # from the dashboards data produced by extract_dashboard_field_usage().
    _prov: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for db in parsed.get("dashboards", []):
        db_name = db.get("name", "")
        for sheet in db.get("sheets", []):
            ws_name = sheet.get("name", "")
            for ds_key, fields in sheet.get("fields_by_datasource", {}).items():
                for field in fields:
                    _prov.setdefault((ds_key, field), []).append((db_name, ws_name))

    for ds in parsed.get("datasources", []):
        ds_name = ds.get("name", "UNKNOWN")

        # A datasource field has no physical-table binding in multi-table exports.
        # Keep its logical datasource scope; ambiguous bindings block downstream.
        source_tables = ds.get("tables", [])
        connection = ds.get("connection", {})
        inv["tables"].append({
            "name": ds_name,
            "physical_table": source_tables[0].get("table", "") if len(source_tables) == 1 else "",
            "database": connection.get("dbname", ""),
            "schema": connection.get("schema", ""),
        })
        if len(source_tables) != 1 or ds.get("joins"):
            inv["flagged"].append({"source": ds_name, "reason": "Multi-table Tableau datasource needs explicit logical bindings"})
        for key in ("filters", "groups", "bins", "parameters"):
            if ds.get(key):
                inv["flagged"].append({"source": ds_name, "reason": "Preserve Tableau " + key, "original": ds[key]})
        inv["errors"].extend(ds.get("errors", []))

        # Joins
        for join in ds.get("joins", []):
            inv["relationships"].append({
                "left_table": join.get("left_table", "").strip("[]").upper(),
                "right_table": join.get("right_table", "").strip("[]").upper(),
                "join_type": join.get("type", "inner"),
                "condition": join.get("clause", ""),
            })

        # Columns
        for col in ds.get("columns", []) + ds.get("calculated_fields", []):
            role = col.get("role", "dimension")
            complexity = col.get("complexity", "simple")

            # Resolve dashboard/worksheet provenance for this field
            col_name = col.get("name", "")
            usages = _prov.get((ds_name, col_name), [])
            dash_name = ", ".join(sorted({u[0] for u in usages})) if usages else ""
            page_name = ", ".join(sorted({u[1] for u in usages})) if usages else ""

            item = _normalize_item(
                name=col.get("caption") or col_name.strip("[]"),
                expr=col.get("formula") or col_name,
                data_type=_map_tableau_type(col.get("datatype", "string")),
                table=ds_name,
                description=col.get("desc", ""),
                complexity=complexity,
                original=col,
                source_view=ds_name,
                dashboard_name=dash_name,
                page_name=page_name,
            )
            if role == "measure" or col.get("formula", ""):
                if any(agg in (col.get("formula", "") or "").upper()
                       for agg in ("SUM(", "COUNT(", "AVG(", "MIN(", "MAX(")):
                    inv["metrics"].append(item)
                else:
                    inv["facts"].append(item)
            else:
                inv["dimensions"].append(item)

            if complexity == "manual_required":
                inv["flagged"].append({
                    "item": item["name"],
                    "source": ds_name,
                    "reason": "complex_formula",
                    "expression": col.get("formula", ""),
                })


def _from_looker(parsed: dict, inv: dict) -> None:
    """Normalize Looker parsed data into unified inventory."""

    # -- Build view→explore mapping so fields get dashboard/page labels ------
    # Each explore references a base view (from_view) and joined views.
    # We map view_name → list of (project_dir, explore_name) tuples.
    view_to_explores: dict[str, list[tuple[str, str]]] = {}
    for explore in parsed.get("explores", []):
        if explore.get("hidden"):
            continue
        exp_name = explore.get("name", "")
        # Derive project name from _source_file (e.g. "looker-hr-mart/models/hr.model.lkml")
        src_file = explore.get("_source_file", "")
        project = src_file.split("/")[0] if "/" in src_file else src_file
        # Clean up common prefixes for readability
        if project.startswith("looker_") or project.startswith("looker-"):
            project = project[7:]

        # Base view
        base_view = explore.get("from_view", explore.get("from", exp_name))
        view_to_explores.setdefault(base_view, []).append((project, exp_name))
        # Joined views
        for join in explore.get("joins", []):
            join_view = join.get("from", join.get("name", ""))
            if join_view:
                view_to_explores.setdefault(join_view, []).append((project, exp_name))

    for view in parsed.get("views", []):
        view_name = view.get("name", "UNKNOWN")
        sql_table = (view.get("sql_table_name") or view_name).strip("`").upper()

        inv["tables"].append({
            "name": sql_table,
            "physical_table": view.get("sql_table_name", ""),
            "view_name": view_name,
        })

        # Resolve dashboard/page labels from explore mapping
        explore_refs = view_to_explores.get(view_name, [])
        if explore_refs:
            # dashboard_name = unique project names
            projects = sorted({p for p, _ in explore_refs})
            dash_name = ", ".join(projects)
            # page_name = unique explore names
            exp_names = sorted({e for _, e in explore_refs})
            page_name = ", ".join(exp_names)
        else:
            # View not referenced by any explore — use source_file path
            src = view.get("_source_file", "")
            project = src.split("/")[0] if "/" in src else ""
            if project.startswith("looker_") or project.startswith("looker-"):
                project = project[7:]
            dash_name = project
            page_name = ""

        for dim in view.get("dimensions", []):
            if dim.get("hidden"):
                continue
            item = _normalize_item(
                name=dim.get("name", ""),
                expr=(dim.get("sql") or "").replace("${TABLE}", sql_table),
                data_type=_map_looker_type(dim.get("type", "string")),
                table=sql_table,
                description=dim.get("description", ""),
                synonyms=[dim["label"]] if dim.get("label") else [],
                complexity=dim.get("complexity", "simple"),
                original=dim,
                source_view=view_name,
                dashboard_name=dash_name,
                page_name=page_name,
            )
            if dim.get("primary_key"):
                item["primary_key"] = True
            inv["dimensions"].append(item)

        for measure in view.get("measures", []):
            if measure.get("hidden"):
                continue
            inv["metrics"].append(_normalize_item(
                name=measure.get("name", ""),
                expr=(measure.get("sql") or "").replace("${TABLE}", sql_table),
                data_type="NUMBER",
                table=sql_table,
                description=measure.get("description", ""),
                synonyms=[measure["label"]] if measure.get("label") else [],
                complexity=measure.get("complexity", "simple"),
                original=measure,
                source_view=view_name,
                dashboard_name=dash_name,
                page_name=page_name,
            ))

    # Joins from explores
    for explore in parsed.get("explores", []):
        for join in explore.get("joins", []):
            inv["relationships"].append({
                "left_table": explore.get("from", explore.get("name", "")).upper(),
                "right_table": join.get("name", "").upper(),
                "join_type": join.get("type", "left_outer"),
                "relationship_type": join.get("relationship", ""),
                "condition": join.get("sql_on", ""),
            })


def _from_powerbi(parsed: dict, inv: dict) -> None:
    """Normalize Power BI parsed data into unified inventory."""

    # Build reverse provenance index: queryRef → [(page_display_name, visual_type)]
    # from the report_pages data produced by extract_report_pages().
    # queryRefs look like "Table.Column" or "Table.Measure".
    _prov: dict[str, list[tuple[str, str]]] = {}
    for page in parsed.get("report_pages", []):
        page_display = page.get("display_name", page.get("name", ""))
        for visual in page.get("visuals", []):
            vtype = visual.get("type", "")
            for qref in visual.get("fields", []):
                _prov.setdefault(qref, []).append((page_display, vtype))

    def _resolve_pbi_prov(table_name: str, col_name: str) -> tuple[str, str, str]:
        """Look up page/visual provenance for a Power BI field."""
        # Try exact queryRef match: "TableName.ColumnName"
        qref = f"{table_name}.{col_name}"
        usages = _prov.get(qref, [])
        if not usages:
            # Try with original (non-uppercased) table name
            qref_orig = f"{table_name}.{col_name}"
            usages = _prov.get(qref_orig, [])
        if usages:
            pages = ", ".join(sorted({u[0] for u in usages}))
            widgets = ", ".join(sorted({u[1] for u in usages if u[1] and u[1] != "unknown"}))
            return ("", pages, widgets)
        return ("", "", "")

    for table in parsed.get("tables", []):
        tname = table.get("name", "UNKNOWN").upper()
        tname_orig = table.get("name", "UNKNOWN")
        inv["tables"].append({
            "name": tname,
            "physical_table": table.get("name", ""),
            "source_query": table.get("source_query"),
            "sql": table.get("source_query") or "",
            "is_passthrough": False,
        })

        for col in table.get("columns", []):
            col_name = col.get("name", "")
            _, pg, wg = _resolve_pbi_prov(tname_orig, col_name)
            item = _normalize_item(
                name=col_name,
                expr=col.get("expression") if col.get("is_calculated") else '"' + (col.get('source_column') or col_name).replace('"', '""') + '"',
                data_type=_map_pbi_type(col.get("data_type", "string")),
                table=tname,
                complexity="needs_translation" if col.get("is_calculated") else col.get("complexity", "simple"),
                original=col,
                source_view=table.get("name", ""),
                page_name=pg,
                widget_name=wg,
            )
            if col.get("summarize_by", "none") not in ("none", "default", "", None):
                inv["facts"].append(item)
            else:
                inv["dimensions"].append(item)

    for measure in parsed.get("measures", []):
        m_table = measure.get("table", "")
        m_name = measure.get("name", "")
        _, pg, wg = _resolve_pbi_prov(m_table, m_name)
        item = _normalize_item(
            name=m_name,
            expr=measure.get("expression", ""),
            data_type="NUMBER",
            table=m_table.upper(),
            description=measure.get("description", ""),
            complexity=measure.get("complexity", "simple"),
            original=measure,
            source_view=m_table,
            page_name=pg,
            widget_name=wg,
        )
        # Propagate DAX enrichment detail if present
        dax_detail = measure.get("dax_detail")
        if dax_detail:
            item["dax_patterns"] = ", ".join(dax_detail.get("matched_patterns", []))
            item["dax_categories"] = ", ".join(dax_detail.get("categories", []))
            item["dax_description"] = dax_detail.get("description", "")
            item["dax_recommendation"] = dax_detail.get("recommendation", "")
            item["dax_effort"] = dax_detail.get("effort", "")
        inv["metrics"].append(item)

    for rel in parsed.get("relationships", []):
        inv["relationships"].append({
            "left_table": rel.get("from_table", "").upper(),
            "left_column": rel.get("from_column", ""),
            "right_table": rel.get("to_table", "").upper(),
            "right_column": rel.get("to_column", ""),
            "relationship_type": rel.get("cardinality", ""),
            "is_active": rel.get("is_active", True),
            "cross_filter_behavior": rel.get("cross_filter_behavior", ""),
        })

    for flagged in parsed.get("flagged", []):
        inv["flagged"].append(flagged)
    for role in parsed.get("roles", []):
        for permission in role.get("tablePermissions", []):
            if permission.get("filterExpression"):
                inv["security_rules"].append({"principal": role.get("name", ""),
                    "entities": [permission.get("name", "").upper()],
                    "expression": permission["filterExpression"], "original": permission})


def _from_denodo(parsed: dict, inv: dict) -> None:
    """Normalize Denodo parsed data into unified inventory."""
    import sqlglot
    from sqlglot import exp
    for view in parsed.get("views", []):
        vname = view.get("name", "UNKNOWN").upper()
        inv["errors"].extend(view.get("errors", []))
        # Lower only a single-table SELECT. Full SQL remains provenance; joins,
        # distinct, unions, limits and HAVING cannot disappear in normalization.
        try:
            trees = sqlglot.parse(view.get("full_sql", ""), read="snowflake")
            if len(trees) != 1 or trees[0] is None:
                raise ValueError("Denodo view contains additional statements requiring review")
            tree = trees[0]
            if isinstance(tree, exp.Create): tree = tree.expression
            sources = list(tree.find_all(exp.Table))
            if not isinstance(tree, exp.Select) or len(sources) != 1 or any(tree.args.get(key) for key in
                    ("joins", "distinct", "having", "limit", "offset", "qualify", "with", "with_")):
                raise ValueError("Denodo query requires reviewed relational translation")
            physical = sources[0].copy()
            physical.set("alias", None)
            inv["tables"].append({"name": vname, "physical_table": physical.sql(dialect="snowflake")})
            for selected in tree.expressions:
                body = selected.this if isinstance(selected, exp.Alias) else selected
                if isinstance(body, exp.Star):
                    raise ValueError("Denodo wildcard needs explicit exported columns")
                body = body.copy()
                for column in body.find_all(exp.Column):
                    if column.table and column.table not in (sources[0].name, sources[0].alias):
                        raise ValueError("Unresolved Denodo column scope")
                    column.set("table", None)
                name = selected.alias_or_name
                if not name: raise ValueError("Denodo expression needs an explicit alias")
                metric = bool(list(body.find_all(exp.AggFunc)))
                item = _normalize_item(name=name, expr=body.sql(dialect="snowflake"), table=vname, original=view)
                inv["metrics" if metric else "dimensions"].append(item)
                if metric:
                    for column in body.find_all(exp.Column):
                        if not any(item["table"] == vname and item["expr"] == column.sql(dialect="snowflake")
                                   for item in inv["facts"] + inv["dimensions"]):
                            inv["facts"].append(_normalize_item(name=column.name, expr=column.sql(dialect="snowflake"), table=vname))
            if tree.args.get("where"):
                predicate = tree.args["where"].this.copy()
                for column in predicate.find_all(exp.Column): column.set("table", None)
                inv["filters"].append({"table": vname, "expr": predicate.sql(dialect="snowflake")})
            if tree.args.get("group"):
                projected = {item["expr"] for item in inv["dimensions"] if item["table"] == vname}
                grouped = set()
                for expression in tree.args["group"].expressions:
                    expression = expression.copy()
                    for column in expression.find_all(exp.Column): column.set("table", None)
                    grouped.add(expression.sql(dialect="snowflake"))
                if projected != grouped:
                    raise ValueError("Denodo group grain differs from projected dimensions")
        except (ValueError, sqlglot.errors.ParseError) as exc:
            inv["flagged"].append({"source": vname, "reason": str(exc), "original": view})


def _from_businessobjects(parsed: dict, inv: dict) -> None:
    """Normalize Business Objects parsed data into unified inventory."""
    # Physical tables
    for table in parsed.get("tables", []):
        inv["tables"].append({
            "name": table.get("name", "").upper(),
            "physical_table": table.get("sql_name") or table.get("name", ""),
            "schema": table.get("owner", ""),
            "database": parsed.get("connection", {}).get("database", ""),
        })

    # Joins
    for join in parsed.get("joins", []):
        inv["relationships"].append({
            "left_table": join.get("table1", "").upper(),
            "left_column": join.get("column1", ""),
            "right_table": join.get("table2", "").upper(),
            "right_column": join.get("column2", ""),
            "join_type": "unresolved_outer" if join.get("outer_join") else "inner",
            "condition": join.get("expression", ""),
            "cardinality": join.get("cardinality", ""),
        })

    # Business objects
    import sqlglot
    from sqlglot import exp
    for obj in parsed.get("objects", []):
        qual = obj.get("qualification", "dimension").lower()
        complexity = obj.get("complexity", "simple")
        text = obj.get("resolved_sql") or obj.get("select_expression", "")
        table_name = obj.get("table", "").upper()
        try:
            parsed_expression = sqlglot.parse_one(text, read="snowflake")
            qualifiers = {column.table.upper() for column in parsed_expression.find_all(exp.Column) if column.table}
            if not table_name and len(qualifiers) == 1: table_name = qualifiers.pop()
            for column in parsed_expression.find_all(exp.Column):
                if column.table and column.table.upper() != table_name:
                    raise ValueError("Cross-table BusinessObjects expression needs translation")
                column.set("table", None)
            text = parsed_expression.sql(dialect="snowflake")
            if qual == "measure":
                for column in parsed_expression.find_all(exp.Column):
                    if not any(item["table"] == table_name and item["name"] == column.name.upper()
                               for item in inv["facts"] + inv["dimensions"]):
                        inv["facts"].append(_normalize_item(name=column.name, expr=column.sql(dialect="snowflake"), table=table_name))
        except (ValueError, sqlglot.errors.ParseError) as exc:
            inv["flagged"].append({"source": obj.get("name"), "reason": str(exc)})
        if obj.get("where_expression"):
            inv["flagged"].append({"source": obj.get("name"), "reason": "Object-level WHERE scope requires translation", "original": obj})
        item = _normalize_item(
            name=obj.get("name", ""),
            expr=text,
            data_type=_map_bo_type(obj.get("data_type", "string")),
            table=table_name,
            description=obj.get("description", ""),
            complexity=complexity,
            original=obj,
            source_view=obj.get("class", obj.get("table", "")),
        )

        if qual == "measure":
            inv["metrics"].append(item)
        elif qual == "detail":
            inv["dimensions"].append(item)
        elif qual == "filter":
            inv["filters"].append(item)
        else:
            inv["dimensions"].append(item)

        if complexity == "manual_required":
            inv["flagged"].append({
                "item": item["name"],
                "reason": obj.get("flag_reason", "complex_expression"),
                "expression": obj.get("select_expression", ""),
            })


def _from_cognos(parsed: dict, inv: dict) -> None:
    """Normalize Cognos Framework Manager parsed data into unified inventory.

    Cognos models data in layered namespaces: an "Import View" that mirrors the
    physical tables, a "Model View" that wraps them for reuse, and a "DMR View"
    holding the dimensional/OLAP layer. Only Import View query subjects map to
    real tables; the others are views over them. Emitting all three as tables
    would triple the table count and create joins between an object and its own
    wrapper, so the physical layer is preferred and Model View subjects are only
    used when they have no Import View counterpart.
    """
    from ..cognos import analysis as cognos_analysis
    from ..cognos import expressions as cognos_expr
    from ..cognos import rls as cognos_rls
    from ..cognos.classifier import (
        classify_cognos_expression,
        classify_query_subject,
    )

    sf_db = inv["snowflake_target"]["database"]

    # Prefer the physical layer for the physical binding, but carry the model
    # layer's filters forward. Neither layer alone is complete: Import View knows
    # which table an entity reads, Model View knows which rows the model keeps.
    # Replacing one with the other loses half the definition.
    by_name: dict[str, dict] = {}
    for qs in parsed.get("query_subjects", []):
        name = qs.get("name", "")
        if not name:
            continue
        existing = by_name.get(name)
        if existing is None:
            by_name[name] = dict(qs)
            continue
        physical_wins = (
            existing.get("namespace") != "Import View"
            and qs.get("namespace") == "Import View"
        )
        keep, other = (qs, existing) if physical_wins else (existing, qs)
        merged = dict(keep)
        # Union the filters from both layers, deduplicated on expression.
        seen_exprs = {f.get("expression") for f in merged.get("filters", [])}
        for f in other.get("filters", []):
            if f.get("expression") not in seen_exprs:
                merged.setdefault("filters", []).append(f)
                seen_exprs.add(f.get("expression"))
        # A filter anywhere in the stack means the entity is not a passthrough.
        if merged.get("filters"):
            merged["is_passthrough"] = False
        for key in ("physical_object", "source_alias", "table_type", "data_source"):
            if not merged.get(key) and other.get(key):
                merged[key] = other[key]
        merged["wraps_entities"] = list(
            dict.fromkeys((keep.get("wraps_entities") or []) + (other.get("wraps_entities") or []))
        )
        # Union query items: model layers may add calculated join keys that are
        # absent from the physical layer. Dropping them breaks source relationships.
        merged_items = list(keep.get("items") or [])
        have = {i.get("name") for i in merged_items}
        for i in other.get("items") or []:
            if i.get("name") not in have:
                merged_items.append(i)
                have.add(i.get("name"))
        merged["items"] = merged_items
        merged["security_filter_count"] = max(
            keep.get("security_filter_count", 0), other.get("security_filter_count", 0)
        )
        by_name[name] = merged

    # Tables
    for name, qs in by_name.items():
        physical = qs.get("physical_object") or name
        inv["tables"].append(
            {
                "name": name,
                "physical_name": physical,
                "database": "",  # Source binding must not inherit the generated target.
                # The Cognos data source alias carries the schema in this model
                # family; the loader maps alias -> schema explicitly rather than
                # guessing, so it is passed through verbatim.
                "source_alias": qs.get("source_alias", "") or qs.get("data_source", ""),
                "description": f"Cognos query subject {name}"
                + (f" ({qs.get('table_type')})" if qs.get("table_type") else ""),
                "source_view": qs.get("namespace", ""),
                "complexity": classify_query_subject(qs),
                "is_passthrough": qs.get("is_passthrough", False),
                "sql": qs.get("sql", ""),
                "status": qs.get("status", ""),
                "wraps_entities": qs.get("wraps_entities", []),
                "entity_filters": qs.get("filters", []),
                "security_filter_count": qs.get("security_filter_count", 0),
            }
        )

        # Query items -> dimensions or facts, by Cognos's own usage declaration.
        for item in qs.get("items", []):
            complexity = classify_cognos_expression(item.get("expression", ""))
            norm = _normalize_item(
                name=item.get("name", ""),
                expr=item.get("expression") or item.get("external_name") or item.get("name", ""),
                data_type=item.get("data_type", "VARCHAR"),
                table=name,
                description=item.get("description", ""),
                complexity=complexity,
                original=item,
                source_view=qs.get("namespace", ""),
                source_file=parsed.get("source_file", ""),
            )
            # Cognos declares usage per item: fact (measure), identifier (key),
            # attribute (descriptive). Trusting the declaration beats inferring
            # from the name -- INVENTORY_ITEM_ID is declared an identifier and
            # would otherwise be classified a metric by any _id/_amount heuristic.
            if item.get("usage") == "fact":
                inv["facts"].append(norm)
            else:
                inv["dimensions"].append(norm)

            if complexity == "manual_required":
                inv["flagged"].append(
                    {
                        "name": item.get("name", ""),
                        "table": name,
                        "reason": "Cognos expression has no direct scalar equivalent",
                        "expression": item.get("expression", ""),
                    }
                )

    # Relationships
    for rel in parsed.get("relationships", []):
        left = rel.get("left", {})
        right = rel.get("right", {})
        cols = rel.get("join_columns", [])
        inv["relationships"].append(
            {
                "name": rel.get("name", ""),
                "left_table": left.get("entity", ""),
                "right_table": right.get("entity", ""),
                "left_column": cols[0] if cols else "",
                "right_column": cols[1] if len(cols) > 1 else (cols[0] if cols else ""),
                "condition": rel.get("expression", ""),
                "cardinality": f"{left.get('cardinality', '')}-to-{right.get('cardinality', '')}",
                # Framework Manager's own validity flag, carried through as
                # metadata. Never used to drop a relationship: it goes stale
                # independently of whether the join predicate is sound.
                "source_status": rel.get("status", ""),
                "is_simple_equality": rel.get("is_simple_equality", False),
            }
        )

    # Calculations -> metrics, with relative-time windows resolved to SQL.
    for calc in parsed.get("calculations", []):
        expr = calc.get("expression", "")
        if not expr:
            # Empty calculation bodies exist in the source model. Record them as
            # flagged rather than emitting a metric with no definition.
            inv["flagged"].append(
                {
                    "name": calc.get("name", ""),
                    "table": "",
                    "reason": "calculation has an empty expression in the source Cognos model",
                    "expression": "",
                }
            )
            continue

        translated = cognos_expr.translate_relative_time_calculation(
            expr, measure="{measure}", date_column="{date_column}"
        )
        if translated["kind"] in ("change", "growth", "rate", "single"):
            complexity = "needs_translation"
        elif translated["kind"] == "needs_fiscal_calendar":
            complexity = "needs_translation"
        else:
            complexity = classify_cognos_expression(expr)

        inv["metrics"].append(
            _normalize_item(
                name=calc.get("canonical_name") or calc.get("name", ""),
                # Templated on {measure}/{date_column}: one Cognos calculation
                # becomes one pattern applicable to either fact table, rather
                # than being bound to whichever fact it happened to reference.
                expr=translated["expr"] or expr,
                data_type="NUMBER",
                table="",
                description=(
                    f"Cognos calculation '{calc.get('name', '')}'"
                    + (
                        f" (fiscal-year clone of {calc.get('canonical_name')})"
                        if calc.get("fiscal_year_clone")
                        else ""
                    )
                ),
                complexity=complexity,
                original={
                    **calc,
                    "translation": translated,
                },
                source_view=calc.get("namespace", ""),
                source_file=parsed.get("source_file", ""),
            )
        )
        if translated["kind"] == "needs_fiscal_calendar":
            inv["flagged"].append(
                {
                    "name": calc.get("name", ""),
                    "table": "",
                    "reason": (
                        "depends on the fiscal calendar; must bind to fiscal "
                        "columns in the date dimension, not a date offset"
                    ),
                    "expression": expr,
                }
            )

    # The four extended concepts.
    inv["grain_declarations"] = cognos_analysis.extract_grain_declarations(parsed)
    inv["aggregation_rules"] = cognos_analysis.extract_aggregation_rules(parsed)
    inv["hierarchies"] = cognos_analysis.extract_hierarchies(parsed)
    inv["security_rules"] = cognos_rls.derive_security_rules(parsed)

    # Model-level analysis, kept alongside the inventory so a consumer does not
    # have to re-derive it from the raw parse.
    inv["source_analysis"] = {
        "model_name": parsed.get("model_name", ""),
        "clones": cognos_analysis.analyze_clones(parsed),
        "data_sources": cognos_analysis.audit_data_sources(parsed),
        "security_summary": cognos_rls.summarize_security(parsed),
        "element_counts": parsed.get("element_counts", {}),
    }

    for f in parsed.get("filters", []):
        inv["filters"].append(
            {
                "name": f.get("name", ""),
                "expression": f.get("expression", ""),
                "source_view": f.get("namespace", ""),
                "entity": "",
                "scope": "model",
            }
        )

    # Entity filters affect the row set and must survive layer normalization.
    # Dropping them changes totals even when the underlying tables are unchanged.
    # Iterate the merged entities, not the raw query subjects: the same filter is
    # declared on both the Import View and Model View copy of an entity, and
    # emitting it twice would double-count the remediation work.
    for qs in by_name.values():
        for f in qs.get("filters", []):
            inv["filters"].append(
                {
                    "name": f.get("name", "") or f"{qs.get('name', '')} filter",
                    "expression": f.get("expression", ""),
                    "source_view": qs.get("namespace", ""),
                    "entity": qs.get("name", ""),
                    "scope": "entity",
                    "application": f.get("application", ""),
                    "apply": f.get("apply", ""),
                }
            )
            inv["flagged"].append(
                {
                    "name": f"{qs.get('name', '')}: {f.get('name', '') or 'filter'}",
                    "table": qs.get("name", ""),
                    "reason": (
                        "entity carries a model-layer filter that must be reproduced; "
                        "omitting it widens the row set relative to the source model"
                    ),
                    "expression": f.get("expression", ""),
                }
            )


# ---------------------------------------------------------------------------
# Type mappers
# ---------------------------------------------------------------------------

def _map_tableau_type(dt: str) -> str:
    return {"string": "VARCHAR", "integer": "NUMBER", "real": "NUMBER",
            "boolean": "BOOLEAN", "date": "DATE", "datetime": "TIMESTAMP"}.get(dt, "VARCHAR")

def _map_looker_type(dt: str) -> str:
    return {"string": "VARCHAR", "number": "NUMBER", "yesno": "BOOLEAN",
            "date": "DATE", "time": "TIMESTAMP", "date_time": "TIMESTAMP",
            "zipcode": "VARCHAR"}.get(dt, "VARCHAR")

def _map_pbi_type(dt: str) -> str:
    return {"string": "VARCHAR", "int64": "NUMBER", "double": "NUMBER",
            "decimal": "NUMBER", "boolean": "BOOLEAN", "dateTime": "TIMESTAMP",
            "binary": "VARCHAR"}.get(dt, "VARCHAR")

def _map_bo_type(dt: str) -> str:
    return {"Character": "VARCHAR", "Numeric": "NUMBER", "Date": "DATE",
            "DateTime": "TIMESTAMP", "Long text": "VARCHAR"}.get(dt, "VARCHAR")
