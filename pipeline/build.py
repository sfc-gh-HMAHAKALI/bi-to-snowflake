#!/usr/bin/env python3
"""One command from a BI model file to any subset of the four consumption paths.

Why this exists: the value claim is that going from a BI export to a governed,
queryable, conversational semantic layer is cheap. Sixteen manual steps in a
README contradict that claim no matter what the README says. So the pipeline is
a single entry point, the paths are independently selectable, and every phase is
timed -- "easy" should be a number, not an adjective.

    python3 build.py --extract <model.xml>        # foundation only
    python3 build.py --paths 1                    # + catalog / glossary / ontology
    python3 build.py --paths 1,3                  # + Ossie semantic view, RLS, agent
    python3 build.py --all                        # all four paths
    python3 build.py --all --dry-run              # print the plan and stop

Every phase is idempotent. The DDL is CREATE OR REPLACE, the knowledge base load
is a MERGE on the source key, and the data loads TRUNCATE before INSERT. Re-running
converges rather than duplicating, which is what makes it safe to demo live.

Phases are ordered by dependency, not by path number. Requesting path 4 pulls in
2 and 3 because an embedded agent needs both a report and an agent to embed.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

import config

HERE = os.path.dirname(os.path.abspath(__file__))
# The repo root: modules/ is vendored here, beside pipeline/.
SKILL_ROOT = os.path.dirname(HERE)

# Set once in main() from the naming flags. The DDL carries {{NAME}}
# placeholders that render_sql substitutes against this, so a build can target
# any account rather than the one the pipeline was first written against.
NAMING = config.DEFAULT


# ---------------------------------------------------------------------------
# Phase definition
# ---------------------------------------------------------------------------
@dataclass
class Phase:
    key: str
    title: str
    # "sql" runs a file through the CLI; "py" runs a generator; "extract" shells
    # out to the extraction skill.
    kind: str
    target: str
    args: list[str] = field(default_factory=list)
    # Which paths need this phase. Empty means foundation -- always run.
    paths: tuple[int, ...] = ()
    note: str = ""
    # Print this phase's output live instead of capturing it. For phases
    # long enough that a silent terminal reads as a hang.
    stream: bool = False
    # Phase keys that must have already run. Declared, not inferred: list order
    # used to be the only record of these, and it was wrong -- the inventory
    # bridge was ordered before the semantic view it reads, and the symptom
    # ("no measures found") arrived at phase 10 looking like an empty knowledge
    # base. Anything relied on here is asserted by check_order() at plan time,
    # so a future reordering fails before the first object is created.
    depends_on: tuple[str, ...] = ()
    # Fully-qualified tables that must hold at least one row once this phase has
    # run. A phase whose INSERT selects from a CTE that matched nothing still
    # returns success: the statement was valid and it inserted zero rows. That is
    # how the territory table came out empty while the phase reported ok, and the
    # symptom surfaced much later as a missing dashboard tab with no failing
    # phase to point at. Declared per phase so the check names the real table
    # rather than guessing from the file.
    expect_rows: tuple[str, ...] = ()


FOUNDATION: list[Phase] = [
    Phase("kb-schema", "Knowledge base schema (14 tables)", "sql", "sql/01_kb_tables.sql",
          note="Source-agnostic: keyed (SOURCE_SYSTEM, SOURCE_MODEL, SOURCE_OBJECT_ID)"),
    Phase("kb-views", "Knowledge base views (the consumer contract)", "sql", "sql/02_kb_views.sql",
          note="Consumers read these, never the tables"),
    Phase("kb-fixes", "Measure-quality detection", "sql", "sql/03_kb_view_fixes.sql",
          note="Catches declared measures that are really identifiers"),
]

# Extraction and load are separate because extraction is the only phase that
# touches the customer's file, and it is the one most likely to be re-run alone
# while iterating on a new BI source.
EXTRACT: list[Phase] = [
    Phase("extract", "Parse the BI model", "extract", "",
          note="Streaming parse; emits the unified inventory"),
    # Between the parse and the load on purpose. The parse takes about two seconds
    # and the load about two minutes, so this is the one point where everything
    # interesting is already known and the user has nothing to do. Give them a
    # readable description of their own model to fill the wait.
    Phase("describe", "Describe the model for the user", "py", "describe_model.py",
          depends_on=("extract",), stream=True,
          note="Writes out/model-overview.md -- read it while the load runs"),
    Phase("kb-load", "Load the knowledge base", "py", "kb_loader.py",
          stream=True,
          note="MERGE on the source key, so a reload updates in place"),
]

PHYSICAL: list[Phase] = [
    Phase("physical", "Physical layer DDL, generated from KB_TERM", "py",
          "generate_physical_layer.py", ["--execute"],
          note="Schema emitted from the knowledge base. NOTE: replaces the tables, "
               "which discards their comments and tags -- path 1 must re-run after this"),
    Phase("dim-data", "Dimension data", "sql", "sql/12_dimension_data.sql",
          expect_rows=("SALES_ANALYTICS.TERRITORY_SITE_SALES_PERSON",),
          note="Territories are drawn from KB_SECURITY_MAPPING, so an empty KB yields zero rows"),
    Phase("fact-data", "Fact data", "sql", "sql/13_fact_data.sql"),
    Phase("backlog", "Open backlog rows", "sql", "sql/14_open_backlog.sql"),
]

def _count_views(sql_file: str) -> int:
    """How many views a DDL file creates. Counted, not remembered.

    The note on this phase said "7 RPT_ views" while the DDL created 8 -- the
    views grew late and the string did not follow. A wrong count in a phase
    description is not cosmetic: it makes a reader stop mid-build and check
    whether a view failed, which is exactly what happened.
    """
    try:
        with open(os.path.join(HERE, sql_file), encoding="utf-8") as fh:
            return len(re.findall(r"CREATE\s+OR\s+REPLACE\s+VIEW", fh.read(), re.I))
    except OSError:
        return 0


PATH_PHASES: list[Phase] = [
    # Path 1 -- no semantic view, no agent.
    Phase("p1-tags", "Tag taxonomy", "sql", "sql/20_tag_taxonomy.sql", paths=(1,)),
    Phase("p1-catalog", "Horizon comments, tags, glossary, ontology handoff", "py",
          "path1_catalog_glossary.py", ["--execute"], paths=(1,), stream=True,
          note="Regenerates the DDL from the KB each run; ~200s of serial COMMENT ON"),

    # Path 3 -- the semantic layer. Needed by path 4.
    Phase("p3-ossie", "Ossie semantic view (deploy + round-trip)", "py",
          "path3_ossie_semantic_view.py", ["--deploy", "--round-trip"], paths=(3, 4),
          note="Created BY Snowflake's Ossie importer; exports back to YAML"),
    Phase("p3-rls", "Row access policy", "sql", "sql/30_row_access_policy.sql", paths=(3, 4),
          note="19,051 Cognos filters collapse to one policy",
          depends_on=("dim-data",)),
    Phase("p3-search", "Cortex Search over the glossary", "sql", "sql/31_glossary_search.sql",
          paths=(3, 4), note="Lets the agent answer definitional questions from governed text"),
    Phase("p3-agent", "Cortex Agent", "sql", "sql/32_agent.sql", paths=(3, 4),
          depends_on=("p3-ossie", "p3-search")),
    # Makes the agent callable from SQL. Needed by both app surfaces rather than by
    # path 3 alone: the Streamlit container runtime has no `_snowflake` module and the
    # React app should not reimplement the event-stream parse in TypeScript.
    Phase("p3-agent-sql", "SQL bridge to the agent", "sql",
          "sql/33_agent_sql_bridge.sql", paths=(3, 4),
          note="one stored procedure both apps and any JDBC client can use",
          depends_on=("p3-agent",)),

    # Reporting views. Required by path 2 because SEMANTIC_VIEW() is its own query
    # syntax that no ordinary SQL client -- including the Node driver the React app
    # uses -- can speak.
    Phase("p2-views", "Reporting views over the semantic view", "sql",
          "sql/45_reporting_views.sql", paths=(2, 4),
          note="%d RPT_ views, so plain-SQL clients read the governed definitions"
               % _count_views("sql/45_reporting_views.sql"),
          depends_on=("p3-ossie",)),

    # Paths 2 and 4 are the same surfaces: a report with the agent embedded in it. A
    # chat panel that cannot see the report's filter state is a separate product, not
    # an embedded assistant, so they deploy together.
    Phase("p24-streamlit", "Deploy the Streamlit report (container runtime)", "app",
          "sql/50_deploy_streamlit.sql", paths=(2, 4),
          note="STREAMLIT object on a compute pool, dependencies pinned via PyPI repo",
          depends_on=("bridge", "p2-views")),
    # Must precede the React deploy. Without these the app deploys and runs, and every
    # query fails with "does not exist or not authorized" -- which reads as a missing
    # object rather than a missing grant, and is the single most confusing failure in
    # this build.
    Phase("p24-grants", "Caller grants for the App Runtime report", "sql",
          "sql/51_caller_grants.sql", paths=(2, 4),
          note="Restricted caller's rights: the app may only use privileges the viewer "
               "already holds, and only where a caller grant allows it"),
    Phase("p24-react", "Deploy the React report (App Runtime)", "react",
          "app_react", paths=(2, 4),
          note="APPLICATION SERVICE via snow app deploy; needs CLI 3.15+",
          depends_on=("bridge", "p2-views", "p24-grants")),
]

FOUNDATION_BRIDGE = Phase(
    "bridge", "Knowledge base to app inventory", "py", "kb_to_inventory.py",
    paths=(2, 4),
    note="Dimensions, measures and drill paths for the in-scope entities -- what the app generators read",
    # Reads INFORMATION_SCHEMA.SEMANTIC_METRICS off the deployed semantic view.
    depends_on=("p3-ossie",))


def check_order(plan: list[Phase]) -> list[str]:
    """Report any phase whose declared dependencies are not already satisfied.

    Called at plan time, before anything is created. A dependency on a phase that
    the requested --paths excluded is not an error: the object may already exist
    from an earlier build, and refusing to run would make incremental builds
    impossible. Ordering *within* the plan is the invariant worth enforcing.
    """
    problems = []
    seen: set[str] = set()
    in_plan = {p.key for p in plan}
    for phase in plan:
        for need in phase.depends_on:
            if need in in_plan and need not in seen:
                problems.append(
                    "phase %r runs before %r, which it depends on"
                    % (phase.key, need))
        seen.add(phase.key)
    return problems


def expand(paths: set[int]) -> set[int]:
    """Resolve path dependencies.

    Path 4 is an agent embedded in a report, so it needs both a report (2) and an
    agent (3). Expanding once here means the plan display and the executor agree
    on what was requested.
    """
    effective = set(paths)
    if 4 in effective:
        effective |= {2, 3}
    return effective


def resolve(paths: set[int], with_extract: bool, with_physical: bool,
            deploy: str = "all") -> list[Phase]:
    """Order phases by dependency and drop anything unrequested."""
    plan = list(FOUNDATION)
    if with_extract:
        plan += EXTRACT
    if with_physical:
        plan += PHYSICAL
    effective = expand(paths)

    phases = [p for p in PATH_PHASES if effective & set(p.paths)]
    deploy_norm = (deploy or "all").lower().strip()
    if deploy_norm in ("none", "local", "skip"):
        phases = [p for p in phases if p.key not in ("p24-streamlit", "p24-grants", "p24-react")]
    elif deploy_norm == "streamlit":
        phases = [p for p in phases if p.key not in ("p24-grants", "p24-react")]
    elif deploy_norm == "react":
        phases = [p for p in phases if p.key != "p24-streamlit"]

    # The bridge sits between path 3 and path 2, not before both. It reads
    # INFORMATION_SCHEMA.SEMANTIC_METRICS for the semantic view, so it must run
    # *after* p3-ossie creates that view and *before* the p2 phases that consume
    # its inventory file. Appending it with the foundation phases instead -- as
    # this did -- made it query a view that did not exist yet and report
    # "no measures found", which reads like an empty knowledge base and is not.
    # Only run it when an app is actually being built: it writes a file nobody
    # asked for on a catalog-only build.
    if effective & set(FOUNDATION_BRIDGE.paths):
        insert_at = next((i for i, p in enumerate(phases)
                          if p.key.startswith("p2")), len(phases))
        phases.insert(insert_at, FOUNDATION_BRIDGE)

    plan += phases
    return plan


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def run_sql_file(path: str, connection: str) -> tuple[bool, str]:
    """Render the DDL's naming placeholders, then run the rendered copy.

    The rendered file goes to a temp path rather than over the source, so a
    failed build never leaves the repo holding one account's names. render_sql
    raises on an unknown placeholder, which is deliberate: an unsubstituted
    {{NAME}} would reach Snowflake as an invalid identifier and fail the phase
    halfway, with the earlier statements already committed.
    """
    src = os.path.join(HERE, path)
    rendered = config.render_sql(io.open(src, encoding="utf-8").read(), NAMING)
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(rendered)
        tmp = fh.name
    try:
        proc = subprocess.run(
            ["snow", "sql", "-f", tmp, "-c", connection],
            capture_output=True, text=True,
        )
    finally:
        os.unlink(tmp)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out


def run_py(script: str, extra: list[str], connection: str,
           stream: bool = False) -> tuple[bool, str]:
    # Forward the naming too. Previously only --connection was passed, so every
    # child script silently fell back to its own defaults and a build could
    # write half its objects into one database and half into another.
    cmd = [sys.executable, os.path.join(HERE, script), *extra,
           "--connection", connection, *config.forward_flags(NAMING)]
    if not stream:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=HERE)
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, out

    # Long phases print their own progress, and capturing it means nobody sees it
    # until the phase is over -- which for the knowledge base load is around two
    # minutes of a silent terminal that reads as a hang. Tee it: show each line as
    # it arrives, and keep a copy so the failure path still reports everything.
    lines: list[str] = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, cwd=HERE)
    assert proc.stdout is not None
    for line in proc.stdout:
        lines.append(line)
        # Drop the logging prefix for the live view; the captured copy keeps it,
        # so a failure report still has timestamps and levels.
        shown = line.rstrip("\n")
        if " | " in shown:
            shown = shown.split(" | ")[-1]
        print("      " + shown, flush=True)
    proc.wait()
    return proc.returncode == 0, "".join(lines)


def run_app_deploy(sql_path: str, connection: str) -> tuple[bool, str]:
    """Create the pool and stage, upload the app source, then create the STREAMLIT object.

    The upload is separate from the SQL file because PUT needs an absolute client-side
    path, which cannot go into a checked-in .sql file without hard-coding somebody's home
    directory.

    Order matters: pool and stage before PUT, and the full source on the stage before
    CREATE STREAMLIT -- otherwise the object is created pointing at an incomplete location
    and fails only when somebody opens it.

    Every module is listed explicitly rather than globbed. A glob would have quietly kept
    deploying the superseded single-file app that still sits in this directory, and the
    regression would look like the fixes had been reverted.
    """
    app_dir = os.path.join(HERE, "app_streamlit")
    root = [
        "app.py",          # entrypoint
        "data.py",         # every query
        "pages_impl.py",   # rendering
        "metrics.py",      # formatting and safe arithmetic
        "pyproject.toml",  # pinned deps -- the fix for the 1.22 outage
    ]
    pkg = ["bim_ui/%s" % m for m in STREAMLIT_LIB_MODULES]
    # Not `config`: that is the name of the module imported at the top of this file,
    # and shadowing it here raised AttributeError thirty lines later, at
    # config.render_sql -- after the compute pool, the stage and every PUT had already
    # succeeded. So the phase failed having done nearly all its work, and the error
    # named a list, with nothing in it pointing at deployment.
    config_files = [".streamlit/config.toml"]

    missing = [f for f in root + pkg + config_files
               if not os.path.exists(os.path.join(app_dir, f))]
    if missing:
        return False, f"app source incomplete, missing: {', '.join(missing)}"

    ok, out = run_sql_file(sql_path, connection)
    if not ok:
        return False, out

    stage = "@%s.%s/%s" % (NAMING.analytics, NAMING.app_stage,
                           NAMING.stage_dir)
    # Clear the stage first so a file removed from the project does not linger in the
    # deployed app.
    subprocess.run(["snow", "sql", "-c", connection, "-q", f"REMOVE {stage}/"],
                   capture_output=True, text=True)

    for rel in root + pkg + config_files:
        src = os.path.join(app_dir, rel)
        subdir = os.path.dirname(rel)
        dest = f"{stage}/{subdir}/" if subdir else f"{stage}/"
        put = subprocess.run(
            ["snow", "sql", "-c", connection, "-q",
             f"PUT file://{src} {dest} OVERWRITE=TRUE AUTO_COMPRESS=FALSE"],
            capture_output=True, text=True,
        )
        out += (put.stdout or "") + (put.stderr or "")
        if put.returncode != 0:
            return False, out + f"\nfailed uploading {rel}"

    for template in (STREAMLIT_DDL, STREAMLIT_LIVE_VERSION):
        # Same placeholder substitution the .sql files get -- these two are
        # inline only because they have to run between the PUT and the ALTER.
        stmt = config.render_sql(template, NAMING)
        proc = subprocess.run(["snow", "sql", "-c", connection, "-q", stmt],
                              capture_output=True, text=True)
        out += (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            return False, out
    return True, out


# FROM, not ROOT_LOCATION. ROOT_LOCATION is legacy and container runtimes reject it
# outright -- "ROOT_LOCATION stages are not supported for vNext applications" -- which
# surfaces only when somebody opens the app, not when it is created.
STREAMLIT_DDL = """
CREATE OR REPLACE STREAMLIT {{DB}}.{{ANALYTICS_SCHEMA}}.{{STREAMLIT}}
  FROM '@{{DB}}.{{ANALYTICS_SCHEMA}}.{{STAGE}}/{{STAGE_DIR}}'
  MAIN_FILE = 'app.py'
  RUNTIME_NAME = 'SYSTEM$ST_CONTAINER_RUNTIME_PY3_11'
  COMPUTE_POOL = {{POOL}}
  QUERY_WAREHOUSE = AI_ML_WH_SALES
  ARTIFACT_REPOSITORIES = (snowflake.snowpark.pypi_shared_repository)
  TITLE = '{{MODEL_LABEL}}'
  COMMENT = 'Cognos-style BI with {{AGENT}} embedded via {{ASK_PROC}}. Container runtime.'
