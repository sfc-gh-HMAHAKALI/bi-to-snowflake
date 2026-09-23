"""CLI entry point for semantic-extraction modules.

CoCo invokes this via: python -m modules.cli <command> [args]

All commands emit structured JSON to stdout for CoCo to parse.
Human-readable logs go to stderr and to the log file.
"""

import argparse
import datetime
import glob
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Ensure the parent of 'modules' is on sys.path so relative imports work
_MODULES_DIR = Path(__file__).resolve().parent
_SKILL_DIR = _MODULES_DIR.parent
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))

from modules.common.logger import setup_logging, get_logger
from modules.common.errors import ExtractionError, fail_step
from modules.common.file_crawler import discover_files


log = get_logger("cli")


# ---------------------------------------------------------------------------
# Dependency management — auto-check and install per source type
# ---------------------------------------------------------------------------

# Maps source_type -> list of (import_name, pip_package_name)
_PARSER_DEPENDENCIES: dict[str, list[tuple[str, str]]] = {
    "looker": [("lkml", "lkml")],
    "report": [("openpyxl", "openpyxl")],
    # Add future deps here, e.g.:
    # "tableau": [("tableauhyperapi", "tableauhyperapi")],
}


def _ensure_dependencies(source_type: str) -> None:
    """Check that required packages for *source_type* are importable.

    Missing packages are installed automatically via pip.  Raises
    ``ExtractionError`` only if installation itself fails.
    """
    deps = _PARSER_DEPENDENCIES.get(source_type)
    if not deps:
        return

    for import_name, pip_name in deps:
        try:
            importlib.import_module(import_name)
        except ImportError:
            log.warning(
                "Required package '%s' not found for source type '%s' — installing automatically…",
                pip_name, source_type,
            )
            try:
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", pip_name, "-q"],
                    stdout=subprocess.DEVNULL,
                )
                log.info("Successfully installed '%s'.", pip_name)
            except subprocess.CalledProcessError as exc:
                raise ExtractionError(
                    f"Failed to install required package '{pip_name}' for "
                    f"source type '{source_type}'. Install manually with: "
                    f"pip install {pip_name}"
                ) from exc


def cmd_crawl(args: argparse.Namespace) -> dict:
    """Discover source files in a directory."""
    log.info("CRAWL: path=%s, type=%s", args.path, args.type)
    start = time.time()

    files = discover_files(
        root_path=args.path,
        source_type=args.type,
        max_depth=args.max_depth,
        follow_symlinks=args.follow_symlinks,
    )

    result = {
        "status": "ok",
        "command": "crawl",
        "path": args.path,
        "source_type": args.type,
        "file_count": len(files),
        "files": [f.to_dict() for f in files],
        "elapsed_seconds": round(time.time() - start, 2),
    }

    log.info("CRAWL complete: %d files in %.2fs", len(files), result["elapsed_seconds"])
    return result


def cmd_parse(args: argparse.Namespace) -> dict:
    """Parse source files and produce an inventory."""
    log.info("PARSE: path=%s, type=%s, mode=%s", args.path, args.type, getattr(args, "mode", "file"))
    start = time.time()

    source_type = args.type
    path = args.path
    mode = getattr(args, "mode", "file")

    # Server mode — connect to live PBI service
    if mode == "server":
        if source_type != "powerbi":
            return {
                "status": "error",
                "command": "parse",
                "failure": fail_step("parse", ExtractionError(
                    "--mode server is currently only supported for --type powerbi"
                )),
            }
        if not args.config:
            return {
                "status": "error",
                "command": "parse",
                "failure": fail_step("parse", ExtractionError(
                    "--config is required when using --mode server"
                )),
            }
        return _cmd_parse_pbi_server(args, start)

    # File mode — local parse
    if not path:
        return {
            "status": "error",
            "command": "parse",
            "failure": fail_step("parse", ExtractionError(
                "path is required when using --mode file"
            )),
        }

    try:
        parsed = _run_parser(path, source_type)
    except Exception as e:
        return {
            "status": "error",
            "command": "parse",
            "failure": fail_step("parse", e),
        }

    # Build unified inventory
    from modules.output.inventory import build_unified_inventory, save_inventory

    sf_target = {}
    if args.database:
        sf_target["database"] = args.database
    if args.schema:
        sf_target["schema"] = args.schema

    inventory = build_unified_inventory(parsed, source_type, sf_target or None)

    # Save if output specified
    output_path = None
    if args.output:
        output_path = save_inventory(inventory, args.output)

    result = {
        "status": "ok",
        "command": "parse",
        "source_type": source_type,
        "table_count": len(inventory.get("tables", [])),
        "dimension_count": len(inventory.get("dimensions", [])),
        "fact_count": len(inventory.get("facts", [])),
        "metric_count": len(inventory.get("metrics", [])),
        "flagged_count": len(inventory.get("flagged", [])),
        "complexity_summary": inventory.get("complexity_summary", {}),
        "errors": inventory.get("errors", []),
        "output_path": output_path,
        "elapsed_seconds": round(time.time() - start, 2),
    }

    if not args.output:
        result["inventory"] = inventory

    log.info("PARSE complete in %.2fs", result["elapsed_seconds"])
    return result


