"""Parse Tableau workbook and datasource files (.twb, .twbx, .tds, .tdsx).

Handles both plain XML files and ZIP-packaged variants. All parsing is
defensive: missing attributes fall back to empty strings, malformed elements
are recorded in the errors list rather than aborting the parse.
"""

import os
import tempfile
import zipfile
import xml.etree.ElementTree as _StdET
try:
    import defusedxml.ElementTree as ET
    # defusedxml doesn't expose Element — use stdlib's for type hints
    ET.Element = _StdET.Element
except ImportError:
    import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from ..common.logger import get_logger
from ..common.errors import ParseError, fail_step

log = get_logger(__name__)

# Security limits for ZIP handling
_MAX_UNCOMPRESSED_SIZE = 1_073_741_824  # 1 GB
_MAX_MEMBER_COUNT = 10_000


def _validate_zip_member(zf: zipfile.ZipFile, member_name: str, dest_dir: str) -> None:
    """Validate a ZIP member is safe to extract (no zip slip, no zip bomb)."""
    real_dest = os.path.realpath(dest_dir)
    member_path = os.path.realpath(os.path.join(dest_dir, member_name))
    if not member_path.startswith(real_dest + os.sep) and member_path != real_dest:
        raise ParseError(
            f"Zip slip detected: member '{member_name}' escapes extraction directory",
            context={"member": member_name},
        )


def _check_zip_bomb(zf: zipfile.ZipFile, archive_path: str) -> None:
    """Reject archives with excessive uncompressed size or member count."""
    members = zf.infolist()
    if len(members) > _MAX_MEMBER_COUNT:
        raise ParseError(
            f"Archive has {len(members)} members, exceeding limit of {_MAX_MEMBER_COUNT}",
            context={"path": archive_path, "member_count": len(members)},
        )
    total_size = sum(info.file_size for info in members)
    if total_size > _MAX_UNCOMPRESSED_SIZE:
        raise ParseError(
            f"Archive uncompressed size ({total_size:,} bytes) exceeds limit ({_MAX_UNCOMPRESSED_SIZE:,} bytes)",
            context={"path": archive_path, "uncompressed_size": total_size},
        )

# Tableau XML attribute names vary by connector; these are the most common.
_CONN_ATTRS = (
    "server", "dbname", "schema", "port", "username",
    "class", "directory", "filename", "authentication",
)


# ---------------------------------------------------------------------------
# Unpacking helpers
# ---------------------------------------------------------------------------

def _extract_inner_file(archive_path: str, extension: str, prefix: str) -> tuple[str, str]:
    """Unzip a packaged Tableau file and return (inner_file_content_path, xml_text).

    Extracts the inner file to a temporary directory, reads the XML content,
    then cleans up the temp dir automatically.

    Args:
        archive_path: Filesystem path to the .twbx or .tdsx file.
        extension: File extension to look for inside the archive (e.g. '.twb').
        prefix: Prefix for temp directory naming.

    Returns:
        Tuple of (path_to_extracted_file, raw_xml_bytes).

    Raises:
        ParseError: If the file is not a valid ZIP, contains no matching inner
            file, or the inner file cannot be extracted.
    """
    log.info("_extract_inner_file: entry — path=%s, ext=%s", archive_path, extension)

    archive_path = str(Path(archive_path).resolve())
    if not os.path.isfile(archive_path):
        raise ParseError(f"File not found: {archive_path}", context={"path": archive_path})

    try:
        if not zipfile.is_zipfile(archive_path):
            raise ParseError(
                f"Not a valid ZIP file: {archive_path}",
                context={"path": archive_path},
            )
        zf = zipfile.ZipFile(archive_path, "r")
    except zipfile.BadZipFile as exc:
        raise ParseError(
            f"Corrupt archive: {archive_path}",
            context={"path": archive_path},
        ) from exc

    with zf:
        inner_names = [n for n in zf.namelist() if n.lower().endswith(extension)]
        if not inner_names:
            raise ParseError(
                f"No {extension} file found inside {archive_path}",
                context={"path": archive_path},
            )

        # Prefer the top-level file (shortest path component count)
        inner_name = min(inner_names, key=lambda n: len(n.split("/")))
        log.debug("_extract_inner_file: found inner file=%s", inner_name)

        _check_zip_bomb(zf, archive_path)

        with tempfile.TemporaryDirectory(prefix=prefix) as tmp_dir:
            _validate_zip_member(zf, inner_name, tmp_dir)
            try:
                zf.extract(inner_name, path=tmp_dir)
            except Exception as exc:
                raise ParseError(
                    f"Failed to extract {inner_name} from {archive_path}: {exc}",
                    context={"path": archive_path, "inner_file": inner_name},
                ) from exc

            extracted = os.path.join(tmp_dir, inner_name)
            with open(extracted, "rb") as fh:
                raw_bytes = fh.read()

    log.info("_extract_inner_file: exit — read %d bytes", len(raw_bytes))
    return inner_name, raw_bytes


