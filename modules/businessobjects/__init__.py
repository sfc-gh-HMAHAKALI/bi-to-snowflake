"""SAP Business Objects extraction: parse universes and reports, resolve @Functions,
classify complexity, and recommend Snowflake Semantic View splits.
"""

from .parser import (
    extract_biar,
    load_bo_json,
    extract_bo_inventory,
    extract_from_rest,
)
from .at_resolver import (
    resolve_at_select,
    resolve_at_where,
    strip_at_prompt,
    resolve_at_variable,
    resolve_at_aggregate_aware,
    resolve_all_at_functions,
)
from .classifier import (
    classify_bo_complexity,
    classify_all_objects,
)
from .context_resolver import (
    extract_context_inventory,
    recommend_semantic_view_split,
    print_context_summary,
    map_objects_to_contexts,
)

__all__ = [
    # parser
    "extract_biar",
    "load_bo_json",
    "extract_bo_inventory",
    "extract_from_rest",
    # at_resolver
    "resolve_at_select",
    "resolve_at_where",
    "strip_at_prompt",
    "resolve_at_variable",
    "resolve_at_aggregate_aware",
    "resolve_all_at_functions",
    # classifier
    "classify_bo_complexity",
    "classify_all_objects",
    # context_resolver
    "extract_context_inventory",
    "recommend_semantic_view_split",
    "print_context_summary",
    "map_objects_to_contexts",
]
