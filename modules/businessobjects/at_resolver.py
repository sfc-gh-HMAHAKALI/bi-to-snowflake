"""Resolve SAP Business Objects @Function expressions.

BO universes use @Functions as macros in object SQL expressions:
- @Select(Class\\Object) — reference another object's SELECT expression
- @Where(Class\\Object) — reference another object's WHERE clause
- @Variable('name') — session/system variable
- @Prompt(...) — runtime user prompt (filter prompt)
- @Aggregate_Aware(agg, ...) — multi-aggregation fallback list
- @Derived_Table(name) — reference a derived table

All resolvers:
- Log entry/exit with parameters
- Handle malformed syntax gracefully (log warning, leave original)
- Detect and break circular references
- Never raise on missing references (return original with warning)
"""

import re
from typing import Any

from ..common.errors import ParseError, fail_step
from ..common.logger import get_logger

log = get_logger(__name__)

# Maximum recursion depth for @Select/@Where resolution
_MAX_DEPTH = 5

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

# @Select(Class\Object) or @Select(Class\SubClass\Object)
_RE_AT_SELECT = re.compile(
    r"@Select\s*\(\s*([^)]+?)\s*\)", re.IGNORECASE
)

# @Where(Class\Object)
_RE_AT_WHERE = re.compile(
    r"@Where\s*\(\s*([^)]+?)\s*\)", re.IGNORECASE
)

# @Prompt(...) — may contain nested parens (e.g. LOV reference)
_RE_AT_PROMPT = re.compile(
    r"@Prompt\s*\((?:[^()]*|\((?:[^()]*|\([^()]*\))*\))*\)", re.IGNORECASE
)

# @Variable('name')
_RE_AT_VARIABLE = re.compile(
    r"@Variable\s*\(\s*'([^']+)'\s*\)", re.IGNORECASE
)

# @Aggregate_Aware(...) — outer parens only; args parsed separately
_RE_AT_AGGREGATE_AWARE = re.compile(
    r"@Aggregate_Aware\s*\(", re.IGNORECASE
)

# @Derived_Table('name') or @Derived_Table(name)
_RE_AT_DERIVED_TABLE = re.compile(
    r"@Derived_Table\s*\(\s*'?([^')]+?)'?\s*\)", re.IGNORECASE
)

# @Script(...) — custom JavaScript/VBA — always flag manual_required
_RE_AT_SCRIPT = re.compile(
    r"@Script\s*\(", re.IGNORECASE
)

# Snowflake SESSION_CONTEXT equivalents for common BO @Variables
_VARIABLE_SNOWFLAKE_MAP: dict[str, str] = {
    "BOUSER": "CURRENT_USER()",
    "DBUSER": "CURRENT_USER()",
    "BOPASS": "NULL",  # password — not available in SQL
    "BOREPOSITORY": "CURRENT_DATABASE()",
    "BOSTARTDATE": "CURRENT_DATE()",
    "BOENDDATE": "CURRENT_DATE()",
    "BOSTARTTIME": "CURRENT_TIMESTAMP()",
    "BOENDTIME": "CURRENT_TIMESTAMP()",
}


# ---------------------------------------------------------------------------
# resolve_at_select
# ---------------------------------------------------------------------------

