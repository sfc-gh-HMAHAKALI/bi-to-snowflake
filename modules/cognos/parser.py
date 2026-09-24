"""Cognos Framework Manager `model.xml` parser.

Uses ``iterparse`` rather than a full-tree parse. A real Framework Manager
export is large -- the reference model is 18MB with 57,763 ``refobj`` nodes and
19,921 ``securityFilterDefinition`` nodes -- and building the whole DOM before
extracting costs several hundred MB of resident memory for no benefit, since
every construct we care about is self-contained within one element subtree.

The parse is single-pass. Each top-level construct is handled when its closing
tag is reached, then ``elem.clear()`` releases the subtree. Ancestors are
tracked on an explicit stack because ``iterparse`` gives no parent pointers.
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterator
from xml.etree import ElementTree as ET

from ..common.errors import ParseError, fail_step
from ..common.logger import get_logger

log = get_logger(__name__)

# Security limits for zipped Framework Manager exports.
_MAX_UNCOMPRESSED_SIZE = 1_073_741_824  # 1 GB
_MAX_MEMBER_COUNT = 10_000

# Framework Manager writes an unprefixed default namespace, so every tag comes
# back from iterparse as "{http://...}querySubject". We strip it rather than
# hardcode the version, because the BMT schema URI carries a version number
# (".../bmt/60/12") that differs across Cognos releases.
_NS_RE = re.compile(r"^\{[^}]*\}")

# A Cognos object reference: "[Import View].[FACT_SALES_SUMMARY].[PROCESSED_DATE]"
_REFOBJ_PART_RE = re.compile(r"\[([^\]]*)\]")

# Constructs we handle at close tag. Anything else is skipped but counted, so
# the coverage report can show what the model contained that we ignored.
_HANDLED = frozenset(
    {
        "querySubject",
        "relationship",
        "calculation",
        "dimension",
        "securityFilterDefinition",
        "package",
        "dataSource",
        "filter",
    }
)


def _tag(elem: ET.Element) -> str:
    """Local tag name with any XML namespace stripped."""
    return _NS_RE.sub("", elem.tag)


def _text(elem: ET.Element | None, default: str = "") -> str:
    """Whitespace-collapsed text of an element, ignoring child markup."""
    if elem is None:
        return default
    raw = "".join(elem.itertext())
    return re.sub(r"\s+", " ", raw).strip() or default


def _child(elem: ET.Element, name: str) -> ET.Element | None:
    """First direct child with the given local tag name."""
    for c in elem:
        if _tag(c) == name:
            return c
    return None


def _children(elem: ET.Element, name: str) -> list[ET.Element]:
    """All direct children with the given local tag name."""
    return [c for c in elem if _tag(c) == name]


def _name_of(elem: ET.Element) -> str:
    """Value of the <name> child. Cognos localises names via locale attr."""
    return _text(_child(elem, "name"))


def parse_refobj(ref: str) -> dict[str, str]:
    """Split a Cognos object reference into its bracketed parts.

    ``[Import View].[FACT_SALES_SUMMARY].[PROCESSED_DATE]`` becomes
    ``{"namespace": "Import View", "object": "FACT_SALES_SUMMARY",
    "item": "PROCESSED_DATE"}``.

    Four-part references occur inside DMR hierarchies
    (``[DMR View].[WTD Grouped].[WTD Grouped].[RELATIVE_TIME_DESC]``); the
    duplicated middle part is the hierarchy name, so the last part is still the
    item and the second is still the owning object.
    """
    parts = _REFOBJ_PART_RE.findall(ref or "")
    if not parts:
        return {"namespace": "", "object": "", "item": "", "raw": ref or ""}
    return {
        "namespace": parts[0] if len(parts) > 1 else "",
        "object": parts[1] if len(parts) > 2 else (parts[0] if len(parts) > 1 else ""),
        "item": parts[-1] if len(parts) > 1 else parts[0],
        "raw": ref or "",
    }


def _refobjs(elem: ET.Element) -> list[str]:
    """Every refobj value anywhere beneath an element, in document order."""
    return [
        (r.text or "").strip()
        for r in elem.iter()
        if _tag(r) == "refobj" and (r.text or "").strip()
    ]


# ---------------------------------------------------------------------------
# Datatype mapping
# ---------------------------------------------------------------------------

# Framework Manager datatype tokens -> Snowflake types. Cognos encodes width in
# the token itself (characterLength16 = 16-bit length prefix, i.e. varchar), so
# the token is not a width and must not be parsed as one.
_DATATYPE_MAP: dict[str, str] = {
    "characterlength16": "VARCHAR",
    "character": "VARCHAR",
    "nvarchar": "VARCHAR",
    "varchar": "VARCHAR",
    "int16": "NUMBER",
    "int32": "NUMBER",
    "int64": "NUMBER",
    "decimal": "NUMBER",
    "numeric": "NUMBER",
    "float": "FLOAT",
    "double": "FLOAT",
    "date": "DATE",
    "datetime": "TIMESTAMP_NTZ",
    "time": "TIME",
    "interval": "VARCHAR",
    "boolean": "BOOLEAN",
    "binary": "BINARY",
    "unknown": "VARCHAR",
}


def map_cognos_datatype(
    datatype: str, scale: str = "", precision: str = "", size: str = ""
) -> str:
    """Map a Framework Manager datatype token to a Snowflake type.

    ``dateTime`` needs care. The Cognos JDBC driver reports a physical Snowflake
    DATE column as ``dateTime``, sometimes with ``scale`` 6, which looks like a
    timestamp but is not: every such item in the observed models carries
    ``precision`` 0 and ``size`` 12. A genuine timestamp reports a nonzero
    precision. So precision, not scale, is the discriminator -- keying off scale
    alone misclassifies 168 of the 285 date columns in the reference model as
    TIMESTAMP_NTZ, which would then generate wrong DDL for the physical layer.
    """
    key = (datatype or "unknown").strip().lower()
    mapped = _DATATYPE_MAP.get(key, "VARCHAR")

    if mapped == "TIMESTAMP_NTZ":
        try:
            prec = int(precision or 0)
        except (TypeError, ValueError):
            prec = 0
        try:
            sz = int(size or 0)
        except (TypeError, ValueError):
            sz = 0
        # precision 0 => no sub-day component declared. size <= 12 corroborates:
        # no timestamp representation fits in 12 bytes.
        if prec == 0 and (sz == 0 or sz <= 12):
            return "DATE"

    return mapped


# ---------------------------------------------------------------------------
# Query subject / query item
# ---------------------------------------------------------------------------

# "Select * from [RAPID_Snowflake_Heavy1].DIM_DATE" -> passthrough.
# Anything with a WHERE, JOIN, CASE, or a column list is not a passthrough.
_PASSTHROUGH_RE = re.compile(
    r"^\s*select\s+(distinct\s+)?\*\s+from\s+\[?([\w$]+)\]?\.\[?([\w$.]+)\]?\s*;?\s*$",
    re.IGNORECASE,
)


def _sql_parts(sql_elem: ET.Element | None) -> dict[str, Any]:
    """Extract the structured parts of a Cognos ``<sql>`` element.

    Framework Manager marks up the SQL rather than storing it as flat text::

        <sql type="cognos">Select <column>*</column>from<table>[CONN].FACT_X</table></sql>

    Reading the ``<table>`` and ``<column>`` children is both more reliable and
    cheaper than regex-matching the reconstructed string -- and necessary here,
    because ``itertext`` concatenates without whitespace, producing ``*from`` and
    defeating any ``SELECT \\* FROM`` pattern. Some subjects store plain text
    instead, so the text path is retained as a fallback.
    """
    if sql_elem is None:
        return {"text": "", "tables": [], "columns": [], "tagged": False}
    tables = [_text(t) for t in sql_elem.iter() if _tag(t) == "table" and _text(t)]
    columns = [_text(c) for c in sql_elem.iter() if _tag(c) == "column" and _text(c)]
    return {
        "text": _text(sql_elem),
        "tables": tables,
        "columns": columns,
        "tagged": bool(tables or columns),
    }


def _classify_sql(sql: str) -> tuple[bool, str, str]:
    """Return (is_passthrough, source_ref, physical_object) for plain-text SQL.

    Fallback for query subjects that store SQL as text rather than as tagged
    markup. ``_sql_parts`` handles the tagged form.
    """
    m = _PASSTHROUGH_RE.match(sql or "")
    if not m:
        return False, "", ""
    return True, m.group(2), m.group(3)


def _split_qualified(ref: str) -> tuple[str, str]:
    """Split ``[CONN].SCHEMA_OR_TABLE`` into (connection_alias, object)."""
    ref = (ref or "").strip()
    m = re.match(r"^\[?([\w$]+)\]?\.(.+)$", ref)
    if not m:
        return "", ref
    return m.group(1), m.group(2).strip().strip("[]")


def _parse_query_item(qi: ET.Element, owner: str, namespace: str) -> dict[str, Any]:
    """Parse one <queryItem> into a term record."""
    expr_elem = _child(qi, "expression")
    expression = _text(expr_elem)
    datatype = _text(_child(qi, "datatype"), "unknown")
    scale = _text(_child(qi, "scale"))
    precision = _text(_child(qi, "precision"))
    size = _text(_child(qi, "size"))

    roles = [
        _name_of(r)
        for r in (_children(_child(qi, "roles"), "role") if _child(qi, "roles") is not None else [])
    ]

    return {
        "name": _name_of(qi),
        "namespace": namespace,
        "owner": owner,
        # externalName is the physical column. Where absent (calculated items)
        # the item exists only in the model layer.
        "external_name": _text(_child(qi, "external" "Name")),
        "usage": (_text(_child(qi, "usage"), "attribute") or "attribute").lower(),
        "datatype_raw": datatype,
        "data_type": map_cognos_datatype(datatype, scale, precision, size),
        "precision": precision,
        "scale": scale,
        "size": size,
        "nullable": _text(_child(qi, "nullable")).lower() == "true",
        "regular_aggregate": (_text(_child(qi, "regularAggregate"), "unsupported") or "").lower(),
        "semi_aggregate": (_text(_child(qi, "semiAggregate"), "unsupported") or "").lower(),
        "expression": expression,
        "expression_refs": [parse_refobj(r) for r in _refobjs(expr_elem)] if expr_elem is not None else [],
        "is_calculated": bool(expression),
        "roles": roles,
        "description": _text(_child(qi, "description")),
        "last_changed": _text(_child(qi, "lastChanged")),
        "last_changed_by": _text(_child(qi, "lastChangedBy")),
    }


def _parse_determinants(qs: ET.Element, owner: str) -> list[dict[str, Any]]:
    """Parse <determinants> into grain declarations.

    A determinant says: *these key columns uniquely identify a row at this
    level, and these attribute columns are functionally dependent on them.*
    ``identifiesRow=true`` means the key is the table's actual grain;
    ``canGroup=true`` means Cognos may group to this level, which is what makes
    multi-grain reporting safe (requirement E2).
    """
    out: list[dict[str, Any]] = []
    dets = _child(qs, "determinants")
    if dets is None:
        return out
    for d in _children(dets, "determinant"):
        key_elem = _child(d, "key")
        attrs_elem = _child(d, "attributes")
        out.append(
            {
                "name": _text(_child(d, "name")),
                "entity": owner,
                "key_columns": [parse_refobj(r)["item"] for r in _refobjs(key_elem)] if key_elem is not None else [],
                "attribute_columns": [parse_refobj(r)["item"] for r in _refobjs(attrs_elem)] if attrs_elem is not None else [],
                "can_group": _text(_child(d, "canGroup")).lower() == "true",
                "identifies_row": _text(_child(d, "identifiesRow")).lower() == "true",
            }
        )
    return out


def _parse_query_subject(qs: ET.Element, namespace: str) -> dict[str, Any]:
    """Parse one <querySubject> into an entity record plus its items."""
    name = _name_of(qs)
    definition = _child(qs, "definition")
    db_query = _child(definition, "dbQuery") if definition is not None else None
    model_query = _child(definition, "modelQuery") if definition is not None else None

    sql = ""
    data_source = ""
    table_type = ""
    kind = "model"
    src_alias = ""
    physical = ""
    wraps: list[str] = []
    container = None

    if db_query is not None:
        kind = "db"
        container = db_query
        table_type = _text(_child(db_query, "tableType"))
        sources = _child(db_query, "sources")
        if sources is not None:
            # The connection is referenced by <dataSourceRef>, not <refobj>:
            #   [].[dataSources].[RAPID_Snowflake_Heavy]
            ds_refs = [_text(d) for d in _children(sources, "dataSourceRef")]
            if not ds_refs:
                ds_refs = _refobjs(sources)
            if ds_refs:
                data_source = parse_refobj(ds_refs[0])["item"]
    elif model_query is not None:
        kind = "model"
        container = model_query

    if container is not None:
        parts = _sql_parts(_child(container, "sql"))
        sql = parts["text"]
        if parts["tables"]:
            # Tagged form: the <table> child names the physical object outright.
            src_alias, physical = _split_qualified(parts["tables"][0])
            is_passthrough = parts["columns"] == ["*"] and len(parts["tables"]) == 1
        elif parts["tagged"]:
            # Tagged but with an empty <table/>: a model-layer subject that wraps
            # another query subject rather than reading a physical table.
            is_passthrough = parts["columns"] == ["*"]
        else:
            is_passthrough, src_alias, physical = _classify_sql(sql)
    else:
        is_passthrough = False

    # Model-layer subjects declare what they wrap through their items' refobjs.
    if kind == "model":
        for qi in _children(qs, "queryItem"):
            expr = _child(qi, "expression")
            if expr is None:
                continue
            for r in _refobjs(expr):
                obj = parse_refobj(r)["object"]
                if obj and obj != name and obj not in wraps:
                    wraps.append(obj)

    # Filters attached to the query subject itself. These are real model logic --
    # the model-layer FACT_SALES_SUMMARY carries PROCESSED_DATE <= CURRENT_DATE --
    # so a conversion that ignores them silently widens the row set.
    filters: list[dict[str, Any]] = []
    for holder in (container, qs):
        if holder is None:
            continue
        fl = _child(holder, "filters")
        if fl is None:
            continue
        for fd in _children(fl, "filterDefinition"):
            filters.append(
                {
                    "name": _text(_child(fd, "displayName")),
                    "expression": _text(_child(fd, "expression")),
                    "application": fd.get("application", ""),
                    "apply": fd.get("apply", ""),
                }
            )

    # Security filters declared on this subject name the entity being protected,
    # which the global filter population does not -- the predicates reference the
    # territory table, not the fact table they guard.
    protected_by: list[str] = []
    sec = _child(qs, "securityFilters")
    if sec is not None and _children(sec, "securityFilterDefinition"):
        protected_by = [
            _text(_child(sf, "displayName"))
            for sf in _children(sec, "securityFilterDefinition")
        ]

    items = [
        _parse_query_item(qi, name, namespace)
        for qi in _children(qs, "queryItem")
    ]

    return {
        "name": name,
        "namespace": namespace,
        "status": qs.get("status", "unknown"),
        "kind": kind,
        "sql": sql,
        "data_source": data_source,
        "source_alias": src_alias or data_source,
        "physical_object": physical,
        "wraps_entities": wraps,
        "table_type": table_type,
        "is_passthrough": bool(is_passthrough) and not filters,
        "filters": filters,
        "has_security_filters": bool(protected_by),
        "security_filter_count": len(protected_by),
        "items": items,
        "determinants": _parse_determinants(qs, name),
        "last_changed": _text(_child(qs, "lastChanged")),
        "last_changed_by": _text(_child(qs, "lastChangedBy")),
    }


# ---------------------------------------------------------------------------
# Relationship
# ---------------------------------------------------------------------------

_CARD_MAP = {
    ("one", "one"): "one",
    ("one", "many"): "many",
    ("zero", "one"): "zero_or_one",
    ("zero", "many"): "zero_or_many",
}


def _parse_relationship(rel: ET.Element) -> dict[str, Any]:
    """Parse one <relationship> into a join record.

    Cognos states cardinality as a (mincard, maxcard) pair per side. The
    ``status`` attribute is Framework Manager's own validity bookkeeping, which
    goes stale independently of whether the join is sound -- so it is recorded
    as metadata, never used to filter the join out.
    """
    expr_elem = _child(rel, "expression")
    expression = _text(expr_elem)
    left = _child(rel, "left")
    right = _child(rel, "right")

    def side(s: ET.Element | None) -> dict[str, Any]:
        if s is None:
            return {"entity": "", "mincard": "", "maxcard": "", "cardinality": ""}
        refs = _refobjs(s)
        mn = _text(_child(s, "mincard")).lower()
        mx = _text(_child(s, "maxcard")).lower()
        return {
            "entity": parse_refobj(refs[0])["item"] if refs else "",
            "mincard": mn,
            "maxcard": mx,
            "cardinality": _CARD_MAP.get((mn, mx), f"{mn}_{mx}"),
        }

    # The join predicate's refobjs alternate left-column, right-column for a
    # simple equality join. Multi-predicate joins (AND-ed) yield more pairs.
    ref_parts = [parse_refobj(r) for r in (_refobjs(expr_elem) if expr_elem is not None else [])]
    is_equality = bool(expression) and "=" in expression and not re.search(
        r"\b(between|like|>|<|case|or)\b", expression, re.IGNORECASE
    )

    return {
        "name": _text(_child(rel, "name")),
        "status": rel.get("status", "unknown"),
        "is_valid": rel.get("status", "") == "valid",
        "expression": expression,
        "left": side(left),
        "right": side(right),
        "join_columns": [p["item"] for p in ref_parts],
        "is_simple_equality": is_equality,
        "predicate_count": expression.upper().count(" AND ") + 1 if expression else 0,
    }


# ---------------------------------------------------------------------------
# Calculation
# ---------------------------------------------------------------------------

# Cognos stores the display format as doubly-escaped XML inside <format>.
_FMT_CURRENCY_RE = re.compile(r"currencyFormat", re.IGNORECASE)
_FMT_PERCENT_RE = re.compile(r"percentFormat", re.IGNORECASE)
_FMT_DECIMALSIZE_RE = re.compile(r"decimalSize=&?(?:amp;)?quot;(\d+)", re.IGNORECASE)

# Fiscal-year clone suffix. Framework Manager has no way to parameterise a
# calculation across fiscal years, so the whole object is duplicated per year.
_FY_SUFFIX_RE = re.compile(r"[\s_-]*(?:_)?FY\s?(\d{2,4})\s*$", re.IGNORECASE)


def canonical_calculation_name(name: str) -> tuple[str, str | None]:
    """Strip a fiscal-year clone suffix.

    ``"YTD Growth_FY23"`` -> ``("YTD Growth", "FY23")``.
    ``"YTD Growth"``      -> ``("YTD Growth", None)``.

    Used to collapse the clone families into one canonical pattern per metric.
    """
    m = _FY_SUFFIX_RE.search(name or "")
    if not m:
        return (name or "").strip(), None
    return _FY_SUFFIX_RE.sub("", name).strip(), f"FY{m.group(1)}"


def _parse_format(fmt: str) -> dict[str, Any]:
    """Extract the presentation format from a Cognos <format> blob."""
    if not fmt:
        return {"kind": "unknown", "decimals": None}
    kind = "number"
    if _FMT_CURRENCY_RE.search(fmt):
        kind = "currency"
    elif _FMT_PERCENT_RE.search(fmt):
        kind = "percent"
    m = _FMT_DECIMALSIZE_RE.search(fmt)
    return {"kind": kind, "decimals": int(m.group(1)) if m else None}


def _parse_calculation(calc: ET.Element, namespace: str) -> dict[str, Any]:
    """Parse one <calculation> into a metric record."""
    name = _name_of(calc)
    canonical, fy = canonical_calculation_name(name)
    expr_elem = _child(calc, "expression")
    expression = _text(expr_elem)
    fmt = _parse_format(_text(_child(calc, "format")))

    return {
        "name": name,
        "canonical_name": canonical,
        "fiscal_year_clone": fy,
        "namespace": namespace,
        "status": calc.get("status", "unknown"),
        "expression": expression,
        "expression_refs": [parse_refobj(r) for r in (_refobjs(expr_elem) if expr_elem is not None else [])],
        "format_kind": fmt["kind"],
        "format_decimals": fmt["decimals"],
        "currency": _text(_child(calc, "currency")),
        "last_changed": _text(_child(calc, "lastChanged")),
        "last_changed_by": _text(_child(calc, "lastChangedBy")),
    }


# ---------------------------------------------------------------------------
# Dimension / hierarchy / level
# ---------------------------------------------------------------------------


def _parse_dimension(dim: ET.Element, namespace: str) -> dict[str, Any]:
    """Parse one <dimension> into a hierarchy record with ordered levels.

    The first level of a Cognos hierarchy is conventionally the synthetic "(All)"
    root, marked ``isManual``. It carries no query item and is recorded with
    ``is_all_level`` so a UI can render it as the root node rather than a
    selectable attribute.
    """
    name = _name_of(dim)
    hierarchies: list[dict[str, Any]] = []

    for h in _children(dim, "hierarchy"):
        levels: list[dict[str, Any]] = []
        for ordinal, lv in enumerate(_children(h, "level")):
            qis = _children(lv, "queryItem")
            items = [_parse_query_item(qi, name, namespace) for qi in qis]
            # Cognos marks which item supplies the displayed caption and which
            # is the business key via intrinsic roles.
            caption = next((i["name"] for i in items if "_memberCaption" in i["roles"]), "")
            bkey = next((i["name"] for i in items if "_businessKey" in i["roles"]), "")
            levels.append(
                {
                    "name": _name_of(lv),
                    "ordinal": ordinal,
                    "is_all_level": _text(_child(lv, "isManual")).lower() == "true" and not qis,
                    "items": items,
                    "caption_item": caption,
                    "business_key_item": bkey,
                }
            )
        hierarchies.append(
            {
                "name": _name_of(h),
                "levels": levels,
                "root_caption": _text(_child(h, "rootCaption")),
                "root_mun": _text(_child(h, "rootMUN")),
            }
        )

    return {
        "name": name,
        "namespace": namespace,
        "status": dim.get("status", "unknown"),
        "type": _text(_child(dim, "type"), "regular"),
        "members_rollup": _text(_child(dim, "membersRollup")).lower() == "true",
        "default_hierarchy": _text(_child(dim, "defaultHierarchy")),
        "hierarchies": hierarchies,
    }


# ---------------------------------------------------------------------------
# Security filter definition
# ---------------------------------------------------------------------------

# "CAMID(":SALES:REGION:AREA-ROLE:EAST_GREATLAKES_AREA_CLEVELAND")"
_CAMID_RE = re.compile(r'CAMID\(\s*"?:?([^")]*)"?\s*\)')
# A single equality predicate against a string literal.
_EQ_PRED_RE = re.compile(r"\[([^\]]+)\]\s*=\s*'([^']*)'")


def _parse_security_filter(sf: ET.Element) -> dict[str, Any]:
    """Parse one <securityFilterDefinition> into a row-level-security rule.

    Each definition binds a Cognos account/group to a row filter. In the reference model
    model these are almost all of the form
    ``ROLE = '<role>' AND TERRITORY_LEVEL4 = '<territory>'`` -- i.e. a two-column
    mapping from principal to (role, territory), which is exactly the shape of a
    Snowflake row access policy mapping table.
    """
    so = _child(sf, "securityObject")
    display_path = _text(_child(so, "displayPath")) if so is not None else ""
    cm_path = _text(_child(so, "cmSearchPath")) if so is not None else ""
    expr_elem = _child(sf, "expression")
    expression = _text(expr_elem)

    camid = _CAMID_RE.search(cm_path)
    principal_path = camid.group(1) if camid else ""
    principal_parts = [p for p in principal_path.split(":") if p]

    # Pull (column, literal) pairs out of the predicate so the rule can be
    # written to a mapping table instead of re-parsed downstream.
    refs = [parse_refobj(r) for r in (_refobjs(expr_elem) if expr_elem is not None else [])]
    literals = re.findall(r"'([^']*)'", expression)
    predicates: list[dict[str, str]] = []
    for i, ref in enumerate(refs):
        predicates.append(
            {
                "entity": ref["object"],
                "column": ref["item"],
                "operator": "=",
                "value": literals[i] if i < len(literals) else "",
            }
        )

    return {
        "principal": principal_parts[-1] if principal_parts else "",
        "principal_path": principal_path,
        "principal_groups": principal_parts[:-1],
        "principal_type": so.get("type", "unknown") if so is not None else "unknown",
        "display_path": display_path,
        "display_name": _text(_child(sf, "displayName")),
        "expression": expression,
        "predicates": predicates,
        "columns": sorted({p["column"] for p in predicates if p["column"]}),
    }


# ---------------------------------------------------------------------------
# Package
# ---------------------------------------------------------------------------


def _parse_package(pkg: ET.Element) -> dict[str, Any]:
    """Parse one <package>, the unit Cognos publishes to report authors."""
    name = _name_of(pkg)
    definition = _child(pkg, "definition")
    viewrefs: list[str] = []
    if definition is not None:
        viewrefs = [_text(v) for v in _children(definition, "viewref")]
    # "_CORP_NATIONAL_AER [Directory > Cognos > SALES > REGION > NAM-REGION]"
    base = name.split("[")[0].strip()
    folder = name.split("[", 1)[1].rstrip("]").strip() if "[" in name else ""
    return {
        "name": name,
        "base_name": base,
        "folder_path": folder,
        "is_role_based": pkg.get("isRoleBased", "") == "true",
        "view_refs": viewrefs,
        "last_changed": _text(_child(pkg, "lastChanged")),
        "last_changed_by": _text(_child(pkg, "lastChangedBy")),
    }


def _parse_data_source(ds: ET.Element) -> dict[str, Any]:
    """Parse one <dataSource> connection declaration."""
    return {
        "name": _name_of(ds),
        "cm_data_source": _text(_child(ds, "cmDataSource")),
        "schema": _text(_child(ds, "schema")),
        "catalog": _text(_child(ds, "catalog")),
        "type": _text(_child(ds, "type")),
        "interface": _text(_child(ds, "interface")),
    }


# ---------------------------------------------------------------------------
# Top-level parse
# ---------------------------------------------------------------------------


def _find_model_xml(root: str) -> str | None:
    """Find the shallowest model.xml under a directory.

    A Framework Manager export is usually zipped with the project folder at the
    top, so model.xml sits one or two levels down rather than at the root. The
    shallowest match wins: a deeper one is a segment or a backup copy.
    """
    best: tuple[int, str] | None = None
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith("__MACOSX")]
        for name in filenames:
            if name.lower() == "model.xml":
                found = os.path.join(dirpath, name)
                depth = found[len(root):].count(os.sep)
                if best is None or depth < best[0]:
                    best = (depth, found)
    return None if best is None else best[1]


def _extract_archive(path: str) -> str:
    """Extract a zipped export into a fresh private directory and return it.

    Always a newly created temporary directory, never a fixed path that would
    have to be emptied first: a blind delete in an agent's shell call is a
    destructive command the user has to approve, and the run stalls there.

    Guards against zip slip and zip bombs, matching the Tableau and Power BI
    archive paths.
    """
    import tempfile
    import zipfile

    dest = tempfile.mkdtemp(prefix="bi2sf-cognos-")
    try:
        with zipfile.ZipFile(path, "r") as zf:
            members = zf.infolist()
            if len(members) > _MAX_MEMBER_COUNT:
                raise ParseError(
                    "Archive has %d members, exceeding limit of %d"
                    % (len(members), _MAX_MEMBER_COUNT),
                    context={"path": path, "member_count": len(members)},
                )
            total = sum(info.file_size for info in members)
            if total > _MAX_UNCOMPRESSED_SIZE:
                raise ParseError(
                    "Archive uncompressed size (%d bytes) exceeds limit (%d bytes)"
                    % (total, _MAX_UNCOMPRESSED_SIZE),
                    context={"path": path, "uncompressed_size": total},
                )
            real_dest = os.path.realpath(dest)
            for info in members:
                target = os.path.realpath(os.path.join(dest, info.filename))
                if target != real_dest and not target.startswith(real_dest + os.sep):
                    raise ParseError(
                        "Zip slip detected: member '%s' escapes the extraction "
                        "directory" % info.filename,
                        context={"member": info.filename},
                    )
            zf.extractall(dest)
    except zipfile.BadZipFile as exc:
        raise ParseError(
            "Archive is not readable as a zip file",
            context={"path": path, "error": str(exc)},
        ) from exc
    log.info("Extracted %s to %s", path, dest)
    return dest


def _resolve_model_xml(path: str) -> str:
    """Accept a model.xml, a zipped export, a .cpf file, or a project directory.

    A zipped export is extracted here rather than by the caller, so no shell
    unzip step is needed to feed ``--extract`` a download straight from Cognos.
    """
    if os.path.isdir(path):
        found = _find_model_xml(path)
        if found:
            return found
        raise ParseError(
            "Directory contains no model.xml",
            context={"path": path},
        )
    if not os.path.exists(path):
        raise ParseError("File not found", context={"path": path})
    import zipfile

    if zipfile.is_zipfile(path):
        dest = _extract_archive(path)
        found = _find_model_xml(dest)
        if found:
            return found
        raise ParseError(
            "Archive contains no model.xml",
            context={"path": path, "extracted_to": dest},
        )
    if path.lower().endswith(".cpf"):
        found = _find_model_xml(os.path.dirname(path) or ".")
        if found:
            return found
        raise ParseError(
            "No model.xml beside the .cpf project file",
            context={"cpf": path},
        )
    if not os.path.isfile(path):
        raise ParseError("File not found", context={"path": path})
    return path


def parse_framework_manager_model(path: str) -> dict[str, Any]:
    """Parse a Framework Manager project into raw Cognos structures.

    Args:
        path: A ``model.xml``, a zipped export (``.zip``, or a ``.cpf``
            stored as a zip), a ``.cpf`` project file beside its ``model.xml``,
            or the directory containing any of these. An archive is extracted
            into a fresh temporary directory automatically.

    Returns:
        Dict with keys ``model_name``, ``namespaces``, ``data_sources``,
        ``query_subjects``, ``relationships``, ``calculations``, ``dimensions``,
        ``security_filters``, ``packages``, ``element_counts``, ``errors``.

    Never raises for a malformed individual construct: the construct is recorded
    in ``errors`` and the parse continues, so a partially-corrupt export still
    yields a usable inventory.
    """
    model_xml = _resolve_model_xml(path)
    size_mb = os.path.getsize(model_xml) / (1024 * 1024)
    log.info("Parsing Cognos Framework Manager model: %s (%.1f MB)", model_xml, size_mb)

    result: dict[str, Any] = {
        "source_file": model_xml,
        "source_size_mb": round(size_mb, 2),
        "model_name": "",
        "namespaces": [],
        "data_sources": [],
        "query_subjects": [],
        "relationships": [],
        "calculations": [],
        "dimensions": [],
        "security_filters": [],
        "packages": [],
        "filters": [],
        "element_counts": {},
        "errors": [],
    }

    counts: dict[str, int] = {}
    # Namespace stack, so a construct knows which Cognos namespace owns it
    # ("Import View", "Model View", "DMR View"). iterparse gives no parents.
    #
    # A namespace's own <name> child closes before its sibling constructs do, so
    # the slot is filled by the "name" end handler below rather than at the
    # namespace end tag -- by then every child has already been emitted and
    # would have been tagged with an empty namespace.
    ns_stack: list[str] = []
    # Stack of every open element tag, needed to tell a namespace's own <name>
    # from a <name> belonging to some nested construct.
    open_tags: list[str] = []
    # Depth counter per handled construct, so nested queryItems inside a
    # dimension are not also emitted as top-level query subjects.
    inside: dict[str, int] = {k: 0 for k in _HANDLED}

    try:
        context: Iterator[tuple[str, ET.Element]] = ET.iterparse(
            model_xml, events=("start", "end")
        )
        for event, elem in context:
            tag = _tag(elem)

            if event == "start":
                counts[tag] = counts.get(tag, 0) + 1
                open_tags.append(tag)
                if tag in inside:
                    inside[tag] += 1
                elif tag == "namespace":
                    # Name is not available yet; the "name" end handler fills it.
                    ns_stack.append("")
                continue

            # event == "end"
            open_tags.pop() if open_tags else None
            try:
                if tag == "name":
                    # Direct <name> child of the innermost open namespace: this
                    # is the namespace's own name. open_tags has already had this
                    # <name> popped, so the top is now its parent.
                    if open_tags and open_tags[-1] == "namespace" and ns_stack:
                        if not ns_stack[-1]:
                            ns_stack[-1] = _text(elem)
                    continue

                if tag == "namespace":
                    nm = ns_stack.pop() if ns_stack else ""
                    if nm:
                        result["namespaces"].append(nm)
                    # A namespace subtree is fully consumed by its children's
                    # own end handlers, so it is safe to clear here.
                    elem.clear()
                    continue

                if tag in inside:
                    inside[tag] -= 1

                current_ns = next((n for n in reversed(ns_stack) if n), "")

                if tag == "querySubject" and inside.get("dimension", 0) == 0:
                    result["query_subjects"].append(_parse_query_subject(elem, current_ns))
                    elem.clear()
                elif tag == "relationship":
                    result["relationships"].append(_parse_relationship(elem))
                    elem.clear()
                elif tag == "calculation":
                    result["calculations"].append(_parse_calculation(elem, current_ns))
                    elem.clear()
                elif tag == "dimension" and inside.get("dimension", 0) == 0:
                    result["dimensions"].append(_parse_dimension(elem, current_ns))
                    elem.clear()
                elif tag == "securityFilterDefinition":
                    result["security_filters"].append(_parse_security_filter(elem))
                    elem.clear()
                elif tag == "package":
                    result["packages"].append(_parse_package(elem))
                    elem.clear()
                elif tag == "dataSource":
                    result["data_sources"].append(_parse_data_source(elem))
                    elem.clear()
                elif tag == "filter" and inside.get("querySubject", 0) == 0:
                    result["filters"].append(
                        {
                            "name": _name_of(elem),
                            "namespace": current_ns,
                            "expression": _text(_child(elem, "filterDefinition")) or _text(_child(elem, "expression")),
                        }
                    )
                    elem.clear()
                elif tag == "project":
                    result["model_name"] = _name_of(elem)
            except Exception as e:  # one bad construct must not abort the parse
                result["errors"].append(
                    fail_step(f"parse_cognos_{tag}", e, partial_results={"tag": tag})
                )
    except ET.ParseError as e:
        raise ParseError(
            f"Malformed Cognos model.xml: {e}",
            context={"path": model_xml},
        ) from e

    result["element_counts"] = counts
    # <project>'s own <name> is consumed by the "name" handler above before any
    # namespace is open, so recover the model name from the first namespace if
    # the project end-tag handler did not capture it.
    if not result["model_name"] and result["namespaces"]:
        result["model_name"] = result["namespaces"][0]

    log.info(
        "Cognos parse complete: %d query subjects, %d items, %d relationships, "
        "%d calculations, %d dimensions, %d security filters, %d packages, %d errors",
        len(result["query_subjects"]),
        sum(len(qs["items"]) for qs in result["query_subjects"]),
        len(result["relationships"]),
        len(result["calculations"]),
        len(result["dimensions"]),
        len(result["security_filters"]),
        len(result["packages"]),
        len(result["errors"]),
    )
    return result


# Alias matching the naming used by the Power BI adapter's entry point.
parse_model = parse_framework_manager_model
