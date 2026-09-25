"""Check a composed app only names columns the reporting views actually expose.

The most damaging class of composition defect, measured twice: the composed React
report referenced FISCAL_QUARTER_LABEL, SALES_AMOUNT, BOOKED_AMOUNT,
GPC1_DESCRIPTION and TERRITORY_L1, none of which exist. The views expose
FISCAL_QUARTER_YEAR, SALES_AMOUNT_TOTAL, BOOKED_AMOUNT_TOTAL, GPC1 and
TERRITORY_LEVEL1. Every chart rendered empty, and nothing failed -- the query
returned rows, the component received a key that was not in them, and an empty
chart is a plausible-looking outcome.

The cause is that composition reads the semantic model's logical names while the app
queries the RPT_ views with SELECT *, so the physical names are never in front of
whoever is writing the props. Telling the agent to be careful does not fix that. This
does: the reporting views declare their columns statically in
pipeline/sql/45_reporting_views.sql, so the real names are knowable with no Snowflake
connection, and a mismatch is a mechanical diff rather than a judgement call.

Runs in well under a second and needs no warehouse, which is why it is one of the two
checks that survive the "hand the app over, do not audition it" rule.

    python3 pipeline/verify_composition.py                     # both surfaces
    python3 pipeline/verify_composition.py --app-react DIR     # explicit paths
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VIEWS_SQL = os.path.join(HERE, "sql", "45_reporting_views.sql")

# Props whose value is a column name in a dataset row. Anything a chart or table
# uses to look a value up out of a row belongs here; a name that is not a column
# produces an empty render rather than an error, which is why this list matters.
KEY_PROPS = ("xKey", "yKey", "valueKey", "labelKey", "categoryKey", "seriesKey",
             "sortKey", "rowKey", "colKey", "dataKey", "nameKey")

# The locked Streamlit libraries. A column reaches a chart as an argument to one of
# these, so their call sites are where the names worth checking live.
LIB_MODULES = {"charts", "kpi", "ui", "filters", "compat", "metrics"}

# Upper-case constants that are configuration, not columns. Excluded by the name they
# are bound to rather than by pattern-matching their value, which would be guesswork.
INFRA_NAMES = {"DATABASE", "DB", "SCHEMA", "WAREHOUSE", "WH", "CONNECTION",
               "CONNECTION_NAME", "ACCOUNT", "ROLE", "SEMANTIC_VIEW", "AGENT",
               "ASK_PROC", "ASK_CONTEXT", "PREFIX", "KB_DATABASE", "KB_DB",
               "KB_SCHEMA", "STAGE", "APP_SERVICE", "POOL", "MODEL_LABEL"}

_COLUMN_SHAPED = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")

_VIEW = re.compile(r"CREATE OR REPLACE VIEW\s+(RPT_\w+)(.*?)(?=CREATE OR REPLACE VIEW|\Z)",
                   re.S | re.I)
# Inside a SEMANTIC_VIEW(...) body, columns appear one per line, optionally
# qualified by a table alias, optionally followed by a comma or a closing paren.
_COL = re.compile(r"^\s*(?:\w+\.)?([A-Z][A-Z0-9_]*)\s*,?\s*$")
# Not every reporting view is built from SEMANTIC_VIEW(...). RPT_CALENDAR_BOUNDARY_GAP
# is a plain SELECT whose output names come from AS aliases, and missing them would
# make this check reject STRANDED_USD -- a real column -- which is the fastest way to
# get a validator ignored.
_ALIAS = re.compile(r"\bAS\s+([A-Z][A-Z0-9_]*)\s*,?\s*$", re.M)
_SKIP = {"DIMENSIONS", "METRICS", "FACTS", "AS", "SELECT", "FROM", "SEMANTIC_VIEW",
         "WHERE", "GROUP", "ORDER", "BY", "COMMENT", "USE", "SCHEMA", "WITH"}


def view_columns(sql_text: str) -> dict[str, set[str]]:
    """Map each RPT_ view to the column names it exposes.

    Parsed from the DDL rather than queried, so this works before a build, offline,
    and on a machine with no credentials.
    """
    out: dict[str, set[str]] = {}
    for name, body in _VIEW.findall(sql_text):
        cols: set[str] = set()
        for line in body.splitlines():
            if "--" in line:
                line = line.split("--")[0]
            m = _COL.match(line)
            if not m:
                continue
            token = m.group(1)
            if token in _SKIP or token.startswith("RPT_") or "{{" in line:
                continue
            cols.add(token)
        # Plain-SELECT views name their output with AS aliases.
        for alias in _ALIAS.findall(body):
            if alias not in _SKIP:
                cols.add(alias)
        if cols:
            out[name.upper()] = cols
    return out


def react_keys(app_dir: str) -> list[tuple[str, int, str]]:
    """Every (file, line, name) a React component uses to index into a row."""
    found: list[tuple[str, int, str]] = []
    prop = re.compile(r"\b(?:%s)\s*[:=]\s*[\"'`]([A-Za-z_][A-Za-z0-9_]*)[\"'`]"
                      % "|".join(KEY_PROPS))
    # levels={["A", "B"]} -- hierarchy drill paths, all of them column names.
    levels = re.compile(r"levels\s*=\s*\{?\s*\[([^\]]*)\]", re.S)
    literal = re.compile(r"[\"'`]([A-Za-z_][A-Za-z0-9_]*)[\"'`]")
    for root, _dirs, files in os.walk(app_dir):
        if "node_modules" in root or ".next" in root:
            continue
        for fn in files:
            if not fn.endswith((".tsx", ".ts")):
                continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, app_dir)
            text = open(path, encoding="utf-8").read()
            for i, line in enumerate(text.splitlines(), 1):
                for name in prop.findall(line):
                    found.append((rel, i, name))
            for block in levels.findall(text):
                start = text[:text.index(block)].count("\n") + 1
                for name in literal.findall(block):
                    found.append((rel, start, name))
    return found


def streamlit_keys(app_dir: str) -> list[tuple[str, int, str]]:
    """Column names a composed Streamlit page passes to the locked libraries.

    Parsed with ast rather than matched with regexes, because the first version of this
    used regexes for ``df["COL"]`` and a few keyword forms and found **zero** references
    in a real composed app -- reporting "0 unknown" and passing, while the React half of
    the same check was finding thirty. A check that silently checks nothing is worse
    than no check, since it also removes the suspicion that would have caught it.

    What the composed pages actually do is pass columns positionally to the locked
    library: ``charts.ranked_bar(territory, "TERRITORY_LEVEL1", SALES, emits=...)``,
    and hold the measures in module constants. So collect string literals that are:

      * any argument to a call on one of the locked library modules,
      * assigned to a module-level constant (``SALES = "SALES_AMOUNT_TOTAL"``),
      * a dict key (the label maps are keyed by column), or
      * a subscript (``df["COL"]``).

    Infrastructure constants are excluded by the name they are bound to, not by
    guessing from their value: DATABASE, SCHEMA and WAREHOUSE are upper-case and
    column-shaped, and on the reference app those three were the only false positives.
    """
    found: list[tuple[str, int, str]] = []
    for root, dirs, files in os.walk(app_dir):
        dirs[:] = [d for d in dirs if d not in ("bim_ui", "__pycache__", "node_modules")]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, app_dir)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read())
            except SyntaxError:
                # Reported by the compile check, not here.
                continue

            def take(node, line: int) -> None:
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and _COLUMN_SHAPED.match(node.value)):
                    found.append((rel, line, node.value))

            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id in LIB_MODULES):
                    for arg in node.args:
                        take(arg, node.lineno)
                    for kw in node.keywords:
                        take(kw.value, node.lineno)
                elif isinstance(node, ast.Assign):
                    names = {t.id for t in node.targets if isinstance(t, ast.Name)}
                    if names & INFRA_NAMES:
                        continue
                    take(node.value, node.lineno)
                elif isinstance(node, ast.Dict):
                    for key in node.keys:
                        if key is not None:
                            take(key, node.lineno)
                elif isinstance(node, ast.Subscript):
                    take(node.slice, node.lineno)
    return found


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--app-react", default=os.path.join(HERE, "app_react"))
    ap.add_argument("--app-streamlit", default=os.path.join(HERE, "app_streamlit"))
    ap.add_argument("--views", default=VIEWS_SQL)
    args = ap.parse_args(argv)

    views = view_columns(open(args.views, encoding="utf-8").read())
    if not views:
        print("FAIL could not parse any RPT_ view from %s" % args.views)
        return 2
    known: set[str] = set()
    for cols in views.values():
        known |= cols
    print("%d reporting views expose %d distinct columns"
          % (len(views), len(known)))

    problems: list[str] = []
    checked = 0
    for label, path, collect in (("React", args.app_react, react_keys),
                                 ("Streamlit", args.app_streamlit, streamlit_keys)):
        if not os.path.isdir(path):
            print("  %-9s not composed yet, skipped" % label)
            continue
        refs = collect(path)
        checked += len(refs)
        bad = [(f, n, name) for f, n, name in refs if name.upper() not in known]
        print("  %-9s %d column references, %d unknown" % (label, len(refs), len(bad)))
        for f, n, name in bad:
            near = sorted(k for k in known
                          if name.upper()[:6] in k or k[:6] in name.upper())
            hint = "  did you mean %s?" % ", ".join(near[:3]) if near else ""
            problems.append("%s %s:%d references %r, which no reporting view "
                            "exposes.%s" % (label, f, n, name, hint))

    if problems:
        print("\nFAIL %d reference(s) name a column that does not exist:" % len(problems))
        for p in problems:
            print("  - %s" % p)
        print("\nThese do not raise at runtime. The query succeeds, the component "
              "receives a key that is not in the row, and the panel renders empty.")
        return 1
    if not checked:
        print("\nNothing composed yet -- nothing to check.")
        return 0
    print("\nOK every column reference resolves to a reporting view column.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