def cmd_classify(args: argparse.Namespace) -> dict:
    """Run complexity classification on an existing inventory."""
    log.info("CLASSIFY: input=%s", args.input)
    start = time.time()

    from modules.output.inventory import load_inventory

    inventory = load_inventory(args.input)

    # Re-classify all items
    source_type = inventory.get("source_type", "unknown")
    classifier = _get_classifier(source_type)

    reclassified = 0
    for item_list_key in ("dimensions", "facts", "metrics"):
        for item in inventory.get(item_list_key, []):
            if classifier and item.get("original"):
                try:
                    new_complexity = classifier(item["original"])
                    if new_complexity != item.get("complexity"):
                        item["complexity"] = new_complexity
                        reclassified += 1
                except Exception as e:
                    log.warning("Classification failed for %s: %s", item.get("name"), e)

    # Retally
    summary = {"simple": 0, "needs_translation": 0, "manual_required": 0}
    for item_list_key in ("dimensions", "facts", "metrics"):
        for item in inventory.get(item_list_key, []):
            c = item.get("complexity", "simple")
            if c in summary:
                summary[c] += 1
    inventory["complexity_summary"] = summary

    result = {
        "status": "ok",
        "command": "classify",
        "reclassified_count": reclassified,
        "complexity_summary": summary,
        "elapsed_seconds": round(time.time() - start, 2),
    }

    if args.output:
        from modules.output.inventory import save_inventory
        save_inventory(inventory, args.output)
        result["output_path"] = args.output
    else:
        result["inventory"] = inventory

    log.info("CLASSIFY complete: %d reclassified in %.2fs", reclassified, result["elapsed_seconds"])
    return result


def cmd_generate(args: argparse.Namespace) -> dict:
    """Generate Semantic View YAML from an inventory."""
    log.info("GENERATE: input=%s, output=%s", args.input, getattr(args, 'output', None))
    start = time.time()

    from modules.output.inventory import load_inventory
    from modules.output.yaml_generator import generate_all_yamls

    output_dir = _resolve_output_dir(args, command="generate")
    inventory = load_inventory(args.input)

    paths = generate_all_yamls(
        inventory,
        output_dir=output_dir,
        split_threshold=args.split_threshold,
    )

    # Move yaml files into subfolder if multiple
    folders = _organize_subfolders(output_dir)

    result = {
        "status": "ok",
        "command": "generate",
        "output_dir": output_dir,
        "yaml_dir": folders["yaml_dir"],
        "yaml_files": paths,
        "file_count": len(paths),
        "elapsed_seconds": round(time.time() - start, 2),
    }

    log.info("GENERATE complete: %d files in %.2fs", len(paths), result["elapsed_seconds"])
    return result


def cmd_test_connection(args: argparse.Namespace) -> dict:
    """Test connectivity to a source system."""
    log.info("TEST-CONNECTION: type=%s", args.type)
    start = time.time()

    try:
        client = _get_api_client(args.type, args)
        success = client.test_connection()
        client.close()
        status = "ok" if success else "failed"
    except Exception as e:
        status = "error"
        return {
            "status": "error",
            "command": "test-connection",
            "source_type": args.type,
            "failure": fail_step("test_connection", e),
            "elapsed_seconds": round(time.time() - start, 2),
        }

    result = {
        "status": status,
        "command": "test-connection",
        "source_type": args.type,
        "elapsed_seconds": round(time.time() - start, 2),
    }

    log.info("TEST-CONNECTION: %s in %.2fs", status, result["elapsed_seconds"])
    return result


_DEFAULT_OUTPUT_ROOT = os.path.expanduser("~/Downloads")


def _resolve_output_dir(args, *, command: str = "report") -> str:
    """Build an auto-named output directory or return the explicit -o path.

    Auto-naming pattern:
        {customer} - {YYYY-MM-DD} - run{N}/

    The run number is determined by scanning existing folders that match
    the same customer + date prefix and incrementing.

    Falls back to ~/Downloads/ as the parent directory.
    """
    # Explicit -o always wins
    explicit = getattr(args, "output", None)
    if explicit:
        return explicit

    customer = getattr(args, "customer", None)
    if not customer:
        raise ExtractionError(
            f"Either --output (-o) or --customer is required for the "
            f"'{command}' command.  Use --customer to auto-generate "
            f"an output folder under ~/Downloads/."
        )

    today = datetime.date.today().isoformat()           # e.g. 2026-04-04
    prefix = f"{customer} - {today} - run"              # e.g. "Sharp Healthcare - 2026-04-04 - run"
    parent = _DEFAULT_OUTPUT_ROOT

    # Find highest existing run number for this customer + date
    existing = glob.glob(os.path.join(parent, f"{prefix}*"))
    max_run = 0
    pattern = re.compile(re.escape(prefix) + r"(\d+)$")
    for folder in existing:
        m = pattern.search(os.path.basename(folder))
        if m:
            max_run = max(max_run, int(m.group(1)))

    run_dir = os.path.join(parent, f"{prefix}{max_run + 1}")
    os.makedirs(run_dir, exist_ok=True)
    log.info("Auto-created output directory: %s", run_dir)
    return run_dir


def _organize_subfolders(root_dir: str) -> dict:
    """Move xlsx and yaml files into subfolders if there are multiple of each.

    Returns dict with keys 'excel_dir' and 'yaml_dir' indicating where
    the files ended up (root_dir itself or the subfolder).
    """
    result = {"excel_dir": root_dir, "yaml_dir": root_dir}

    xlsx_files = glob.glob(os.path.join(root_dir, "*.xlsx"))
    if len(xlsx_files) > 1:
        excel_dir = os.path.join(root_dir, "excel")
        os.makedirs(excel_dir, exist_ok=True)
        for f in xlsx_files:
            shutil.move(f, os.path.join(excel_dir, os.path.basename(f)))
        result["excel_dir"] = excel_dir
        log.info("Moved %d Excel files to %s", len(xlsx_files), excel_dir)

    yaml_files = glob.glob(os.path.join(root_dir, "*.yaml"))
    if len(yaml_files) > 1:
        yaml_dir = os.path.join(root_dir, "yaml")
        os.makedirs(yaml_dir, exist_ok=True)
        for f in yaml_files:
            shutil.move(f, os.path.join(yaml_dir, os.path.basename(f)))
        result["yaml_dir"] = yaml_dir
        log.info("Moved %d YAML files to %s", len(yaml_files), yaml_dir)

    return result


