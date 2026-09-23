"""Denodo Virtual DataPort (VQL) extraction modules.

Exports the primary entry points for parsing Denodo VQL export files and
classifying views/columns for conversion to Snowflake Semantic Views.
"""

from .vql_parser import (
    parse_vql_file,
    parse_vql_export,
    extract_select_columns,
    extract_joins_from_sql,
    extract_group_by_cols,
    extract_aggregations,
)
from .classifier import (
    classify_column,
    classify_column_by_name,
    classify_with_catalog,
    apply_override,
    classify_denodo_complexity,
    build_view_inventory,
    print_inventory_summary,
)

from .vdp_client import VDPClient
from .catalog_client import CatalogClient

__all__ = [
    # vql_parser
    "parse_vql_file",
    "parse_vql_export",
    "extract_select_columns",
    "extract_joins_from_sql",
    "extract_group_by_cols",
    "extract_aggregations",
    # classifier
    "classify_column",
    "classify_column_by_name",
    "classify_with_catalog",
    "apply_override",
    "classify_denodo_complexity",
    "build_view_inventory",
    "print_inventory_summary",
    # connection managers
    "VDPClient",
    "CatalogClient",
]
