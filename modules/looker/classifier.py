"""Classify LookML fields and measures by conversion complexity.

Three tiers:
  simple           — direct 1:1 mapping to Snowflake Semantic View constructs
  needs_translation — requires SQL rewriting but is automatable
  manual_required   — Liquid templates, NDTs, or types with no direct mapping
"""

import re

from ..common.errors import ClassificationError, fail_step
from ..common.logger import get_logger

log = get_logger("looker.classifier")

# ---------------------------------------------------------------------------
# Type classification tables
# ---------------------------------------------------------------------------

# Dimension types that require manual review
_MANUAL_DIMENSION_TYPES = frozenset({
    "location",
    "distance",
    "custom_calendar",
    "date_raw",  # raw epoch — needs context
})

# Dimension types that need SQL translation
_NEEDS_TRANSLATION_DIMENSION_TYPES = frozenset({
    "tier",
    "bin",
    "case",
    "duration",
    "yesno",
    "date",
    "time",
    "percentile",
    "list",
})

# Measure types that require manual review
_MANUAL_MEASURE_TYPES = frozenset({
    "percent_of_total",
    "percent_of_previous",
    "running_total",
    "sum_distinct",
    "average_distinct",
    "median_distinct",
    "percentile_distinct",
    "period_over_period",
    "distance",
})

# Measure types that need translation
_NEEDS_TRANSLATION_MEASURE_TYPES = frozenset({
    "percentile",
    "list",
})

# Fully simple measure types
_SIMPLE_MEASURE_TYPES = frozenset({
    "count",
    "count_distinct",
    "sum",
    "average",
    "min",
    "max",
    "median",
    "number",  # only simple if no cross-view refs or filters
})

# Fully simple dimension types
_SIMPLE_DIMENSION_TYPES = frozenset({
    "string",
    "number",
    "zipcode",
    "date_time",
    "time",
})


def classify_lookml_complexity(
    field_dict: dict,
    view_dict: dict | None = None,
) -> str:
    """Classify a single LookML field's conversion complexity.

    Args:
        field_dict: Normalized dimension, measure, or dimension_group dict.
        view_dict: Parent view dict (used to detect cross-view SQL references).

    Returns:
        'simple', 'needs_translation', or 'manual_required'.
    """
    name = field_dict.get("name", "<unknown>")
    field_type = (field_dict.get("type") or "").lower()
    sql = field_dict.get("sql") or ""

    log.debug("classify_lookml_complexity: entering — name=%s, type=%s", name, field_type)

    try:
        result = _classify(field_dict, field_type, sql, view_dict)
    except Exception as exc:
        fail_step(f"classify_field:{name}", exc)
        result = "manual_required"

    log.debug(
        "classify_lookml_complexity: done — name=%s, type=%s, result=%s",
        name, field_type, result,
    )
    return result


def _classify(
    field_dict: dict,
    field_type: str,
    sql: str,
    view_dict: dict | None,
) -> str:
    from .resolver import flag_liquid_templates

    # ------------------------------------------------------------------
    # Hard manual_required checks (order matters — most restrictive first)
    # ------------------------------------------------------------------

    # 1. Liquid templates anywhere in sql
    if sql and flag_liquid_templates(sql):
        log.debug("  -> manual_required: Liquid template detected")
        return "manual_required"

    # 2. NDT (explore_source derived tables) — cannot be auto-converted
    if _is_ndt(field_dict, view_dict):
        log.debug("  -> manual_required: NDT (explore_source derived table)")
        return "manual_required"

    # 3. Manual measure types
    if field_type in _MANUAL_MEASURE_TYPES:
        log.debug("  -> manual_required: measure type '%s'", field_type)
        return "manual_required"

    # 4. Manual dimension types
    if field_type in _MANUAL_DIMENSION_TYPES:
        log.debug("  -> manual_required: dimension type '%s'", field_type)
        return "manual_required"

    # ------------------------------------------------------------------
    # needs_translation checks
    # ------------------------------------------------------------------

    # 5. Dimension group (any type other than simple time/duration handled below)
    if _is_dimension_group(field_dict):
        dg_type = field_type
        if dg_type == "duration":
            # duration groups get exploded by resolver — still needs_translation
            log.debug("  -> needs_translation: duration dimension_group")
            return "needs_translation"
        # time dimension groups are automatable
        log.debug("  -> needs_translation: time dimension_group")
        return "needs_translation"

    # 6. Translation-required dimension types
    if field_type in _NEEDS_TRANSLATION_DIMENSION_TYPES:
        log.debug("  -> needs_translation: dimension type '%s'", field_type)
        return "needs_translation"

    # 7. Translation-required measure types
    if field_type in _NEEDS_TRANSLATION_MEASURE_TYPES:
        log.debug("  -> needs_translation: measure type '%s'", field_type)
        return "needs_translation"

    # 8. Number measure with cross-view SQL refs
    if field_type == "number" and _has_cross_view_ref(sql):
        log.debug("  -> needs_translation: number measure with cross-view ref")
        return "needs_translation"

    # 9. Filtered measures (any type with filters: block)
    if field_dict.get("filters"):
        log.debug("  -> needs_translation: filtered measure")
        return "needs_translation"

    # ------------------------------------------------------------------
    # Simple
    # ------------------------------------------------------------------

    if field_type in _SIMPLE_DIMENSION_TYPES or field_type in _SIMPLE_MEASURE_TYPES:
        log.debug("  -> simple: type '%s'", field_type)
        return "simple"

    # Default: unknown type — flag for review
    log.debug("  -> needs_translation: unknown type '%s'", field_type)
    return "needs_translation"


