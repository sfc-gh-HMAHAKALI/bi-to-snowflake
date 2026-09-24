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
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

import config

HERE = os.path.dirname(os.path.abspath(__file__))

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
    Phase("kb-load", "Load the knowledge base", "py", "kb_loader.py",
          note="MERGE on the source key, so a reload updates in place"),
]

PHYSICAL: list[Phase] = [
    Phase("physical", "Physical layer DDL, generated from KB_TERM", "py",
          "generate_physical_layer.py", ["--execute"],
          note="Schema emitted from the knowledge base. NOTE: replaces the tables, "
               "which discards their comments and tags -- path 1 must re-run after this"),
    Phase("dim-data", "Dimension data", "sql", "sql/12_dimension_data.sql",
          note="Territories are drawn from KB_SECURITY_MAPPING, so an empty KB yields zero rows"),
    Phase("fact-data", "Fact data", "sql", "sql/13_fact_data.sql"),
    Phase("backlog", "Open backlog rows", "sql", "sql/14_open_backlog.sql"),
]

PATH_PHASES: list[Phase] = [
    # Path 1 -- no semantic view, no agent.
    Phase("p1-tags", "Tag taxonomy", "sql", "sql/20_tag_taxonomy.sql", paths=(1,)),
    Phase("p1-catalog", "Horizon comments, tags, glossary, ontology handoff", "py",
          "path1_catalog_glossary.py", ["--execute"], paths=(1,),
          note="Regenerates the DDL from the KB each run"),

    # Path 3 -- the semantic layer. Needed by path 4.
    Phase("p3-ossie", "Ossie semantic view (deploy + round-trip)", "py",
          "path3_ossie_semantic_view.py", ["--deploy", "--round-trip"], paths=(3, 4),
          note="Created BY Snowflake's Ossie importer; exports back to YAML"),
    Phase("p3-rls", "Row access policy", "sql", "sql/30_row_access_policy.sql", paths=(3, 4),
          note="19,051 Cognos filters collapse to one policy"),
    Phase("p3-search", "Cortex Search over the glossary", "sql", "sql/31_glossary_search.sql",
          paths=(3, 4), note="Lets the agent answer definitional questions from governed text"),
    Phase("p3-agent", "Cortex Agent", "sql", "sql/32_agent.sql", paths=(3, 4)),
    # Makes the agent callable from SQL. Needed by both app surfaces rather than by
    # path 3 alone: the Streamlit container runtime has no `_snowflake` module and the
    # React app should not reimplement the event-stream parse in TypeScript.
    Phase("p3-agent-sql", "SQL bridge to the agent", "sql",
          "sql/33_agent_sql_bridge.sql", paths=(3, 4),
          note="one stored procedure both apps and any JDBC client can use"),

    # Reporting views. Required by path 2 because SEMANTIC_VIEW() is its own query
    # syntax that no ordinary SQL client -- including the Node driver the React app
    # uses -- can speak.
    Phase("p2-views", "Reporting views over the semantic view", "sql",
          "sql/45_reporting_views.sql", paths=(2, 4),
          note="7 RPT_ views, so plain-SQL clients read the governed definitions"),

    # Paths 2 and 4 are the same surfaces: a report with the agent embedded in it. A
    # chat panel that cannot see the report's filter state is a separate product, not
    # an embedded assistant, so they deploy together.
    Phase("p24-streamlit", "Deploy the Streamlit report (container runtime)", "app",
          "sql/50_deploy_streamlit.sql", paths=(2, 4),
          note="STREAMLIT object on a compute pool, dependencies pinned via PyPI repo"),
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
          note="APPLICATION SERVICE via snow app deploy; needs CLI 3.15+"),
]