def cmd_report(args: argparse.Namespace) -> dict:
    """Generate HTML report + Excel workbook from an inventory JSON."""
    log.info("REPORT: input=%s, output=%s", args.input, getattr(args, 'output', None))
    start = time.time()

    _ensure_dependencies("report")

    from modules.output.inventory import load_inventory
    from modules.output.analysis import analyze_inventory
    from modules.output.html_report import generate_html_report
    from modules.output.excel_report import generate_excel_workbook

    inventory = load_inventory(args.input)

    # Run local analysis
    analysis = analyze_inventory(inventory)

    # AI-powered rationalization (optional)
    ai_recommendations = {}
    if getattr(args, 'ai', False):
        from modules.output.analysis import ai_rationalize_groups
        ai_recommendations = ai_rationalize_groups(
            analysis.get("parallel_definitions", []),
            model=getattr(args, 'ai_model', 'llama3.1-70b'),
            max_groups=getattr(args, 'ai_max_groups', 100),
        )

    # Business question generation (Eval Framework)
    from modules.output.analysis import analyze_report_narrative, generate_business_questions
    narrative = analyze_report_narrative(inventory, analysis)
    page_profiles = narrative.get("page_profiles", []) if narrative else []
    verified_queries = generate_business_questions(
        page_profiles, inventory,
        connection=getattr(args, 'connection', None),
        model=getattr(args, 'questions_model', 'claude-sonnet-4-6'),
    )

    # Resolve output directory (auto-named or explicit)
    output_dir = _resolve_output_dir(args, command="report")
    os.makedirs(output_dir, exist_ok=True)

    base = os.path.splitext(os.path.basename(args.input))[0]
    if base.endswith("_inventory"):
        base = base[:-len("_inventory")]

    html_path = os.path.join(output_dir, f"{base}_report.html")
    xlsx_path = os.path.join(output_dir, f"{base}_workbook.xlsx")

    generate_html_report(inventory, analysis, output_path=html_path,
                         source_path=args.input,
                         customer_name=getattr(args, 'customer', None),
                         source_label=getattr(args, 'source_label', None),
                         ai_recommendations=ai_recommendations,
                         verified_queries=verified_queries)
    generate_excel_workbook(inventory, analysis, output_path=xlsx_path,
                            verified_queries=verified_queries)

    # Copy inventory JSON alongside
    json_path = os.path.join(output_dir, f"{base}_inventory.json")
    if os.path.abspath(args.input) != os.path.abspath(json_path):
        shutil.copy2(args.input, json_path)

    # Organize into subfolders if multiple xlsx or yaml files
    folders = _organize_subfolders(output_dir)

    result = {
        "status": "ok",
        "command": "report",
        "output_dir": output_dir,
        "html_report": html_path,
        "excel_workbook": xlsx_path,
        "excel_dir": folders["excel_dir"],
        "yaml_dir": folders["yaml_dir"],
        "inventory_json": json_path,
        "analysis": analysis["stats"],
        "ai_recommendations_count": len(ai_recommendations),
        "verified_queries_count": len(verified_queries),
        "elapsed_seconds": round(time.time() - start, 2),
    }

    log.info("REPORT complete in %.2fs → %s", result["elapsed_seconds"], output_dir)
    return result


def cmd_compare(args: argparse.Namespace) -> dict:
    """Compare two inventory JSON files side-by-side."""
    log.info("COMPARE: a=%s, b=%s", args.inventory_a, args.inventory_b)
    start = time.time()

    from modules.output.inventory import load_inventory
    from modules.output.diff import diff_inventories, diff_to_json, diff_to_html

    inv_a = load_inventory(args.inventory_a)
    inv_b = load_inventory(args.inventory_b)

    diff = diff_inventories(
        inv_a, inv_b,
        label_a=args.label_a,
        label_b=args.label_b,
    )

    output_dir = args.output or os.getcwd()
    os.makedirs(output_dir, exist_ok=True)

    fmt = args.format
    paths = []

    if fmt in ("json", "both"):
        p = os.path.join(output_dir, "comparison.json")
        diff_to_json(diff, p)
        paths.append(p)

    if fmt in ("html", "both"):
        p = os.path.join(output_dir, "comparison.html")
        diff_to_html(diff, p)
        paths.append(p)

    result = {
        "status": "ok",
        "command": "compare",
        "summary": diff["summary"],
        "output_files": paths,
        "elapsed_seconds": round(time.time() - start, 2),
    }

    log.info("COMPARE complete in %.2fs", result["elapsed_seconds"])
    return result


def cmd_generate_from_workbook(args: argparse.Namespace) -> dict:
    """Generate Semantic View YAML from a customer-curated Excel workbook."""
    log.info("GENERATE-FROM-WORKBOOK: input=%s, output=%s", args.input, getattr(args, 'output', None))
    start = time.time()

    _ensure_dependencies("report")

    from modules.output.excel_reader import read_curated_workbook
    from modules.output.yaml_generator import generate_all_yamls

    output_dir = _resolve_output_dir(args, command="generate-from-workbook")
    inventory = read_curated_workbook(args.input, include_review=args.include_review)

    # Apply Snowflake target if specified
    sf_target = {}
    if args.database:
        sf_target["database"] = args.database
    if args.schema:
        sf_target["schema"] = args.schema
    if sf_target:
        inventory["snowflake_target"] = sf_target

    paths = generate_all_yamls(
        inventory,
        output_dir=output_dir,
        split_threshold=args.split_threshold,
    )

    # Move yaml files into subfolder if multiple
    folders = _organize_subfolders(output_dir)

    total_items = (
        len(inventory.get("dimensions", []))
        + len(inventory.get("facts", []))
        + len(inventory.get("metrics", []))
    )

    result = {
        "status": "ok",
        "command": "generate-from-workbook",
        "output_dir": output_dir,
        "yaml_dir": folders["yaml_dir"],
        "yaml_files": paths,
        "file_count": len(paths),
        "table_count": len(inventory.get("tables", [])),
        "total_items_included": total_items,
        "elapsed_seconds": round(time.time() - start, 2),
    }

    log.info("GENERATE-FROM-WORKBOOK complete: %d files in %.2fs", len(paths), result["elapsed_seconds"])
    return result


