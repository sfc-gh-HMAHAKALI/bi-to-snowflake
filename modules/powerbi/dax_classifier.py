"""DAX expression complexity classifier for Power BI measures and calculated columns.

Returns enriched classification detail: tier, matched patterns, semantic
category, natural-language description, Snowflake recommendation, and effort.
"""

import re
from typing import Any

from ..common.errors import ClassificationError, fail_step
from ..common.logger import get_logger

log = get_logger("powerbi.dax_classifier")

# ---------------------------------------------------------------------------
# Pattern sets — ordered from most to least restrictive
# ---------------------------------------------------------------------------

# Patterns that require full manual translation — context-sensitive filter
# manipulation, advanced table functions, or financial builtins with no
# direct SQL analog.
_MANUAL_REQUIRED_PATTERNS: list[str] = [
    r"\bCALCULATE\b",
    r"\bCALCULATETABLE\b",
    r"\bALL\b",
    r"\bALLEXCEPT\b",
    r"\bALLSELECTED\b",
    r"\bRANKX\b",
    r"\bEARLIER\b",
    r"\bEARLIEST\b",
    r"\bISINSCOPE\b",
    r"\bUSERELATIONSHIP\b",
    r"\bCROSSFILTER\b",
    r"\bTREATAS\b",
    r"\bSUMMARIZECOLUMNS\b",
    r"\bADDCOLUMNS\b",
    r"\bSELECTCOLUMNS\b",
    r"\bGENERATE\b(?!SERIES)",   # GENERATE but not GENERATESERIES (handled below)
    r"\bGENERATESERIES\b",
    r"\bPATH\b",
    r"\bPATHITEM\b",
    r"\bPATHITEMREVERSE\b",
    r"\bPATHLENGTH\b",
    r"\bHASONEVALUE\b",
    r"\bHASONEFILTER\b",
    r"\bISFILTERED\b",
    r"\bISCROSSFILTERED\b",
    r"\bSELECTEDVALUE\b",
    r"\bPARALLELPERIOD\b",
    r"\bOPENINGBALANCEMONTH\b",
    r"\bOPENINGBALANCEQUARTER\b",
    r"\bOPENINGBALANCEYEAR\b",
    r"\bCLOSINGBALANCEMONTH\b",
    r"\bCLOSINGBALANCEQUARTER\b",
    r"\bCLOSINGBALANCEYEAR\b",
    r"\bCALENDAR\b",
    r"\bCALENDARAUTO\b",
    r"\bPERCENTILEX\.INC\b",
    r"\bPERCENTILEX\.EXC\b",
    r"\bMEDIANX\b",
    # Financial functions
    r"\bNPV\b",
    r"\bXNPV\b",
    r"\bIRR\b",
    r"\bXIRR\b",
    r"\bPMT\b",
    r"\bPV\b",
    r"\bFV\b",
    r"\bGROUPBY\b",
    r"\bNATURALINNERJOIN\b",
    r"\bNATURALLEFTOUTERJOIN\b",
]

# Patterns that have a Snowflake SQL translation but require manual effort.
_NEEDS_TRANSLATION_PATTERNS: list[str] = [
    r"\bSUMX\b",
    r"\bAVERAGEX\b",
    r"\bCOUNTX\b",
    r"\bMINX\b",
    r"\bMAXX\b",
    r"\bRELATED\b",
    r"\bRELATEDTABLE\b",
    r"\bDIVIDE\b",
    r"\bIF\b",
    r"\bSWITCH\b",
    r"\bIFERROR\b",
    r"\bFORMAT\b",
    # Time intelligence
    r"\bDATESYTD\b",
    r"\bDATESMTD\b",
    r"\bDATESQTD\b",
    r"\bDATEADD\b",
    r"\bDATESBETWEEN\b",
    r"\bDATESINPERIOD\b",
    r"\bSAMEPERIODLASTYEAR\b",
    r"\bTOTALYTD\b",
    r"\bTOTALMTD\b",
    r"\bTOTALQTD\b",
    r"\bPREVIOUSDAY\b",
    r"\bPREVIOUSMONTH\b",
    r"\bPREVIOUSQUARTER\b",
    r"\bPREVIOUSYEAR\b",
    r"\bNEXTDAY\b",
    r"\bNEXTMONTH\b",
    r"\bNEXTQUARTER\b",
    r"\bNEXTYEAR\b",
    r"\bDISTINCTCOUNT\b",
    r"\bCOALESCE\b",
    r"\bCONCATENATEX\b",
    r"\bFIRSTDATE\b",
    r"\bLASTDATE\b",
    r"\bISBLANK\b",
    r"\bLOOKUPVALUE\b",
    # Text transforms
    r"\bMID\b",
    r"\bSUBSTITUTE\b",
    r"\bREPLACE\b",
    r"\bSEARCH\b",
    r"\bFIND\b",
    r"\bVALUE\b",
    r"\bFIXED\b",
    r"\bCOMBINEVALUES\b",
    # Math
    r"\bROUNDUP\b",
    r"\bROUNDDOWN\b",
    r"\bINT\b",
    r"\bCEILING\b",
    r"\bFLOOR\b",
    r"\bLOG\b",
    r"\bLOG10\b",
    r"\bRAND\b",
    r"\bRANDBETWEEN\b",
    r"\bEVEN\b",
    r"\bODD\b",
    r"\bQUOTIENT\b",
    r"\bCURRENCY\b",
    r"\bMROUND\b",
    # Info / user context
    r"\bISERROR\b",
    r"\bISNUMBER\b",
    r"\bISTEXT\b",
    r"\bISLOGICAL\b",
    r"\bISNONTEXT\b",
    r"\bUSERNAME\b",
    r"\bUSERPRINCIPALNAME\b",
    # Stats
    r"\bPERCENTILE\.INC\b",
    r"\bPERCENTILE\.EXC\b",
    r"\bNORM\.DIST\b",
]

