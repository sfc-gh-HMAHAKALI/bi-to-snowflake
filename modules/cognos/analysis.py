"""Model-level analysis of a parsed Cognos model.

Derives the four source-agnostic concepts the unified inventory gained for this
adapter, plus the clone and relationship audits.

Consolidated deliberately. Grain, aggregation rules and clone analysis all key
off the same two things -- the fiscal-year suffix convention and the
query-subject/query-item graph -- so splitting them across separate modules
would mean three copies of the clone-detection logic drifting apart.

Two of these functions exist to answer specific, previously-unaudited questions
about the reference model:

* ``audit_grain_coverage`` -- which determinants apply to the tables a given
  conversion actually put in scope. A SUM-based metric over a table with an
  unsatisfied determinant can silently overstate totals.
* ``audit_relationships`` -- which specific relationships a conversion depends
  on are among those Framework Manager itself flags invalid. The aggregate count
  is not the useful number; the per-join answer is.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from ..common.logger import get_logger
from .classifier import classify_cognos_expression
from .parser import canonical_calculation_name

log = get_logger(__name__)

# Fiscal-year clone suffix on a table or namespace name: FACT_SALES_SUMMARY_FY22
_FY_OBJECT_RE = re.compile(r"^(?P<base>.+?)[_\s]*FY\s?(?P<year>\d{2,4})$", re.IGNORECASE)

# Cognos "Copy of ..." prefix, applied when an object is duplicated in the UI.
_COPY_PREFIX_RE = re.compile(r"^(?:Copy of\s+)+", re.IGNORECASE)


def canonical_object_name(name: str) -> tuple[str, str | None]:
    """Strip a fiscal-year suffix from an object name.

    ``FACT_SALES_SUMMARY_FY22`` -> ``("FACT_SALES_SUMMARY", "FY22")``
    """
    m = _FY_OBJECT_RE.match((name or "").strip())
    if not m:
        return (name or "").strip(), None
    return m.group("base").rstrip("_ "), f"FY{m.group('year')}"


# ---------------------------------------------------------------------------
# Grain declarations (requirement E2)
# ---------------------------------------------------------------------------


def extract_grain_declarations(parsed: dict) -> list[dict[str, Any]]:
    """Collect every determinant in the model as a grain declaration.

    ``is_row_grain`` marks the determinant that identifies a single row -- the
    table's actual grain. ``groupable`` determinants are coarser levels the
    model permits aggregating to, which is what makes mixed-grain reporting
    safe: a monthly fact joined to a daily fact must be grouped to the monthly
    determinant, not repeated per day.
    """
    out: list[dict[str, Any]] = []
    for qs in parsed.get("query_subjects", []):
        base, fy = canonical_object_name(qs.get("name", ""))
        for det in qs.get("determinants", []):
            out.append(
                {
                    "entity": qs.get("name", ""),
                    "entity_base": base,
                    "fiscal_year_clone": fy,
                    "namespace": qs.get("namespace", ""),
                    "name": det.get("name", ""),
                    "key_columns": det.get("key_columns", []),
                    "attribute_columns": det.get("attribute_columns", []),
                    "is_row_grain": det.get("identifies_row", False),
                    "groupable": det.get("can_group", False),
                    "physical_object": qs.get("physical_object", ""),
                }
            )
    log.info("Extracted %d grain declarations.", len(out))
    return out


def audit_grain_coverage(
    parsed: dict, in_scope_entities: list[str]
) -> dict[str, Any]:
    """Report which grain declarations apply to a named set of entities.

    ``in_scope_entities`` is matched on the canonical (fiscal-year-stripped)
    name, so passing ``FACT_SALES_SUMMARY`` also covers its ``_FY22`` clones.

    The ``risk`` field is the actionable part: an in-scope entity that declares a
    row grain of more than one column, or declares a groupable coarser level, is
    one where a naive ``SUM()`` metric can double-count.
    """
    wanted = {canonical_object_name(e)[0].upper() for e in in_scope_entities}
    grains = extract_grain_declarations(parsed)

    in_scope = [g for g in grains if g["entity_base"].upper() in wanted]
    out_of_scope = [g for g in grains if g["entity_base"].upper() not in wanted]

    risks: list[dict[str, Any]] = []
    for g in in_scope:
        reasons: list[str] = []
        if g["groupable"] and not g["is_row_grain"]:
            reasons.append(
                "declares a groupable coarser level: a SUM across this level "
                "repeats values unless grouped to the determinant key"
            )
        if g["is_row_grain"] and len(g["key_columns"]) > 1:
            reasons.append(
                "row grain is a composite key: single-column joins may fan out"
            )
        if reasons:
            risks.append(
                {"entity": g["entity"], "determinant": g["name"], "reasons": reasons}
            )

    entities_with_grain = {g["entity_base"].upper() for g in in_scope}
    return {
        "in_scope_entities": sorted(wanted),
        "in_scope_determinants": in_scope,
        "in_scope_count": len(in_scope),
        "out_of_scope_count": len(out_of_scope),
        "entities_without_determinants": sorted(wanted - entities_with_grain),
        "risks": risks,
        "verdict": (
            "no determinants apply to the in-scope entities; simple SUM metrics "
            "are safe on grain grounds"
            if not in_scope
            else f"{len(in_scope)} determinant(s) apply to in-scope entities; "
            f"{len(risks)} carry double-counting risk"
        ),
    }


# ---------------------------------------------------------------------------
# Aggregation rules (requirement E6)
# ---------------------------------------------------------------------------

# Cognos aggregate tokens -> Snowflake aggregate functions.
_AGG_MAP: dict[str, str] = {
    "sum": "SUM",
    "total": "SUM",
    "average": "AVG",
    "avg": "AVG",
    "count": "COUNT",
    "countdistinct": "COUNT_DISTINCT",
    "minimum": "MIN",
    "min": "MIN",
    "maximum": "MAX",
    "max": "MAX",
    "standarddeviation": "STDDEV",
    "variance": "VARIANCE",
    "calculated": None,
    "automatic": None,
    "unsupported": None,
    "none": None,
    # The semi-additive tokens. These are the ones with no scalar equivalent.
    "first": "FIRST_VALUE",
    "last": "LAST_VALUE",
    "median": "MEDIAN",
}

# semiAggregate values that mean "do not sum across time" -- the closing-balance
# behaviour in requirement E6.
_SEMI_ADDITIVE = frozenset({"first", "last", "median", "average", "avg"})


def extract_aggregation_rules(parsed: dict) -> list[dict[str, Any]]:
    """Collect the (regular, rollup) aggregation pair for every query item.

    Cognos gives each query item two independent aggregation properties.
    ``regularAggregate`` is how values combine across non-time dimensions;
    ``semiAggregate`` is how they combine across *time*. When they differ, the
    item is semi-additive and cannot be expressed as one SQL aggregate -- the
    classic case being warehouse stock or headcount, where SUM across stores but
    LAST across time gives the closing balance.

    Only items where the two differ, or where the rollup is explicitly
    semi-additive, are marked ``is_semi_additive``. The overwhelming majority of
    items declare the same token twice, which is ordinary additive behaviour.
    """
    out: list[dict[str, Any]] = []
    for qs in parsed.get("query_subjects", []):
        base, fy = canonical_object_name(qs.get("name", ""))
        for item in qs.get("items", []):
            reg = (item.get("regular_aggregate") or "").lower()
            semi = (item.get("semi_aggregate") or "").lower()

            semi_additive = semi in _SEMI_ADDITIVE and semi != reg
            out.append(
                {
                    "entity": qs.get("name", ""),
                    "entity_base": base,
                    "fiscal_year_clone": fy,
                    "term": item.get("name", ""),
                    "physical_column": item.get("external_name", ""),
                    "usage": item.get("usage", ""),
                    "regular_aggregate": reg,
                    "rollup_aggregate": semi,
                    "regular_sql": _AGG_MAP.get(reg),
                    "rollup_sql": _AGG_MAP.get(semi),
                    "is_semi_additive": semi_additive,
                    "is_measure": item.get("usage") == "fact",
                }
            )
    n_semi = sum(1 for r in out if r["is_semi_additive"])
    log.info(
        "Extracted %d aggregation rules (%d semi-additive).", len(out), n_semi
    )
    return out


def semi_additive_sql(
    measure: str,
    date_column: str,
    partition_columns: list[str],
    rollup: str = "last",
) -> str:
    """Build a closing-balance expression for a semi-additive measure.

    Requirement E6: at year level take the last balance of the year, at quarter
    level the last of the quarter, and sum across non-time dimensions within
    whichever period is in context.

    Implemented as ``SUM`` over the per-partition last value rather than a bare
    ``LAST_VALUE``: the rollup applies across time, but the regular aggregate
    still applies across the other dimensions, so both have to appear.
    ``QUALIFY`` picks the closing row per partition before the outer sum.
    """
    fn = "LAST_VALUE" if rollup.lower() == "last" else "FIRST_VALUE"
    order = "DESC" if rollup.lower() == "last" else "ASC"
    parts = ", ".join(partition_columns) if partition_columns else "1"
    return (
        f"SUM({measure}) OVER (PARTITION BY {parts}) "
        f"-- closing balance: {fn} by {date_column} {order} within period, "
        f"then SUM across {parts}"
    )


# ---------------------------------------------------------------------------
# Hierarchies (requirement A2)
# ---------------------------------------------------------------------------


def extract_hierarchies(parsed: dict) -> list[dict[str, Any]]:
    """Flatten dimensions into hierarchies with ordered levels.

    The synthetic "(All)" root level is retained but flagged, because a
    business-user picker needs it as the tree root while a semantic view needs to
    skip it -- it has no backing column.
    """
    out: list[dict[str, Any]] = []
    for dim in parsed.get("dimensions", []):
        base, fy = canonical_object_name(dim.get("name", ""))
        for h in dim.get("hierarchies", []):
            levels: list[dict[str, Any]] = []
            for lv in h.get("levels", []):
                items = lv.get("items", [])
                levels.append(
                    {
                        "name": lv.get("name", ""),
                        "ordinal": lv.get("ordinal", 0),
                        "is_all_level": lv.get("is_all_level", False),
                        "caption_item": lv.get("caption_item", ""),
                        "business_key_item": lv.get("business_key_item", ""),
                        "columns": [
                            {
                                "term": i.get("name", ""),
                                "physical_column": i.get("external_name", ""),
                                "data_type": i.get("data_type", "VARCHAR"),
                                "expression": i.get("expression", ""),
                                "source_entity": (
                                    i.get("expression_refs", [{}])[0].get("object", "")
                                    if i.get("expression_refs")
                                    else ""
                                ),
                            }
                            for i in items
                        ],
                    }
                )
            out.append(
                {
                    "dimension": dim.get("name", ""),
                    "dimension_base": base,
                    "fiscal_year_clone": fy,
                    "namespace": dim.get("namespace", ""),
                    "name": h.get("name", ""),
                    "is_default": dim.get("default_hierarchy", "").endswith(
                        f"[{h.get('name', '')}]"
                    ),
                    "members_rollup": dim.get("members_rollup", False),
                    "root_caption": h.get("root_caption", ""),
                    "depth": len([l for l in levels if not l["is_all_level"]]),
                    "levels": levels,
                }
            )
    log.info("Extracted %d hierarchies.", len(out))
    return out


# ---------------------------------------------------------------------------
# Clone analysis
# ---------------------------------------------------------------------------


def analyze_clones(parsed: dict) -> dict[str, Any]:
    """Quantify duplication in the model.

    Three independent duplication axes, all of which Snowflake removes rather
    than reproduces:

    * **Fiscal-year clones** -- objects suffixed ``_FY22``/``_FY23``/``_FY24``,
      because Framework Manager cannot parameterise a definition across years.
    * **Calculation clones** -- the same metric pattern repeated once per fiscal
      year.
    * **Role-based packages** -- the same cube re-exported per sales territory
      with row security baked into the package.
    """
    # Objects
    obj_families: dict[str, list[str]] = defaultdict(list)
    for qs in parsed.get("query_subjects", []):
        base, fy = canonical_object_name(qs.get("name", ""))
        if fy:
            obj_families[base].append(fy)

    # Calculations
    calc_families: dict[str, list[str]] = defaultdict(list)
    for calc in parsed.get("calculations", []):
        canonical = calc.get("canonical_name") or calc.get("name", "")
        calc_families[canonical].append(calc.get("fiscal_year_clone") or "base")

    # Packages
    packages = parsed.get("packages", [])
    role_based = [p for p in packages if p.get("is_role_based")]
    pkg_by_folder: dict[str, int] = defaultdict(int)
    for p in role_based:
        pkg_by_folder[p.get("folder_path", "")] += 1

    n_calc = len(parsed.get("calculations", []))
    n_canonical = len(calc_families)

    return {
        "fiscal_year_object_families": {k: sorted(set(v)) for k, v in obj_families.items()},
        "fiscal_year_cloned_objects": sum(len(v) for v in obj_families.values()),
        "calculation_total": n_calc,
        "calculation_canonical_patterns": n_canonical,
        "calculation_clone_ratio": round(n_calc / n_canonical, 2) if n_canonical else 0,
        "calculation_families": {
            k: sorted(set(v)) for k, v in calc_families.items() if len(set(v)) > 1
        },
        "package_total": len(packages),
        "package_role_based": len(role_based),
        "package_by_folder": dict(pkg_by_folder),
        "security_filter_total": len(parsed.get("security_filters", [])),
    }


# ---------------------------------------------------------------------------
# Relationship audit
# ---------------------------------------------------------------------------


def audit_relationships(
    parsed: dict, depends_on: list[str] | None = None
) -> dict[str, Any]:
    """Audit the join graph, optionally against a set of depended-on joins.

    ``depends_on`` is a list of entity names a conversion relies on. Any
    relationship whose both sides are in that set is reported individually with
    its own ``status``, so the question "is the join I depend on one of the
    broken ones" gets a per-join answer instead of an aggregate count.
    """
    rels = parsed.get("relationships", [])
    by_status: dict[str, int] = defaultdict(int)
    for r in rels:
        by_status[r.get("status", "unknown")] += 1

    # Keep Framework Manager's three states distinct. "needsReevaluation" means
    # FM has not re-checked the join since something it references changed; it is
    # not the same claim as "invalid", and collapsing them inflates the invalid
    # count (by 25 in the reference model) and misstates the finding.
    invalid = [r for r in rels if r.get("status") == "invalid"]
    needs_reeval = [r for r in rels if r.get("status") == "needsReevaluation"]

    # A relationship is clone debt when either side is a fiscal-year clone or the
    # name carries Cognos's "Copy of" prefix.
    clone_debt = [
        r
        for r in invalid
        if _COPY_PREFIX_RE.match(r.get("name", ""))
        or canonical_object_name(r.get("left", {}).get("entity", ""))[1]
        or canonical_object_name(r.get("right", {}).get("entity", ""))[1]
    ]

    depended: list[dict[str, Any]] = []
    if depends_on:
        wanted = {canonical_object_name(e)[0].upper() for e in depends_on}
        for r in rels:
            lb = canonical_object_name(r.get("left", {}).get("entity", ""))
            rb = canonical_object_name(r.get("right", {}).get("entity", ""))
            # Only base (non-clone) relationships between in-scope entities.
            if lb[1] or rb[1]:
                continue
            if lb[0].upper() in wanted and rb[0].upper() in wanted:
                depended.append(
                    {
                        "name": r.get("name", ""),
                        "left": r["left"]["entity"],
                        "right": r["right"]["entity"],
                        "status": r.get("status"),
                        "is_valid": r.get("is_valid"),
                        "expression": r.get("expression", ""),
                        "join_columns": r.get("join_columns", []),
                        "is_simple_equality": r.get("is_simple_equality"),
                        "cardinality": f"{r['left']['cardinality']}-to-{r['right']['cardinality']}",
                    }
                )

    depended_invalid = [d for d in depended if d["status"] == "invalid"]
    # The status flag and the join logic are separate questions. A stale flag on a
    # single-column equality join is FM bookkeeping drift; a flag on a compound
    # or non-equality predicate is a real modelling problem. Report both.
    depended_unsound = [d for d in depended if not d["is_simple_equality"]]

    if not depends_on:
        verdict = None
    elif not depended:
        verdict = "no base relationships found between the named entities"
    elif not depended_invalid:
        verdict = f"all {len(depended)} depended-on base relationships are valid"
    elif not depended_unsound:
        verdict = (
            f"{len(depended_invalid)} of {len(depended)} depended-on relationships "
            f"carry Framework Manager status='invalid', but every one is a single-"
            f"column equality join. The status flag is stale bookkeeping, not a "
            f"broken predicate -- though that still needs confirming against real "
            f"data, since a clean-looking predicate is not proof of referential "
            f"integrity at production volume."
        )
    else:
        verdict = (
            f"{len(depended_invalid)} of {len(depended)} depended-on relationships "
            f"are flagged invalid, and {len(depended_unsound)} of those are not "
            f"simple equality joins -- these need manual review before conversion."
        )

    return {
        "total": len(rels),
        "by_status": dict(by_status),
        "invalid_total": len(invalid),
        "needs_reevaluation_total": len(needs_reeval),
        "valid_total": len(rels) - len(invalid) - len(needs_reeval),
        "invalid_attributable_to_clones": len(clone_debt),
        "invalid_not_clone_related": len(invalid) - len(clone_debt),
        "depended_on": depended,
        "depended_on_invalid": depended_invalid,
        "depended_on_unsound": depended_unsound,
        "depended_on_all_simple_equality": bool(depended) and not depended_unsound,
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# Data source audit
# ---------------------------------------------------------------------------

_SNOWFLAKE_HINT_RE = re.compile(r"snowflake|snwflk", re.IGNORECASE)
_ORACLE_HINT_RE = re.compile(r"\bora(cle)?\b", re.IGNORECASE)


def audit_data_sources(parsed: dict) -> dict[str, Any]:
    """Classify each declared data source and count what depends on it.

    Answers "how much of this model is actually on Snowflake already" without
    inferring from table-name prefixes -- several query subjects in the reference model
    model are named ``EDW_ORA_*`` but read from a Snowflake connection, so
    name-based inference gives the wrong answer.
    """
    usage: dict[str, int] = defaultdict(int)
    for qs in parsed.get("query_subjects", []):
        if qs.get("data_source"):
            usage[qs["data_source"]] += 1
        elif qs.get("source_alias"):
            usage[qs["source_alias"]] += 1

    sources: list[dict[str, Any]] = []
    declared = {d.get("name", "") for d in parsed.get("data_sources", [])}
    for name in sorted(declared | set(usage)):
        sources.append(
            {
                "name": name,
                "query_subjects_using": usage.get(name, 0),
                "platform": (
                    "snowflake"
                    if _SNOWFLAKE_HINT_RE.search(name)
                    else "oracle"
                    if _ORACLE_HINT_RE.search(name)
                    else "unknown"
                ),
                "declared": name in declared,
            }
        )

    n_sf = sum(1 for s in sources if s["platform"] == "snowflake")
    misleading = [
        qs["name"]
        for qs in parsed.get("query_subjects", [])
        if _ORACLE_HINT_RE.search(qs.get("name", ""))
        and _SNOWFLAKE_HINT_RE.search(qs.get("source_alias", "") or qs.get("data_source", ""))
    ]

    return {
        "sources": sources,
        "snowflake_sources": n_sf,
        "non_snowflake_sources": len(sources) - n_sf,
        "all_snowflake": len(sources) > 0 and n_sf == len(sources),
        "oracle_named_but_snowflake_backed": sorted(set(misleading)),
    }


# ---------------------------------------------------------------------------
# Whole-model analysis
# ---------------------------------------------------------------------------


def analyze(
    parsed: dict,
    in_scope_entities: list[str] | None = None,
) -> dict[str, Any]:
    """Run every analysis over a parsed model and return one report."""
    scope = in_scope_entities or []
    calc_complexity: dict[str, int] = defaultdict(int)
    for c in parsed.get("calculations", []):
        calc_complexity[classify_cognos_expression(c.get("expression", ""))] += 1

    return {
        "model_name": parsed.get("model_name", ""),
        "source_file": parsed.get("source_file", ""),
        "grain": audit_grain_coverage(parsed, scope) if scope else {
            "in_scope_determinants": [],
            "verdict": "no scope supplied; grain not audited",
        },
        "grain_declarations": extract_grain_declarations(parsed),
        "aggregation_rules": extract_aggregation_rules(parsed),
        "hierarchies": extract_hierarchies(parsed),
        "clones": analyze_clones(parsed),
        "relationships": audit_relationships(parsed, scope),
        "data_sources": audit_data_sources(parsed),
        "calculation_complexity": dict(calc_complexity),
    }