# ---------------------------------------------------------------------------
# PBI Server mode
# ---------------------------------------------------------------------------

def _cmd_parse_pbi_server(args: argparse.Namespace, start: float) -> dict:
    """Connect to PBI service, scan all workspaces, pull DMV data, merge."""
    from modules.powerbi.api_client import PowerBIApiClient
    from modules.output.inventory import build_unified_inventory, merge_inventories, save_inventory

    config_path = args.config
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    client = PowerBIApiClient(
        client_id=config.get("client_id", ""),
        client_secret=config.get("client_secret", ""),
        tenant_id=config.get("tenant_id", ""),
    )

    try:
        client.authenticate()
        workspaces = client.list_workspaces()
        log.info("PBI server mode: found %d workspaces", len(workspaces))

        inventories = []
        workspace_summary = []

        for ws in workspaces:
            ws_id = ws.get("id", "")
            ws_name = ws.get("name", "")
            log.info("Processing workspace: %s (%s)", ws_name, ws_id)

            try:
                datasets = client.list_datasets(ws_id)
                reports = client.list_reports(ws_id)
            except Exception as exc:
                log.warning("Skipping workspace %s: %s", ws_name, exc)
                workspace_summary.append({
                    "workspace": ws_name, "workspace_id": ws_id,
                    "status": "error", "error": str(exc),
                })
                continue

            # Build report_id → report_name lookup
            report_lookup = {r["id"]: r.get("name", r["id"]) for r in reports}

            for ds in datasets:
                ds_id = ds.get("id", "")
                ds_name = ds.get("name", "")
                source_label = f"{ws_name}/{ds_name}"
                log.info("  Dataset: %s (%s)", ds_name, ds_id)

                try:
                    # Pull DMV data
                    measures = client.get_all_measures(ws_id, ds_id)
                    columns = client.get_all_columns(ws_id, ds_id)
                    relationships = client.get_all_relationships(ws_id, ds_id)

                    # Build a parsed dict compatible with build_unified_inventory
                    parsed = _dmv_to_parsed(columns, measures, relationships, source_label)

                    # Get report pages for provenance
                    for report in reports:
                        if report.get("datasetId") == ds_id:
                            try:
                                pages = client.get_report_pages(ws_id, report["id"])
                                rname = report.get("name", "")
                                parsed.setdefault("report_pages", [])
                                for page in pages:
                                    parsed["report_pages"].append({
                                        "name": page.get("name", ""),
                                        "display_name": page.get("displayName", page.get("name", "")),
                                        "report_name": rname,
                                        "visuals": [],  # Page-level API doesn't return visuals
                                    })
                            except Exception as exc:
                                log.debug("Could not get pages for report %s: %s",
                                         report.get("name", ""), exc)

                    sf_target = {}
                    if args.database:
                        sf_target["database"] = args.database
                    if args.schema:
                        sf_target["schema"] = args.schema

                    inv = build_unified_inventory(parsed, "powerbi", sf_target or None)
                    # Tag every item with source_file for cross-workspace traceability
                    for key in ("dimensions", "facts", "metrics"):
                        for item in inv.get(key, []):
                            if not item.get("source_file"):
                                item["source_file"] = source_label
                            if not item.get("dashboard_name"):
                                item["dashboard_name"] = ws_name

                    inventories.append(inv)
                    workspace_summary.append({
                        "workspace": ws_name, "workspace_id": ws_id,
                        "dataset": ds_name, "dataset_id": ds_id,
                        "status": "ok",
                        "tables": len(inv.get("tables", [])),
                        "dimensions": len(inv.get("dimensions", [])),
                        "facts": len(inv.get("facts", [])),
                        "metrics": len(inv.get("metrics", [])),
                    })
                except Exception as exc:
                    log.warning("Failed dataset %s in %s: %s", ds_name, ws_name, exc)
                    workspace_summary.append({
                        "workspace": ws_name, "dataset": ds_name,
                        "status": "error", "error": str(exc),
                    })
    finally:
        client.close()

    if not inventories:
        return {
            "status": "error",
            "command": "parse",
            "failure": fail_step("parse", ExtractionError("No datasets found across workspaces")),
            "workspace_summary": workspace_summary,
        }

    # Merge all per-dataset inventories into one
    if len(inventories) == 1:
        inventory = inventories[0]
    else:
        inventory = merge_inventories(inventories)
    inventory["source_type"] = "powerbi"

    output_path = None
    if args.output:
        output_path = save_inventory(inventory, args.output)

    result = {
        "status": "ok",
        "command": "parse",
        "source_type": "powerbi",
        "mode": "server",
        "workspace_count": len(workspaces),
        "dataset_count": sum(1 for s in workspace_summary if s.get("status") == "ok"),
        "table_count": len(inventory.get("tables", [])),
        "dimension_count": len(inventory.get("dimensions", [])),
        "fact_count": len(inventory.get("facts", [])),
        "metric_count": len(inventory.get("metrics", [])),
        "flagged_count": len(inventory.get("flagged", [])),
        "complexity_summary": inventory.get("complexity_summary", {}),
        "errors": inventory.get("errors", []),
        "workspace_summary": workspace_summary,
        "output_path": output_path,
        "elapsed_seconds": round(time.time() - start, 2),
    }

    if not args.output:
        result["inventory"] = inventory

    log.info("PARSE (server) complete in %.2fs — %d workspaces, %d datasets",
             result["elapsed_seconds"], result["workspace_count"], result["dataset_count"])
    return result


