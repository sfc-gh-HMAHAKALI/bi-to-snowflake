"""Parse LookML project files using the lkml package.

Walks a project directory, parses all .lkml/.lookml files, and normalizes
views, explores, dashboards, and model metadata into a consistent structure.
Each file is parsed independently — errors are logged and collection continues.
"""

import os
from pathlib import Path
from typing import Any

from ..common.errors import ParseError, fail_step
from ..common.logger import get_logger
from .resolver import resolve_refinements, resolve_extends

log = get_logger("looker.parser")


def parse_lookml_project(project_dir: str) -> dict:
    """Walk a LookML project directory and parse all .lkml/.lookml files.

    Args:
        project_dir: Path to the root of the LookML project.

    Returns:
        Dict with keys: connection, views, explores, dashboards, models, errors.
        Each item in views/explores carries a _source_file key.
    """
    log.info("parse_lookml_project: entering — project_dir=%s", project_dir)

    try:
        import lkml
    except ImportError as exc:
        raise ParseError(
            "lkml package is not installed. Run: pip install lkml",
            context={"project_dir": project_dir},
        ) from exc

    root = Path(project_dir).resolve()
    if not root.exists():
        raise ParseError(
            f"Project directory does not exist: {project_dir}",
            context={"project_dir": project_dir},
        )
    if not root.is_dir():
        raise ParseError(
            f"Project path is not a directory: {project_dir}",
            context={"project_dir": project_dir},
        )

    result: dict[str, Any] = {
        "connection": None,
        "views": [],
        "explores": [],
        "dashboards": [],
        "models": [],
        "errors": [],
    }

    lkml_extensions = {".lkml", ".lookml"}

    files_found = 0
    files_parsed = 0

    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in sorted(filenames):
            ext = Path(filename).suffix.lower()
            if ext not in lkml_extensions:
                continue

            # Skip dashboard files — they contain no semantic definitions
            # and always fail to parse with the lkml library.
            name_lower = filename.lower()
            if name_lower.endswith(".dashboard.lookml") or name_lower.endswith(".dashboard.lkml"):
                log.debug("Skipping dashboard file: %s", filename)
                continue

            files_found += 1
            file_path = os.path.join(dirpath, filename)
            rel_path = os.path.relpath(file_path, root)

            try:
                parsed = _parse_single_file(lkml, file_path)
            except Exception as exc:
                err = fail_step(f"parse_file:{rel_path}", exc)
                err["file"] = rel_path
                result["errors"].append(err)
                log.warning("Skipping %s due to parse error: %s", rel_path, exc)
                continue

            files_parsed += 1

            # Extract connection from model files
            for model in parsed.get("models", []):
                if model.get("connection") and result["connection"] is None:
                    result["connection"] = model["connection"]
                model_entry = {
                    "name": model.get("name", ""),
                    "connection": model.get("connection"),
                    "label": model.get("label"),
                    "includes": model.get("includes", []),
                    "_source_file": rel_path,
                }
                result["models"].append(model_entry)

            # Views
            for view_raw in parsed.get("views", []):
                view_raw["_source_file"] = rel_path
                result["views"].append(view_raw)

            # Explores (may appear in model files)
            for explore_raw in parsed.get("explores", []):
                explore_raw["_source_file"] = rel_path
                result["explores"].append(explore_raw)

            # Dashboards
            for dash_raw in parsed.get("dashboards", []):
                dash_raw["_source_file"] = rel_path
                result["dashboards"].append(dash_raw)

    # Normalize views before returning: parse_view, then merge refinements,
    # then resolve extends inheritance.
    result["views"] = [parse_view(v) for v in result["views"]]
    result["views"] = resolve_refinements(result["views"])
    result["views"] = resolve_extends(result["views"])

    log.info(
        "parse_lookml_project: complete — files_found=%d, files_parsed=%d, "
        "views=%d, explores=%d, dashboards=%d, models=%d, errors=%d",
        files_found, files_parsed,
        len(result["views"]), len(result["explores"]),
        len(result["dashboards"]), len(result["models"]),
        len(result["errors"]),
    )
    return result