def extract_from_twbx(path: str) -> str:
    """Unzip a packaged Tableau workbook (.twbx) and return the inner .twb path.

    The inner .twb is extracted to a temporary directory that is automatically
    cleaned up after reading. The returned path points to a new temp file
    containing the extracted XML content.

    Args:
        path: Filesystem path to the .twbx file.

    Returns:
        Absolute path to the extracted .twb file.

    Raises:
        ParseError: If the file is not a valid ZIP, contains no .twb, or the
            inner file cannot be extracted.
    """
    inner_name, raw_bytes = _extract_inner_file(path, ".twb", "twbx_")
    # Write to a temp file that persists for the lifetime of the process
    # (parse_workbook reads it immediately after this call)
    tmp = tempfile.NamedTemporaryFile(
        prefix="twb_", suffix=".twb", delete=False,
    )
    tmp.write(raw_bytes)
    tmp.close()
    return tmp.name


def extract_from_tdsx(path: str) -> str:
    """Unzip a packaged Tableau datasource (.tdsx) and return the inner .tds path.

    Args:
        path: Filesystem path to the .tdsx file.

    Returns:
        Absolute path to the extracted .tds file.

    Raises:
        ParseError: If the file is not a valid ZIP, contains no .tds, or the
            inner file cannot be extracted.
    """
    inner_name, raw_bytes = _extract_inner_file(path, ".tds", "tdsx_")
    tmp = tempfile.NamedTemporaryFile(
        prefix="tds_", suffix=".tds", delete=False,
    )
    tmp.write(raw_bytes)
    tmp.close()
    return tmp.name


# ---------------------------------------------------------------------------
# XML loading
# ---------------------------------------------------------------------------

def _load_xml(path: str) -> ET.Element:
    """Parse an XML file and return the root element.

    Handles UTF-8 BOM and common encoding issues.

    Raises:
        ParseError: On any XML parse failure.
    """
    try:
        # Read raw bytes and strip BOM if present so ElementTree doesn't choke
        with open(path, "rb") as fh:
            raw = fh.read()
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        root = ET.fromstring(raw)
        return root
    except ET.ParseError as exc:
        raise ParseError(
            f"Malformed XML in {path}: {exc}",
            context={"path": path, "xml_error": str(exc)},
        ) from exc
    except OSError as exc:
        raise ParseError(
            f"Cannot read file {path}: {exc}",
            context={"path": path},
        ) from exc


# ---------------------------------------------------------------------------
# Main workbook parser
# ---------------------------------------------------------------------------