# Simple patterns — direct SQL aggregate/expression equivalents.
_SIMPLE_PATTERNS: list[str] = [
    r"\bSUM\b",
    r"\bCOUNT\b",
    r"\bCOUNTROWS\b",
    r"\bAVERAGE\b",
    r"\bMIN\b",
    r"\bMAX\b",
    r"\bDISTINCTCOUNT\b",
    # Text
    r"\bCONCATENATE\b",
    r"\bLEFT\b",
    r"\bRIGHT\b",
    r"\bLEN\b",
    r"\bUPPER\b",
    r"\bLOWER\b",
    r"\bTRIM\b",
    r"\bEXACT\b",
    r"\bREPT\b",
    r"\bUNICHAR\b",
    r"\bUNICODE\b",
    # Math
    r"\bABS\b",
    r"\bROUND\b",
    r"\bMOD\b",
    r"\bPOWER\b",
    r"\bSQRT\b",
    r"\bLN\b",
    r"\bEXP\b",
    r"\bSIGN\b",
    r"\bPI\b",
    r"\bTRUNC\b",
    # Date parts
    r"\bYEAR\b",
    r"\bMONTH\b",
    r"\bDAY\b",
    r"\bHOUR\b",
    r"\bMINUTE\b",
    r"\bSECOND\b",
    r"\bTODAY\b",
    r"\bNOW\b",
    r"\bDATE\b",
    r"\bTIME\b",
    r"\bWEEKDAY\b",
    r"\bWEEKNUM\b",
    r"\bEOMONTH\b",
    # Stats
    r"\bSTDEV\.S\b",
    r"\bSTDEV\.P\b",
    r"\bVAR\.S\b",
    r"\bVAR\.P\b",
    r"\bMEDIAN\b",
    # Logical
    r"\bAND\b",
    r"\bOR\b",
    r"\bNOT\b",
    r"\bTRUE\b",
    r"\bFALSE\b",
    r"\bBLANK\b",
    # Period start/end
    r"\bSTARTOFMONTH\b",
    r"\bSTARTOFQUARTER\b",
    r"\bSTARTOFYEAR\b",
    r"\bENDOFMONTH\b",
    r"\bENDOFQUARTER\b",
    r"\bENDOFYEAR\b",
]

# Pre-compile all pattern sets for performance
_MANUAL_RE = re.compile("|".join(_MANUAL_REQUIRED_PATTERNS), re.IGNORECASE)
_NEEDS_RE = re.compile("|".join(_NEEDS_TRANSLATION_PATTERNS), re.IGNORECASE)
# _SIMPLE_RE is not needed for the decision logic but kept for documentation

# ---------------------------------------------------------------------------
# Pattern metadata — each known DAX pattern gets a category, Snowflake hint,
# and a short human-readable description.
# ---------------------------------------------------------------------------