def _dmv_to_parsed(columns: list, measures: list, relationships: list,
                   source_label: str) -> dict:
    """Convert DMV query results into a parsed dict compatible with _from_powerbi()."""
    # Group columns by table
    tables_dict: dict[str, dict] = {}
    for col in columns:
        tname = col.get("table_name", col.get("TableName", ""))
        if tname not in tables_dict:
            tables_dict[tname] = {"name": tname, "columns": [], "source_query": None}
        tables_dict[tname]["columns"].append({
            "name": col.get("column_name", col.get("ColumnName", "")),
            "source_column": col.get("column_name", col.get("ColumnName", "")),
            "data_type": col.get("data_type", col.get("DataType", "string")),
            "is_hidden": col.get("is_hidden", col.get("IsHidden", False)),
        })

    parsed_measures = []
    for m in measures:
        parsed_measures.append({
            "name": m.get("measure_name", m.get("MeasureName", "")),
            "table": m.get("table_name", m.get("TableName", "")),
            "expression": m.get("expression", m.get("Expression", "")),
            "description": m.get("description", m.get("Description", "")),
        })

    parsed_rels = []
    for r in relationships:
        parsed_rels.append({
            "from_table": r.get("from_table", r.get("FromTableName", "")),
            "from_column": r.get("from_column", r.get("FromColumnName", "")),
            "to_table": r.get("to_table", r.get("ToTableName", "")),
            "to_column": r.get("to_column", r.get("ToColumnName", "")),
            "is_active": r.get("is_active", r.get("IsActive", True)),
            "cardinality": r.get("cardinality", r.get("Cardinality", "many_to_one")),
        })

    return {
        "tables": list(tables_dict.values()),
        "measures": parsed_measures,
        "relationships": parsed_rels,
        "report_pages": [],
        "source_label": source_label,
    }


# ---------------------------------------------------------------------------
# Router helpers
# ---------------------------------------------------------------------------

def _run_parser(path: str, source_type: str) -> dict:
    """Route to the correct source parser."""
    _ensure_dependencies(source_type)

    if source_type == "tableau":
        from modules.tableau.parser import parse_workbook
        return parse_workbook(path)
    elif source_type == "looker":
        from modules.looker.parser import parse_lookml_project
        return parse_lookml_project(path)
    elif source_type == "powerbi":
        from modules.powerbi.parser import parse_model
        return parse_model(path)
    elif source_type == "denodo":
        from modules.denodo.vql_parser import parse_vql_file
        return parse_vql_file(path)
    elif source_type == "businessobjects":
        from modules.businessobjects.parser import load_bo_json
        parsed = load_bo_json(path)
        from modules.businessobjects.parser import extract_bo_inventory
        return extract_bo_inventory(parsed)
    elif source_type == "cognos":
        from modules.cognos.parser import parse_framework_manager_model
        return parse_framework_manager_model(path)
    else:
        raise ExtractionError(f"Unknown source type: {source_type}")


def _get_classifier(source_type: str):
    """Get the complexity classifier function for a source type."""
    try:
        if source_type == "tableau":
            from modules.tableau.classifier import classify_tableau_complexity
            return lambda item: classify_tableau_complexity(item.get("formula", ""))
        elif source_type == "looker":
            from modules.looker.classifier import classify_lookml_complexity
            return lambda item: classify_lookml_complexity(item)
        elif source_type == "powerbi":
            from modules.powerbi.dax_classifier import classify_dax_complexity
            return lambda item: classify_dax_complexity(item.get("expression", ""))
        elif source_type == "denodo":
            from modules.denodo.classifier import classify_denodo_complexity
            return lambda item: classify_denodo_complexity(item)
        elif source_type == "cognos":
            from modules.cognos.classifier import classify_cognos_complexity
            return lambda item: classify_cognos_complexity(item)
        elif source_type == "businessobjects":
            from modules.businessobjects.classifier import classify_bo_complexity
            return lambda item: classify_bo_complexity(
                item.get("select_expression", ""), item.get("where_expression")
            )
    except ImportError:
        log.warning("Classifier not available for %s", source_type)
    return None


def _get_api_client(source_type: str, args: argparse.Namespace):
    """Instantiate an API client for connection testing."""
    config_path = getattr(args, "config", None)
    config = {}
    if config_path:
        with open(config_path) as f:
            config = json.load(f)

    if source_type == "tableau":
        from modules.tableau.api_client import TableauApiClient
        return TableauApiClient(
            server_url=config.get("server_url", ""),
            token_name=config.get("token_name", ""),
            token_secret=config.get("token_secret", ""),
            site_name=config.get("site_name", ""),
        )
    elif source_type == "looker":
        from modules.looker.api_client import LookerApiClient
        return LookerApiClient(
            base_url=config.get("base_url", ""),
            client_id=config.get("client_id", ""),
            client_secret=config.get("client_secret", ""),
        )
    elif source_type == "powerbi":
        from modules.powerbi.api_client import PowerBIApiClient
        return PowerBIApiClient(
            client_id=config.get("client_id", ""),
            client_secret=config.get("client_secret", ""),
            tenant_id=config.get("tenant_id", ""),
        )
    elif source_type == "denodo":
        from modules.denodo.vdp_client import VDPClient
        return VDPClient(
            host=config.get("host", ""),
            port=config.get("port", 9999),
            database=config.get("database", ""),
            username=config.get("username", ""),
            password=config.get("password", ""),
        )
    elif source_type == "businessobjects":
        from modules.businessobjects.api_client import BOApiClient
        return BOApiClient(
            base_url=config.get("base_url", ""),
            username=config.get("username", ""),
            password=config.get("password", ""),
        )
    else:
        raise ExtractionError(f"No API client for source type: {source_type}")


