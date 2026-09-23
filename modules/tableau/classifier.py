"""Classify Tableau calculated field formulas by migration complexity.

Three tiers:
  simple            — Direct SQL/Snowflake equivalent exists; auto-translate.
  needs_translation — Tableau-specific function with a Snowflake equivalent
                      that requires manual mapping or parameter adjustment.
  manual_required   — LOD expressions, table calcs, spatial, or analytics
                      extensions that have no direct Snowflake equivalent.
"""

import re
from typing import Optional

from ..common.logger import get_logger
from ..common.errors import ClassificationError, fail_step

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

# Each group is a set of function-name prefixes.  Matching is
# case-insensitive and requires a word boundary or opening parenthesis
# after the name so "ROUND" doesn't match "ROUNDTRIP".

_MANUAL_REQUIRED_PATTERNS: list[re.Pattern] = [
    # LOD expressions
    re.compile(r'\{(?:FIXED|INCLUDE|EXCLUDE)\b', re.IGNORECASE),
    # Table calculations
    re.compile(
        r'\b(?:LOOKUP|RUNNING_SUM|RUNNING_AVG|RUNNING_MIN|RUNNING_MAX|'
        r'RUNNING_COUNT|WINDOW_SUM|WINDOW_AVG|WINDOW_MIN|WINDOW_MAX|'
        r'WINDOW_COUNT|WINDOW_MEDIAN|WINDOW_PERCENTILE|WINDOW_STDEV|'
        r'WINDOW_STDEVP|WINDOW_VAR|WINDOW_VARP|WINDOW_CORR|WINDOW_COVAR|'
        r'WINDOW_COVARP|INDEX|FIRST|LAST|SIZE|RANK|RANK_DENSE|RANK_MODIFIED|'
        r'RANK_PERCENTILE|RANK_UNIQUE|TOTAL|PREVIOUS_VALUE|'
        r'MODEL_EXTENSION_BOOL|MODEL_EXTENSION_INT|MODEL_EXTENSION_REAL|'
        r'MODEL_EXTENSION_STR|MODEL_PERCENTILE|MODEL_QUANTILE|'
        r'MODEL_OUTLIER_DETECTOR|MODEL_TRIM_MEAN)\s*\(',
        re.IGNORECASE,
    ),
    # Spatial functions
    re.compile(
        r'\b(?:HEXBINX|HEXBINY|MAKEPOINT|MAKELINE|BUFFER|AREA|DISTANCE|'
        r'INTERSECTS|ISCOLLECTION|ISEMPTY|ISEQUAL|ISVALID|ASTEXT|'
        r'COLLECTIONEXTRACT|HEXTOBINARY|BINARYTOHEX|DEGREES|RADIANS)\s*\(',
        re.IGNORECASE,
    ),
    # Analytics extensions
    re.compile(r'\b(?:SCRIPT_REAL|SCRIPT_STR|SCRIPT_INT|SCRIPT_BOOL)\s*\(', re.IGNORECASE),
]

_NEEDS_TRANSLATION_PATTERNS: list[re.Pattern] = [
    re.compile(
        r'\b(?:ZN|ATTR|IIF|COUNTD|STR|FLOAT|INT|LEN|MID|FIND|FINDNTH|SPLIT|'
        r'SPACE|ISDATE|PROPER|DATE|DATETIME|DATEPARSE|MAKEDATE|MAKEDATETIME|'
        r'MAKETIME|DATENAME|WEEK|ISOWEEK|ISOYEAR|ISOWEEKDAY|ISOQUARTER|'
        r'LOG|DIV|REGEXP_MATCH|REGEXP_EXTRACT|REGEXP_EXTRACT_NTH|REGEXP_REPLACE|'
        r'STDEV|STDEVP|VAR|VARP|COVAR|COVARP|CORR|PERCENTILE|COLLECT|'
        r'RAWSQL_BOOL|RAWSQL_DATE|RAWSQL_DATETIME|RAWSQL_INT|RAWSQL_REAL|'
        r'RAWSQL_STR|RAWSQLAGG_BOOL|RAWSQLAGG_DATE|RAWSQLAGG_DATETIME|'
        r'RAWSQLAGG_INT|RAWSQLAGG_REAL|RAWSQLAGG_STR)\s*\(',
        re.IGNORECASE,
    ),
]