_DAX_PATTERN_META: dict[str, dict[str, str]] = {
    # -- Filter context manipulation (manual) --
    "CALCULATE": {
        "category": "filter_context",
        "snowflake": "No direct equivalent — rewrite as WHERE/CASE WHEN for simple filters",
        "desc": "Evaluates expression under modified filter context",
    },
    "CALCULATETABLE": {
        "category": "filter_context",
        "snowflake": "No direct equivalent — rewrite as filtered subquery",
        "desc": "Returns a table under modified filter context",
    },
    "ALL": {
        "category": "filter_context",
        "snowflake": "No direct equivalent — removes all filters (use unfiltered subquery)",
        "desc": "Removes all filters from a table or column",
    },
    "ALLEXCEPT": {
        "category": "filter_context",
        "snowflake": "No direct equivalent — removes filters except specified columns",
        "desc": "Removes all filters except on specified columns",
    },
    "ALLSELECTED": {
        "category": "filter_context",
        "snowflake": "No direct equivalent — respects outer slicer context only",
        "desc": "Removes inner filters but keeps outer slicer context",
    },
    "FILTER": {
        "category": "filter_context",
        "snowflake": "WHERE clause or CASE WHEN (simple cases)",
        "desc": "Iterates a table and returns rows matching a condition",
    },
    "EARLIER": {
        "category": "row_context",
        "snowflake": "No direct equivalent — nested row context has no SQL parallel",
        "desc": "References outer row context in nested calculations",
    },
    "EARLIEST": {
        "category": "row_context",
        "snowflake": "No direct equivalent — nested row context",
        "desc": "References outermost row context",
    },
    # -- Slicer / filter inspection (manual) --
    "SELECTEDVALUE": {
        "category": "slicer_context",
        "snowflake": "No direct equivalent — depends on BI slicer selection",
        "desc": "Returns the value when exactly one value is in filter context",
    },
    "HASONEVALUE": {
        "category": "slicer_context",
        "snowflake": "No direct equivalent — filter context inspection",
        "desc": "Tests if exactly one value is visible in filter context",
    },
    "HASONEFILTER": {
        "category": "slicer_context",
        "snowflake": "No direct equivalent — filter context inspection",
        "desc": "Tests if exactly one direct filter exists on a column",
    },
    "ISFILTERED": {
        "category": "slicer_context",
        "snowflake": "No direct equivalent — filter context inspection",
        "desc": "Tests if a column has direct filters applied",
    },
    "ISCROSSFILTERED": {
        "category": "slicer_context",
        "snowflake": "No direct equivalent — filter context inspection",
        "desc": "Tests if a column is filtered via cross-filter",
    },
    "ISINSCOPE": {
        "category": "slicer_context",
        "snowflake": "No direct equivalent — visual grouping inspection",
        "desc": "Tests if a column is used as a grouping level in the visual",
    },
    # -- Ranking (manual) --
    "RANKX": {
        "category": "ranking",
        "snowflake": "RANK() OVER (ORDER BY expr) — approximate, context nuances differ",
        "desc": "Ranks values across a table with filter context awareness",
    },
    # -- Relationship manipulation (manual) --
    "USERELATIONSHIP": {
        "category": "relationship",
        "snowflake": "No direct equivalent — activates an inactive relationship",
        "desc": "Activates an inactive model relationship for the calculation",
    },
    "CROSSFILTER": {
        "category": "relationship",
        "snowflake": "No direct equivalent — changes cross-filter direction",
        "desc": "Changes the cross-filter direction of a relationship",
    },
    "TREATAS": {
        "category": "relationship",
        "snowflake": "No direct equivalent — creates a virtual relationship",
        "desc": "Applies column values as filters on an unrelated table",
    },
    "RELATED": {
        "category": "table_navigation",
        "snowflake": "Direct column reference via JOIN",
        "desc": "Follows a many-to-one relationship to get a related value",
    },
    "RELATEDTABLE": {
        "category": "table_navigation",
        "snowflake": "Correlated subquery or JOIN",
        "desc": "Follows a one-to-many relationship to get related rows",
    },
    # -- Table manipulation (manual) --
    "SUMMARIZECOLUMNS": {
        "category": "table_manipulation",
        "snowflake": "GROUP BY with aggregate expressions",
        "desc": "Groups by columns and evaluates aggregate expressions",
    },
    "ADDCOLUMNS": {
        "category": "table_manipulation",
        "snowflake": "SELECT with computed columns",
        "desc": "Adds calculated columns to a table expression",
    },
    "SELECTCOLUMNS": {
        "category": "table_manipulation",
        "snowflake": "SELECT with column projection",
        "desc": "Selects and renames columns from a table",
    },
    "GENERATE": {
        "category": "table_manipulation",
        "snowflake": "CROSS JOIN with row-context evaluation",
        "desc": "Cross-joins two tables with row context",
    },
    "GENERATESERIES": {
        "category": "table_manipulation",
        "snowflake": "TABLE(GENERATOR(ROWCOUNT => n)) or FLATTEN + ARRAY_GENERATE_RANGE",
        "desc": "Generates a single-column table of sequential values",
    },
    "GROUPBY": {
        "category": "table_manipulation",
        "snowflake": "GROUP BY (approximate — context differs)",
        "desc": "Groups a table by columns with limited aggregation",
    },
    "NATURALINNERJOIN": {
        "category": "table_manipulation",
        "snowflake": "NATURAL INNER JOIN or explicit INNER JOIN",
        "desc": "Inner join on common columns between two tables",
    },
    "NATURALLEFTOUTERJOIN": {
        "category": "table_manipulation",
        "snowflake": "NATURAL LEFT JOIN or explicit LEFT JOIN",
        "desc": "Left outer join on common columns",
    },
    # -- Hierarchy (manual) --
    "PATH": {
        "category": "hierarchy",
        "snowflake": "Recursive CTE or CONNECT BY",
        "desc": "Builds a delimited path string from parent-child hierarchy",
    },
    "PATHITEM": {
        "category": "hierarchy",
        "snowflake": "SPLIT_PART on recursive CTE output",
        "desc": "Extracts the nth item from a hierarchy path string",
    },
    "PATHITEMREVERSE": {
        "category": "hierarchy",
        "snowflake": "SPLIT_PART from end on recursive CTE output",
        "desc": "Extracts the nth item from end of a hierarchy path",
    },
    "PATHLENGTH": {
        "category": "hierarchy",
        "snowflake": "REGEXP_COUNT(path, delimiter) + 1",
        "desc": "Returns the number of levels in a hierarchy path",
    },
    # -- Time intelligence (manual) --
    "PARALLELPERIOD": {
        "category": "time_intelligence",
        "snowflake": "DATEADD on date filter — requires manual time range logic",
        "desc": "Returns a parallel date period shifted by intervals",
    },
    "OPENINGBALANCEMONTH": {
        "category": "time_intelligence",
        "snowflake": "LAG/FIRST_VALUE with month boundary logic",
        "desc": "Evaluates expression at the start of the month",
    },
    "OPENINGBALANCEQUARTER": {
        "category": "time_intelligence",
        "snowflake": "LAG/FIRST_VALUE with quarter boundary logic",
        "desc": "Evaluates expression at the start of the quarter",
    },
    "OPENINGBALANCEYEAR": {
        "category": "time_intelligence",
        "snowflake": "LAG/FIRST_VALUE with year boundary logic",
        "desc": "Evaluates expression at the start of the year",
    },
    "CLOSINGBALANCEMONTH": {
        "category": "time_intelligence",
        "snowflake": "LAST_VALUE with month boundary logic",
        "desc": "Evaluates expression at the end of the month",
    },
    "CLOSINGBALANCEQUARTER": {
        "category": "time_intelligence",
        "snowflake": "LAST_VALUE with quarter boundary logic",
        "desc": "Evaluates expression at the end of the quarter",
    },
    "CLOSINGBALANCEYEAR": {
        "category": "time_intelligence",
        "snowflake": "LAST_VALUE with year boundary logic",
        "desc": "Evaluates expression at the end of the year",
    },
    "CALENDAR": {
        "category": "time_intelligence",
        "snowflake": "Date dimension view or TABLE(GENERATOR(...))",
        "desc": "Generates a single-column date table for a range",
    },
    "CALENDARAUTO": {
        "category": "time_intelligence",
        "snowflake": "Date dimension view spanning model date range",
        "desc": "Auto-generates a date table from model date columns",
    },
    # -- Translation-tier time intelligence --
    "DATESYTD": {
        "category": "time_intelligence",
        "snowflake": "WHERE date BETWEEN DATE_TRUNC('YEAR', CURRENT_DATE) AND CURRENT_DATE",
        "desc": "Returns year-to-date dates",
    },
    "DATESMTD": {
        "category": "time_intelligence",
        "snowflake": "WHERE date BETWEEN DATE_TRUNC('MONTH', CURRENT_DATE) AND CURRENT_DATE",
        "desc": "Returns month-to-date dates",
    },
    "DATESQTD": {
        "category": "time_intelligence",
        "snowflake": "WHERE date BETWEEN DATE_TRUNC('QUARTER', CURRENT_DATE) AND CURRENT_DATE",
        "desc": "Returns quarter-to-date dates",
    },
    "DATEADD": {
        "category": "time_intelligence",
        "snowflake": "DATEADD(part, n, date) — argument order differs from DAX",
        "desc": "Shifts dates by a specified interval",
    },
    "DATESBETWEEN": {
        "category": "time_intelligence",
        "snowflake": "WHERE date BETWEEN start AND end",
        "desc": "Returns dates between two boundaries",
    },
    "DATESINPERIOD": {
        "category": "time_intelligence",
        "snowflake": "WHERE date BETWEEN anchor AND DATEADD(part, n, anchor)",
        "desc": "Returns dates in a period relative to an anchor date",
    },
    "SAMEPERIODLASTYEAR": {
        "category": "time_intelligence",
        "snowflake": "DATEADD('YEAR', -1, date) in filter or LAG window function",
        "desc": "Returns the same date range shifted back one year",
    },
    "TOTALYTD": {
        "category": "time_intelligence",
        "snowflake": "SUM(CASE WHEN date >= DATE_TRUNC('YEAR', CURRENT_DATE) ... THEN col END)",
        "desc": "Evaluates a measure for year-to-date",
    },
    "TOTALMTD": {
        "category": "time_intelligence",
        "snowflake": "SUM(CASE WHEN date >= DATE_TRUNC('MONTH', CURRENT_DATE) ... THEN col END)",
        "desc": "Evaluates a measure for month-to-date",
    },
    "TOTALQTD": {
        "category": "time_intelligence",
        "snowflake": "SUM(CASE WHEN date >= DATE_TRUNC('QUARTER', CURRENT_DATE) ... THEN col END)",
        "desc": "Evaluates a measure for quarter-to-date",
    },
    "PREVIOUSDAY": {
        "category": "time_intelligence",
        "snowflake": "DATEADD('DAY', -1, date)",
        "desc": "Returns the previous day",
    },
    "PREVIOUSMONTH": {
        "category": "time_intelligence",
        "snowflake": "DATEADD('MONTH', -1, date) range",
        "desc": "Returns the previous month date range",
    },
    "PREVIOUSQUARTER": {
        "category": "time_intelligence",
        "snowflake": "DATEADD('QUARTER', -1, date) range",
        "desc": "Returns the previous quarter date range",
    },
    "PREVIOUSYEAR": {
        "category": "time_intelligence",
        "snowflake": "DATEADD('YEAR', -1, date) range",
        "desc": "Returns the previous year date range",
    },
    "NEXTDAY": {
        "category": "time_intelligence",
        "snowflake": "DATEADD('DAY', 1, date)",
        "desc": "Returns the next day",
    },
    "NEXTMONTH": {
        "category": "time_intelligence",
        "snowflake": "DATEADD('MONTH', 1, date) range",
        "desc": "Returns the next month date range",
    },
    "NEXTQUARTER": {
        "category": "time_intelligence",
        "snowflake": "DATEADD('QUARTER', 1, date) range",
        "desc": "Returns the next quarter date range",
    },
    "NEXTYEAR": {
        "category": "time_intelligence",
        "snowflake": "DATEADD('YEAR', 1, date) range",
        "desc": "Returns the next year date range",
    },
    "FIRSTDATE": {
        "category": "time_intelligence",
        "snowflake": "MIN(date) in filtered context",
        "desc": "Returns the first date in a filtered date column",
    },
    "LASTDATE": {
        "category": "time_intelligence",
        "snowflake": "MAX(date) in filtered context",
        "desc": "Returns the last date in a filtered date column",
    },
    # -- Statistical (manual) --
    "PERCENTILEXINC": {
        "category": "statistical",
        "snowflake": "PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY expr)",
        "desc": "Calculates a percentile using inclusive interpolation over an iterator",
    },
    "PERCENTILEXEXC": {
        "category": "statistical",
        "snowflake": "PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY expr)",
        "desc": "Calculates a percentile using exclusive interpolation over an iterator",
    },
    "MEDIANX": {
        "category": "statistical",
        "snowflake": "MEDIAN(expr) or PERCENTILE_CONT(0.5)",
        "desc": "Calculates the median over an iterator expression",
    },
    # -- Financial (manual) --
    "NPV": {
        "category": "financial",
        "snowflake": "No built-in — implement with SUM of discounted cash flows",
        "desc": "Calculates net present value of cash flows",
    },
    "XNPV": {
        "category": "financial",
        "snowflake": "No built-in — implement with date-weighted discounting",
        "desc": "Calculates net present value with irregular dates",
    },
    "IRR": {
        "category": "financial",
        "snowflake": "No built-in — implement with Newton's method UDF",
        "desc": "Calculates internal rate of return for periodic cash flows",
    },
    "XIRR": {
        "category": "financial",
        "snowflake": "No built-in — implement with Newton's method UDF",
        "desc": "Calculates internal rate of return with irregular dates",
    },
    "PMT": {
        "category": "financial",
        "snowflake": "No built-in — implement with formula UDF",
        "desc": "Calculates periodic payment for a loan",
    },
    "PV": {
        "category": "financial",
        "snowflake": "No built-in — implement with formula UDF",
        "desc": "Calculates present value of a series of payments",
    },
    "FV": {
        "category": "financial",
        "snowflake": "No built-in — implement with formula UDF",
        "desc": "Calculates future value of a series of payments",
    },
    # -- Translation-tier patterns --
    "SUMX": {
        "category": "iterator",
        "snowflake": "SUM(expr) — simple iterator maps to aggregate",
        "desc": "Iterates a table and sums an expression per row",
    },
    "AVERAGEX": {
        "category": "iterator",
        "snowflake": "AVG(expr) — simple iterator maps to aggregate",
        "desc": "Iterates a table and averages an expression per row",
    },
    "COUNTX": {
        "category": "iterator",
        "snowflake": "COUNT(expr) — simple iterator maps to aggregate",
        "desc": "Iterates a table and counts an expression per row",
    },
    "MINX": {
        "category": "iterator",
        "snowflake": "MIN(expr)",
        "desc": "Iterates a table and returns the minimum",
    },
    "MAXX": {
        "category": "iterator",
        "snowflake": "MAX(expr)",
        "desc": "Iterates a table and returns the maximum",
    },
    "DIVIDE": {
        "category": "null_handling",
        "snowflake": "DIV0NULL(a, b) or CASE WHEN b = 0 THEN NULL ELSE a/b END",
        "desc": "Divides two numbers, returns BLANK on divide-by-zero",
    },
    "IF": {
        "category": "conditional",
        "snowflake": "CASE WHEN condition THEN true_val ELSE false_val END",
        "desc": "Conditional branching",
    },
    "SWITCH": {
        "category": "conditional",
        "snowflake": "CASE expr WHEN val THEN result ... END",
        "desc": "Multi-value conditional (like CASE WHEN)",
    },
    "IFERROR": {
        "category": "conditional",
        "snowflake": "TRY_* functions or CASE with error check",
        "desc": "Returns alternate value if expression errors",
    },
    "FORMAT": {
        "category": "formatting",
        "snowflake": "TO_VARCHAR(value, format) — format string syntax differs",
        "desc": "Formats a value as text with a format string",
    },
    "ISBLANK": {
        "category": "null_handling",
        "snowflake": "expr IS NULL",
        "desc": "Tests if a value is blank (null)",
    },
    "DISTINCTCOUNT": {
        "category": "aggregation",
        "snowflake": "COUNT(DISTINCT column)",
        "desc": "Counts distinct values in a column",
    },
    "COALESCE": {
        "category": "null_handling",
        "snowflake": "COALESCE(a, b)",
        "desc": "Returns first non-blank argument",
    },
    "CONCATENATEX": {
        "category": "text",
        "snowflake": "LISTAGG(col, separator) WITHIN GROUP (ORDER BY ...)",
        "desc": "Concatenates values from a table with a delimiter",
    },
    "LOOKUPVALUE": {
        "category": "table_navigation",
        "snowflake": "Scalar subquery with WHERE or JOIN",
        "desc": "Looks up a value from another table by matching criteria",
    },
    # -- Text translation --
    "MID": {
        "category": "text",
        "snowflake": "SUBSTR(text, start, length)",
        "desc": "Extracts a substring by position",
    },
    "SUBSTITUTE": {
        "category": "text",
        "snowflake": "REPLACE(text, old, new)",
        "desc": "Replaces occurrences of a string",
    },
    "REPLACE": {
        "category": "text",
        "snowflake": "INSERT(text, start, length, new_text)",
        "desc": "Replaces characters at a position",
    },
    "SEARCH": {
        "category": "text",
        "snowflake": "CHARINDEX(find, text, start) — case-insensitive",
        "desc": "Finds text position (case-insensitive)",
    },
    "FIND": {
        "category": "text",
        "snowflake": "CHARINDEX(find, text, start) — case-sensitive",
        "desc": "Finds text position (case-sensitive)",
    },
    "VALUE": {
        "category": "type_conversion",
        "snowflake": "TRY_CAST(text AS NUMBER)",
        "desc": "Converts text to a number",
    },
    "FIXED": {
        "category": "formatting",
        "snowflake": "TO_VARCHAR(number, format_string)",
        "desc": "Formats a number with fixed decimal places",
    },
    "COMBINEVALUES": {
        "category": "text",
        "snowflake": "CONCAT_WS(separator, val1, val2, ...)",
        "desc": "Concatenates values with a delimiter",
    },
    # -- Math translation --
    "ROUNDUP": {
        "category": "math",
        "snowflake": "CEIL(n * POWER(10, d)) / POWER(10, d)",
        "desc": "Rounds a number up away from zero",
    },
    "ROUNDDOWN": {
        "category": "math",
        "snowflake": "TRUNC(n, digits)",
        "desc": "Rounds a number down toward zero",
    },
    "INT": {
        "category": "math",
        "snowflake": "FLOOR(n)",
        "desc": "Rounds down to the nearest integer",
    },
    "CEILING": {
        "category": "math",
        "snowflake": "CEIL(n)",
        "desc": "Rounds up to nearest multiple",
    },
    "FLOOR": {
        "category": "math",
        "snowflake": "FLOOR(n, significance)",
        "desc": "Rounds down to nearest multiple",
    },
    "LOG": {
        "category": "math",
        "snowflake": "LOG(base, n)",
        "desc": "Logarithm with specified base",
    },
    "LOG10": {
        "category": "math",
        "snowflake": "LOG(10, n)",
        "desc": "Base-10 logarithm",
    },
    "RAND": {
        "category": "math",
        "snowflake": "UNIFORM(0::float, 1::float, RANDOM())",
        "desc": "Returns a random number between 0 and 1",
    },
    "RANDBETWEEN": {
        "category": "math",
        "snowflake": "UNIFORM(low, high, RANDOM())",
        "desc": "Returns a random integer in a range",
    },
    "EVEN": {
        "category": "math",
        "snowflake": "CEIL(n / 2) * 2",
        "desc": "Rounds up to the nearest even integer",
    },
    "ODD": {
        "category": "math",
        "snowflake": "CEIL((n - 1) / 2) * 2 + 1",
        "desc": "Rounds up to the nearest odd integer",
    },
    "QUOTIENT": {
        "category": "math",
        "snowflake": "TRUNC(a / b)",
        "desc": "Integer division (discards remainder)",
    },
    "CURRENCY": {
        "category": "type_conversion",
        "snowflake": "CAST(n AS DECIMAL(19, 4))",
        "desc": "Converts to fixed-point currency type",
    },
    "MROUND": {
        "category": "math",
        "snowflake": "ROUND(n / multiple) * multiple",
        "desc": "Rounds to the nearest specified multiple",
    },
    # -- Info functions --
    "ISERROR": {
        "category": "error_handling",
        "snowflake": "TRY_* wrapper or IS NULL check",
        "desc": "Tests if an expression results in an error",
    },
    "ISNUMBER": {
        "category": "type_check",
        "snowflake": "TRY_CAST(expr AS NUMBER) IS NOT NULL",
        "desc": "Tests if a value is numeric",
    },
    "ISTEXT": {
        "category": "type_check",
        "snowflake": "TYPEOF(expr) = 'VARCHAR'",
        "desc": "Tests if a value is text",
    },
    "ISLOGICAL": {
        "category": "type_check",
        "snowflake": "TYPEOF(expr) = 'BOOLEAN'",
        "desc": "Tests if a value is boolean",
    },
    "ISNONTEXT": {
        "category": "type_check",
        "snowflake": "TYPEOF(expr) != 'VARCHAR'",
        "desc": "Tests if a value is not text",
    },
    "USERNAME": {
        "category": "user_context",
        "snowflake": "CURRENT_USER()",
        "desc": "Returns the current user's domain name",
    },
    "USERPRINCIPALNAME": {
        "category": "user_context",
        "snowflake": "CURRENT_USER()",
        "desc": "Returns the current user's UPN",
    },
    # -- Stats translation --
    "PERCENTILEINC": {
        "category": "statistical",
        "snowflake": "PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY col)",
        "desc": "Calculates percentile with inclusive interpolation",
    },
    "PERCENTILEEXC": {
        "category": "statistical",
        "snowflake": "PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY col)",
        "desc": "Calculates percentile with exclusive interpolation",
    },
    "NORMDIST": {
        "category": "statistical",
        "snowflake": "NORMAL_CDF(mean, stddev, x) — Snowflake built-in",
        "desc": "Returns the normal distribution probability",
    },
}

