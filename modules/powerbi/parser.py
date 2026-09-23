"""Power BI model parser — supports .pbit, .pbip, .bim, and .pbix formats."""

import codecs
import json
import os
import zipfile
from pathlib import Path
from typing import Any

from ..common.errors import ParseError, fail_step
from ..common.logger import get_logger
from .dax_classifier import classify_all_measures
from .m_resolver import resolve_all_sources

log = get_logger("powerbi.parser")

# Security limits for archive processing
_MAX_UNCOMPRESSED_SIZE = 1_073_741_824  # 1 GB
_MAX_MEMBER_COUNT = 10_000

# Internal Power BI system tables to skip
_INTERNAL_TABLE_PREFIXES = ("DateTableTemplate", "LocalDateTable")


# ---------------------------------------------------------------------------
# Format-specific extractors
# ---------------------------------------------------------------------------


def extract_from_pbit(pbit_path: str) -> dict:
    """Extract model from a .pbit (Power BI template) file.

    Unzips the archive, reads DataModelSchema (handling BOM), and parses JSON.

    Args:
        pbit_path: Absolute path to the .pbit file.

    Returns:
        Parsed semantic model dict from extract_semantics().
    """
    log.info("extract_from_pbit: entry — path=%s", pbit_path)
    errors: list[dict] = []

    try:
        if not zipfile.is_zipfile(pbit_path):
            raise ParseError(
                f"Not a valid zip archive: {pbit_path}",
                {"path": pbit_path},
            )

        with zipfile.ZipFile(pbit_path, "r") as zf:
            # Zip bomb protection
            members = zf.infolist()
            if len(members) > _MAX_MEMBER_COUNT:
                raise ParseError(
                    f"Archive has too many members ({len(members)} > {_MAX_MEMBER_COUNT})",
                    {"path": pbit_path, "member_count": len(members)},
                )
            total_size = sum(info.file_size for info in members)
            if total_size > _MAX_UNCOMPRESSED_SIZE:
                raise ParseError(
                    f"Archive uncompressed size exceeds limit "
                    f"({total_size} > {_MAX_UNCOMPRESSED_SIZE})",
                    {"path": pbit_path, "total_size": total_size},
                )

            names = zf.namelist()
            log.debug("extract_from_pbit: archive members=%s", names)

            schema_name = next(
                (n for n in names if n == "DataModelSchema"),
                None,
            )
            if schema_name is None:
                raise ParseError(
                    "DataModelSchema not found in .pbit archive",
                    {"path": pbit_path, "member_count": len(names)},
                )

            raw_bytes = zf.read(schema_name)

            # Decode with encoding auto-detection:
            # 1. UTF-16 with BOM
            # 2. UTF-8 with BOM
            # 3. UTF-16-LE without BOM (common in .pbit — null bytes interleaved)
            # 4. UTF-8 fallback
            if raw_bytes[:2] in (b"\xff\xfe", b"\xfe\xff"):
                raw_text = raw_bytes.decode("utf-16")
            elif raw_bytes[:3] == b"\xef\xbb\xbf":
                raw_text = raw_bytes.decode("utf-8-sig")
            elif len(raw_bytes) >= 2 and raw_bytes[1:2] == b"\x00":
                # UTF-16-LE without BOM — second byte is null for ASCII chars
                raw_text = raw_bytes.decode("utf-16-le")
            else:
                raw_text = raw_bytes.decode("utf-8")

            model_json = json.loads(raw_text)

            # Optionally grab report layout
            layout_json = None
            if "Report/Layout" in names:
                try:
                    layout_bytes = zf.read("Report/Layout")
                    if layout_bytes[:2] in (b"\xff\xfe", b"\xfe\xff"):
                        layout_text = layout_bytes.decode("utf-16")
                    elif len(layout_bytes) >= 2 and layout_bytes[1:2] == b"\x00":
                        layout_text = layout_bytes.decode("utf-16-le")
                    else:
                        layout_text = layout_bytes.decode("utf-8")
                    layout_json = json.loads(layout_text)
                except Exception as exc:
                    err = fail_step("extract_report_layout", exc)
                    errors.append(err)

    except (ParseError, Exception) as exc:
        if not isinstance(exc, ParseError):
            exc = ParseError(str(exc), {"path": pbit_path})
        raise exc

    result = extract_semantics(model_json)
    result["errors"].extend(errors)

    if layout_json is not None:
        try:
            result["report_pages"] = extract_report_pages(layout_json)
        except Exception as exc:
            err = fail_step("extract_report_pages", exc)
            result["errors"].append(err)

    log.info(
        "extract_from_pbit: exit — tables=%d, measures=%d, errors=%d",
        len(result.get("tables", [])),
        len(result.get("measures", [])),
        len(result.get("errors", [])),
    )
    return result