# Note: "simple" is the fallback — if no manual/translation pattern matches,
# the formula is considered simple.  We also compile a positive-match pattern
# for documentation clarity, but it is not used for control flow.
_SIMPLE_PATTERNS: list[re.Pattern] = [
    re.compile(
        r'\b(?:SUM|AVG|COUNT|MIN|MAX|MEDIAN|'
        # String
        r'UPPER|LOWER|TRIM|LTRIM|RTRIM|LEFT|RIGHT|REPLACE|CONTAINS|'
        r'STARTSWITH|ENDSWITH|CHAR|ASCII|UNICODE|'
        # Math
        r'ABS|ROUND|CEILING|FLOOR|SIGN|SQRT|POWER|SQUARE|LN|EXP|MOD|PI|'
        r'RANDOM|'
        # Date (simple)
        r'YEAR|MONTH|DAY|QUARTER|TODAY|NOW|DATETRUNC|DATEDIFF|DATEADD|DATEPART|'
        # Null handling
        r'ISNULL|IFNULL|COALESCE|'
        # Trig
        r'SIN|COS|TAN|ASIN|ACOS|ATAN|ATAN2|COT|'
        # Logical (keywords, not functions)
        r'IF|THEN|ELSE|ELSEIF|END|CASE|WHEN|AND|OR|NOT|IN|'
        # Type
        r'BOOLEAN)\s*[\(\s]',
        re.IGNORECASE,
    ),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_tableau_complexity(formula: str) -> str:
    """Classify a Tableau calculated field formula by migration complexity.

    Args:
        formula: The raw formula string from the Tableau workbook.

    Returns:
        One of:
            'manual_required'   — Cannot be auto-translated.
            'needs_translation' — Requires mapping to a Snowflake equivalent.
            'simple'            — Can be auto-translated directly.

    Raises:
        ClassificationError: Only if formula is not a string. For any other
            unexpected error, returns 'manual_required' conservatively.
    """
    log.debug("classify_tableau_complexity: entry — formula_len=%d", len(formula or ""))

    if not isinstance(formula, str):
        raise ClassificationError(
            f"formula must be a str, got {type(formula).__name__}",
            context={"formula_type": type(formula).__name__},
        )

    # Treat empty/blank formulas as simple (no translation needed)
    if not formula.strip():
        log.debug("classify_tableau_complexity: exit — empty formula → simple")
        return "simple"

    try:
        for pattern in _MANUAL_REQUIRED_PATTERNS:
            if pattern.search(formula):
                log.debug(
                    "classify_tableau_complexity: exit — matched manual_required pattern"
                )
                return "manual_required"

        for pattern in _NEEDS_TRANSLATION_PATTERNS:
            if pattern.search(formula):
                log.debug(
                    "classify_tableau_complexity: exit — matched needs_translation pattern"
                )
                return "needs_translation"

        log.debug("classify_tableau_complexity: exit — no complex pattern → simple")
        return "simple"

    except Exception as exc:
        # Defensive: never let classifier crash the pipeline; treat as
        # manual_required so a human reviews it.
        log.error(
            "classify_tableau_complexity: unexpected error — %s. "
            "Defaulting to manual_required.",
            exc,
        )
        return "manual_required"


def classify_all_fields(datasource: dict) -> dict:
    """Classify every calculated field in a parsed datasource dict.

    Iterates ``datasource["calculated_fields"]`` and appends a
    ``complexity`` key to each field in-place.  Fields that raise errors
    during classification are recorded in the returned summary.

    Args:
        datasource: A datasource dict as returned by ``extract_datasource()``.

    Returns:
        dict with keys:
            classified_fields  (list[dict]):  Calculated fields with added
                                              ``complexity`` key.
            summary            (dict):        Counts per complexity tier.
            errors             (list[dict]):  Per-field classification errors.

    Example::

        {
            "classified_fields": [
                {"name": "[Profit Ratio]", "formula": "SUM([Profit]) / SUM([Sales])",
                 "complexity": "simple"},
                ...
            ],
            "summary": {"simple": 12, "needs_translation": 5, "manual_required": 3},
            "errors": [],
        }
    """
    log.info(
        "classify_all_fields: entry — datasource=%s, calc_fields=%d",
        datasource.get("name", "<unknown>"),
        len(datasource.get("calculated_fields", [])),
    )

    summary: dict[str, int] = {
        "simple": 0,
        "needs_translation": 0,
        "manual_required": 0,
    }
    classified: list[dict] = []
    errors: list[dict] = []

    for field in datasource.get("calculated_fields", []):
        field_name = field.get("name", "<unknown>")
        formula = field.get("formula", "")
        try:
            complexity = classify_tableau_complexity(formula)
            enriched = dict(field)
            enriched["complexity"] = complexity
            classified.append(enriched)
            summary[complexity] = summary.get(complexity, 0) + 1
        except ClassificationError as exc:
            errors.append(fail_step(f"classify_field[{field_name}]", exc))
            # Default to manual_required so the field is not silently skipped
            enriched = dict(field)
            enriched["complexity"] = "manual_required"
            classified.append(enriched)
            summary["manual_required"] = summary.get("manual_required", 0) + 1
        except Exception as exc:
            errors.append(fail_step(f"classify_field[{field_name}]", exc))
            enriched = dict(field)
            enriched["complexity"] = "manual_required"
            classified.append(enriched)
            summary["manual_required"] = summary.get("manual_required", 0) + 1

    log.info(
        "classify_all_fields: exit — simple=%d, needs_translation=%d, "
        "manual_required=%d, errors=%d",
        summary["simple"],
        summary["needs_translation"],
        summary["manual_required"],
        len(errors),
    )
    return {
        "classified_fields": classified,
        "summary": summary,
        "errors": errors,
    }