def resolve_at_select(
    expression: str,
    object_map: dict,
    _depth: int = 0,
    _visited: set | None = None,
) -> str:
    """Resolve @Select(Class\\Object) references to their actual SQL.

    Args:
        expression: BO expression potentially containing @Select refs.
        object_map: Dict mapping "Class\\Object" paths to their SELECT expressions.
        _depth: Internal recursion counter (do not pass).
        _visited: Internal cycle detection set (do not pass).

    Returns:
        Expression with @Select references resolved. Unresolvable references are
        left in place with a warning comment appended.
    """
    log.debug("resolve_at_select: depth=%d expression=%.80s", _depth, expression)

    if _depth >= _MAX_DEPTH:
        log.warning(
            "resolve_at_select: max nesting depth %d reached — stopping: %.80s",
            _MAX_DEPTH,
            expression,
        )
        return expression

    if _visited is None:
        _visited = set()

    def _replacer(match: re.Match) -> str:
        ref_path = match.group(1).strip()
        # Normalize backslash variants
        key = ref_path.replace("/", "\\")

        if key in _visited:
            log.warning(
                "resolve_at_select: circular reference detected for '%s' — breaking",
                key,
            )
            return f"/* CIRCULAR_REF:{key} */ NULL"

        resolved = (
            object_map.get(key)
            or object_map.get(ref_path)
        )
        if resolved is None:
            log.warning(
                "resolve_at_select: no object found for path '%s' — leaving original",
                key,
            )
            return match.group(0)  # Leave original @Select intact

        log.debug("resolve_at_select: resolved '%s' → %.80s", key, resolved)
        _visited.add(key)
        # Recurse to resolve any nested @Select in the resolved expression
        further_resolved = resolve_at_select(
            resolved, object_map, _depth + 1, _visited
        )
        _visited.discard(key)
        return f"({further_resolved})"

    result = _RE_AT_SELECT.sub(_replacer, expression)
    log.debug("resolve_at_select: result=%.80s", result)
    return result


# ---------------------------------------------------------------------------
# resolve_at_where
# ---------------------------------------------------------------------------

def resolve_at_where(
    expression: str,
    object_map: dict,
    _depth: int = 0,
    _visited: set | None = None,
) -> str:
    """Resolve @Where(Class\\Object) references to their WHERE clause SQL.

    Args:
        expression: BO expression potentially containing @Where refs.
        object_map: Dict mapping "Class\\Object" paths to their WHERE expressions.
        _depth: Internal recursion counter (do not pass).
        _visited: Internal cycle detection set (do not pass).

    Returns:
        Expression with @Where references resolved.
    """
    log.debug("resolve_at_where: depth=%d expression=%.80s", _depth, expression)

    if _depth >= _MAX_DEPTH:
        log.warning(
            "resolve_at_where: max depth %d reached — stopping", _MAX_DEPTH
        )
        return expression

    if _visited is None:
        _visited = set()

    def _replacer(match: re.Match) -> str:
        ref_path = match.group(1).strip()
        key = ref_path.replace("/", "\\")

        if key in _visited:
            log.warning(
                "resolve_at_where: circular reference '%s' — breaking", key
            )
            return f"/* CIRCULAR_REF:{key} */ 1=1"

        resolved = object_map.get(key) or object_map.get(ref_path)
        if resolved is None:
            log.warning(
                "resolve_at_where: no where clause for '%s' — leaving original", key
            )
            return match.group(0)

        _visited.add(key)
        further = resolve_at_where(resolved, object_map, _depth + 1, _visited)
        _visited.discard(key)
        return f"({further})"

    result = _RE_AT_WHERE.sub(_replacer, expression)
    log.debug("resolve_at_where: result=%.80s", result)
    return result


# ---------------------------------------------------------------------------
# strip_at_prompt
# ---------------------------------------------------------------------------

def strip_at_prompt(expression: str) -> str:
    """Remove @Prompt(...) placeholders from an expression.

    Replaces prompts with a ``/* PROMPT:<name> */`` comment preserving
    readability. If the prompt appears inside a WHERE clause (common pattern),
    the surrounding AND/OR is preserved.

    Args:
        expression: BO expression potentially containing @Prompt calls.

    Returns:
        Expression with @Prompt placeholders removed/commented.
    """
    log.debug("strip_at_prompt: expression=%.80s", expression)

    def _replacer(match: re.Match) -> str:
        full_match = match.group(0)
        # Try to extract the prompt name (first quoted argument)
        name_match = re.search(r"['\"]([^'\"]+)['\"]", full_match)
        label = name_match.group(1) if name_match else "prompt"
        return f"/* PROMPT:{label} */"

    result = _RE_AT_PROMPT.sub(_replacer, expression)
    log.debug("strip_at_prompt: removed %d prompt(s)", expression.count("@Prompt") + expression.count("@prompt"))
    return result