def extract_from_pbip(pbip_folder: str) -> dict:
    """Extract model from a .pbip project folder (Power BI Project format).

    Searches for model.bim recursively inside a SemanticModel subfolder.

    Args:
        pbip_folder: Root folder of the .pbip project.

    Returns:
        Parsed semantic model dict from extract_semantics().
    """
    log.info("extract_from_pbip: entry — folder=%s", pbip_folder)

    root = Path(pbip_folder)
    if not root.is_dir():
        raise ParseError(
            f"pbip_folder is not a directory: {pbip_folder}",
            {"folder": pbip_folder},
        )

    # Search for model.bim under any SemanticModel subfolder
    candidates = list(root.rglob("model.bim"))
    if not candidates:
        # Fallback: any .bim file in the tree
        candidates = list(root.rglob("*.bim"))

    if not candidates:
        raise ParseError(
            "No model.bim file found in pbip folder",
            {"folder": pbip_folder},
        )

    bim_path = str(candidates[0])
    log.debug("extract_from_pbip: using bim_path=%s", bim_path)

    result = extract_from_bim(bim_path)
    log.info(
        "extract_from_pbip: exit — tables=%d, measures=%d, errors=%d",
        len(result.get("tables", [])),
        len(result.get("measures", [])),
        len(result.get("errors", [])),
    )
    return result


def extract_from_bim(bim_path: str) -> dict:
    """Extract model from a .bim (Tabular Object Model JSON) file.

    Args:
        bim_path: Absolute path to the .bim file.

    Returns:
        Parsed semantic model dict from extract_semantics().
    """
    log.info("extract_from_bim: entry — path=%s", bim_path)

    try:
        with open(bim_path, "r", encoding="utf-8-sig") as fh:
            model_json = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ParseError(
            f"Invalid JSON in .bim file: {exc}",
            {"path": bim_path, "line": exc.lineno, "col": exc.colno},
        )
    except OSError as exc:
        raise ParseError(str(exc), {"path": bim_path})

    result = extract_semantics(model_json)
    log.info(
        "extract_from_bim: exit — tables=%d, measures=%d, errors=%d",
        len(result.get("tables", [])),
        len(result.get("measures", [])),
        len(result.get("errors", [])),
    )
    return result