# Compile per-pattern regexes for detailed matching (needed for findall)
_INDIVIDUAL_PATTERNS: dict[str, re.Pattern] = {}
for _name, _regex in [
    *[(re.sub(r"[\\b()]|\(\?!.*?\)", "", p).replace(".", ""), p)
      for p in _MANUAL_REQUIRED_PATTERNS],
    *[(re.sub(r"[\\b()]|\(\?!.*?\)", "", p).replace(".", ""), p)
      for p in _NEEDS_TRANSLATION_PATTERNS],
]:
    # Normalize name: strip regex escapes to get "CALCULATE", "PERCENTILEXINC" etc.
    clean = _name.upper().strip()
    if clean and clean not in _INDIVIDUAL_PATTERNS:
        _INDIVIDUAL_PATTERNS[clean] = re.compile(_regex, re.IGNORECASE)

# Also add FILTER since it's important but not in the tier lists (it's implicit
# in CALCULATE usage) — we still want to detect it for description purposes.
_INDIVIDUAL_PATTERNS["FILTER"] = re.compile(r"\bFILTER\b", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Semantic category labels (human-readable)
# ---------------------------------------------------------------------------

_CATEGORY_LABELS: dict[str, str] = {
    "filter_context": "Filter Context Manipulation",
    "row_context": "Row Context (Nested)",
    "slicer_context": "Slicer / Filter Inspection",
    "ranking": "Ranking",
    "relationship": "Relationship Manipulation",
    "table_navigation": "Table Navigation",
    "table_manipulation": "Table Manipulation",
    "hierarchy": "Parent-Child Hierarchy",
    "time_intelligence": "Time Intelligence",
    "iterator": "Iterator (Row-by-Row)",
    "statistical": "Statistical",
    "financial": "Financial",
    "null_handling": "Null / Blank Handling",
    "conditional": "Conditional Logic",
    "formatting": "Formatting / Display",
    "text": "Text Manipulation",
    "math": "Math",
    "type_conversion": "Type Conversion",
    "type_check": "Type Checking",
    "error_handling": "Error Handling",
    "user_context": "User Context",
    "aggregation": "Aggregation",
    "presentation": "Presentation / UI Logic",
}


# ---------------------------------------------------------------------------
# Recommendation engine
# ---------------------------------------------------------------------------

def _build_recommendation(
    categories: set[str],
    matched: list[str],
    tier: str,
    table_name: str,
    expression: str,
) -> tuple[str, str]:
    """Determine a Snowflake recommendation and effort estimate.

    Returns:
        (recommendation_text, effort_size) where effort is "S", "M", or "L".
    """
    matched_upper = {m.upper() for m in matched}
    expr_upper = expression.upper()

    # -- Presentation-layer detection --
    # Chart titles, conditional formatting, slicer-dependent display text
    is_layout_table = any(kw in table_name.lower() for kw in ("layout", "title", "format"))
    slicer_only = categories <= {"slicer_context", "conditional", "formatting", "presentation"}
    uses_nameof = "NAMEOF" in expr_upper
    # SELECTEDVALUE + SWITCH/IF in a layout table → presentation logic
    if is_layout_table or (slicer_only and ("SELECTEDVALUE" in matched_upper or uses_nameof)):
        return (
            "Keep in DAX — presentation-layer logic (chart titles, conditional "
            "formatting, slicer-dependent display). Not applicable to Semantic View.",
            "S",
        )

    if tier == "simple":
        return ("Direct SQL equivalent — auto-convert.", "S")

    # -- Category-based recommendations --
    if "hierarchy" in categories:
        return (
            "Rewrite: Use recursive CTE (WITH RECURSIVE) or "
            "CONNECT BY to traverse parent-child hierarchy.",
            "L",
        )

    if "financial" in categories:
        return (
            "Rewrite: Implement financial function as Snowflake UDF "
            "(no built-in equivalent). Consider a Python UDF for complex formulas.",
            "L",
        )

    if "time_intelligence" in categories and "filter_context" not in categories:
        return (
            "Translate: Use DATEADD / DATE_TRUNC with WHERE clause or "
            "window functions (LAG, LEAD, SUM OVER) for time-based comparison.",
            "M",
        )

    if categories == {"filter_context"} and matched_upper <= {"CALCULATE", "FILTER"}:
        return (
            "Translate: Simple CALCULATE+FILTER → rewrite as "
            "aggregate with WHERE clause or CASE WHEN predicate.",
            "M",
        )

    # -- CALCULATE + simple aggregation with literal filter predicates --
    # e.g. CALCULATE(DISTINCTCOUNT(col), table[col] = "literal")
    # → COUNT(DISTINCT col) WHERE col = 'value'
    _SIMPLE_AGGS = {"DISTINCTCOUNT", "COUNTROWS", "COUNT", "SUM", "MAX", "MIN", "AVERAGE", "COUNTA"}
    _CONTEXT_REMOVERS = {"ALL", "ALLEXCEPT", "ALLSELECTED", "ISINSCOPE"}
    if (
        "filter_context" in categories
        and "CALCULATE" in matched_upper
        and (matched_upper & _SIMPLE_AGGS)
        and not (matched_upper & _CONTEXT_REMOVERS)
        and "iterator" not in categories
        and "slicer_context" not in categories
        and categories <= {"filter_context", "aggregation", "null_handling"}
    ):
        agg_fn = (matched_upper & _SIMPLE_AGGS).pop()
        sql_agg = {
            "DISTINCTCOUNT": "COUNT(DISTINCT col)",
            "COUNTROWS": "COUNT(*)",
            "COUNT": "COUNT(col)",
            "COUNTA": "COUNT(col)  -- non-blank",
            "SUM": "SUM(col)",
            "MAX": "MAX(col)",
            "MIN": "MIN(col)",
            "AVERAGE": "AVG(col)",
        }.get(agg_fn, "aggregate")
        null_note = ""
        if "null_handling" in categories:
            null_note = " Add IS [NOT] NULL predicate for blank checks."
        return (
            f"Translate to SQL: CALCULATE({agg_fn}(...), filter) → "
            f"{sql_agg} with WHERE clause.{null_note}",
            "S",
        )

    # -- CALCULATE + COUNTROWS/aggregation + ISBLANK (no other complexity) --
    # e.g. CALCULATE(COUNTROWS(t), NOT ISBLANK(t[col]))
    # → COUNT(*) WHERE col IS NOT NULL
    if (
        "filter_context" in categories
        and "null_handling" in categories
        and "CALCULATE" in matched_upper
        and not (matched_upper & _CONTEXT_REMOVERS)
        and "iterator" not in categories
        and "slicer_context" not in categories
        and categories <= {"filter_context", "null_handling", "conditional"}
    ):
        return (
            "Translate to SQL: CALCULATE with ISBLANK filter → "
            "aggregate with WHERE col IS [NOT] NULL.",
            "S",
        )

    # -- SUMX + VALUES + CALCULATE(simple agg) — GROUP BY pattern --
    # e.g. SUMX(VALUES(t[key]), CALCULATE(MAX(t[col])))
    # → SELECT key, MAX(col) FROM t GROUP BY key — then SUM in outer query
    if (
        "filter_context" in categories
        and "iterator" in categories
        and "SUMX" in matched_upper
        and "CALCULATE" in matched_upper
        and not (matched_upper & _CONTEXT_REMOVERS)
        and "slicer_context" not in categories
        and categories <= {"filter_context", "iterator", "aggregation"}
    ):
        return (
            "Translate to SQL: SUMX(VALUES(...), CALCULATE(agg)) → "
            "subquery with GROUP BY, then SUM in outer query.",
            "M",
        )

    if "filter_context" in categories and "table_navigation" in categories:
        return (
            "Rewrite: CALCULATE with RELATED/table navigation → "
            "JOIN + WHERE/CASE WHEN. Map each RELATED() to the joined column.",
            "M",
        )

    if "filter_context" in categories:
        context_fns = matched_upper & {"ALL", "ALLEXCEPT", "ALLSELECTED"}
        if context_fns:
            return (
                "Manual review: Filter context removal (ALL/ALLEXCEPT) — "
                "requires understanding which filters to remove. Consider "
                "unfiltered subquery or separate pre-aggregated view.",
                "L",
            )
        return (
            "Manual review: Complex filter context manipulation. "
            "Present original DAX to developer for Snowflake equivalent.",
            "M",
        )

    if "slicer_context" in categories:
        return (
            "Keep in DAX — slicer-dependent logic that requires "
            "BI tool filter context. Not translatable to static SQL.",
            "S",
        )

    if "ranking" in categories:
        return (
            "Translate: Use RANK() / ROW_NUMBER() / DENSE_RANK() "
            "OVER (ORDER BY expr). Note: DAX RANKX context nuances may differ.",
            "M",
        )

    if "relationship" in categories:
        return (
            "Manual review: Relationship manipulation "
            "(USERELATIONSHIP/CROSSFILTER/TREATAS). Requires model redesign "
            "or custom JOIN logic in Snowflake.",
            "L",
        )

    if "table_manipulation" in categories:
        return (
            "Rewrite: Table manipulation function → Snowflake view or CTE "
            "with SELECT/GROUP BY equivalent.",
            "M",
        )

    if tier == "needs_translation":
        return (
            "Translate: Has Snowflake SQL equivalent with syntax adjustments. "
            "See pattern-level hints for specific translations.",
            "S",
        )

    # Fallback for manual_required
    return (
        "Manual review: Complex DAX with no direct SQL equivalent. "
        "Present original DAX to developer for Snowflake equivalent.",
        "M",
    )


# ---------------------------------------------------------------------------
# Natural-language description builder
# ---------------------------------------------------------------------------

def _build_description(
    matched: list[str],
    expression: str,
    table_name: str,
) -> str:
    """Build a natural-language description of what the DAX measure does.

    Composes a description from the combination of matched patterns, the
    aggregate function, and table/column references found in the expression.
    """
    if not expression or not expression.strip():
        return "Empty expression"

    parts: list[str] = []
    expr_upper = expression.upper()

    # Detect the core aggregate/operation
    agg_map = {
        "SUM": "Sums", "COUNT": "Counts", "COUNTROWS": "Counts rows of",
        "DISTINCTCOUNT": "Counts distinct values of",
        "AVERAGE": "Averages", "MIN": "Finds minimum of", "MAX": "Finds maximum of",
        "SUMX": "Iterates and sums", "AVERAGEX": "Iterates and averages",
        "COUNTX": "Iterates and counts", "RANKX": "Ranks by",
        "CONCATENATEX": "Concatenates values of",
    }
    for fn, verb in agg_map.items():
        if re.search(rf"\b{fn}\b", expr_upper):
            # Try to extract the column reference
            col_match = re.search(
                rf"\b{fn}\s*\(\s*(?:\'[^']+\'\s*,\s*)?\'?([^'\[\]]+)\[([^\]]+)\]",
                expression, re.IGNORECASE,
            )
            if col_match:
                parts.append(f"{verb} {col_match.group(1).strip()}.{col_match.group(2).strip()}")
            else:
                parts.append(verb)
            break

    # Detect filters
    if "CALCULATE" in (m.upper() for m in matched):
        filter_clauses: list[str] = []
        # Simple predicate filters: Column[col] op value
        for fm in re.finditer(
            r"'([^']+)'\[([^\]]+)\]\s*([<>=!]+)\s*(\d+|\"[^\"]*\")",
            expression,
        ):
            filter_clauses.append(
                f"{fm.group(1)}.{fm.group(2)} {fm.group(3)} {fm.group(4)}"
            )
        # ISBLANK / NOT ISBLANK
        if re.search(r"\bISBLANK\b", expr_upper):
            blank_col = re.search(
                r"ISBLANK\s*\(\s*(?:RELATED\s*\(\s*)?'([^']+)'\[([^\]]+)\]",
                expression, re.IGNORECASE,
            )
            if blank_col:
                if re.search(r"\bNOT\b\s*\(?\s*ISBLANK", expr_upper):
                    filter_clauses.append(f"{blank_col.group(1)}.{blank_col.group(2)} is not blank")
                else:
                    filter_clauses.append(f"{blank_col.group(1)}.{blank_col.group(2)} is blank")

        if filter_clauses:
            parts.append("where " + " and ".join(filter_clauses))

    # Detect RELATED (joins)
    related_refs = re.findall(
        r"RELATED\s*\(\s*'([^']+)'\[([^\]]+)\]", expression, re.IGNORECASE,
    )
    if related_refs:
        refs = [f"{r[0]}.{r[1]}" for r in related_refs]
        parts.append(f"(joins to {', '.join(refs)})")

    # Detect SELECTEDVALUE (slicer-dependent)
    if re.search(r"\bSELECTEDVALUE\b", expr_upper):
        sv_col = re.search(
            r"SELECTEDVALUE\s*\(\s*'([^']+)'\[([^\]]+)\]",
            expression, re.IGNORECASE,
        )
        if sv_col:
            parts.append(f"Returns selected slicer value of {sv_col.group(1)}.{sv_col.group(2)}")
        else:
            parts.append("Returns the currently selected slicer value")

    # Detect SWITCH (conditional branching)
    if re.search(r"\bSWITCH\b", expr_upper):
        switch_count = len(re.findall(r",", expression))
        parts.append(f"with conditional branching ({max(1, switch_count // 2)} cases)")

    # Detect PATH hierarchy
    if re.search(r"\bPATH\b", expr_upper):
        path_cols = re.search(
            r"PATH\s*\(\s*[^,]+\[([^\]]+)\]\s*,\s*[^,]+\[([^\]]+)\]",
            expression, re.IGNORECASE,
        )
        if path_cols:
            parts.append(f"Builds hierarchy path from {path_cols.group(1)} to {path_cols.group(2)}")
        else:
            parts.append("Builds parent-child hierarchy path")

    # Detect time intelligence
    time_fns = {"TOTALYTD", "TOTALMTD", "TOTALQTD", "SAMEPERIODLASTYEAR",
                "DATEADD", "PARALLELPERIOD", "PREVIOUSYEAR", "PREVIOUSMONTH"}
    found_time = [fn for fn in time_fns if re.search(rf"\b{fn}\b", expr_upper)]
    if found_time:
        parts.append(f"with time intelligence ({', '.join(found_time)})")

    if not parts:
        # Fallback: summarize from expression length and pattern count
        n_patterns = len(matched)
        if n_patterns == 0:
            return "Simple expression"
        return f"DAX expression using {', '.join(matched)}"

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_dax_complexity(expression: str) -> str:
    """Classify the complexity of a DAX expression.

    Evaluation order (first match wins):
    1. manual_required — context-manipulation or advanced table functions
    2. needs_translation — iterators, time intelligence, or complex text/math
    3. simple — direct aggregate or SQL-equivalent scalar functions

    Args:
        expression: DAX expression string (measure or calculated column body).

    Returns:
        One of: 'simple', 'needs_translation', 'manual_required'
    """
    log.debug("classify_dax_complexity: entry — len=%d", len(expression))

    if not expression or not expression.strip():
        log.debug("classify_dax_complexity: empty expression → simple")
        return "simple"

    try:
        if _MANUAL_RE.search(expression):
            log.debug("classify_dax_complexity: manual_required match")
            return "manual_required"

        if _NEEDS_RE.search(expression):
            log.debug("classify_dax_complexity: needs_translation match")
            return "needs_translation"

        return "simple"

    except Exception as exc:
        err = fail_step("classify_dax_complexity", exc)
        log.warning(
            "classify_dax_complexity: classification failed, defaulting to "
            "manual_required — %s",
            err["error_message"],
        )
        return "manual_required"


def classify_dax_detail(
    expression: str,
    table_name: str = "",
    measure_name: str = "",
) -> dict[str, Any]:
    """Classify a DAX expression with full enrichment detail.

    Returns a dict with:
        tier: str — 'simple', 'needs_translation', 'manual_required'
        matched_patterns: list[str] — DAX function names detected
        categories: list[str] — semantic category labels
        description: str — natural-language description of what the measure does
        recommendation: str — Snowflake translation recommendation
        effort: str — 'S', 'M', or 'L'
        hints: list[dict] — per-pattern Snowflake translation hints
    """
    tier = classify_dax_complexity(expression)

    if not expression or not expression.strip():
        return {
            "tier": tier,
            "matched_patterns": [],
            "categories": [],
            "description": "Empty expression",
            "recommendation": "No action needed.",
            "effort": "S",
            "hints": [],
        }

    # Find all matched patterns
    matched: list[str] = []
    for name, regex in _INDIVIDUAL_PATTERNS.items():
        if regex.search(expression):
            matched.append(name)

    # Collect categories and hints
    categories: set[str] = set()
    hints: list[dict[str, str]] = []
    for pat in matched:
        meta = _DAX_PATTERN_META.get(pat, _DAX_PATTERN_META.get(pat.upper()))
        if meta:
            categories.add(meta["category"])
            hints.append({
                "pattern": pat,
                "category": _CATEGORY_LABELS.get(meta["category"], meta["category"]),
                "snowflake": meta["snowflake"],
                "description": meta["desc"],
            })

    # Build description and recommendation
    description = _build_description(matched, expression, table_name)
    recommendation, effort = _build_recommendation(
        categories, matched, tier, table_name, expression,
    )

    return {
        "tier": tier,
        "matched_patterns": matched,
        "categories": sorted(categories),
        "description": description,
        "recommendation": recommendation,
        "effort": effort,
        "hints": hints,
    }


def classify_all_measures(tables: list[dict]) -> dict[str, dict[str, Any]]:
    """Classify every measure and calculated column across all tables.

    Args:
        tables: List of table dicts as produced by extract_semantics(), each
                containing 'name', 'measures', and 'columns' keys.

    Returns:
        Dict mapping "<table_name>.<measure_or_column_name>" → detail dict.
        Each value has 'tier' (the complexity string) plus full enrichment.
        Never raises — items that fail classification default to 'manual_required'.
    """
    log.info(
        "classify_all_measures: entry — tables=%d",
        len(tables),
    )

    result: dict[str, dict[str, Any]] = {}

    for tbl in tables:
        tbl_name: str = tbl.get("name", "")

        # Measures
        for meas in tbl.get("measures", []):
            meas_name = meas.get("name", "")
            expr = meas.get("expression", "")
            if isinstance(expr, list):
                expr = "\n".join(expr)
            key = f"{tbl_name}.{meas_name}"
            try:
                result[key] = classify_dax_detail(expr, tbl_name, meas_name)
            except Exception as exc:
                fail_step(f"classify_measure:{key}", exc)
                result[key] = {
                    "tier": "manual_required",
                    "matched_patterns": [],
                    "categories": [],
                    "description": "Classification failed",
                    "recommendation": "Manual review required — classification error.",
                    "effort": "M",
                    "hints": [],
                }

        # Calculated columns
        for col in tbl.get("columns", []):
            if not col.get("is_calculated"):
                continue
            col_name = col.get("name", "")
            expr = col.get("expression", "") or ""
            if isinstance(expr, list):
                expr = "\n".join(expr)
            key = f"{tbl_name}.{col_name}"
            try:
                result[key] = classify_dax_detail(expr, tbl_name, col_name)
            except Exception as exc:
                fail_step(f"classify_column:{key}", exc)
                result[key] = {
                    "tier": "manual_required",
                    "matched_patterns": [],
                    "categories": [],
                    "description": "Classification failed",
                    "recommendation": "Manual review required — classification error.",
                    "effort": "M",
                    "hints": [],
                }

    counts: dict[str, int] = {}
    for v in result.values():
        t = v["tier"]
        counts[t] = counts.get(t, 0) + 1

    log.info(
        "classify_all_measures: exit — classified=%d, distribution=%s",
        len(result),
        counts,
    )
    return result