# ---------------------------------------------------------------------------
# resolve_at_variable
# ---------------------------------------------------------------------------

def resolve_at_variable(
    expression: str,
    variable_map: dict,
) -> str:
    """Translate @Variable('VAR_NAME') to Snowflake equivalents.

    Resolution priority:
    1. Caller-supplied ``variable_map`` (account/universe-specific overrides)
    2. Built-in BO→Snowflake mapping (_VARIABLE_SNOWFLAKE_MAP)
    3. SESSION_CONTEXT fallback with the variable name
    4. NULL literal with a warning comment

    Args:
        expression: Expression potentially containing @Variable calls.
        variable_map: Dict mapping BO variable names (case-insensitive) to
                      Snowflake SQL expressions.

    Returns:
        Expression with @Variable references resolved.
    """
    log.debug("resolve_at_variable: expression=%.80s", expression)

    # Normalize variable_map keys to uppercase
    upper_vmap = {k.upper(): v for k, v in variable_map.items()}

    def _replacer(match: re.Match) -> str:
        var_name = match.group(1).strip()
        key = var_name.upper()

        # Priority 1: caller override
        if key in upper_vmap:
            resolved = upper_vmap[key]
            log.debug("resolve_at_variable: '%s' → '%s' (caller map)", var_name, resolved)
            return resolved

        # Priority 2: built-in BO→Snowflake map
        if key in _VARIABLE_SNOWFLAKE_MAP:
            resolved = _VARIABLE_SNOWFLAKE_MAP[key]
            log.debug("resolve_at_variable: '%s' → '%s' (built-in)", var_name, resolved)
            return resolved

        # Priority 3: SESSION_CONTEXT fallback
        log.warning(
            "resolve_at_variable: no mapping for '%s' — using SESSION_CONTEXT fallback",
            var_name,
        )
        return f"SESSION_CONTEXT('{var_name}')"

    result = _RE_AT_VARIABLE.sub(_replacer, expression)
    log.debug("resolve_at_variable: result=%.80s", result)
    return result


# ---------------------------------------------------------------------------
# resolve_at_aggregate_aware
# ---------------------------------------------------------------------------

