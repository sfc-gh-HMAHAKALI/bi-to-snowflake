"""Parse SAP Business Objects exports.

Supports:
- BIAR archives (ZIP-based binary export format)
- JSON exports from starschema/business-objects-universe-extractor or manual IDT export
- BO REST API responses

Every function logs entry with parameters and exit with result summary.
Errors are wrapped in ParseError and do not abort processing of remaining items.
"""

import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from ..common.errors import ParseError, fail_step
from ..common.logger import get_logger

log = get_logger(__name__)

# Keys expected at the top level of a BO JSON export
_REQUIRED_JSON_KEYS = {"businessLayer", "dataFoundation"}
_OPTIONAL_JSON_KEYS = {"contexts", "connections", "derivedTables", "listOfValues"}


# ---------------------------------------------------------------------------
# extract_biar
# ---------------------------------------------------------------------------

def extract_biar(biar_path: str, output_dir: str | None = None) -> str:
    """Unzip a BIAR archive to a directory and return the extraction path.

    BIAR files are standard ZIP archives containing universe (.unv/.unx),
    report (.rpt), and connection definition files.

    Args:
        biar_path: Absolute path to the .biar file.
        output_dir: Target extraction directory. If None, a temp directory is
                    created (caller is responsible for cleanup).

    Returns:
        Absolute path to the directory containing extracted contents.

    Raises:
        ParseError: If the archive cannot be opened or is empty.
    """
    log.info("extract_biar: path=%s output_dir=%s", biar_path, output_dir)

    biar_path = str(Path(biar_path).expanduser().resolve())

    if not os.path.isfile(biar_path):
        raise ParseError(
            f"BIAR file not found: {biar_path}",
            context={"path": biar_path},
        )

    # Determine extraction target
    if output_dir is None:
        extract_to = tempfile.mkdtemp(prefix="biar_extract_")
        log.debug("extract_biar: created temp dir %s", extract_to)
    else:
        extract_to = str(Path(output_dir).expanduser().resolve())
        os.makedirs(extract_to, exist_ok=True)

    # Validate and extract
    if not zipfile.is_zipfile(biar_path):
        raise ParseError(
            f"File is not a valid ZIP/BIAR archive: {biar_path}",
            context={"path": biar_path},
        )

    try:
        with zipfile.ZipFile(biar_path, "r") as zf:
            members = zf.namelist()
            if not members:
                raise ParseError(
                    f"BIAR archive is empty: {biar_path}",
                    context={"path": biar_path},
                )
            log.debug("extract_biar: %d entries in archive", len(members))

            # Zip slip protection: validate all member paths before extraction
            real_dest = os.path.realpath(extract_to)
            for member in members:
                member_path = os.path.realpath(os.path.join(extract_to, member))
                if not member_path.startswith(real_dest + os.sep) and member_path != real_dest:
                    raise ParseError(
                        f"Zip slip detected: member '{member}' escapes extraction directory",
                        context={"path": biar_path, "member": member},
                    )

            # Zip bomb protection: check total uncompressed size
            total_size = sum(info.file_size for info in zf.infolist())
            max_size = 1_073_741_824  # 1 GB
            if total_size > max_size:
                raise ParseError(
                    f"BIAR archive uncompressed size ({total_size:,} bytes) exceeds limit ({max_size:,} bytes)",
                    context={"path": biar_path, "uncompressed_size": total_size},
                )

            zf.extractall(extract_to)
    except zipfile.BadZipFile as exc:
        raise ParseError(
            f"Corrupt BIAR archive: {biar_path} — {exc}",
            context={"path": biar_path},
        ) from exc
    except OSError as exc:
        raise ParseError(
            f"OS error while extracting BIAR: {exc}",
            context={"path": biar_path, "output_dir": extract_to},
        ) from exc

    file_count = sum(1 for _ in Path(extract_to).rglob("*") if _.is_file())
    log.info(
        "extract_biar: extracted %d files to %s",
        file_count,
        extract_to,
    )
    return extract_to


# ---------------------------------------------------------------------------
# load_bo_json
# ---------------------------------------------------------------------------