def parse_workbook(path: str) -> dict:
    """Parse a Tableau workbook or datasource file.

    Automatically detects and unpacks .twbx and .tdsx packages before parsing.
    For .tds files the return dict will have an empty ``worksheets``,
    ``dashboards``, and ``parameters`` list (those only exist in workbooks).

    Args:
        path: Path to a .twb, .twbx, .tds, or .tdsx file.

    Returns:
        dict with keys:
            datasources (list[dict]):  Parsed datasource objects.
            worksheets  (list[dict]):  Worksheet-to-field mappings.
            dashboards  (list[dict]):  Dashboard-to-sheet mappings.
            parameters  (list[dict]):  Parameter definitions.
            errors      (list[dict]):  Any non-fatal parse errors.
    """
    log.info("parse_workbook: entry — path=%s", path)

    path = str(Path(path).resolve())
    ext = Path(path).suffix.lower()
    errors: list[dict] = []
    tmp_file: str | None = None  # Track temp file for cleanup

    # Unpack packaged formats
    try:
        if ext == ".twbx":
            path = extract_from_twbx(path)
            tmp_file = path
            ext = ".twb"
        elif ext == ".tdsx":
            path = extract_from_tdsx(path)
            tmp_file = path
            ext = ".tds"
    except ParseError as exc:
        err = fail_step("unpack", exc)
        log.error("parse_workbook: unpack failed, returning empty result")
        return {"datasources": [], "worksheets": [], "dashboards": [],
                "parameters": [], "errors": [err]}

    # Load XML
    try:
        root = _load_xml(path)
    except ParseError as exc:
        err = fail_step("load_xml", exc)
        return {"datasources": [], "worksheets": [], "dashboards": [],
                "parameters": [], "errors": [err]}

    result: dict = {
        "datasources": [],
        "worksheets": [],
        "dashboards": [],
        "parameters": [],
        "errors": [],
    }

    # --- Datasources ---------------------------------------------------------
    ds_container = root.find("datasources")
    ds_elements = (
        list(ds_container) if ds_container is not None
        else (root.findall("datasource") if root.tag == "datasource" else [])
    )
    # For a .tds the root *is* the datasource element
    if root.tag == "datasource":
        ds_elements = [root]

    for ds_el in ds_elements:
        try:
            ds_dict = extract_datasource(ds_el)
            result["datasources"].append(ds_dict)
        except Exception as exc:
            ds_name = ds_el.get("name", "<unknown>")
            err = fail_step(f"extract_datasource[{ds_name}]", exc)
            errors.append(err)

    # --- Worksheets ----------------------------------------------------------
    ws_container = root.find("worksheets")
    if ws_container is not None:
        for ws_el in ws_container:
            try:
                ws_dict = _extract_worksheet(ws_el)
                result["worksheets"].append(ws_dict)
            except Exception as exc:
                ws_name = ws_el.get("name", "<unknown>")
                err = fail_step(f"extract_worksheet[{ws_name}]", exc)
                errors.append(err)

    # --- Dashboards ----------------------------------------------------------
    try:
        result["dashboards"] = extract_dashboard_field_usage(root)
    except Exception as exc:
        err = fail_step("extract_dashboard_field_usage", exc)
        errors.append(err)

    # --- Parameters ----------------------------------------------------------
    try:
        result["parameters"] = _extract_parameters(root)
    except Exception as exc:
        err = fail_step("extract_parameters", exc)
        errors.append(err)

    result["errors"] = errors

    # Clean up temp file from twbx/tdsx extraction
    if tmp_file:
        try:
            os.unlink(tmp_file)
        except OSError:
            pass

    log.info(
        "parse_workbook: exit — datasources=%d, worksheets=%d, "
        "dashboards=%d, parameters=%d, errors=%d",
        len(result["datasources"]),
        len(result["worksheets"]),
        len(result["dashboards"]),
        len(result["parameters"]),
        len(result["errors"]),
    )
    return result


# ---------------------------------------------------------------------------
# Datasource parser
# ---------------------------------------------------------------------------