# ---------------------------------------------------------------------------
# seed-data command
# ---------------------------------------------------------------------------

def cmd_seed_data(args: argparse.Namespace) -> dict:
    """Generate seed-data SQL from an inventory."""
    log.info("SEED-DATA: input=%s, output=%s", args.input, getattr(args, 'output', None))
    start = time.time()

    from modules.output.inventory import load_inventory
    from modules.output.sample_data import (
        assess_inventory, generate_seed_sql, format_assessment_table,
    )

    inventory = load_inventory(args.input)

    # Override snowflake_target if CLI args provided
    if args.database or args.schema:
        sf = inventory.setdefault("snowflake_target", {})
        if args.database:
            sf["database"] = args.database
        if args.schema:
            sf["schema"] = args.schema

    # Assess-only mode
    if args.assess_only:
        assessment = assess_inventory(inventory)
        print(format_assessment_table(assessment), file=sys.stderr)
        return {
            "status": "ok",
            "command": "seed-data",
            "mode": "assess-only",
            "assessment": {
                "source_type": assessment["source_type"],
                "total_tables": assessment["total_tables"],
                "total_columns": assessment["total_columns"],
                "pages": assessment["pages"],
                "items_with_pages": assessment["items_with_pages"],
                "items_without_pages": assessment["items_without_pages"],
            },
            "elapsed_seconds": round(time.time() - start, 2),
        }

    # Parse page/table filters
    pages = [p.strip() for p in args.pages.split(",")] if args.pages else None
    tables = [t.strip() for t in args.tables.split(",")] if args.tables else None

    result_data = generate_seed_sql(
        inventory,
        rows_per_table=args.rows,
        pages=pages,
        tables=tables,
        include_all=args.all,
        include_facts=getattr(args, 'include_facts', False),
        fact_row_multiplier=getattr(args, 'fact_multiplier', 5),
    )

    if not result_data["statements"]:
        return {
            "status": "ok",
            "command": "seed-data",
            "warning": "No tables in scope. Use --all, --pages, or --tables.",
            "elapsed_seconds": round(time.time() - start, 2),
        }

    # Write SQL file
    output_dir = _resolve_output_dir(args, command="seed-data")
    os.makedirs(output_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(args.input))[0]
    sql_path = os.path.join(output_dir, f"{base}_seed.sql")
    with open(sql_path, "w") as f:
        f.write("\n".join(result_data["statements"]))
    log.info("Wrote seed SQL to %s", sql_path)

    # Execute if requested
    executed = False
    exec_results = []
    if args.execute:
        from modules.output.analysis import _get_snowflake_connection
        conn = _get_snowflake_connection(
            connection_name=getattr(args, 'connection', None)
        )
        try:
            cur = conn.cursor()
            for stmt in result_data["statements"]:
                stmt = stmt.strip()
                if not stmt or stmt.startswith("--"):
                    continue
                try:
                    cur.execute(stmt)
                    exec_results.append({"sql": stmt[:80], "status": "ok"})
                    log.info("Executed: %s...", stmt[:60])
                except Exception as e:
                    exec_results.append({"sql": stmt[:80], "status": "error",
                                          "error": str(e)})
                    log.error("Failed: %s — %s", stmt[:60], e)
        finally:
            conn.close()
        executed = True

    result = {
        "status": "ok",
        "command": "seed-data",
        "output_dir": output_dir,
        "sql_file": sql_path,
        "tables_seeded": len(result_data["tables_seeded"]),
        "total_columns": sum(t["columns"] for t in result_data["tables_seeded"]),
        "rows_per_table": args.rows,
        "executed": executed,
        "elapsed_seconds": round(time.time() - start, 2),
    }
    if executed:
        result["exec_results"] = exec_results

    log.info("SEED-DATA complete: %d tables in %.2fs",
             len(result_data["tables_seeded"]), result["elapsed_seconds"])
    return result


# ---------------------------------------------------------------------------
# si-agent command
# ---------------------------------------------------------------------------