def load_bo_json(json_path: str) -> dict:
    """Load and validate a BO universe JSON export.

    Accepts exports from:
    - starschema/business-objects-universe-extractor
    - Manual IDT (Information Design Tool) JSON exports

    Args:
        json_path: Absolute path to the .json file.

    Returns:
        Parsed dict with at least ``businessLayer`` and ``dataFoundation`` keys.

    Raises:
        ParseError: If the file cannot be read, parsed, or fails structure validation.
    """
    log.info("load_bo_json: path=%s", json_path)

    json_path = str(Path(json_path).expanduser().resolve())

    if not os.path.isfile(json_path):
        raise ParseError(
            f"BO JSON file not found: {json_path}",
            context={"path": json_path},
        )

    # Try common encodings
    raw: str | None = None
    for encoding in ("utf-8", "utf-8-sig", "latin-1", "cp1252"):
        try:
            with open(json_path, "r", encoding=encoding) as fh:
                raw = fh.read()
            log.debug("load_bo_json: read with encoding=%s", encoding)
            break
        except UnicodeDecodeError:
            log.debug("load_bo_json: encoding %s failed, trying next", encoding)
            continue

    if raw is None:
        raise ParseError(
            f"Could not decode BO JSON file with any supported encoding: {json_path}",
            context={"path": json_path},
        )

    try:
        data: dict = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ParseError(
            f"Invalid JSON in {json_path}: {exc}",
            context={"path": json_path, "error": str(exc)},
        ) from exc

    if not isinstance(data, dict):
        raise ParseError(
            f"Expected top-level object in BO JSON, got {type(data).__name__}",
            context={"path": json_path},
        )

    missing = _REQUIRED_JSON_KEYS - data.keys()
    if missing:
        raise ParseError(
            f"BO JSON missing required keys: {sorted(missing)}",
            context={"path": json_path, "present_keys": sorted(data.keys())},
        )

    log.info(
        "load_bo_json: loaded successfully — keys=%s",
        sorted(data.keys()),
    )
    return data


# ---------------------------------------------------------------------------
# extract_bo_inventory
# ---------------------------------------------------------------------------

def extract_bo_inventory(bo_json: dict) -> dict:
    """Parse a universe JSON export into a normalized inventory.

    Args:
        bo_json: Dict returned by :func:`load_bo_json`.

    Returns:
        Normalized inventory dict:

        .. code-block:: python

            {
                "name": str,
                "connection": {...},
                "tables": [...],
                "joins": [...],
                "contexts": [...],
                "objects": [...],
                "derived_tables": [...],
                "lov_queries": [...],
                "errors": [...]
            }
    """
    log.info("extract_bo_inventory: starting extraction")

    errors: list[dict] = []

    # ---- Universe name -------------------------------------------------------
    name = (
        bo_json.get("universeName")
        or bo_json.get("name")
        or bo_json.get("businessLayer", {}).get("name", "")
        or "unknown"
    )
    log.debug("extract_bo_inventory: universe name=%s", name)

    # ---- Connection ----------------------------------------------------------
    connection: dict[str, Any] = {}
    try:
        raw_conn = (
            bo_json.get("connection")
            or (bo_json.get("connections") or [{}])[0]
            or {}
        )
        connection = {
            "name": raw_conn.get("name", ""),
            "type": raw_conn.get("type") or raw_conn.get("dbms", ""),
            "host": raw_conn.get("host") or raw_conn.get("server", ""),
            "database": raw_conn.get("database") or raw_conn.get("catalog", ""),
            "schema": raw_conn.get("schema", ""),
        }
    except Exception as exc:
        errors.append(
            fail_step("extract_connection", exc, partial_results=connection)
        )

    # ---- Data foundation: tables and joins -----------------------------------
    data_foundation = bo_json.get("dataFoundation") or {}
    tables = _extract_tables(data_foundation, errors)
    joins = _extract_joins(data_foundation, errors)

    # ---- Contexts ------------------------------------------------------------
    raw_contexts = bo_json.get("contexts") or data_foundation.get("contexts") or []
    contexts = _extract_contexts(raw_contexts, errors)

    # ---- Business layer objects ----------------------------------------------
    business_layer = bo_json.get("businessLayer") or {}
    objects = _extract_objects(business_layer, errors)

    # ---- Derived tables ------------------------------------------------------
    derived_tables = _extract_derived_tables(data_foundation, errors)

    # ---- List-of-values queries ----------------------------------------------
    lov_queries = _extract_lov_queries(bo_json, errors)

    inventory = {
        "name": name,
        "connection": connection,
        "tables": tables,
        "joins": joins,
        "contexts": contexts,
        "objects": objects,
        "derived_tables": derived_tables,
        "lov_queries": lov_queries,
        "errors": errors,
    }

    log.info(
        "extract_bo_inventory: done — name=%s tables=%d joins=%d objects=%d errors=%d",
        name,
        len(tables),
        len(joins),
        len(objects),
        len(errors),
    )
    return inventory


