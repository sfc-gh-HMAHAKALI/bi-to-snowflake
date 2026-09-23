"""Verification suite for the two deployed report surfaces.

Checks what can be checked from SQL. It deliberately does not claim anything about how
either app renders -- that needs a browser, and when the browser is unavailable the honest
output is a stated gap rather than an inference from "the query works".
"""

import argparse
import json
import pathlib
import re
import sys

import snowflake.connector

import config

_ap = argparse.ArgumentParser(description=__doc__)
_ap.add_argument("--connection", default="default")
config.add_arguments(_ap)
_args = _ap.parse_args()
naming = config.from_args(_args)
DB = naming.database
ANALYTICS = naming.analytics

conn = snowflake.connector.connect(connection_name=_args.connection)
cur = conn.cursor()


def one(sql, params=None):
    cur.execute(sql, params or ())
    return cur.fetchone()


def show(sql):
    cur.execute(sql)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


checks = []

# Every dataset either app can request.
for name, view in [
    ("kpi", "RPT_KPI_SUMMARY"),
    ("period", "RPT_SALES_BY_PERIOD"),
    ("product", "RPT_SALES_BY_PRODUCT"),
    ("territory", "RPT_SALES_BY_TERRITORY"),
    ("customer", "RPT_SALES_BY_CUSTOMER"),
    ("detail", "RPT_SALES_DETAIL"),
    ("boundaryGap", "RPT_CALENDAR_BOUNDARY_GAP"),
    # Added after it broke. This view reads the KB database, which the caller
    # grants do not
    # cover, so it is the one dataset whose failure mode differs between the owner-rights
    # Streamlit app and the caller-rights React app.
    ("fiscalCalendar", "RPT_FISCAL_CALENDAR_EXPOSURE"),
]:
    n = one(f"SELECT COUNT(*) FROM {ANALYTICS}.{view}")[0]
    checks.append((f"dataset {name}", f"{n:,} rows", n > 0))

n = one(f"""SELECT COUNT(*) FROM {DB}.INFORMATION_SCHEMA.SEMANTIC_METRICS
            WHERE SEMANTIC_VIEW_NAME = '{naming.semantic_view}'""")[0]
checks.append(("semantic view metrics", str(n), n == 98))

svcs = show(f"SHOW APPLICATION SERVICES IN SCHEMA {ANALYTICS}")
svc = svcs[0] if svcs else {}
# SUSPENDED and SUSPENDING are healthy states, not failures. The service auto-suspends
# after 300s idle and AUTO_RESUME brings it back on the next request, so a check that
# insists on RUNNING fails purely as a function of how recently someone opened the app.
healthy_states = {"RUNNING", "SUSPENDED", "SUSPENDING", "RESUMING", "PENDING"}
checks.append(("React APPLICATION SERVICE",
               f"{svc.get('name')} status={svc.get('status')} "
               f"auto_resume={svc.get('auto_resume')}",
               str(svc.get("status")).upper() in healthy_states
               and str(svc.get("auto_resume")).lower() == "true"))
checks.append(("React app has an endpoint",
               str(svc.get("url") or "(none)"),
               bool(svc.get("url"))))

# runtime_name is not in SHOW STREAMLITS output, so read it where it actually lives.
desc = show(f"DESCRIBE STREAMLIT {ANALYTICS}.{naming.streamlit}")
d = desc[0] if desc else {}
checks.append(("Streamlit on container runtime",
               f"{d.get('name')} runtime={d.get('runtime_name')} pool={d.get('compute_pool')}",
               "CONTAINER" in str(d.get("runtime_name", "")).upper()))
checks.append(("Streamlit has a live version",
               str(d.get("live_version_location_uri") or "(none)"),
               bool(d.get("live_version_location_uri"))))

# The agent, through the route both apps use.
cur.execute(f"CALL {ANALYTICS}.{naming.ask_procedure}(%s, %s)",
            ("What were total sales bookings for FY2027 to date?",
             "No filters applied. Fiscal year runs July to June."))
result = json.loads(cur.fetchone()[0])
answer = result.get("answer") or ""
checks.append(("agent via the ASK procedure",
               f"{len(answer)} chars, error={result.get('error')!r}",
               not result.get("error") and len(answer) > 50))
# The answer arrives three times over in the event stream; this catches a regression in
# the precedence logic that dedupes it.
opening = answer[:50]
checks.append(("agent answer not duplicated",
               f"opening seen {answer.count(opening) if answer else 0}x",
               bool(answer) and answer.count(opening) == 1))
checks.append(("agent exposes its SQL",
               "yes" if result.get("sql") else "no",
               bool(result.get("sql"))))
checks.append(("agent figure present",
               "37,711,379 in answer" if "37,711,379" in answer else "figure absent",
               "37,711,379" in answer))

# The calendar boundary finding, as a regression test. If someone later "fixes" one of the
# two row filters, this stops reconciling and the walkthrough's headline number is stale.
total = float(one(f"SELECT SALES_AMOUNT_TOTAL FROM {ANALYTICS}.RPT_KPI_SUMMARY")[0])
by_fy = float(one(f"""SELECT SUM(SALES_AMOUNT_TOTAL) FROM {ANALYTICS}.RPT_SALES_BY_PERIOD
                     WHERE FISCAL_YEAR_NAME IS NOT NULL""")[0])