""".strip()

# A newly created Streamlit is not live until a version is published. Without this the
# object exists, SHOW STREAMLITS looks healthy, and opening it does nothing useful.
STREAMLIT_LIVE_VERSION = (
    "ALTER STREAMLIT {{DB}}.{{ANALYTICS_SCHEMA}}.{{STREAMLIT}} ADD LIVE VERSION FROM LAST"
)


def find_snow_cli() -> tuple[str, str]:
    """Locate a `snow` binary that has the App Runtime command surface.

    The App Runtime commands (`snow app setup` / `snow app deploy`) only exist from CLI
    3.15 onwards. Before that, `snow app` is the Native App surface and `snow app setup`
    simply does not exist -- which fails with "No such command", not with a version
    message, so it reads as a broken install rather than an old one.

    Searched rather than hardcoded because a machine can easily have two: a conda-managed
    `snow` first on PATH and a newer pip-managed one elsewhere. Picking the first on PATH
    is how this silently used 3.14 and reported a missing command.
    """
    candidates = ["snow"]
    for base in ("/Library/Frameworks/Python.framework/Versions",
                 os.path.expanduser("~/Library/Python")):
        if os.path.isdir(base):
            for v in sorted(os.listdir(base), reverse=True):
                candidates.append(os.path.join(base, v, "bin", "snow"))

    for cand in candidates:
        try:
            probe = subprocess.run([cand, "app", "setup", "--help"],
                                   capture_output=True, text=True, timeout=60)
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            ver = subprocess.run([cand, "--version"], capture_output=True, text=True)
            return cand, (ver.stdout or "").strip()
    return "", ""


def run_react_deploy(app_dir: str, connection: str) -> tuple[bool, str]:
    """Deploy the Next.js app to Snowflake App Runtime.

    Not idempotent in the way the SQL phases are: `snow app deploy` restarts the whole
    upload-build-promote pipeline each time, and the build runs remotely on a
    Snowflake-managed pool. Budget a few minutes.
    """
    path = os.path.join(HERE, app_dir)
    if not os.path.isdir(path):
        return False, f"react app directory not found: {path}"
    if not os.path.exists(os.path.join(path, "app.yml")):
        return False, (f"{path}/app.yml is missing. Generate it with "
                       f"`snow app setup --app-name {NAMING.app_service}`, then set "
                       f"database/schema to {NAMING.database}/{NAMING.analytics_schema} -- the default resolves "
                       f"to your personal database, which cannot be shared.")

    cli, version = find_snow_cli()
    if not cli:
        return False, ("No `snow` CLI with App Runtime support found. `snow app setup` "
                       "needs CLI 3.15 or later; earlier builds expose only the Native "
                       "App surface. Upgrade with `pip install -U snowflake-cli`.")

    proc = subprocess.run(
        [cli, "app", "deploy", "--verbose", "-c", connection],
        capture_output=True, text=True, cwd=path,
    )
    out = f"using {cli} ({version})\n" + (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out


def run_extract(model: str, inventory: str, skill_dir: str) -> tuple[bool, str]:
    """Parse the BI model and also emit the normalised security mapping.

    Two outputs rather than one because the row access policy is built from the
    normalised form, and normalising 19,051 filters at load time would put
    tool-specific logic inside the loader.
    """
    probe = os.path.join(skill_dir, "modules", "cognos", "parser.py")
    if not os.path.isfile(probe):
        return False, ("--skill-dir has no modules/cognos/parser.py: %s\n"
                       "Extraction runs `python -m modules.cli` from that "
                       "directory, so it must hold the modules/ tree. Omit the "
                       "flag to use this repo (%s)." % (skill_dir, SKILL_ROOT))
    os.makedirs(os.path.dirname(inventory) or ".", exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-m", "modules.cli", "parse", "--type", "cognos", model,
         "--database", NAMING.database,
         "--schema", NAMING.analytics_schema, "-o", inventory],
        capture_output=True, text=True, cwd=skill_dir,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return False, out

    mapping = os.path.join(os.path.dirname(inventory), "security_mapping.json")
    helper = (
        "import json,sys;"
        "from modules.cognos.parser import parse_framework_manager_model as P;"
        "from modules.cognos import rls;"
        f"json.dump(rls.build_mapping_rows(P({model!r})), open({mapping!r},'w'))"
    )
    proc2 = subprocess.run([sys.executable, "-c", helper],
                           capture_output=True, text=True, cwd=skill_dir)
    return proc2.returncode == 0, out + (proc2.stderr or "")


def empty_expected_tables(p: Phase, connection: str) -> list[str]:
    """Which of a phase's ``expect_rows`` tables came out empty.

    A valid INSERT whose SELECT matched nothing is a successful statement, so the
    phase reports ok having produced no data. The territory table was built exactly
    that way -- its rows come from KB_SECURITY_MAPPING through a pattern match -- and
    when that match found nothing, every phase stayed green and the failure surfaced
    much later as a missing dashboard tab with nothing to trace it to. One count per
    declared table is far cheaper than debugging the symptom.

    An unreadable count is reported rather than treated as zero: a permissions or
    connection problem must not be mistaken for an empty table.
    """
    empty: list[str] = []
    for fqn in p.expect_rows:
        proc = subprocess.run(
            ["snow", "sql", "-c", connection, "--format", "json",
             "-q", "SELECT COUNT(*) AS N FROM %s.%s" % (NAMING.database, fqn)],
            capture_output=True, text=True,
        )
        qualified = "%s.%s" % (NAMING.database, fqn)
        if proc.returncode != 0:
            empty.append("%s (could not be read)" % qualified)
            continue
        try:
            n = int(json.loads(proc.stdout[proc.stdout.find("["):])[0]["N"])
        except Exception:
            empty.append("%s (row count unreadable)" % qualified)
            continue
        if n == 0:
            empty.append(qualified)
    return empty


def kb_row_count(connection: str) -> int:
    """How many terms the knowledge base currently holds. -1 if unreadable."""
    proc = subprocess.run(
        ["snow", "sql", "-c", connection, "--format", "json",
         "-q", f"SELECT COUNT(*) AS N FROM {NAMING.kb}.KB_TERM"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return -1
    try:
        out = proc.stdout[proc.stdout.find("["):]
        return int(json.loads(out)[0]["N"])
    except Exception:
        return -1


def _streamlit_lib_modules() -> list[str]:
    """Every module in the locked Streamlit library, read from the library itself.

    Derived rather than listed, because a hand-written list drifted and the drift was
    invisible until someone opened the deployed app. `assets/streamlit_ui/__init__.py`
    does `from . import charts, compat, filters, metrics, ui`, but only __init__,
    compat and filters were being staged -- so the deployed app raised ImportError on
    its first line while SHOW STREAMLITS looked perfectly healthy.

    The comment on the upload list was right that globbing the *app* directory is
    dangerous, since it would sweep up superseded files. This is a different thing: a
    directory whose contents are exactly the contract.
    """
    lib = os.path.join(SKILL_ROOT, "assets", "streamlit_ui")
    mods = sorted(f for f in os.listdir(lib) if f.endswith(".py"))
    if "__init__.py" in mods:                    # first, so the package imports cleanly
        mods.remove("__init__.py")
        mods.insert(0, "__init__.py")
    return mods


STREAMLIT_LIB_MODULES = _streamlit_lib_modules()

STREAMLIT_SOURCE = [
    "app.py", "data.py", "pages_impl.py", "metrics.py", "pyproject.toml",
    *("bim_ui/%s" % m for m in STREAMLIT_LIB_MODULES),
    ".streamlit/config.toml",
]
# lib/queries.ts is listed because verify_deployment.py enforces a specific shape
# inside it (each dataset carrying a `sql:` template literal). Without it in this
# list the preflight passed and the failure arrived after the build, from the
# verifier, as a message that read like a pass. assets/react_ui/queries.template.ts
# is the shape to copy.
REACT_SOURCE = ["app.yml", "package.json", "lib/queries.ts"]


def missing_app_source(plan: list[Phase]) -> list[str]:
    """Which composed app files a planned deploy phase would not find.

    The app source is not in this repo. It is composed per model against the
    locked libraries in assets/, written to pipeline/app_streamlit and
    pipeline/app_react. Checking here turns "phase 15 of 17 failed" into one
    message before anything is created.
    """
    problems: list[str] = []
    keys = {p.key for p in plan}
    if "p24-streamlit" in keys:
        d = os.path.join(HERE, "app_streamlit")
        gone = [f for f in STREAMLIT_SOURCE if not os.path.exists(os.path.join(d, f))]
        if gone:
            problems.append(
                "Streamlit app source is not composed yet. Missing in "
                "pipeline/app_streamlit/: %s. Compose the pages against "
                "assets/streamlit_ui/ per references/composition-rules.md, or "
                "pass --deploy none to build the backend only."
                % ", ".join(gone))
    if "p24-react" in keys:
        d = os.path.join(HERE, "app_react")
        gone = [f for f in REACT_SOURCE if not os.path.exists(os.path.join(d, f))]
        if gone:
            problems.append(
                "React app source is not composed yet. Missing in "
                "pipeline/app_react/: %s. Compose against assets/react_ui/ per "
                "references/composition-rules.md, or pass --deploy streamlit "
                "or --deploy none."
                % ", ".join(gone))
    return problems


def app_source_status(paths: set[int]) -> list[str]:
    """Informational notes on whether app source exists, in EVERY deploy mode.

    ``missing_app_source`` only fires when a deploy phase is in the plan, so
    ``--deploy none`` -- the documented "run both dashboards locally" choice --
    skipped the check entirely. A build then printed "18/18 phases ok" with no
    app source anywhere, and the first sign of trouble was
    ``streamlit run pipeline/app_streamlit/app.py`` failing with a plain "no such
    file". The guidance to check by hand was already in the wizard; this makes the
    build say it.

    Information, not failure: on a backend-only build an absent app is the correct
    outcome, and returning an error would make the honest case look broken.
    """
    effective = expand(paths)
    if not (effective & {2, 4}):
        return []
    notes: list[str] = []
    for label, folder, required in (
        ("Streamlit", "app_streamlit", STREAMLIT_SOURCE),
        ("React", "app_react", REACT_SOURCE),
    ):
        d = os.path.join(HERE, folder)
        gone = [f for f in required if not os.path.exists(os.path.join(d, f))]
        if not gone:
            notes.append("%s app source present in pipeline/%s/" % (label, folder))
        elif len(gone) == len(required):
            notes.append(
                "%s app source NOT composed (pipeline/%s/ is empty or absent). "
                "The backend is complete; the dashboard is not. Compose it against "
                "assets/%s/ per references/composition-rules.md."
                % (label, folder, "streamlit_ui" if label == "Streamlit" else "react_ui"))
        else:
            notes.append(
                "%s app source INCOMPLETE in pipeline/%s/: missing %s"
                % (label, folder, ", ".join(gone)))
    return notes


def deploy_cli_report(plan: list[Phase]) -> str:
    """Which `snow` the deploy phases will use, and what is first on PATH.

    Printed whenever a plan deploys, because these are commonly different binaries
    and the difference is actively misleading: a conda `snow` at 3.14 ahead of a pip
    one at 3.28 makes `snow --version` report a number that looks like it needs
    upgrading when nothing does. find_snow_cli() already searches past it. Saying so
    out loud is cheaper than someone spending ten minutes on an install that changes
    nothing.
    """
    if not any(p.kind in ("app", "react") for p in plan):
        return ""
    cli, version = find_snow_cli()
    on_path = shutil.which("snow") or ""
    path_ver = ""
    if on_path:
        probe = subprocess.run([on_path, "--version"], capture_output=True, text=True)
        path_ver = (probe.stdout or probe.stderr or "").strip()
    lines = ["App Runtime CLI: %s" % (version or "none found")]
    if cli:
        lines.append("  using: %s" % cli)
        if on_path and os.path.realpath(on_path) != os.path.realpath(cli):
            lines.append("  note:  `snow` first on PATH is a different binary -- %s"
                         % (path_ver or on_path))
            lines.append("         that one is not used here, and does not need upgrading.")
    return "\n".join(lines)


def missing_cli_for_deploy(plan: list[Phase]) -> list[str]:
    """Refuse a plan that ends in a deploy no `snow` on this machine can perform.

    The App Runtime commands only exist from CLI 3.15. On an older one `snow app
    setup` fails with "No such command", which reads as a broken install rather than
    an old one -- and the deploy phases run last, so a 3.14 machine builds the entire
    backend, forty minutes of it, and only then discovers a prerequisite that was
    knowable before anything was created.

    This is a *precondition*, not a failure: it is true or false at second zero and
    nothing the build does changes it. So it belongs in the preflight, next to the
    empty-knowledge-base check, rather than at the phase that happens to need it.
    """
    if not any(p.kind in ("app", "react") for p in plan):
        return []
    cli, version = find_snow_cli()
    if cli:
        return []
    have = shutil.which("snow")
    found = ""
    if have:
        probe = subprocess.run([have, "--version"], capture_output=True, text=True)
        found = (probe.stdout or probe.stderr or "").strip()
    return [
        "No `snow` CLI with App Runtime support, and this plan deploys an app.\n"
        "    `snow app setup` needs CLI 3.15 or later; earlier builds expose only\n"
        "    the Native App surface, where that command does not exist.\n"
        "    %s\n"
        "    Fix: pip install -U snowflake-cli\n"
        "    Or re-run with --deploy none to build the backend only."
        % ("Found: %s" % found if found else "No `snow` on PATH at all.")
    ]


def connection_problem(connection: str) -> str:
    """One line naming the real problem if ``connection`` is not usable, else "".

    The default used to be a connection name that existed only on the machine this
    was written on, so a first run elsewhere failed on a name the user had never
    chosen. The failure then arrived from whichever phase happened to run first --
    in one run as "the knowledge base is empty", which is a true statement about a
    database the build had never managed to reach.

    Lists what is actually configured, because the fix is almost always to pick one
    of those rather than to create anything.
    """
    proc = subprocess.run(["snow", "connection", "list", "--format", "json"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        # No CLI, or it cannot read its own config. Let the phases report that;
        # guessing here would replace a real error with a speculative one.
        return ""
    try:
        entries = json.loads(proc.stdout[proc.stdout.find("["):])
        names = [e.get("connection_name") or e.get("name") for e in entries]
        names = [n for n in names if n]
    except Exception:
        return ""
    if not names:
        return ("No Snowflake connections are configured. Add one with "
                "`snow connection add`, then pass its name with --connection.")
    if connection in names:
        return ""
    return ("--connection %r is not configured, so no phase can reach Snowflake. "
            "Configured connections: %s. Pass one of those, or add a new one with "
            "`snow connection add`." % (connection, ", ".join(sorted(names))))


def preflight(paths: set[int], with_extract: bool, with_physical: bool,
              connection: str) -> list[str]:
    """Refuse to run a plan whose outputs would be built from an empty input.

    The specific failure this prevents: running the build without --extract against
    an empty knowledge base. Every path phase reads the KB, so they all "succeed"
    while producing nothing, and the first visible symptom is a semantic view with
    no datasets several minutes later. Checking up front turns that into one clear
    message.
    """
    problems: list[str] = []
    # Check the connection before anything that uses it. Every other preflight
    # check runs SQL, so a bad connection name makes them all fail with the same
    # unhelpful error -- and the build's first real symptom was "the knowledge
    # base is empty", which sent the diagnosis to the knowledge base rather than
    # to the connection. A wrong name here also costs a full phase to discover.
    bad = connection_problem(connection)
    if bad:
        return [bad]
    effective = expand(paths)
    needs_kb = bool(effective & {1, 3, 4})
    if needs_kb and not with_extract:
        n = kb_row_count(connection)
        if n == 0:
            problems.append(
                "The knowledge base is empty and no --extract was given. "
                "Path phases read the knowledge base, so they would produce empty "
                "artifacts. Re-run with --extract <model.xml>."
            )
        elif n < 0:
            problems.append(
                f"Could not read {NAMING.kb}.KB_TERM. Run the foundation "
                "phases first, or check the connection."
            )
    # Rebuilding the physical layer replaces the tables, which discards the
    # comments and tags path 1 wrote onto them. Requesting the rebuild without
    # path 1 therefore silently strips the catalog.
    if with_physical and 1 not in effective:
        problems.append(
            "Rebuilding the physical layer discards column comments and tags, and "
            "path 1 is not in this run, so the Horizon catalog would be left "
            "stripped. Add path 1, or pass --skip-physical."
        )
    return problems


class _Tee:
    """Write every line to the terminal and to a log file at the same time.

    The streaming phases print as they go, which is what a person running this in a
    terminal needs. It does nothing at all for the far more common case: an agent
    running the build through a single tool call, where stdout is captured and handed
    back only when the process exits. A measured run took 7.7 minutes and the user saw
    four sentences of the agent's own prose -- none of the narration the build had
    carefully produced, because it was still sitting in a pipe.

    So the narration also goes to a file, flushed line by line, whose path is printed
    before any work starts. That file is readable while the build runs, which makes it
    the one channel that works whether a human or an agent is driving.
    """

    def __init__(self, stream, path: str) -> None:
        self.stream = stream
        self.file = open(path, "w", encoding="utf-8", buffering=1)

    def write(self, s: str) -> int:
        self.stream.write(s)
        # Unbuffered on our side too: a half-written line in the file at the moment
        # someone opens it is the whole point of writing it out.
        self.file.write(s)
        self.file.flush()
        return len(s)

    def flush(self) -> None:
        self.stream.flush()
        self.file.flush()

    def isatty(self) -> bool:
        return False

    def close(self) -> None:
        try:
            self.file.close()
        except Exception:
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Build the knowledge base and any subset of the four paths",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--extract", metavar="MODEL_XML",
                    help="Parse this BI model and load the knowledge base")
    ap.add_argument("--paths", default="",
                    help="Comma-separated: 1=catalog 2=BI 3=semantic view+agent 4=embedded AI")
    ap.add_argument("--all", action="store_true",
                    help="All four paths")
    ap.add_argument("--deploy",
                    choices=["all", "both", "streamlit", "react", "none", "local", "skip"],
                    default="none",
                    help="Which dashboards to deploy to Snowflake: 'none'/'local' "
                         "(default -- backend on Snowflake, dashboards run locally), "
                         "'streamlit', 'react', or 'all'/'both'. Deploying creates a "
                         "compute pool, a STREAMLIT and an APPLICATION SERVICE, so it "
                         "is always an explicit choice")
    # Accepted because it is the name anyone looks for first. Without it the
    # obvious guess is an argparse error, which reads as "the pipeline cannot do
    # this" rather than "that flag is spelled differently".
    ap.add_argument("--skip-deploy", action="store_true",
                    help="Alias for --deploy none")
    ap.add_argument("--skip-physical", action="store_true",
                    help="Leave the demo data layer alone")
    ap.add_argument("--connection", default="my-demo-account")
    ap.add_argument("--inventory", default="/tmp/b2s/cognos_inventory.json")
    # Defaults to this repo. The modules/ tree is vendored here (see VENDOR.md),
    # so extraction must run against it: pointing at a sibling skill silently
    # ran a different, independently-updated copy of the parser.
    ap.add_argument("--skill-dir", default=SKILL_ROOT,
                    help="Directory holding the modules/ tree to extract with "
                         "(default: this repo)")
    ap.add_argument("--dry-run", action="store_true", help="Print the plan and stop")
    config.add_arguments(ap)
    args = ap.parse_args(argv)

    # Bind the naming before any phase runs, so every phase and every child
    # process sees one set of names.
    global NAMING
    NAMING = config.from_args(args)

    if args.skip_deploy:
        args.deploy = "none"

    paths = ({1, 2, 3, 4} if args.all
             else {int(p) for p in args.paths.split(",") if p.strip().isdigit()})

    plan = resolve(paths, with_extract=bool(args.extract),
                   with_physical=not args.skip_physical,
                   deploy=args.deploy)

    # Open the live log before the first print, so the plan, the preflight and any
    # early failure are all in the file rather than only the part after work began.
    os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
    log_path = os.path.join(HERE, "out", "build-log.txt")
    tee = _Tee(sys.stdout, log_path)
    sys.stdout = tee
    try:
        print("Live log: %s" % log_path)
        print("  Open that file to watch this run. It is written line by line, so it")
        print("  is readable while the build is still going.")
        print()
        return _run(args, plan, paths, log_path)
    finally:
        sys.stdout = tee.stream
        tee.close()


def _run(args, plan: list[Phase], paths: set[int], log_path: str) -> int:
    print("=" * 74)
    print("BUILD PLAN")
    print("=" * 74)
    for i, p in enumerate(plan, 1):
        if p.paths:
            tag = f"path {'/'.join(map(str, p.paths))}"
        else:
            tag = "foundation"
        print(f"  {i:2}. [{tag:>11}] {p.title}")
        if p.note:
            print(f"      {p.note}")
    print()

    cli_report = deploy_cli_report(plan)
    if cli_report:
        print(cli_report)
        print()

    if args.dry_run:
        print("dry run: nothing executed")
        return 0

    issues = preflight(paths, bool(args.extract), not args.skip_physical, args.connection)
    issues += missing_app_source(plan)
    issues += check_order(plan)
    issues += missing_cli_for_deploy(plan)
    if issues:
        print("PREFLIGHT FAILED")
        for i in issues:
            print(f"  - {i}")
        print("\nNothing executed.")
        return 2

    results = []
    t_all = time.time()
    for i, p in enumerate(plan, 1):
        if p.stream:
            print(f"[{i}/{len(plan)}] {p.title} ...", flush=True)
        else:
            print(f"[{i}/{len(plan)}] {p.title} ... ", end="", flush=True)
        t0 = time.time()
        if p.kind == "sql":
            ok, out = run_sql_file(p.target, args.connection)
        elif p.kind == "py":
            ok, out = run_py(p.target, p.args, args.connection,
                             stream=p.stream)
        elif p.kind == "app":
            ok, out = run_app_deploy(p.target, args.connection)
        elif p.kind == "react":
            ok, out = run_react_deploy(p.target, args.connection)
        else:
            ok, out = run_extract(args.extract, args.inventory, args.skill_dir)
            if ok:
                # The loader needs the inventory and the mapping produced above.
                plan_idx = next((j for j, q in enumerate(plan) if q.key == "kb-load"), None)
                if plan_idx is not None:
                    plan[plan_idx].args = [
                        "--inventory", args.inventory,
                        "--source-system", "cognos",
                        "--security-mapping",
                        os.path.join(os.path.dirname(args.inventory), "security_mapping.json"),
                    ]
                # And the digest needs the inventory plus the original filename,
                # since the inventory does not record where it came from and
                # "parsed from inv.json" means nothing to the person reading it.
                desc_idx = next((j for j, q in enumerate(plan) if q.key == "describe"), None)
                if desc_idx is not None:
                    plan[desc_idx].args = [
                        "--inventory", args.inventory,
                        "--source", args.extract,
                        # This copy is written while the load runs, so it is framed
                        # that way. The wizard produces the same document earlier,
                        # before anything exists, and says so instead.
                        "--stage", "build",
                    ]
        dt = time.time() - t0
        # A phase can succeed and still have produced nothing. Check before
        # recording success, so the failure stops the build here rather than
        # surfacing as a missing dashboard tab several phases later.
        if ok and p.expect_rows:
            empty = empty_expected_tables(p, args.connection)
            if empty:
                ok = False
                out = ("%s reported success but left these empty: %s\n\n"
                       "The statements were valid; they matched no rows. A phase "
                       "that populates a table from the knowledge base produces "
                       "nothing when the rows it selects are absent, so check that "
                       "the knowledge base load ran against --kb-database %r and "
                       "that the source model actually declares what this phase "
                       "reads.\n%s"
                       % (p.title, ", ".join(empty), NAMING.kb_database,
                          ("Phase note: %s" % p.note) if p.note else ""))
        results.append({"phase": p.key, "title": p.title, "ok": ok, "seconds": round(dt, 1)})
        if p.stream:
            print(f"      -> {'ok' if ok else 'FAILED'}  {dt:6.1f}s")
        else:
            print(f"{'ok' if ok else 'FAILED'}  {dt:6.1f}s")
        if not ok:
            # Stop on failure: later phases read what earlier ones produce, so
            # continuing would report a cascade of errors with one real cause.
            print("\n--- failure output (last 2500 chars) ---")
            print(out[-2500:])
            break

    total = time.time() - t_all
    print()
    print("=" * 74)
    ok_n = sum(1 for r in results if r["ok"])
    print(f"{ok_n}/{len(results)} phases succeeded in {total/60:.1f} min")
    print("=" * 74)
    for r in results:
        print(f"  {'ok ' if r['ok'] else 'ERR'}  {r['seconds']:6.1f}s  {r['title']}")

    # Every deploy mode, including none. "18/18 phases ok" reads as finished, and
    # on a local-dashboards build it is only the backend that is finished.
    notes = app_source_status(paths)
    if notes:
        print()
        for n in notes:
            print("  app source: %s" % n)

    os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
    timings_path = os.path.join(HERE, "out", "build_timings.json")
    with open(timings_path, "w", encoding="utf-8") as f:
        json.dump({"total_seconds": round(total, 1), "phases": results}, f, indent=2)
    # The path as written, relative to the repo root rather than to pipeline/.
    # "wrote out/build_timings.json" sent a reader looking in the wrong directory.
    print("\nwrote %s" % os.path.relpath(timings_path, SKILL_ROOT))
    print("full transcript: %s" % log_path)

    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