def _extract_tables(data_foundation: dict, errors: list) -> list[dict]:
    """Extract physical table definitions from dataFoundation."""
    raw_tables = (
        data_foundation.get("tables")
        or data_foundation.get("tablesMappings")
        or []
    )
    tables: list[dict] = []
    for raw in raw_tables:
        try:
            tables.append({
                "name": raw.get("name") or raw.get("tableName", ""),
                "alias": raw.get("alias", ""),
                "sql_name": (
                    raw.get("sqlName")
                    or raw.get("qualifiedName")
                    or raw.get("name", "")
                ),
                "is_derived": bool(raw.get("isDerived") or raw.get("derivedTableSQL")),
                "owner": raw.get("owner") or raw.get("schema", ""),
            })
        except Exception as exc:
            errors.append(fail_step("extract_table", exc, partial_results=raw))
    return tables


def _extract_joins(data_foundation: dict, errors: list) -> list[dict]:
    """Extract join definitions from dataFoundation."""
    raw_joins = data_foundation.get("joins") or data_foundation.get("links") or []
    joins: list[dict] = []
    for raw in raw_joins:
        try:
            joins.append({
                "expression": raw.get("expression") or raw.get("sql", ""),
                "table1": raw.get("table1") or raw.get("leftTable", ""),
                "column1": raw.get("column1") or raw.get("leftColumn", ""),
                "table2": raw.get("table2") or raw.get("rightTable", ""),
                "column2": raw.get("column2") or raw.get("rightColumn", ""),
                "cardinality": raw.get("cardinality", ""),
                "outer_join": raw.get("outerJoin") or raw.get("isOuter", False),
            })
        except Exception as exc:
            errors.append(fail_step("extract_join", exc, partial_results=raw))
    return joins


def _extract_contexts(raw_contexts: list, errors: list) -> list[dict]:
    """Extract context definitions."""
    contexts: list[dict] = []
    for raw in raw_contexts:
        try:
            contexts.append({
                "name": raw.get("name", ""),
                "description": raw.get("description", ""),
                "included_joins": raw.get("includedJoins") or raw.get("joins") or [],
                "excluded_joins": raw.get("excludedJoins") or [],
            })
        except Exception as exc:
            errors.append(fail_step("extract_context", exc, partial_results=raw))
    return contexts


def _extract_objects(business_layer: dict, errors: list) -> list[dict]:
    """Recursively extract business layer objects (dimensions, measures, filters)."""
    objects: list[dict] = []
    folders = business_layer.get("folders") or business_layer.get("classes") or []
    if not folders and business_layer.get("objects"):
        # Flat structure — no folder hierarchy
        _extract_objects_flat(
            business_layer.get("objects", []), "", objects, errors
        )
        return objects

    for folder in folders:
        try:
            _extract_folder(folder, "", objects, errors)
        except Exception as exc:
            errors.append(fail_step("extract_folder", exc, partial_results=folder))
    return objects


def _extract_folder(folder: dict, parent_path: str, objects: list, errors: list) -> None:
    """Recursively walk a folder/class node."""
    folder_name = folder.get("name") or folder.get("className", "")
    folder_path = f"{parent_path}\\{folder_name}" if parent_path else folder_name

    # Sub-folders / sub-classes
    sub_folders = (
        folder.get("subFolders")
        or folder.get("subClasses")
        or folder.get("classes")
        or []
    )
    for sub in sub_folders:
        try:
            _extract_folder(sub, folder_path, objects, errors)
        except Exception as exc:
            errors.append(fail_step("extract_subfolder", exc, partial_results=sub))

    # Objects inside this folder
    raw_objects = folder.get("objects") or folder.get("items") or []
    _extract_objects_flat(raw_objects, folder_path, objects, errors)


def _extract_objects_flat(
    raw_objects: list, folder_path: str, objects: list, errors: list
) -> None:
    """Extract individual BO objects from a flat list."""
    for raw in raw_objects:
        try:
            obj_type_raw = (
                raw.get("objectType")
                or raw.get("type")
                or raw.get("qualification")
                or ""
            ).lower()

            # Normalize object_type
            if obj_type_raw in ("dimension", "dim"):
                object_type = "Dimension"
            elif obj_type_raw in ("measure", "fact", "metric"):
                object_type = "Measure"
            elif obj_type_raw in ("detail", "attribute", "information"):
                object_type = "Detail"
            elif obj_type_raw in ("filter", "condition", "predefined_condition"):
                object_type = "Filter"
            else:
                object_type = obj_type_raw.capitalize() or "Unknown"

            objects.append({
                "name": raw.get("name") or raw.get("label", ""),
                "folder_path": folder_path,
                "object_type": object_type,
                "select_expression": (
                    raw.get("selectExpression")
                    or raw.get("select")
                    or raw.get("expression")
                    or ""
                ),
                "where_expression": (
                    raw.get("whereExpression")
                    or raw.get("where")
                    or raw.get("filter")
                    or ""
                ),
                "data_type": raw.get("dataType") or raw.get("type", ""),
                "description": raw.get("description") or raw.get("comment", ""),
                "qualification": (
                    raw.get("qualification")
                    or obj_type_raw
                    or ""
                ),
            })
        except Exception as exc:
            errors.append(fail_step("extract_object", exc, partial_results=raw))