def resolve_at_aggregate_aware(expression: str) -> list[str]:
    """Parse @Aggregate_Aware(expr1, expr2, ...) into a prioritized list.

    The first argument is the most aggregated (preferred) source; subsequent
    arguments are progressively more granular fallbacks. Returns all alternatives
    so the caller can pick the most appropriate one for the target semantic view
    granularity.

    Args:
        expression: A BO expression starting with or containing @Aggregate_Aware.

    Returns:
        List of alternative expressions in priority order (most→least aggregated).
        Empty list if the expression does not contain @Aggregate_Aware or parsing
        fails.
    """
    log.debug("resolve_at_aggregate_aware: expression=%.80s", expression)

    match = _RE_AT_AGGREGATE_AWARE.search(expression)
    if not match:
        return []

    # Find the start of the argument list
    start = match.end()  # position after opening '('
    depth = 1
    idx = start
    end = len(expression)

    # Walk character by character tracking paren depth
    while idx < end and depth > 0:
        ch = expression[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        idx += 1

    if depth != 0:
        log.warning(
            "resolve_at_aggregate_aware: unbalanced parentheses — returning empty"
        )
        return []

    inner = expression[start : idx - 1]  # exclude closing ')'
    log.debug("resolve_at_aggregate_aware: inner=%.120s", inner)

    # Split on commas that are not inside parentheses
    alternatives: list[str] = []
    current: list[str] = []
    paren_depth = 0
    for ch in inner:
        if ch == "(":
            paren_depth += 1
            current.append(ch)
        elif ch == ")":
            paren_depth -= 1
            current.append(ch)
        elif ch == "," and paren_depth == 0:
            alternatives.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        alternatives.append("".join(current).strip())

    log.info(
        "resolve_at_aggregate_aware: found %d alternatives", len(alternatives)
    )
    return alternatives


# ---------------------------------------------------------------------------
# resolve_all_at_functions
# ---------------------------------------------------------------------------

def resolve_all_at_functions(
    expression: str,
    object_map: dict,
    variable_map: dict | None = None,
) -> dict[str, Any]:
    """Full @Function resolution pipeline for a single BO expression.

    Resolution order:
    1. @Select (may contain nested @Functions)
    2. @Where
    3. @Variable
    4. @Prompt (stripped)
    5. Flag @Aggregate_Aware and @Derived_Table for review

    Args:
        expression: BO SQL expression with @Function references.
        object_map: Dict mapping "Class\\Object" paths to expressions.
        variable_map: Optional dict of BO variable overrides.

    Returns:
        .. code-block:: python

            {
                "resolved_sql": str,      # best-effort resolved expression
                "unresolved": list[str],  # @Function references that remain
                "warnings": list[str],    # non-fatal issues encountered
            }
    """
    log.info("resolve_all_at_functions: expression=%.100s", expression)

    if variable_map is None:
        variable_map = {}

    warnings: list[str] = []
    current = expression

    # ---- 1. @Select ----------------------------------------------------------
    try:
        current = resolve_at_select(current, object_map)
    except Exception as exc:
        w = f"@Select resolution failed: {exc}"
        warnings.append(w)
        log.warning("resolve_all_at_functions: %s", w)

    # ---- 2. @Where -----------------------------------------------------------
    try:
        current = resolve_at_where(current, object_map)
    except Exception as exc:
        w = f"@Where resolution failed: {exc}"
        warnings.append(w)
        log.warning("resolve_all_at_functions: %s", w)

    # ---- 3. @Variable --------------------------------------------------------
    try:
        current = resolve_at_variable(current, variable_map)
    except Exception as exc:
        w = f"@Variable resolution failed: {exc}"
        warnings.append(w)
        log.warning("resolve_all_at_functions: %s", w)

    # ---- 4. @Prompt ----------------------------------------------------------
    try:
        current = strip_at_prompt(current)
    except Exception as exc:
        w = f"@Prompt stripping failed: {exc}"
        warnings.append(w)
        log.warning("resolve_all_at_functions: %s", w)

    # ---- 5. Flag remaining special @Functions --------------------------------
    if _RE_AT_AGGREGATE_AWARE.search(current):
        warnings.append(
            "@Aggregate_Aware detected — review alternatives and select target "
            "aggregation level for semantic view"
        )

    if _RE_AT_DERIVED_TABLE.search(current):
        warnings.append(
            "@Derived_Table reference detected — ensure derived table SQL is "
            "included in the semantic view or converted to a CTE"
        )

    if _RE_AT_SCRIPT.search(current):
        warnings.append(
            "@Script detected — requires manual translation; "
            "JavaScript/VBA logic cannot be auto-converted to Snowflake SQL"
        )

    # ---- Collect remaining unresolved @Functions ----------------------------
    remaining = re.findall(
        r"@(Select|Where|Variable|Prompt|Aggregate_Aware|Derived_Table|Script)\s*\(",
        current,
        re.IGNORECASE,
    )
    unresolved = [f"@{r}" for r in remaining]
    if unresolved:
        log.warning(
            "resolve_all_at_functions: %d unresolved @Functions remain: %s",
            len(unresolved),
            unresolved,
        )

    result = {
        "resolved_sql": current,
        "unresolved": unresolved,
        "warnings": warnings,
    }

    log.info(
        "resolve_all_at_functions: done — unresolved=%d warnings=%d",
        len(unresolved),
        len(warnings),
    )
    return result