def extract_datasource(ds_element: ET.Element) -> dict:
    """Parse a single ``<datasource>`` XML element.

    Args:
        ds_element: The ``<datasource>`` element from the workbook XML.

    Returns:
        dict with keys:
            name (str), caption (str), connection (dict),
            tables (list[dict]), joins (list[dict]),
            columns (list[dict]), calculated_fields (list[dict]),
            groups (list[dict]), bins (list[dict]),
            parameters (list[dict]), filters (list[dict]),
            folders (list[dict]), aliases (dict),
            errors (list[dict])
    """
    name = ds_element.get("name", "")
    caption = ds_element.get("caption", "")
    log.info("extract_datasource: entry — name=%s caption=%s", name, caption)

    ds: dict = {
        "name": name,
        "caption": caption,
        "connection": {},
        "tables": [],
        "joins": [],
        "columns": [],
        "calculated_fields": [],
        "groups": [],
        "bins": [],
        "parameters": [],
        "filters": [],
        "folders": [],
        "aliases": {},
        "errors": [],
    }
    errors: list[dict] = []

    # --- Connection info -----------------------------------------------------
    try:
        conn_el = ds_element.find("connection")
        if conn_el is not None:
            ds["connection"] = {attr: conn_el.get(attr, "") for attr in _CONN_ATTRS}
            ds["connection"]["raw_class"] = conn_el.get("class", "")
    except Exception as exc:
        errors.append(fail_step("connection", exc))

    # --- Relations (tables and joins) ----------------------------------------
    try:
        tables, joins = _extract_relations(ds_element)
        ds["tables"] = tables
        ds["joins"] = joins
    except Exception as exc:
        errors.append(fail_step("relations", exc))

    # --- Columns (regular + calculated fields) --------------------------------
    for col_el in ds_element.iter("column"):
        try:
            col = _extract_column(col_el)
            calc_el = col_el.find("calculation")
            if calc_el is not None:
                col["formula"] = calc_el.get("formula", "")
                col["calculation_class"] = calc_el.get("class", "")
                ds["calculated_fields"].append(col)
            else:
                ds["columns"].append(col)
        except Exception as exc:
            col_name = col_el.get("name", "<unknown>")
            errors.append(fail_step(f"column[{col_name}]", exc))

    # --- Groups and sets ------------------------------------------------------
    for group_el in ds_element.iter("group"):
        try:
            ds["groups"].append({
                "name": group_el.get("name", ""),
                "caption": group_el.get("caption", ""),
                "field": group_el.get("field", ""),
                "members": [
                    {"value": m.get("value", ""), "alias": m.get("alias", "")}
                    for m in group_el.iter("member")
                ],
            })
        except Exception as exc:
            errors.append(fail_step(f"group[{group_el.get('name', '?')}]", exc))

    # --- Bins ----------------------------------------------------------------
    for bin_el in ds_element.iter("bin"):
        try:
            ds["bins"].append({
                "name": bin_el.get("name", ""),
                "caption": bin_el.get("caption", ""),
                "field": bin_el.get("field", ""),
                "size": bin_el.get("size", ""),
            })
        except Exception as exc:
            errors.append(fail_step(f"bin[{bin_el.get('name', '?')}]", exc))

    # --- Parameters (datasource-level param columns) -------------------------
    for col_el in ds_element.iter("column"):
        if col_el.get("param-domain-type"):
            try:
                ds["parameters"].append({
                    "name": col_el.get("name", ""),
                    "caption": col_el.get("caption", ""),
                    "datatype": col_el.get("datatype", ""),
                    "type": col_el.get("type", ""),
                    "param_domain_type": col_el.get("param-domain-type", ""),
                    "default_value": col_el.get("value", ""),
                    "range_min": col_el.get("range-min", ""),
                    "range_max": col_el.get("range-max", ""),
                })
            except Exception as exc:
                errors.append(fail_step(f"param_col[{col_el.get('name', '?')}]", exc))

    # --- Datasource filters --------------------------------------------------
    for filt_el in ds_element.iter("filter"):
        try:
            ds["filters"].append({
                "class": filt_el.get("class", ""),
                "column": filt_el.get("column", ""),
                "type": filt_el.get("type", ""),
            })
        except Exception as exc:
            errors.append(fail_step("filter", exc))

    # --- Folders -------------------------------------------------------------
    for folder_el in ds_element.iter("folder"):
        try:
            ds["folders"].append({
                "name": folder_el.get("name", ""),
                "role": folder_el.get("role", ""),
                "fields": [
                    fi.get("name", "") for fi in folder_el.iter("folder-item")
                ],
            })
        except Exception as exc:
            errors.append(fail_step(f"folder[{folder_el.get('name', '?')}]", exc))

    # --- Aliases -------------------------------------------------------------
    try:
        aliases_el = ds_element.find("aliases")
        if aliases_el is not None:
            for alias_el in aliases_el.iter("alias"):
                key = alias_el.get("key", "")
                value = alias_el.get("value", "")
                if key:
                    ds["aliases"][key] = value
    except Exception as exc:
        errors.append(fail_step("aliases", exc))

    ds["errors"] = errors
    log.info(
        "extract_datasource: exit — name=%s, columns=%d, calc_fields=%d, "
        "tables=%d, joins=%d, errors=%d",
        name,
        len(ds["columns"]),
        len(ds["calculated_fields"]),
        len(ds["tables"]),
        len(ds["joins"]),
        len(errors),
    )
    return ds