def _try_pbitools_extract(pbix_path: str) -> dict | None:
    """Attempt to extract a .pbix using pbi-tools CLI (requires PBI Desktop).

    Returns parsed model dict on success, or None if pbi-tools is unavailable.
    """
    import shutil
    import subprocess
    import tempfile

    pbitools = shutil.which("pbi-tools") or shutil.which("pbi-tools.exe")
    if not pbitools:
        log.debug("_try_pbitools_extract: pbi-tools not found on PATH")
        return None

    log.info("_try_pbitools_extract: found pbi-tools at %s", pbitools)

    with tempfile.TemporaryDirectory(prefix="pbix_extract_") as tmpdir:
        try:
            result = subprocess.run(
                [pbitools, "extract", pbix_path, "-extractFolder", tmpdir],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                log.warning(
                    "_try_pbitools_extract: pbi-tools failed (rc=%d): %s",
                    result.returncode,
                    result.stderr[:500],
                )
                return None
        except (subprocess.TimeoutExpired, OSError) as exc:
            log.warning("_try_pbitools_extract: pbi-tools execution error: %s", exc)
            return None

        # pbi-tools extracts into a subfolder named after the file
        extract_root = Path(tmpdir)
        # Look for model.bim (generated by pbi-tools)
        bim_candidates = list(extract_root.rglob("model.bim"))
        if not bim_candidates:
            # Try generate-bim as a second step
            try:
                subprocess.run(
                    [pbitools, "generate-bim", str(extract_root)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                bim_candidates = list(extract_root.rglob("model.bim"))
            except (subprocess.TimeoutExpired, OSError):
                pass

        if not bim_candidates:
            # Look for database.json (older format)
            db_json = list(extract_root.rglob("database.json"))
            if db_json:
                bim_candidates = db_json

        if not bim_candidates:
            log.warning("_try_pbitools_extract: no model.bim found after extraction")
            return None

        bim_path = str(bim_candidates[0])
        log.info("_try_pbitools_extract: using extracted model at %s", bim_path)
        return extract_from_bim(bim_path)


def extract_from_pbix(pbix_path: str) -> dict:
    """Extract model from a .pbix file using PBIXRay, with pbi-tools fallback.

    Tries (in order):
      1. PBIXRay Python library (handles xpress9 decompression via prebuilt wheels)
      2. pbi-tools CLI (requires Power BI Desktop on the machine)

    Args:
        pbix_path: Absolute path to the .pbix file.

    Returns:
        Parsed semantic model dict from extract_semantics().

    Raises:
        ParseError: If all extraction methods fail.
    """
    log.info("extract_from_pbix: entry — path=%s", pbix_path)

    pbixray_error: str | None = None

    # --- Attempt 1: PBIXRay library ---
    try:
        from pbixray import PBIXRay  # type: ignore[import]

        model = PBIXRay(pbix_path)
    except ImportError as exc:
        pbixray_error = f"pbixray not installed: {exc}"
        log.warning("extract_from_pbix: %s — trying pbi-tools fallback", pbixray_error)
    except Exception as exc:
        pbixray_error = f"PBIXRay failed: {exc}"
        log.warning("extract_from_pbix: %s — trying pbi-tools fallback", pbixray_error)
    else:
        # PBIXRay succeeded — proceed with normal flow
        pbixray_error = None

    if pbixray_error is None:
        # PBIXRay succeeded — convert DataFrame API to TOM dict
        model_json = _pbixray_to_tom(model)
        log.info(
            "extract_from_pbix: built TOM dict — %d tables, %d relationships",
            len(model_json.get("model", {}).get("tables", [])),
            len(model_json.get("model", {}).get("relationships", [])),
        )
        result = extract_semantics(model_json)
    else:
        # --- Attempt 2: pbi-tools CLI fallback ---
        pbitools_result = _try_pbitools_extract(pbix_path)
        if pbitools_result is not None:
            log.info("extract_from_pbix: pbi-tools fallback succeeded")
            result = pbitools_result
        else:
            # Both methods failed
            raise ParseError(
                f"Cannot read .pbix file — pbixray failed ({pbixray_error}) "
                f"and pbi-tools is not available. Install pbixray: "
                f"pip install pbixray (requires no C compiler — prebuilt wheels "
                f"are available). Or install pbi-tools from "
                f"https://github.com/pbi-tools/pbi-tools/releases and ensure "
                f"Power BI Desktop is installed.",
                {"path": pbix_path, "pbixray_error": pbixray_error},
            )

    # --- Extract report layout for page/visual provenance ---
    # .pbix files are zip archives with a Report/Layout entry (same as .pbit).
    try:
        if zipfile.is_zipfile(pbix_path):
            with zipfile.ZipFile(pbix_path, "r") as zf:
                if "Report/Layout" in zf.namelist():
                    try:
                        layout_bytes = zf.read("Report/Layout")
                        if layout_bytes[:2] in (b"\xff\xfe", b"\xfe\xff"):
                            layout_text = layout_bytes.decode("utf-16")
                        elif len(layout_bytes) >= 2 and layout_bytes[1:2] == b"\x00":
                            layout_text = layout_bytes.decode("utf-16-le")
                        else:
                            layout_text = layout_bytes.decode("utf-8")
                        layout_json = json.loads(layout_text)
                        result["report_pages"] = extract_report_pages(layout_json)
                        log.info(
                            "extract_from_pbix: extracted %d report pages",
                            len(result["report_pages"]),
                        )
                    except Exception as exc:
                        err = fail_step("extract_report_layout", exc)
                        result["errors"].append(err)
    except Exception as exc:
        log.warning("extract_from_pbix: failed to read Report/Layout — %s", exc)

    log.info(
        "extract_from_pbix: exit — tables=%d, measures=%d, errors=%d",
        len(result.get("tables", [])),
        len(result.get("measures", [])),
        len(result.get("errors", [])),
    )
    return result


# Pandas-dtype → TOM dataType mapping (best effort)
_PANDAS_TO_TOM_DTYPE: dict[str, str] = {
    "int64": "int64",
    "Int64": "int64",
    "float64": "double",
    "Float64": "double",
    "bool": "boolean",
    "boolean": "boolean",
    "string": "string",
    "object": "string",
    "datetime64[ns]": "dateTime",
    "datetime64[ns, UTC]": "dateTime",
}


def _pbixray_to_tom(model: "PBIXRay") -> dict:  # type: ignore[name-defined]
    """Convert PBIXRay DataFrame API into a TOM-style JSON dict.

    Maps:
        model.schema           → tables[].columns[]
        model.dax_measures     → tables[].measures[]
        model.dax_columns      → calculated columns in tables[].columns[]
        model.power_query      → tables[].partitions[].source.expression
        model.relationships    → model.relationships[]
    """
    import pandas as pd  # already a dep of pbixray

    # -- Index measures by table --
    measures_by_table: dict[str, list[dict]] = {}
    if hasattr(model, "dax_measures") and model.dax_measures is not None:
        for _, row in model.dax_measures.iterrows():
            tbl = str(row.get("TableName", ""))
            entry = {
                "name": str(row.get("Name", "")),
                "expression": str(row.get("Expression", "") or ""),
                "displayFolder": str(row.get("DisplayFolder", "") or "") if row.get("DisplayFolder") is not None else None,
                "description": str(row.get("Description", "") or "") if row.get("Description") is not None else None,
            }
            measures_by_table.setdefault(tbl, []).append(entry)

    # -- Index calculated columns by table --
    calc_cols_by_table: dict[str, dict[str, str]] = {}
    if hasattr(model, "dax_columns") and model.dax_columns is not None:
        for _, row in model.dax_columns.iterrows():
            tbl = str(row.get("TableName", ""))
            col = str(row.get("ColumnName", ""))
            expr = str(row.get("Expression", "") or "")
            calc_cols_by_table.setdefault(tbl, {})[col] = expr

    # -- Index power query (M expressions) by table --
    pq_by_table: dict[str, str] = {}
    if hasattr(model, "power_query") and model.power_query is not None:
        for _, row in model.power_query.iterrows():
            tbl = str(row.get("TableName", ""))
            expr = str(row.get("Expression", "") or "")
            if tbl and expr:
                pq_by_table[tbl] = expr

    # -- Build tables from schema --
    table_names: list[str] = []
    if hasattr(model, "tables") and model.tables is not None:
        # model.tables is an ArrowStringArray or similar
        table_names = [str(t) for t in model.tables]

    # Group schema columns by table
    cols_by_table: dict[str, list[dict]] = {}
    if hasattr(model, "schema") and model.schema is not None:
        for _, row in model.schema.iterrows():
            tbl = str(row.get("TableName", ""))
            col_name = str(row.get("ColumnName", ""))
            pandas_dtype = str(row.get("PandasDataType", "string"))
            tom_dtype = _PANDAS_TO_TOM_DTYPE.get(pandas_dtype, "string")

            calc_expr = calc_cols_by_table.get(tbl, {}).get(col_name)
            col_entry: dict = {
                "name": col_name,
                "dataType": tom_dtype,
                "sourceColumn": col_name if not calc_expr else None,
            }
            if calc_expr:
                col_entry["type"] = "calculated"
                col_entry["expression"] = calc_expr
            cols_by_table.setdefault(tbl, []).append(col_entry)

    # Also add calculated columns that might not appear in schema
    for tbl, calc_map in calc_cols_by_table.items():
        existing_names = {c["name"] for c in cols_by_table.get(tbl, [])}
        for col_name, expr in calc_map.items():
            if col_name not in existing_names:
                cols_by_table.setdefault(tbl, []).append({
                    "name": col_name,
                    "dataType": "string",
                    "sourceColumn": None,
                    "type": "calculated",
                    "expression": expr,
                })

    # -- Assemble TOM tables --
    tom_tables: list[dict] = []
    all_table_names = set(table_names)
    # Also include tables from measures/power_query that might not be in schema
    all_table_names.update(measures_by_table.keys())
    all_table_names.update(pq_by_table.keys())

    for tbl_name in sorted(all_table_names):
        partitions: list[dict] = []
        if tbl_name in pq_by_table:
            partitions.append({
                "source": {"expression": pq_by_table[tbl_name]},
            })

        tom_tables.append({
            "name": tbl_name,
            "columns": cols_by_table.get(tbl_name, []),
            "measures": measures_by_table.get(tbl_name, []),
            "partitions": partitions,
        })

    # -- Relationships --
    tom_rels: list[dict] = []
    if hasattr(model, "relationships") and model.relationships is not None:
        for _, row in model.relationships.iterrows():
            cardinality = str(row.get("Cardinality", "M:1"))
            parts = cardinality.split(":")
            from_card = parts[0].lower() if parts else "many"
            to_card = parts[1].lower() if len(parts) > 1 else "one"
            # Normalize M → many, 1 → one
            from_card = "many" if from_card == "m" else from_card
            to_card = "many" if to_card == "m" else to_card

            cfb = str(row.get("CrossFilteringBehavior", "Single"))
            tom_rels.append({
                "fromTable": str(row.get("FromTableName", "")),
                "fromColumn": str(row.get("FromColumnName", "")),
                "toTable": str(row.get("ToTableName", "")),
                "toColumn": str(row.get("ToColumnName", "")),
                "fromCardinality": from_card,
                "toCardinality": to_card,
                "crossFilteringBehavior": cfb.lower() if cfb else "onedirection",
                "isActive": bool(row.get("IsActive", 1)),
            })

    return {"model": {"tables": tom_tables, "relationships": tom_rels}}


# ---------------------------------------------------------------------------
# Auto-detect dispatcher
# ---------------------------------------------------------------------------


def parse_model(path: str) -> dict:
    """Auto-detect Power BI file format and route to the correct extractor.

    Supported formats: .pbit, .pbip (folder), .bim, .pbix

    Args:
        path: Path to a .pbit/.pbix/.bim file or a .pbip project folder.

    Returns:
        {
            "tables": [...],        # with columns, measures, source_query
            "dimensions": [...],    # classified columns
            "measures": [...],      # DAX measures with complexity
            "relationships": [...], # from/to table+column, cardinality, etc.
            "flagged": [...],       # items needing manual review
            "report_pages": [...],  # if report layout was available
            "errors": [...]
        }
    """
    log.info("parse_model: entry — path=%s", path)

    p = Path(path)
    suffix = p.suffix.lower()

    if p.is_dir() or suffix == ".pbip":
        result = extract_from_pbip(path)
    elif suffix == ".pbit":
        result = extract_from_pbit(path)
    elif suffix == ".bim":
        result = extract_from_bim(path)
    elif suffix == ".pbix":
        result = extract_from_pbix(path)
    else:
        raise ParseError(
            f"Unrecognised Power BI format: {suffix}",
            {"path": path, "supported": [".pbit", ".pbip", ".bim", ".pbix"]},
        )

    log.info(
        "parse_model: exit — format=%s, tables=%d, measures=%d, relationships=%d, errors=%d",
        suffix or "folder",
        len(result.get("tables", [])),
        len(result.get("measures", [])),
        len(result.get("relationships", [])),
        len(result.get("errors", [])),
    )
    return result


# ---------------------------------------------------------------------------
# Core semantic extraction from TOM JSON
# ---------------------------------------------------------------------------


def extract_semantics(model_json: dict) -> dict:
    """Extract semantic metadata from a Tabular Object Model (TOM) JSON dict.

    Args:
        model_json: Parsed TOM JSON (root or the nested 'model' key).

    Returns:
        {
            "tables": list of table dicts,
            "dimensions": list of classified column dicts,
            "measures": list of measure dicts with DAX complexity,
            "relationships": list of relationship dicts,
            "flagged": list of items needing manual review,
            "report_pages": [],
            "errors": list of error dicts,
        }
    """
    log.info("extract_semantics: entry")

    # TOM JSON may have root key 'model' or be the model directly
    root = model_json.get("model", model_json)
    tables_raw: list[dict] = root.get("tables", [])
    relationships_raw: list[dict] = root.get("relationships", [])

    tables: list[dict] = []
    dimensions: list[dict] = []
    all_measures: list[dict] = []
    flagged: list[dict] = []
    errors: list[dict] = []

    # ---- Tables ----
    for tbl in tables_raw:
        tbl_name: str = tbl.get("name", "")

        # Skip internal Power BI system tables
        if any(tbl_name.startswith(p) for p in _INTERNAL_TABLE_PREFIXES):
            log.debug("extract_semantics: skipping internal table '%s'", tbl_name)
            continue

        is_calculated = bool(tbl.get("calculationGroup"))

        # Source query from partitions (first M expression found)
        source_query: str | None = None
        for partition in tbl.get("partitions", []):
            src = partition.get("source", {})
            expr = src.get("expression")
            if expr:
                if isinstance(expr, list):
                    expr = "\n".join(expr)
                source_query = expr
                break

        # Columns
        cols: list[dict] = []
        for col in tbl.get("columns", []):
            col_name = col.get("name", "")
            try:
                col_entry = {
                    "name": col_name,
                    "data_type": col.get("dataType", ""),
                    "source_column": col.get("sourceColumn"),
                    "summarize_by": col.get("summarizeBy", "default"),
                    "is_calculated": col.get("type") == "calculated",
                    "expression": col.get("expression"),
                    "description": col.get("description"),
                    "display_folder": col.get("displayFolder"),
                    "is_hidden": col.get("isHidden", False),
                    "table": tbl_name,
                }
                cols.append(col_entry)
                dimensions.append(col_entry)
            except Exception as exc:
                err = fail_step(f"extract_column:{tbl_name}.{col_name}", exc)
                errors.append(err)

        # Measures
        tbl_measures: list[dict] = []
        for meas in tbl.get("measures", []):
            meas_name = meas.get("name", "")
            try:
                expr = meas.get("expression", "")
                if isinstance(expr, list):
                    expr = "\n".join(expr)
                meas_entry = {
                    "name": meas_name,
                    "table": tbl_name,
                    "expression": expr,
                    "format_string": meas.get("formatString"),
                    "display_folder": meas.get("displayFolder"),
                    "description": meas.get("description"),
                    "is_hidden": meas.get("isHidden", False),
                }
                tbl_measures.append(meas_entry)
                all_measures.append(meas_entry)
            except Exception as exc:
                err = fail_step(f"extract_measure:{tbl_name}.{meas_name}", exc)
                errors.append(err)

        table_entry: dict[str, Any] = {
            "name": tbl_name,
            "columns": cols,
            "measures": tbl_measures,
            "source_query": source_query,
            "is_calculated": is_calculated,
            "description": tbl.get("description"),
        }
        tables.append(table_entry)

        if is_calculated:
            flagged.append({
                "type": "calculated_table",
                "table": tbl_name,
                "reason": "Calculated tables require manual mapping to Snowflake source",
            })

    # ---- DAX classification ----
    try:
        from .dax_classifier import classify_all_measures as _classify
        detail_map = _classify(tables)
        for m in all_measures:
            key = f"{m['table']}.{m['name']}"
            detail = detail_map.get(key)
            if detail is None:
                m["complexity"] = "simple"
                m["dax_complexity"] = "simple"
                continue
            tier = detail["tier"]
            m["complexity"] = tier          # inventory reads this key
            m["dax_complexity"] = tier       # keep for backward compat
            m["dax_detail"] = {
                "matched_patterns": detail["matched_patterns"],
                "categories": detail["categories"],
                "description": detail["description"],
                "recommendation": detail["recommendation"],
                "effort": detail["effort"],
                "hints": detail["hints"],
            }
            if tier == "manual_required":
                expr_str = m.get("expression", "")
                if isinstance(expr_str, list):
                    expr_str = "\n".join(expr_str)
                flagged.append({
                    "type": "complex_dax",
                    "table": m["table"],
                    "measure": m["name"],
                    "reason": detail["recommendation"],
                    "dax_patterns": ", ".join(detail["matched_patterns"]),
                    "dax_categories": ", ".join(detail["categories"]),
                    "description": detail["description"],
                    "effort": detail["effort"],
                    "expression_excerpt": (expr_str or "")[:300],
                })
            elif tier == "needs_translation":
                expr_str = m.get("expression", "")
                if isinstance(expr_str, list):
                    expr_str = "\n".join(expr_str)
                flagged.append({
                    "type": "translatable_dax",
                    "table": m["table"],
                    "measure": m["name"],
                    "reason": detail["recommendation"],
                    "dax_patterns": ", ".join(detail["matched_patterns"]),
                    "dax_categories": ", ".join(detail["categories"]),
                    "description": detail["description"],
                    "effort": detail["effort"],
                    "expression_excerpt": (expr_str or "")[:300],
                })
    except Exception as exc:
        err = fail_step("classify_dax_measures", exc)
        errors.append(err)

    # ---- M source resolution ----
    try:
        tables = resolve_all_sources(tables)
    except Exception as exc:
        err = fail_step("resolve_m_sources", exc)
        errors.append(err)

    # ---- Relationships ----
    relationships: list[dict] = []
    for rel in relationships_raw:
        try:
            rel_entry = {
                "from_table": rel.get("fromTable", ""),
                "from_column": rel.get("fromColumn", ""),
                "to_table": rel.get("toTable", ""),
                "to_column": rel.get("toColumn", ""),
                "cardinality": rel.get("fromCardinality", "many") + ":" + rel.get("toCardinality", "one"),
                "cross_filter_behavior": rel.get("crossFilteringBehavior", "oneDirection"),
                "is_active": rel.get("isActive", True),
            }
            relationships.append(rel_entry)
            if not rel_entry["is_active"]:
                flagged.append({
                    "type": "inactive_relationship",
                    "from": f"{rel_entry['from_table']}.{rel_entry['from_column']}",
                    "to": f"{rel_entry['to_table']}.{rel_entry['to_column']}",
                    "reason": "Inactive relationship — used via USERELATIONSHIP() in DAX",
                })
        except Exception as exc:
            err = fail_step("extract_relationship", exc)
            errors.append(err)

    result = {
        "tables": tables,
        "dimensions": dimensions,
        "measures": all_measures,
        "relationships": relationships,
        "flagged": flagged,
        "report_pages": [],
        "errors": errors,
    }

    log.info(
        "extract_semantics: exit — tables=%d, dims=%d, measures=%d, "
        "relationships=%d, flagged=%d, errors=%d",
        len(tables),
        len(dimensions),
        len(all_measures),
        len(relationships),
        len(flagged),
        len(errors),
    )
    return result


# ---------------------------------------------------------------------------
# Report layout
# ---------------------------------------------------------------------------


def extract_report_pages(layout_json: dict) -> list:
    """Parse Power BI report layout JSON into a list of page summaries.

    Traverses sections → visualContainers → config → projections to identify
    which fields/measures are used on each page.

    Args:
        layout_json: Parsed report layout dict (from Report/Layout in .pbit).

    Returns:
        List of page dicts:
        [{"name": str, "display_name": str, "visuals": [{"type": str, "fields": [...]}]}]
    """
    log.info("extract_report_pages: entry")

    pages: list[dict] = []
    sections = layout_json.get("sections", [])

    for section in sections:
        page_name = section.get("name", "")
        display_name = section.get("displayName", page_name)
        visuals: list[dict] = []

        for container in section.get("visualContainers", []):
            try:
                config_raw = container.get("config", "{}")
                if isinstance(config_raw, str):
                    config = json.loads(config_raw)
                else:
                    config = config_raw

                visual_type = (
                    config.get("singleVisual", {})
                    .get("visualType", "unknown")
                )

                # Extract projected field references
                projections = (
                    config.get("singleVisual", {})
                    .get("projections", {})
                )
                fields: list[str] = []
                for _role, items in projections.items():
                    if isinstance(items, list):
                        for item in items:
                            qe = item.get("queryRef", "")
                            if qe:
                                fields.append(qe)

                visuals.append({"type": visual_type, "fields": fields})
            except Exception as exc:
                log.warning(
                    "extract_report_pages: skipping malformed visualContainer in "
                    "page '%s': %s",
                    page_name,
                    exc,
                )

        pages.append({
            "name": page_name,
            "display_name": display_name,
            "visuals": visuals,
        })

    log.info("extract_report_pages: exit — pages=%d", len(pages))
    return pages