def cmd_si_agent(args: argparse.Namespace) -> dict:
    """Generate Snowflake Intelligence agent artifacts from an inventory."""
    log.info("SI-AGENT: input=%s, output=%s", args.input, getattr(args, 'output', None))
    start = time.time()

    from modules.output.inventory import load_inventory
    from modules.output.si_agent import (
        assess_for_si, generate_agent_spec, generate_deployment_sql,
        group_tables_by_domain, format_si_assessment,
    )

    inventory = load_inventory(args.input)

    # Override snowflake_target if CLI args provided
    if args.database or args.schema:
        sf = inventory.setdefault("snowflake_target", {})
        if args.database:
            sf["database"] = args.database
        if args.schema:
            sf["schema"] = args.schema

    # Parse explicit domain mapping if provided
    explicit_domains = None
    if args.domains:
        explicit_domains = json.loads(args.domains)

    # Assess-only mode
    if args.assess_only:
        assessment = assess_for_si(inventory)
        print(format_si_assessment(assessment), file=sys.stderr)
        return {
            "status": "ok",
            "command": "si-agent",
            "mode": "assess-only",
            "assessment": {
                "source_type": assessment["source_type"],
                "total_tables": assessment["total_tables"],
                "total_columns": assessment["total_columns"],
                "domain_count": assessment["domain_count"],
                "domains": assessment["domains"],
                "feasible": assessment["feasible"],
                "warnings": assessment["warnings"],
            },
            "elapsed_seconds": round(time.time() - start, 2),
        }

    sf = inventory.get("snowflake_target", {})
    db = sf.get("database", "TARGET_DB")
    sch = sf.get("schema", "PUBLIC")
    agent_name = args.agent_name or "BI_ANALYTICS_AGENT"

    # Resolve domains
    domains = group_tables_by_domain(inventory, explicit_domains)

    # Generate agent spec
    spec = generate_agent_spec(
        inventory,
        agent_name=agent_name,
        database=db,
        schema=sch,
        domains=domains,
    )

    # Generate deployment SQL
    statements = generate_deployment_sql(
        inventory,
        agent_name=agent_name,
        database=db,
        schema=sch,
        domains=domains,
    )

    # Write outputs
    output_dir = _resolve_output_dir(args, command="si-agent")
    os.makedirs(output_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(args.input))[0]
    if base.endswith("_inventory"):
        base = base[:-len("_inventory")]

    # Agent spec JSON
    spec_path = os.path.join(output_dir, f"{base}_agent_spec.json")
    with open(spec_path, "w") as f:
        json.dump(spec, f, indent=2)
    log.info("Wrote agent spec to %s", spec_path)

    # Deployment SQL
    sql_path = os.path.join(output_dir, f"{base}_si_deploy.sql")
    with open(sql_path, "w") as f:
        f.write("\n".join(statements))
    log.info("Wrote deployment SQL to %s", sql_path)

    # Execute if requested
    executed = False
    exec_results = []
    if args.execute:
        from modules.output.analysis import _get_snowflake_connection
        conn = _get_snowflake_connection(
            connection_name=getattr(args, 'connection', None)
        )
        try:
            cur = conn.cursor()
            for stmt in statements:
                stmt = stmt.strip()
                if not stmt or stmt.startswith("--"):
                    continue
                try:
                    cur.execute(stmt)
                    exec_results.append({"sql": stmt[:80], "status": "ok"})
                    log.info("Executed: %s...", stmt[:60])
                except Exception as e:
                    exec_results.append({"sql": stmt[:80], "status": "error",
                                          "error": str(e)})
                    log.error("Failed: %s — %s", stmt[:60], e)
        finally:
            conn.close()
        executed = True

    result = {
        "status": "ok",
        "command": "si-agent",
        "output_dir": output_dir,
        "agent_spec": spec_path,
        "deployment_sql": sql_path,
        "agent_name": f"{db}.{sch}.{agent_name}",
        "domain_count": len(domains),
        "domains": list(domains.keys()),
        "executed": executed,
        "elapsed_seconds": round(time.time() - start, 2),
    }
    if executed:
        result["exec_results"] = exec_results

    log.info("SI-AGENT complete: %d domains in %.2fs",
             len(domains), result["elapsed_seconds"])
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="semantic-extraction",
        description="Semantic extraction automation for CoCo.",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- crawl ---
    p_crawl = subparsers.add_parser("crawl", help="Discover source files.")
    p_crawl.add_argument("path", help="Directory to crawl.")
    p_crawl.add_argument("--type", required=True,
                         choices=["tableau", "looker", "powerbi", "denodo", "businessobjects", "cognos"],
                         help="Source type to filter for.")
    p_crawl.add_argument("--max-depth", type=int, default=10)
    p_crawl.add_argument("--follow-symlinks", action="store_true")

    # --- parse ---
    p_parse = subparsers.add_parser("parse", help="Parse source files into inventory.")
    p_parse.add_argument("path", nargs="?", default="",
                         help="File or directory to parse (required for --mode file).")
    p_parse.add_argument("--type", required=True,
                         choices=["tableau", "looker", "powerbi", "denodo", "businessobjects", "cognos"])
    p_parse.add_argument("--mode", choices=["file", "server"], default="file",
                         help="Parse local files or connect to a live server (default: file).")
    p_parse.add_argument("--config",
                         help="JSON config file with server credentials (required for --mode server).")
    p_parse.add_argument("--output", "-o", help="Save inventory JSON to this path.")
    p_parse.add_argument("--database", help="Target Snowflake database.")
    p_parse.add_argument("--schema", help="Target Snowflake schema.")

    # --- classify ---
    p_classify = subparsers.add_parser("classify", help="Classify inventory complexity.")
    p_classify.add_argument("input", help="Inventory JSON file.")
    p_classify.add_argument("--output", "-o", help="Save updated inventory to this path.")

    # --- generate ---
    p_generate = subparsers.add_parser("generate", help="Generate Semantic View YAML.")
    p_generate.add_argument("input", help="Inventory JSON file.")
    p_generate.add_argument("--output", "-o", help="Output directory (default: auto-named under ~/Downloads/).")
    p_generate.add_argument("--customer", help="Customer name for auto-naming output folder.")
    p_generate.add_argument("--split-threshold", type=int, default=100,
                            help="Max columns per view before splitting.")

    # --- test-connection ---
    p_test = subparsers.add_parser("test-connection", help="Test source connectivity.")
    p_test.add_argument("--type", required=True,
                        choices=["tableau", "looker", "powerbi", "denodo", "businessobjects", "cognos"])
    p_test.add_argument("--config", required=True, help="JSON config file with credentials.")

    # --- report ---
    p_report = subparsers.add_parser("report", help="Generate HTML report + Excel workbook.")
    p_report.add_argument("input", help="Inventory JSON file.")
    p_report.add_argument("--output", "-o", help="Output directory (default: auto-named under ~/Downloads/).")
    p_report.add_argument("--customer", help="Customer name for report header (e.g. 'Sharp Healthcare').")
    p_report.add_argument("--source-label", help="Source description (e.g. 'Sharp LookML All Projects - 31 Projects').")
    p_report.add_argument("--ai", action="store_true",
                          help="Enable AI-powered rationalization via Snowflake Cortex.")
    p_report.add_argument("--ai-model", default="llama3.1-70b",
                          help="Cortex LLM model for AI analysis (default: llama3.1-70b).")
    p_report.add_argument("--ai-max-groups", type=int, default=100,
                          help="Max groups to send to AI (cost control, default: 100).")
    p_report.add_argument("--connection", default=None,
                          help="Snowflake connection name for Cortex Complete question generation.")
    p_report.add_argument("--questions-model", default="claude-sonnet-4-6",
                          help="Cortex LLM model for business question generation (default: claude-sonnet-4-6).")

    # --- compare ---
    p_compare = subparsers.add_parser("compare", help="Compare two inventory JSON files.")
    p_compare.add_argument("inventory_a", help="First inventory JSON file.")
    p_compare.add_argument("inventory_b", help="Second inventory JSON file.")
    p_compare.add_argument("--output", "-o", help="Output directory (default: cwd).")
    p_compare.add_argument("--format", choices=["json", "html", "both"], default="both",
                           help="Output format (default: both).")
    p_compare.add_argument("--label-a", default="Parser A",
                           help="Label for first inventory (default: 'Parser A').")
    p_compare.add_argument("--label-b", default="Parser B",
                           help="Label for second inventory (default: 'Parser B').")

    # --- generate-from-workbook ---
    p_gfw = subparsers.add_parser("generate-from-workbook",
                                   help="Generate YAML from curated Excel workbook.")
    p_gfw.add_argument("input", help="Curated Excel workbook (.xlsx).")
    p_gfw.add_argument("--output", "-o", help="Output directory (default: auto-named under ~/Downloads/).")
    p_gfw.add_argument("--customer", help="Customer name for auto-naming output folder.")
    p_gfw.add_argument("--database", help="Target Snowflake database.")
    p_gfw.add_argument("--schema", help="Target Snowflake schema.")
    p_gfw.add_argument("--split-threshold", type=int, default=100,
                        help="Max columns per view before splitting.")
    p_gfw.add_argument("--include-review", action="store_true",
                        help="Include items marked 'Review' (default: only 'Yes').")

    # --- seed-data ---
    p_seed = subparsers.add_parser("seed-data",
                                    help="Generate seed-data SQL from inventory.")
    p_seed.add_argument("input", help="Inventory JSON file.")
    p_seed.add_argument("--output", "-o",
                        help="Output directory (default: auto-named under ~/Downloads/).")
    p_seed.add_argument("--customer", help="Customer name for auto-naming output folder.")
    p_seed.add_argument("--rows", type=int, default=100,
                        help="Rows per table (default: 100).")
    p_seed.add_argument("--pages",
                        help="Comma-separated page names to scope seed data to.")
    p_seed.add_argument("--tables",
                        help="Comma-separated table names to scope seed data to.")
    p_seed.add_argument("--include-facts", action="store_true",
                        help="Include fact tables with all columns plus FK-referenced "
                             "dimension tables. Recommended for dashboard-representative data.")
    p_seed.add_argument("--fact-multiplier", type=int, default=5,
                        help="Row multiplier for fact tables vs dimension tables (default: 5).")
    p_seed.add_argument("--all", action="store_true",
                        help="Seed every column, not just page-referenced ones.")
    p_seed.add_argument("--assess-only", action="store_true",
                        help="Print assessment table only, do not generate SQL.")
    p_seed.add_argument("--execute", action="store_true",
                        help="Execute generated SQL against Snowflake.")
    p_seed.add_argument("--connection",
                        help="Snowflake connection name for --execute.")
    p_seed.add_argument("--database", help="Target Snowflake database (override).")
    p_seed.add_argument("--schema", help="Target Snowflake schema (override).")

    # --- si-agent ---
    p_si = subparsers.add_parser("si-agent",
                                  help="Generate Snowflake Intelligence agent artifacts.")
    p_si.add_argument("input", help="Inventory JSON file.")
    p_si.add_argument("--output", "-o",
                      help="Output directory (default: auto-named under ~/Downloads/).")
    p_si.add_argument("--customer", help="Customer name for auto-naming output folder.")
    p_si.add_argument("--agent-name",
                      help="Cortex Agent name (default: BI_ANALYTICS_AGENT).")
    p_si.add_argument("--database", help="Target Snowflake database.")
    p_si.add_argument("--schema", help="Target Snowflake schema.")
    p_si.add_argument("--domains",
                      help='Explicit domain→tables JSON, e.g. \'{"Staffing": ["DIM_STAFF", "FACT_SHIFTS"]}\'')
    p_si.add_argument("--assess-only", action="store_true",
                      help="Print domain assessment only, do not generate artifacts.")
    p_si.add_argument("--execute", action="store_true",
                      help="Execute deployment SQL against Snowflake.")
    p_si.add_argument("--connection",
                      help="Snowflake connection name for --execute.")

    args = parser.parse_args()

    # Initialize logging
    setup_logging(level=args.log_level)
    log.info("semantic-extraction CLI invoked: %s", args.command)

    # Route to command
    commands = {
        "crawl": cmd_crawl,
        "parse": cmd_parse,
        "classify": cmd_classify,
        "generate": cmd_generate,
        "test-connection": cmd_test_connection,
        "report": cmd_report,
        "compare": cmd_compare,
        "generate-from-workbook": cmd_generate_from_workbook,
        "seed-data": cmd_seed_data,
        "si-agent": cmd_si_agent,
    }

    try:
        result = commands[args.command](args)
    except Exception as e:
        result = {
            "status": "error",
            "command": args.command,
            "failure": fail_step(args.command, e),
        }

    # Output JSON to stdout
    print(json.dumps(result, indent=2, default=str))
    sys.exit(0 if result.get("status") == "ok" else 1)


if __name__ == "__main__":
    main()