def _extract_column(col_el: ET.Element) -> dict:
    """Extract a column dict from a ``<column>`` element."""
    return {
        "name": col_el.get("name", ""),
        "caption": col_el.get("caption", ""),
        "role": col_el.get("role", ""),        # dimension / measure
        "type": col_el.get("type", ""),        # ordinal / quantitative / nominal
        "datatype": col_el.get("datatype", ""),
        "hidden": col_el.get("hidden", "false").lower() == "true",
        "formula": "",
        "calculation_class": "",
    }


def _extract_relations(ds_element: ET.Element) -> tuple[list, list]:
    """Walk ``<relation>`` elements and separate base tables from joins."""
    tables: list[dict] = []
    joins: list[dict] = []

    # Relations may live directly under <datasource> or under <connection>
    for rel_el in ds_element.iter("relation"):
        rel_type = rel_el.get("type", "")
        if rel_type == "table":
            tables.append({
                "name": rel_el.get("name", ""),
                "table": rel_el.get("table", ""),
                "schema": rel_el.get("connection", ""),
            })
        elif rel_type == "join":
            join: dict = {
                "join_type": rel_el.get("join", "inner"),
                "tables": [],
                "clauses": [],
            }
            for child in rel_el:
                if child.get("type") == "table":
                    join["tables"].append({
                        "name": child.get("name", ""),
                        "table": child.get("table", ""),
                    })
                elif child.tag == "clause":
                    clause_type = child.get("type", "")
                    expr_op = ""
                    left = ""
                    right = ""
                    expr_el = child.find("expression")
                    if expr_el is not None:
                        expr_op = expr_el.get("op", "")
                        exprs = list(expr_el)
                        left = exprs[0].get("op", "") if len(exprs) > 0 else ""
                        right = exprs[1].get("op", "") if len(exprs) > 1 else ""
                    join["clauses"].append({
                        "type": clause_type,
                        "op": expr_op,
                        "left": left,
                        "right": right,
                    })
            joins.append(join)

    return tables, joins


# ---------------------------------------------------------------------------
# Worksheet parser
# ---------------------------------------------------------------------------

def _extract_worksheet(ws_el: ET.Element) -> dict:
    """Extract field usage from a ``<worksheet>`` element."""
    name = ws_el.get("name", "")
    log.debug("_extract_worksheet: name=%s", name)

    fields_by_datasource: dict[str, list[str]] = {}

    # Fields referenced in the view
    table_el = ws_el.find("table")
    if table_el is not None:
        view_el = table_el.find("view")
        if view_el is not None:
            for dep_el in view_el.findall("datasource-dependencies"):
                ds_name = dep_el.get("datasource", "")
                cols = [
                    c.get("name", "")
                    for c in dep_el.findall("column")
                    if c.get("name")
                ]
                if ds_name:
                    fields_by_datasource[ds_name] = cols

    return {
        "name": name,
        "fields_by_datasource": fields_by_datasource,
    }


# ---------------------------------------------------------------------------
# Dashboard field usage
# ---------------------------------------------------------------------------

