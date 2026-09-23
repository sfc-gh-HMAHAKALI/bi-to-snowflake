"""Adapter: converts our inventory.json format → Josh's DiscoveryContext model.

Enables pipeline chaining:
  Our extraction (5 BI tools) → adapter → Josh's build/deploy/test phases.

Usage:
    from modules.adapters.discovery_context_adapter import inventory_to_discovery_context

    inventory = load_inventory("inventory.json")
    ctx = inventory_to_discovery_context(inventory)
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import asdict
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Inline dataclass definitions (mirrors semantic_skills/common/models.py)
# so this module has zero dependency on Josh's package at import time.
# If semantic_skills is installed, callers can isinstance-check freely
# because field names and semantics match 1-to-1.
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field


class ArtifactType(Enum):
    LOOKML = "lookml"
    POWERBI = "powerbi"
    TABLEAU = "tableau"
    DENODO = "denodo"
    BUSINESSOBJECTS = "businessobjects"
    SCHEMA = "schema"


class ElementType(Enum):
    DIMENSION = "dimension"
    TIME_DIMENSION = "time_dimension"
    FACT = "fact"
    METRIC = "metric"


class DataType(Enum):
    VARCHAR = "VARCHAR"
    NUMBER = "NUMBER"
    BOOLEAN = "BOOLEAN"
    DATE = "DATE"
    TIMESTAMP = "TIMESTAMP"


class AggregationType(Enum):
    SUM = "SUM"
    AVG = "AVG"
    COUNT = "COUNT"
    COUNT_DISTINCT = "COUNT_DISTINCT"
    MIN = "MIN"
    MAX = "MAX"


class JoinType(Enum):
    LEFT = "left"
    INNER = "inner"
    FULL = "full"


@dataclass
class ColumnInfo:
    name: str
    data_type: DataType
    expression: str = ""
    description: str = ""
    synonyms: list[str] = field(default_factory=list)
    sample_values: list[str] = field(default_factory=list)
    is_enum: bool = False
    is_primary_key: bool = False
    source_tool: str = ""
    source_expression: str = ""
    conversion_notes: list[str] = field(default_factory=list)


@dataclass
class DimensionInfo(ColumnInfo):
    element_type: ElementType = ElementType.DIMENSION


@dataclass
class TimeDimensionInfo(ColumnInfo):
    element_type: ElementType = ElementType.TIME_DIMENSION


@dataclass
class FactInfo(ColumnInfo):
    element_type: ElementType = ElementType.FACT


@dataclass
class MetricInfo(ColumnInfo):
    element_type: ElementType = ElementType.METRIC
    aggregation: AggregationType = AggregationType.SUM
    is_derived: bool = False
    base_metric_refs: list[str] = field(default_factory=list)


@dataclass
class TableInfo:
    logical_name: str
    physical_database: str
    physical_schema: str
    physical_table: str
    description: str = ""
    primary_key: str = ""
    dimensions: list[DimensionInfo] = field(default_factory=list)
    time_dimensions: list[TimeDimensionInfo] = field(default_factory=list)
    facts: list[FactInfo] = field(default_factory=list)
    metrics: list[MetricInfo] = field(default_factory=list)
    row_count: int | None = None
    source_tool: str = ""

    @property
    def fqn(self) -> str:
        return f"{self.physical_database}.{self.physical_schema}.{self.physical_table}"


@dataclass
class RelationshipInfo:
    name: str
    left_table: str
    right_table: str
    join_columns: list[tuple[str, str]]
    join_type: JoinType = JoinType.LEFT
    relationship_type: str = "many_to_one"
    confidence: float = 1.0
    source: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class EvalQuery:
    question: str
    expected_sql: str
    semantic_sql: str = ""
    category: str = ""
    difficulty: str = "medium"
    tables_involved: list[str] = field(default_factory=list)
    source: str = ""
    id: str = ""


@dataclass
class BusinessRule:
    rule: str
    source: str = ""
    confidence: float = 1.0


@dataclass
class DiscoveryContext:
    domain_description: str = ""
    tables: list[TableInfo] = field(default_factory=list)
    relationships: list[RelationshipInfo] = field(default_factory=list)
    seed_questions: list[EvalQuery] = field(default_factory=list)
    business_rules: list[BusinessRule] = field(default_factory=list)
    artifacts_ingested: list[dict[str, Any]] = field(default_factory=list)
    unconverted_logic: list[dict[str, Any]] = field(default_factory=list)
    ai_instructions: list[str] = field(default_factory=list)

    @property
    def table_names(self) -> list[str]:
        return [t.logical_name for t in self.tables]


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------

_DATA_TYPE_MAP: dict[str, DataType] = {
    "VARCHAR": DataType.VARCHAR,
    "NUMBER": DataType.NUMBER,
    "BOOLEAN": DataType.BOOLEAN,
    "DATE": DataType.DATE,
    "TIMESTAMP": DataType.TIMESTAMP,
}

_JOIN_TYPE_MAP: dict[str, JoinType] = {
    "left": JoinType.LEFT,
    "left_outer": JoinType.LEFT,
    "inner": JoinType.INNER,
    "full": JoinType.FULL,
    "full_outer": JoinType.FULL,
}

_AGG_PATTERN = re.compile(r"\b(SUM|AVG|COUNT_DISTINCT|COUNT|MIN|MAX)\b", re.IGNORECASE)

_AGG_MAP: dict[str, AggregationType] = {
    "SUM": AggregationType.SUM,
    "AVG": AggregationType.AVG,
    "COUNT": AggregationType.COUNT,
    "COUNT_DISTINCT": AggregationType.COUNT_DISTINCT,
    "MIN": AggregationType.MIN,
    "MAX": AggregationType.MAX,
}

_SOURCE_TYPE_TO_ARTIFACT: dict[str, str] = {
    "tableau": ArtifactType.TABLEAU.value,
    "looker": ArtifactType.LOOKML.value,
    "powerbi": ArtifactType.POWERBI.value,
    "denodo": ArtifactType.DENODO.value,
    "businessobjects": ArtifactType.BUSINESSOBJECTS.value,
}

_TIME_TYPES = {DataType.DATE, DataType.TIMESTAMP}


def _resolve_data_type(dt_str: str) -> DataType:
    return _DATA_TYPE_MAP.get(dt_str.upper(), DataType.VARCHAR)


def _resolve_join_type(jt_str: str) -> JoinType:
    return _JOIN_TYPE_MAP.get(jt_str.lower(), JoinType.LEFT)


def _infer_aggregation(expr: str) -> AggregationType:
    match = _AGG_PATTERN.search(expr)
    if match:
        return _AGG_MAP.get(match.group(1).upper(), AggregationType.SUM)
    return AggregationType.SUM


def _is_time_column(item: dict) -> bool:
    dt = _resolve_data_type(item.get("data_type", "VARCHAR"))
    if dt in _TIME_TYPES:
        return True
    name_lower = item.get("name", "").lower()
    return any(tok in name_lower for tok in ("date", "time", "timestamp", "year", "month", "day", "quarter", "week"))


# ---------------------------------------------------------------------------
# Core conversion
# ---------------------------------------------------------------------------

def _convert_dimension(item: dict, source_type: str) -> DimensionInfo:
    return DimensionInfo(
        name=item.get("name", ""),
        data_type=_resolve_data_type(item.get("data_type", "VARCHAR")),
        expression=item.get("expr", ""),
        description=item.get("description", ""),
        synonyms=item.get("synonyms", []),
        is_primary_key=bool(item.get("primary_key")),
        source_tool=source_type,
        source_expression=item.get("expr", ""),
        conversion_notes=[f"complexity={item.get('complexity', 'simple')}"],
    )


def _convert_time_dimension(item: dict, source_type: str) -> TimeDimensionInfo:
    return TimeDimensionInfo(
        name=item.get("name", ""),
        data_type=_resolve_data_type(item.get("data_type", "VARCHAR")),
        expression=item.get("expr", ""),
        description=item.get("description", ""),
        synonyms=item.get("synonyms", []),
        source_tool=source_type,
        source_expression=item.get("expr", ""),
        conversion_notes=[f"complexity={item.get('complexity', 'simple')}"],
    )


def _convert_fact(item: dict, source_type: str) -> FactInfo:
    return FactInfo(
        name=item.get("name", ""),
        data_type=_resolve_data_type(item.get("data_type", "VARCHAR")),
        expression=item.get("expr", ""),
        description=item.get("description", ""),
        synonyms=item.get("synonyms", []),
        source_tool=source_type,
        source_expression=item.get("expr", ""),
        conversion_notes=[f"complexity={item.get('complexity', 'simple')}"],
    )


def _convert_metric(item: dict, source_type: str) -> MetricInfo:
    expr = item.get("expr", "")
    return MetricInfo(
        name=item.get("name", ""),
        data_type=_resolve_data_type(item.get("data_type", "NUMBER")),
        expression=expr,
        description=item.get("description", ""),
        synonyms=item.get("synonyms", []),
        aggregation=_infer_aggregation(expr),
        source_tool=source_type,
        source_expression=expr,
        conversion_notes=[f"complexity={item.get('complexity', 'simple')}"],
    )


def _convert_relationship(rel: dict, source_type: str) -> RelationshipInfo:
    left = rel.get("left_table", "")
    right = rel.get("right_table", "")
    name = f"{left}__{right}"

    # Build join_columns from explicit columns or parse condition
    join_columns: list[tuple[str, str]] = []
    if rel.get("left_column") and rel.get("right_column"):
        join_columns.append((rel["left_column"], rel["right_column"]))
    elif rel.get("condition"):
        # Best-effort parse of "A.col = B.col" style conditions
        for part in rel["condition"].split(" AND "):
            part = part.strip()
            eq_match = re.match(r"(\S+)\s*=\s*(\S+)", part)
            if eq_match:
                lhs = eq_match.group(1).split(".")[-1]
                rhs = eq_match.group(2).split(".")[-1]
                join_columns.append((lhs, rhs))

    return RelationshipInfo(
        name=name,
        left_table=left,
        right_table=right,
        join_columns=join_columns,
        join_type=_resolve_join_type(rel.get("join_type", "left")),
        relationship_type=rel.get("relationship_type", "many_to_one"),
        source=source_type,
        notes=[rel["condition"]] if rel.get("condition") else [],
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def inventory_to_discovery_context(
    inventory: dict,
    domain_description: str = "",
) -> DiscoveryContext:
    """Convert a unified inventory dict into a DiscoveryContext.

    Args:
        inventory: Dict produced by ``build_unified_inventory()`` / ``load_inventory()``.
        domain_description: Optional human-readable domain description.

    Returns:
        A fully populated DiscoveryContext ready for Josh's Phase 2 (Build).
    """
    source_type = inventory.get("source_type", "unknown")
    sf_target = inventory.get("snowflake_target", {})
    sf_db = sf_target.get("database", "TARGET_DB")
    sf_schema = sf_target.get("schema", "PUBLIC")

    # ------------------------------------------------------------------
    # 1. Build a table lookup from inventory["tables"]
    # ------------------------------------------------------------------
    table_lookup: dict[str, dict] = {}
    for t in inventory.get("tables", []):
        tname = t.get("name", "UNKNOWN")
        table_lookup[tname] = t

    # ------------------------------------------------------------------
    # 2. Group flat dims/facts/metrics by their 'table' field
    # ------------------------------------------------------------------
    dims_by_table: dict[str, list[dict]] = defaultdict(list)
    for d in inventory.get("dimensions", []):
        dims_by_table[d.get("table", "UNKNOWN")].append(d)

    facts_by_table: dict[str, list[dict]] = defaultdict(list)
    for f in inventory.get("facts", []):
        facts_by_table[f.get("table", "UNKNOWN")].append(f)

    metrics_by_table: dict[str, list[dict]] = defaultdict(list)
    for m in inventory.get("metrics", []):
        metrics_by_table[m.get("table", "UNKNOWN")].append(m)

    # Collect all referenced table names
    all_table_names = set(table_lookup.keys())
    all_table_names.update(dims_by_table.keys())
    all_table_names.update(facts_by_table.keys())
    all_table_names.update(metrics_by_table.keys())

    # ------------------------------------------------------------------
    # 3. Build TableInfo objects with nested columns
    # ------------------------------------------------------------------
    tables: list[TableInfo] = []
    for tname in sorted(all_table_names):
        raw_table = table_lookup.get(tname, {})

        # Split dimensions into regular vs time dimensions
        regular_dims: list[DimensionInfo] = []
        time_dims: list[TimeDimensionInfo] = []
        for d in dims_by_table.get(tname, []):
            if _is_time_column(d):
                time_dims.append(_convert_time_dimension(d, source_type))
            else:
                regular_dims.append(_convert_dimension(d, source_type))

        # Detect primary key from dimensions
        pk = ""
        for d in dims_by_table.get(tname, []):
            if d.get("primary_key"):
                pk = d.get("name", "")
                break

        tables.append(TableInfo(
            logical_name=tname,
            physical_database=sf_db,
            physical_schema=sf_schema,
            physical_table=raw_table.get("physical_table", tname),
            description=raw_table.get("description", ""),
            primary_key=pk,
            dimensions=regular_dims,
            time_dimensions=time_dims,
            facts=[_convert_fact(f, source_type) for f in facts_by_table.get(tname, [])],
            metrics=[_convert_metric(m, source_type) for m in metrics_by_table.get(tname, [])],
            source_tool=source_type,
        ))

    # ------------------------------------------------------------------
    # 4. Convert relationships
    # ------------------------------------------------------------------
    relationships = [
        _convert_relationship(r, source_type)
        for r in inventory.get("relationships", [])
    ]

    # ------------------------------------------------------------------
    # 5. Map flagged items → unconverted_logic
    # ------------------------------------------------------------------
    unconverted: list[dict[str, Any]] = []
    for flag in inventory.get("flagged", []):
        unconverted.append({
            "item": flag.get("item", ""),
            "reason": flag.get("reason", ""),
            "expression": flag.get("expression", ""),
            "source": flag.get("source", source_type),
        })

    # ------------------------------------------------------------------
    # 6. Build artifact record
    # ------------------------------------------------------------------
    artifact_type = _SOURCE_TYPE_TO_ARTIFACT.get(source_type, source_type)
    artifacts_ingested: list[dict[str, Any]] = [{
        "type": artifact_type,
        "source_type": source_type,
        "table_count": len(tables),
        "relationship_count": len(relationships),
        "complexity_summary": inventory.get("complexity_summary", {}),
        "error_count": len(inventory.get("errors", [])),
    }]

    # ------------------------------------------------------------------
    # 7. AI instructions from complexity data
    # ------------------------------------------------------------------
    ai_instructions: list[str] = []
    cs = inventory.get("complexity_summary", {})
    manual_count = cs.get("manual_required", 0)
    needs_xlat = cs.get("needs_translation", 0)
    if manual_count > 0:
        ai_instructions.append(
            f"{manual_count} items flagged as manual_required — review unconverted_logic."
        )
    if needs_xlat > 0:
        ai_instructions.append(
            f"{needs_xlat} items need SQL translation from {source_type} dialect."
        )

    return DiscoveryContext(
        domain_description=domain_description or f"Extracted from {source_type} source",
        tables=tables,
        relationships=relationships,
        unconverted_logic=unconverted,
        artifacts_ingested=artifacts_ingested,
        ai_instructions=ai_instructions,
    )


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _enum_serializer(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def discovery_context_to_dict(ctx: DiscoveryContext) -> dict[str, Any]:
    """Serialize a DiscoveryContext to a plain dict (JSON-safe)."""
    raw = asdict(ctx)

    # asdict converts tuples in join_columns to lists — that's fine for JSON.
    # Enum values are already converted by asdict, but nested enums in
    # dataclass fields become their .value via the default_factory.
    return json.loads(json.dumps(raw, default=_enum_serializer))


def save_discovery_context(ctx: DiscoveryContext, output_path: str) -> str:
    """Save DiscoveryContext as JSON file."""
    data = discovery_context_to_dict(ctx)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=_enum_serializer)
    return output_path


def load_discovery_context_dict(input_path: str) -> dict[str, Any]:
    """Load a serialized DiscoveryContext dict from JSON."""
    with open(input_path, "r", encoding="utf-8") as f:
        return json.load(f)