gap = float(one(f"SELECT STRANDED_USD FROM {ANALYTICS}.RPT_CALENDAR_BOUNDARY_GAP")[0])
checks.append(("boundary gap reconciles",
               f"total - byFiscalYear = {total - by_fy:,.2f} vs reported {gap:,.2f}",
               abs((total - by_fy) - gap) < 0.01))

# The fiscal-calendar finding, which is the headline claim in the overview document and
# the subject of the Fiscal calendar page in both apps.
#
# Two separate things are asserted here. First, that the fiscal side still reconciles to
# SALES_FISCAL_YTD in the semantic view -- that equality is the entire reason the
# comparison is a check rather than an illustration, and it breaks the moment someone
# adjusts the derived fiscal-start expression. Second, that the metric exposure counts
# still hold, because the document quotes them.
fy_start, cal_start, fiscal_ytd, cal_ytd, n_metrics, n_blocked, n_hardcoded = one(
    """SELECT FISCAL_START, CALENDAR_START, FISCAL_YTD, CALENDAR_YTD,
              TOTAL_METRICS, BLOCKER_METRICS, HARDCODED
         FROM {ANALYTICS}.RPT_FISCAL_CALENDAR_EXPOSURE""")

sv_fiscal_ytd = float(one(
    f"SELECT SALES_FISCAL_YTD FROM {ANALYTICS}.RPT_KPI_SUMMARY")[0])
checks.append(("fiscal YTD ties to semantic view",
               f"{float(fiscal_ytd):,.2f} vs SALES_FISCAL_YTD {sv_fiscal_ytd:,.2f} "
               f"(fiscal year starts {fy_start})",
               abs(float(fiscal_ytd) - sv_fiscal_ytd) < 0.01))

ratio = float(cal_ytd) / float(fiscal_ytd) if float(fiscal_ytd) else 0.0
checks.append(("calendar-year overstatement",
               f"{ratio:.2f}x -- fiscal {float(fiscal_ytd):,.2f} vs calendar "
               f"{float(cal_ytd):,.2f}, a {float(cal_ytd) - float(fiscal_ytd):,.2f} gap",
               ratio > 1.5))

checks.append(("fiscal-calendar metric exposure",
               f"{n_blocked} of {n_metrics} blocked, {n_hardcoded} hard-coded per year",
               int(n_blocked) > 0 and int(n_metrics) > 0
               and int(n_blocked) < int(n_metrics)))

grants = show("SHOW CALLER GRANTS TO ROLE ACCOUNTADMIN")
relevant = [g for g in grants
            if str(g.get("name") or "").upper().startswith(DB)
            or g.get("name") == "AI_ML_WH_SALES"
            or (g.get("inherited_from_database") == DB)]
checks.append(("caller grants for the app",
               f"{len(relevant)} on {DB} objects",
               len(relevant) >= 6))

# The React app must only read objects inside the target analytics schema.
#
# This is the invariant that actually broke, so it is the one worth encoding. The React app
# runs with caller's rights and its caller grants are scoped to the target database; the Streamlit app
# runs with owner's rights and can reach anything. A dataset that reads outside that schema
# therefore works in one app and fails in the other, and no owner-rights query reproduces it.
#
# It happened: the fiscal-calendar dataset queried the knowledge base schema.KB_ISSUE inline
# and failed in the deployed React app with "Could not load Fiscal year to date against
# calendar year to date" while Streamlit rendered it perfectly. The fix was to put it behind
# RPT_FISCAL_CALENDAR_EXPOSURE and let ownership chaining read across databases.
#
# Checked statically against the source rather than by querying, because the failure is about
# *whose* rights are in play, which a connection from here cannot reproduce.
queries_ts = pathlib.Path(__file__).with_name("app_react") / "lib" / "queries.ts"
if not queries_ts.exists():
    checks.append(("React reads only ANALYTICS", f"{queries_ts} not found", False))
else:
    src = queries_ts.read_text()
    # Only the SQL template literals, not the surrounding prose. The comments in that file
    # discuss the KB database by name precisely because reading it from the app was the bug, so
    # scanning the whole file would flag its own explanation.
    sql_blocks = re.findall(r"sql:\s*`(.*?)`", src, re.S)
    joined = "\n".join(sql_blocks)
    # Database-qualified references of the form DB.SCHEMA.OBJECT. The ${DB}.${SCHEMA}
    # template form is the target analytics schema by construction and does not match.
    refs = set(re.findall(r"\b([A-Z][A-Z0-9_]*)\.([A-Z][A-Z0-9_]*)\.[A-Z][A-Z0-9_]*",
                          joined))
    outside = sorted(f"{d}.{s}" for d, s in refs
                     if (d, s) != (DB, naming.analytics_schema))
    checks.append(("React reads only ANALYTICS",
                   f"{len(sql_blocks)} datasets, all inside the target analytics schema"
                   if not outside
                   else f"reads outside the granted schema: {', '.join(outside)}",
                   bool(sql_blocks) and not outside))

conn.close()

width = max(len(k) for k, _, _ in checks)
failed = 0
for key, detail, ok in checks:
    print(f"  {'PASS' if ok else 'FAIL'}  {key:<{width}}  {detail}")
    failed += (not ok)

print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
print("\nNot covered here, deliberately: how either app renders. That needs a browser.")
sys.exit(1 if failed else 0)