def extract_dashboard_field_usage(root: ET.Element) -> list[dict]:
    """Map dashboards → sheets → fields used across all datasources.

    Traverses ``<dashboards>`` → ``<dashboard>`` → ``<zones>`` to collect
    worksheet references, then resolves those worksheets' field usage.

    Args:
        root: Root XML element of the workbook.

    Returns:
        list of dicts, one per dashboard:
            {
                "name": str,
                "sheets": [
                    {
                        "name": str,
                        "fields_by_datasource": {ds_name: [field_name, ...]}
                    },
                    ...
                ]
            }
    """
    log.info("extract_dashboard_field_usage: entry")

    # Build a lookup of worksheet name → field usage from the workbook
    ws_lookup: dict[str, dict] = {}
    ws_container = root.find("worksheets")
    if ws_container is not None:
        for ws_el in ws_container:
            try:
                ws_dict = _extract_worksheet(ws_el)
                ws_lookup[ws_dict["name"]] = ws_dict
            except Exception as exc:
                fail_step(f"ws_lookup[{ws_el.get('name', '?')}]", exc)

    dashboards: list[dict] = []
    db_container = root.find("dashboards")
    if db_container is None:
        log.info("extract_dashboard_field_usage: exit — no <dashboards> element")
        return dashboards

    for db_el in db_container:
        db_name = db_el.get("name", "")
        try:
            sheet_refs = _collect_zone_worksheets(db_el)
            sheets = []
            for sheet_name in sheet_refs:
                if sheet_name in ws_lookup:
                    sheets.append(ws_lookup[sheet_name])
                else:
                    sheets.append({"name": sheet_name, "fields_by_datasource": {}})
            dashboards.append({"name": db_name, "sheets": sheets})
        except Exception as exc:
            fail_step(f"dashboard[{db_name}]", exc)

    log.info("extract_dashboard_field_usage: exit — dashboards=%d", len(dashboards))
    return dashboards


def _collect_zone_worksheets(db_el: ET.Element) -> list[str]:
    """Recursively collect all worksheet names referenced by zones in a dashboard."""
    sheet_names: list[str] = []
    seen: set[str] = set()

    def _walk_zones(el: ET.Element) -> None:
        for zone in el:
            # Zones can reference sheets by name via 'name' attr with type='worksheet'
            if zone.get("type") == "worksheet":
                name = zone.get("name", "")
                if name and name not in seen:
                    seen.add(name)
                    sheet_names.append(name)
            # Some zones use the 'name' attr without type='worksheet'
            # but still reference a real worksheet (when name is a plain string)
            name = zone.get("name", "")
            if name and name not in seen and "[" not in name:
                seen.add(name)
                sheet_names.append(name)
            # 'param' may hold worksheet names in some zone types, but
            # skip anything that looks like a field reference (contains '[')
            # or a file path (contains '/' or '\')
            param = zone.get("param", "")
            if (param and param not in seen
                    and "[" not in param
                    and "/" not in param
                    and "\\" not in param
                    and not param.endswith((".jpg", ".jpeg", ".png", ".gif", ".svg"))):
                seen.add(param)
                sheet_names.append(param)
            _walk_zones(zone)

    zones_el = db_el.find("zones")
    if zones_el is not None:
        _walk_zones(zones_el)

    return sheet_names


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

def _extract_parameters(root: ET.Element) -> list[dict]:
    """Extract top-level workbook parameters from a ``<parameters>`` element."""
    params: list[dict] = []
    params_el = root.find("parameters")
    if params_el is None:
        return params

    for param_el in params_el:
        try:
            params.append({
                "name": param_el.get("name", ""),
                "caption": param_el.get("caption", ""),
                "datatype": param_el.get("datatype", ""),
                "type": param_el.get("type", ""),
                "param_domain_type": param_el.get("param-domain-type", ""),
                "default_value": param_el.get("value", ""),
                "range_min": param_el.get("range-min", ""),
                "range_max": param_el.get("range-max", ""),
                "formula": (
                    param_el.find("calculation").get("formula", "")
                    if param_el.find("calculation") is not None
                    else ""
                ),
            })
        except Exception as exc:
            fail_step(f"parameter[{param_el.get('name', '?')}]", exc)

    return params