FOUNDATION_BRIDGE = Phase(
    "bridge", "Knowledge base to app inventory", "py", "kb_to_inventory.py",
    paths=(2, 4),
    note="217 dimensions, 98 measures, 318 drill paths -- what the React generator reads")


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
    # The bridge runs before the path phases that consume it, and only when an app is
    # actually being built -- it reads the knowledge base and writes a file, so running it
    # for a catalog-only build would be work nobody asked for.
    if effective & set(FOUNDATION_BRIDGE.paths):
        plan.append(FOUNDATION_BRIDGE)

    phases = [p for p in PATH_PHASES if effective & set(p.paths)]
    deploy_norm = (deploy or "all").lower().strip()
    if deploy_norm in ("none", "local", "skip"):
        phases = [p for p in phases if p.key not in ("p24-streamlit", "p24-grants", "p24-react")]
    elif deploy_norm == "streamlit":
        phases = [p for p in phases if p.key not in ("p24-grants", "p24-react")]
    elif deploy_norm == "react":
        phases = [p for p in phases if p.key != "p24-streamlit"]

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


def run_py(script: str, extra: list[str], connection: str) -> tuple[bool, str]:
    # Forward the naming too. Previously only --connection was passed, so every
    # child script silently fell back to its own defaults and a build could
    # write half its objects into one database and half into another.
    cmd = [sys.executable, os.path.join(HERE, script), *extra,
           "--connection", connection, *config.forward_flags(NAMING)]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=HERE)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out


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
    pkg = ["bim_ui/__init__.py", "bim_ui/compat.py", "bim_ui/filters.py"]
    config = [".streamlit/config.toml"]

    missing = [f for f in root + pkg + config
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

    for rel in root + pkg + config:
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


STREAMLIT_SOURCE = [
    "app.py", "data.py", "pages_impl.py", "metrics.py", "pyproject.toml",
    "bim_ui/__init__.py", "bim_ui/compat.py", "bim_ui/filters.py",
    ".streamlit/config.toml",
]
REACT_SOURCE = ["app.yml", "package.json"]


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
                    default="all",
                    help="Which dashboards to deploy to Snowflake: 'all'/'both' (default), "
                         "'streamlit' (deploy Streamlit, React local), 'react', "
                         "or 'none'/'local' (build backend on Snowflake, run dashboards locally)")
    ap.add_argument("--skip-physical", action="store_true",
                    help="Leave the demo data layer alone")
    ap.add_argument("--connection", default="my-demo-account")
    ap.add_argument("--inventory", default="/tmp/b2s/cognos_inventory.json")
    ap.add_argument("--skill-dir",
                    default=os.path.expanduser("~/.snowflake/cortex/skills/semantic-extraction"))
    ap.add_argument("--dry-run", action="store_true", help="Print the plan and stop")
    config.add_arguments(ap)
    args = ap.parse_args(argv)

    # Bind the naming before any phase runs, so every phase and every child
    # process sees one set of names.
    global NAMING
    NAMING = config.from_args(args)

    paths = ({1, 2, 3, 4} if args.all
             else {int(p) for p in args.paths.split(",") if p.strip().isdigit()})

    plan = resolve(paths, with_extract=bool(args.extract),
                   with_physical=not args.skip_physical,
                   deploy=args.deploy)

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

    if args.dry_run:
        print("dry run: nothing executed")
        return 0

    issues = preflight(paths, bool(args.extract), not args.skip_physical, args.connection)
    issues += missing_app_source(plan)
    if issues:
        print("PREFLIGHT FAILED")
        for i in issues:
            print(f"  - {i}")
        print("\nNothing executed.")
        return 2

    results = []
    t_all = time.time()
    for i, p in enumerate(plan, 1):
        print(f"[{i}/{len(plan)}] {p.title} ... ", end="", flush=True)
        t0 = time.time()
        if p.kind == "sql":
            ok, out = run_sql_file(p.target, args.connection)
        elif p.kind == "py":
            ok, out = run_py(p.target, p.args, args.connection)
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
        dt = time.time() - t0
        results.append({"phase": p.key, "title": p.title, "ok": ok, "seconds": round(dt, 1)})
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

    os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
    with open(os.path.join(HERE, "out", "build_timings.json"), "w", encoding="utf-8") as f:
        json.dump({"total_seconds": round(total, 1), "phases": results}, f, indent=2)
    print("\nwrote out/build_timings.json")

    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