def _is_ndt(field_dict: dict, view_dict: dict | None) -> bool:
    """Return True if the field belongs to a view backed by an NDT (explore_source)."""
    if view_dict is None:
        return False
    dt = view_dict.get("derived_table")
    if dt and dt.get("explore_source"):
        return True
    return False


def _is_dimension_group(field_dict: dict) -> bool:
    """Detect if this dict originated from a dimension_group."""
    # dimension_groups have timeframes or intervals list, or _from_duration_group
    return bool(
        field_dict.get("timeframes")
        or field_dict.get("intervals")
        or field_dict.get("_from_duration_group")
    )


def _has_cross_view_ref(sql: str) -> bool:
    """Return True if sql contains ${view_name.field_name} cross-view references."""
    # ${TABLE.field} is not a cross-view ref; ${other_view.field} is
    return bool(re.search(r"\$\{(\w+)\.(\w+)\}", sql or ""))


# ---------------------------------------------------------------------------
# Bulk classification
# ---------------------------------------------------------------------------

def classify_all_fields(project: dict) -> dict:
    """Classify every field in a parsed LookML project.

    Args:
        project: Output of parse_lookml_project() — contains 'views' list.

    Returns:
        Dict with:
          summary: {simple: N, needs_translation: N, manual_required: N, total: N}
          by_view: {view_name: {field_name: classification, ...}}
          manual_fields: [{view, field, type, reason}, ...]
    """
    log.info("classify_all_fields: entering — views=%d", len(project.get("views", [])))

    from .parser import parse_view

    summary = {"simple": 0, "needs_translation": 0, "manual_required": 0, "total": 0}
    by_view: dict[str, dict[str, str]] = {}
    manual_fields: list[dict] = []

    for raw_view in project.get("views", []):
        view_name = raw_view.get("name", "<unknown>")
        try:
            view = parse_view(raw_view)
        except Exception as exc:
            fail_step(f"classify_all_fields:parse_view:{view_name}", exc)
            continue

        by_view[view_name] = {}

        # Classify dimensions, measures, dimension_groups
        all_fields = (
            [(f, view) for f in view.get("dimensions", [])]
            + [(f, view) for f in view.get("measures", [])]
            + [(f, view) for f in view.get("dimension_groups", [])]
        )

        for field, parent_view in all_fields:
            field_name = field.get("name", "<unknown>")
            try:
                classification = classify_lookml_complexity(field, parent_view)
            except Exception as exc:
                fail_step(
                    f"classify_all_fields:classify:{view_name}.{field_name}", exc
                )
                classification = "manual_required"

            by_view[view_name][field_name] = classification
            summary[classification] = summary.get(classification, 0) + 1
            summary["total"] += 1

            if classification == "manual_required":
                manual_fields.append({
                    "view": view_name,
                    "field": field_name,
                    "type": field.get("type", ""),
                    "has_liquid": bool(
                        field.get("sql") and "{%" in (field.get("sql") or "")
                        or "{{" in (field.get("sql") or "")
                    ),
                    "is_ndt": _is_ndt(field, parent_view),
                })

    log.info(
        "classify_all_fields: done — total=%d, simple=%d, "
        "needs_translation=%d, manual_required=%d",
        summary["total"],
        summary["simple"],
        summary["needs_translation"],
        summary["manual_required"],
    )

    return {
        "summary": summary,
        "by_view": by_view,
        "manual_fields": manual_fields,
    }