def _parse_single_file(lkml_module: Any, file_path: str) -> dict:
    """Parse a single LookML file and return the raw lkml dict."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            return lkml_module.load(fh)
    except Exception as exc:
        raise ParseError(
            f"Failed to parse LookML file: {file_path}",
            context={"file_path": file_path, "error": str(exc)},
        ) from exc


# ---------------------------------------------------------------------------
# View normalization
# ---------------------------------------------------------------------------

def parse_view(view_dict: dict) -> dict:
    """Normalize a raw lkml view dict into a consistent structure.

    Args:
        view_dict: Raw view dict as returned by lkml.load().

    Returns:
        Normalized dict with name, sql_table_name, derived_table, dimensions,
        dimension_groups, measures, parameters, sets, and _source_file.
    """
    name = view_dict.get("name", "")
    log.debug("parse_view: entering — name=%s", name)

    try:
        normalized = {
            "name": name,
            "label": view_dict.get("label"),
            "description": view_dict.get("description"),
            "sql_table_name": view_dict.get("sql_table_name"),
            "extends": _as_list(view_dict.get("extends") or view_dict.get("extends__all")),
            "derived_table": _parse_derived_table(view_dict.get("derived_table")),
            "dimensions": [
                _parse_dimension(d)
                for d in view_dict.get("dimensions", [])
            ],
            "dimension_groups": [
                _parse_dimension_group(dg)
                for dg in view_dict.get("dimension_groups", [])
            ],
            "measures": [
                _parse_measure(m)
                for m in view_dict.get("measures", [])
            ],
            "parameters": [
                _parse_parameter(p)
                for p in view_dict.get("parameters", [])
            ],
            "sets": [
                {"name": s.get("name"), "fields": _as_list(s.get("fields"))}
                for s in view_dict.get("sets", [])
            ],
            "_source_file": view_dict.get("_source_file"),
        }
    except Exception as exc:
        raise ParseError(
            f"Failed to normalize view '{name}'",
            context={"view_name": name},
        ) from exc

    log.debug(
        "parse_view: done — name=%s, dimensions=%d, measures=%d",
        name,
        len(normalized["dimensions"]),
        len(normalized["measures"]),
    )
    return normalized


def _parse_dimension(dim: dict) -> dict:
    return {
        "name": dim.get("name", ""),
        "type": dim.get("type", "string"),
        "sql": dim.get("sql"),
        "label": dim.get("label"),
        "description": dim.get("description"),
        "primary_key": dim.get("primary_key", "no").lower() == "yes",
        "hidden": dim.get("hidden", "no").lower() == "yes",
        "tags": _as_list(dim.get("tags")),
        "value_format": dim.get("value_format") or dim.get("value_format_name"),
        "case": dim.get("case"),
        "drill_fields": _as_list(dim.get("drill_fields")),
    }


def _parse_dimension_group(dg: dict) -> dict:
    return {
        "name": dg.get("name", ""),
        "type": dg.get("type", "time"),
        "timeframes": _as_list(dg.get("timeframes")),
        "intervals": _as_list(dg.get("intervals")),
        "sql": dg.get("sql"),
        "sql_start": dg.get("sql_start"),
        "sql_end": dg.get("sql_end"),
        "label": dg.get("label"),
        "description": dg.get("description"),
        "datatype": dg.get("datatype"),
        "convert_tz": dg.get("convert_tz", "yes").lower() == "yes",
        "hidden": dg.get("hidden", "no").lower() == "yes",
    }


def _parse_measure(m: dict) -> dict:
    return {
        "name": m.get("name", ""),
        "type": m.get("type", "count"),
        "sql": m.get("sql"),
        "sql_distinct_key": m.get("sql_distinct_key"),
        "label": m.get("label"),
        "description": m.get("description"),
        "hidden": m.get("hidden", "no").lower() == "yes",
        "filters": _parse_filters(m.get("filters", [])),
        "drill_fields": _as_list(m.get("drill_fields")),
        "value_format": m.get("value_format") or m.get("value_format_name"),
        "tags": _as_list(m.get("tags")),
        "percentile": m.get("percentile"),
    }


def _parse_parameter(p: dict) -> dict:
    return {
        "name": p.get("name", ""),
        "type": p.get("type", "string"),
        "label": p.get("label"),
        "description": p.get("description"),
        "default_value": p.get("default_value"),
        "allowed_values": p.get("allowed_values", []),
        "hidden": p.get("hidden", "no").lower() == "yes",
    }


def _parse_derived_table(dt: dict | None) -> dict | None:
    if dt is None:
        return None
    return {
        "sql": dt.get("sql"),
        "explore_source": dt.get("explore_source"),
        "sql_trigger_value": dt.get("sql_trigger_value"),
        "datagroup_trigger": dt.get("datagroup_trigger"),
        "partition_keys": _as_list(dt.get("partition_keys")),
        "cluster_keys": _as_list(dt.get("cluster_keys")),
        "persist_for": dt.get("persist_for"),
        "materialized_view": dt.get("materialized_view", "no").lower() == "yes"
        if dt.get("materialized_view") is not None
        else False,
    }


def _parse_filters(filters: list) -> list[dict]:
    result = []
    for f in filters:
        if isinstance(f, dict):
            result.append({"field": f.get("field"), "value": f.get("value")})
    return result


# ---------------------------------------------------------------------------
# Explore normalization
# ---------------------------------------------------------------------------

def parse_explore(explore_dict: dict) -> dict:
    """Normalize a raw lkml explore dict into a consistent structure.

    Args:
        explore_dict: Raw explore dict as returned by lkml.load().

    Returns:
        Normalized dict with name, from_view, label, description, joins,
        and filter information.
    """
    name = explore_dict.get("name", "")
    log.debug("parse_explore: entering — name=%s", name)

    try:
        normalized = {
            "name": name,
            "from_view": explore_dict.get("from") or name,
            "label": explore_dict.get("label"),
            "description": explore_dict.get("description"),
            "hidden": explore_dict.get("hidden", "no").lower() == "yes",
            "extends": _as_list(
                explore_dict.get("extends") or explore_dict.get("extends__all")
            ),
            "joins": [_parse_join(j) for j in explore_dict.get("joins", [])],
            "sql_always_where": explore_dict.get("sql_always_where"),
            "always_filter": _parse_always_filter(
                explore_dict.get("always_filter")
            ),
            "access_filters": [
                {
                    "field": af.get("field"),
                    "user_attribute": af.get("user_attribute"),
                }
                for af in explore_dict.get("access_filters", [])
            ],
            "conditionally_filter": _parse_conditionally_filter(
                explore_dict.get("conditionally_filter")
            ),
            "_source_file": explore_dict.get("_source_file"),
        }
    except Exception as exc:
        raise ParseError(
            f"Failed to normalize explore '{name}'",
            context={"explore_name": name},
        ) from exc

    log.debug(
        "parse_explore: done — name=%s, joins=%d",
        name,
        len(normalized["joins"]),
    )
    return normalized


def _parse_join(j: dict) -> dict:
    return {
        "name": j.get("name", ""),
        "from_view": j.get("from") or j.get("name", ""),
        "type": j.get("type", "left_outer"),
        "relationship": j.get("relationship", "many_to_one"),
        "sql_on": j.get("sql_on"),
        "foreign_key": j.get("foreign_key"),
        "fields": _as_list(j.get("fields")),
    }


def _parse_always_filter(af: dict | None) -> list[dict]:
    if not af:
        return []
    filters = af.get("filters", [])
    return [{"field": f.get("field"), "value": f.get("value")} for f in filters]


def _parse_conditionally_filter(cf: dict | None) -> dict:
    if not cf:
        return {}
    return {
        "filters": [
            {"field": f.get("field"), "value": f.get("value")}
            for f in cf.get("filters", [])
        ],
        "unless": _as_list(cf.get("unless")),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _as_list(val: Any) -> list:
    """Coerce None / scalar / list to a list."""
    if val is None:
        return []
    if isinstance(val, list):
        return val
    return [val]