def _extract_derived_tables(data_foundation: dict, errors: list) -> list[dict]:
    """Extract derived table SQL definitions."""
    raw_dts = (
        data_foundation.get("derivedTables")
        or data_foundation.get("derivedTable")
        or []
    )
    if isinstance(raw_dts, dict):
        raw_dts = [raw_dts]
    derived: list[dict] = []
    for raw in raw_dts:
        try:
            derived.append({
                "name": raw.get("name") or raw.get("alias", ""),
                "sql": raw.get("sql") or raw.get("expression") or raw.get("query", ""),
            })
        except Exception as exc:
            errors.append(fail_step("extract_derived_table", exc, partial_results=raw))
    return derived


def _extract_lov_queries(bo_json: dict, errors: list) -> list[dict]:
    """Extract list-of-values (LOV) queries."""
    raw_lovs = bo_json.get("listOfValues") or bo_json.get("lovs") or []
    lovs: list[dict] = []
    for raw in raw_lovs:
        try:
            lovs.append({
                "name": raw.get("name", ""),
                "sql": raw.get("sql") or raw.get("query") or raw.get("expression", ""),
                "object_name": raw.get("objectName") or raw.get("associatedObject", ""),
            })
        except Exception as exc:
            errors.append(fail_step("extract_lov", exc, partial_results=raw))
    return lovs


# ---------------------------------------------------------------------------
# extract_from_rest
# ---------------------------------------------------------------------------

def extract_from_rest(objects: list[dict]) -> dict:
    """Parse BO REST API response objects into a normalized inventory.

    The REST API returns objects with a different schema than IDT JSON exports.
    This function normalizes REST responses into the same format as
    :func:`extract_bo_inventory`.

    Args:
        objects: List of object dicts from a BO REST API response.

    Returns:
        Normalized inventory dict with the same structure as
        :func:`extract_bo_inventory`. Connection, tables, joins, and derived
        tables will be empty (REST objects do not carry this metadata).
    """
    log.info("extract_from_rest: parsing %d REST objects", len(objects))

    errors: list[dict] = []
    normalized: list[dict] = []

    for raw in objects:
        try:
            # REST API uses different field names
            obj_type_raw = (
                raw.get("qualification")
                or raw.get("objectType")
                or raw.get("type", "")
            ).lower()

            if obj_type_raw in ("dimension", "0"):
                object_type = "Dimension"
            elif obj_type_raw in ("measure", "1"):
                object_type = "Measure"
            elif obj_type_raw in ("detail", "2", "attribute"):
                object_type = "Detail"
            elif obj_type_raw in ("filter", "predefined_condition", "3"):
                object_type = "Filter"
            else:
                object_type = obj_type_raw.capitalize() or "Unknown"

            # REST API path is typically "Folder\SubFolder\ObjectName"
            full_path = raw.get("path") or raw.get("fullPath") or ""
            path_parts = full_path.rsplit("\\", 1)
            folder_path = path_parts[0] if len(path_parts) > 1 else ""
            obj_name = raw.get("name") or (path_parts[-1] if path_parts else "")

            normalized.append({
                "name": obj_name,
                "folder_path": folder_path,
                "object_type": object_type,
                "select_expression": (
                    raw.get("select")
                    or raw.get("selectExpression")
                    or raw.get("expression")
                    or ""
                ),
                "where_expression": (
                    raw.get("where")
                    or raw.get("whereExpression")
                    or ""
                ),
                "data_type": raw.get("dataType") or raw.get("type", ""),
                "description": raw.get("description") or raw.get("hint", ""),
                "qualification": obj_type_raw,
            })
        except Exception as exc:
            errors.append(fail_step("extract_from_rest_object", exc, partial_results=raw))

    inventory = {
        "name": "",
        "connection": {},
        "tables": [],
        "joins": [],
        "contexts": [],
        "objects": normalized,
        "derived_tables": [],
        "lov_queries": [],
        "errors": errors,
    }

    log.info(
        "extract_from_rest: done — objects=%d errors=%d",
        len(normalized),
        len(errors),
    )
    return inventory
