#!/usr/bin/env python3
"""Guards for the defects found in the instrumented clean-install run (D1-D19).

Each check corresponds to a defect that a fully green suite did not catch. They are
static and offline on purpose: the point is to fail before a build runs, not to need an
account to notice.

Every guard in here was verified by introducing its defect deliberately and confirming a
non-zero exit. A regression guard that has never failed is a comment.

Run from the repo root.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPE = os.path.join(ROOT, "pipeline")

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


def src(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def tracked() -> set[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout
    return {p for p in out.split("\n") if p}


def ignored(path: str) -> bool:
    """True if git would ignore this path."""
    return subprocess.run(["git", "check-ignore", "-q", path],
                          cwd=ROOT, capture_output=True).returncode == 0


# --------------------------------------------------------------------------
# D19 -- the build rewrote tracked SQL files with customer-specific DDL
# --------------------------------------------------------------------------
def test_generators_cannot_write_into_tracked_paths() -> None:
    """No generator's output directory may be a tracked source directory.

    Before this, both generators defaulted --out-dir to "sql", so every build
    rewrote six tracked files with the customer's database, schema and table
    names -- 552 such lines -- leaving the tree dirty and one `git add -A` away
    from publishing one customer's schema.
    """
    print("D19: generated DDL cannot land on tracked files")
    generators = {
        "generate_physical_layer.py": ["10_physical_layer.sql", "11_date_dimension.sql"],
        "path1_catalog_glossary.py": ["20_tag_taxonomy.sql", "21_object_comments.sql",
                                      "22_tag_assignments.sql", "23_business_glossary.sql"],
    }
    trk = tracked()
    for script, outputs in generators.items():
        text = src("pipeline", script)
        m = re.search(r'"--out-dir",\s*default="([^"]+)"', text)
        check(f"{script} declares an --out-dir default", m is not None)
        if not m:
            continue
        out_dir = m.group(1)
        # The generators run with cwd=pipeline/, so the default is relative to it.
        # Tested via a file path inside the directory: a bare `out/` pattern only
        # resolves for git when the path is known to be a directory, which it is
        # not before the first build creates it.
        check(f"{script} writes to an ignored directory, not sql/",
              out_dir != "sql" and ignored(os.path.join("pipeline", out_dir, "probe.sql")),
              f'--out-dir default is "{out_dir}"')
        # And each artefact it can emit must be un-committable at that location.
        for name in outputs:
            rel = os.path.join("pipeline", out_dir, name)
            check(f"  {name} would be ignored at {out_dir}/", ignored(rel))


def test_generated_names_are_ignored_under_sql_too() -> None:
    """Belt and braces: a stray --out-dir sql must not silently re-arm the leak.

    20_tag_taxonomy.sql is excluded -- it is a real template that build.py's
    p1-tags phase reads, so it is tracked on purpose. The other five are pure
    generated output that nothing reads back.
    """
    print("D19: generated filenames are ignored under pipeline/sql/ as well")
    trk = tracked()
    for name in ["10_physical_layer.sql", "11_date_dimension.sql",
                 "21_object_comments.sql", "22_tag_assignments.sql",
                 "23_business_glossary.sql"]:
        rel = os.path.join("pipeline", "sql", name)
        check(f"{name} is not tracked", rel not in trk)
        check(f"{name} is ignored under sql/", ignored(rel))
    check("20_tag_taxonomy.sql is still tracked as a template",
          os.path.join("pipeline", "sql", "20_tag_taxonomy.sql") in trk)


def test_leak_guard_covers_everything_committable() -> None:
    """The guard must read what `git add -A` would stage, not just tracked files.

    The old guard read `git ls-files` only. An untracked generated file holding a
    customer's database name therefore passed it -- which is precisely the file
    the guard existed to catch.
    """
    print("D19: the customer-identifier guard scans all committable files")
    g = src("tests", "check_identifiers.py")
    check("guard lists untracked-but-not-ignored files",
          "--others" in g and "--exclude-standard" in g)
    check("guard does not rely on plain `git ls-files`",
          not re.search(r'\["git",\s*"ls-files"\]', g))

    # Prove it, rather than trusting the flags: plant a leak and read the exit code.
    # The banned token is assembled from parts so this file does not itself trip the
    # guard it is testing -- which it did, on the first attempt.
    token = "RES" + "MED" + "_TEST_V4"
    probe = os.path.join(ROOT, "pipeline", "sql", "zz_leak_probe.sql")
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("USE DATABASE %s;\n" % token)
        rc = subprocess.run([sys.executable, "tests/check_identifiers.py"],
                            cwd=ROOT, capture_output=True, text=True).returncode
        check("guard fails on an untracked file carrying an identifier", rc != 0,
              f"exit={rc}")
    finally:
        if os.path.exists(probe):
            os.remove(probe)


# --------------------------------------------------------------------------
# D1 -- the React query scanner's contract, and its failure message
# --------------------------------------------------------------------------
def _scan(src_text: str) -> tuple[int, list[str]]:
    """Reimplement verify_deployment's scan, read from its own source.

    Extracted from the file under test rather than copied, so this cannot drift
    into testing a private copy of the regex that the verifier no longer uses.
    """
    v = src("pipeline", "verify_deployment.py")
    assert 'sql:\\s*`(.*?)`' in v, "the scanner's pattern moved; update this test"
    no_comments = re.sub(r"/\*.*?\*/", "", src_text, flags=re.S)
    no_comments = re.sub(r"^\s*//.*$", "", no_comments, flags=re.M)
    blocks = re.findall(r"sql:\s*`(.*?)`", no_comments, re.S)
    return len(blocks), blocks


def test_react_query_scanner_contract() -> None:
    """A bare-key queries.ts must fail, and the message must name the property."""
    print("D1: the React query scanner rejects a bare-key file, and says why")
    bare = """
    export const QUERIES = {
      kpi: `SELECT * FROM ${DB}.${SCHEMA}.RPT_KPI_SUMMARY`,
      period: `SELECT * FROM ${DB}.${SCHEMA}.RPT_SALES_BY_PERIOD`,
    };
    """
    n, _ = _scan(bare)
    check("bare dataset keys yield zero datasets", n == 0, f"found {n}")

    v = src("pipeline", "verify_deployment.py")
    check("the zero-dataset message leads with the count, not a reassurance",
          "no datasets found" in v)
    check("the message names the required property", "`sql:`" in v or "sql: template" in v)
    check("the message points at the template",
          "queries.template.ts" in v)
    # The defect was not the reassuring wording itself -- it is correct when the
    # check passes -- but that it was produced unconditionally, including for zero
    # datasets. So assert the branch, not the absence of the string.
    check("the zero case branches before the message is chosen",
          re.search(r"if not sql_blocks:\s*\n\s*detail\s*=", v) is not None)
    check("the reassuring message is only reachable when datasets were found",
          re.search(r"else:\s*\n\s*detail = f\"\{len\(sql_blocks\)\} datasets, all inside", v)
          is not None)

    # And the shipped template must satisfy its own contract.
    tpl = src("assets", "react_ui", "queries.template.ts")
    n_tpl, blocks = _scan(tpl)
    check("the shipped template parses as datasets", n_tpl >= 1, f"{n_tpl} datasets")
    refs = set(re.findall(r"\b([A-Z][A-Z0-9_]*)\.([A-Z][A-Z0-9_]*)\.[A-Z][A-Z0-9_]*",
                          "\n".join(blocks)))
    check("the template hardcodes no database.schema reference",
          not refs, str(sorted(refs)))


def test_react_scanner_ignores_its_own_prose() -> None:
    """D14: the scanner counted a `sql:` inside its own file-header comment."""
    print("D14: the scanner does not count commented-out prose")
    # Assert against the verifier's own source, not a local reimplementation.
    # Checking a copy of the regex is how this guard first passed while the real
    # scanner had the defect still in it.
    v = src("pipeline", "verify_deployment.py")
    check("the verifier strips block comments before scanning",
          re.search(r're\.sub\(r"/\\\*\.\*\?\\\*/"', v) is not None)
    check("the verifier strips line comments before scanning",
          re.search(r're\.sub\(r"\^\\s\*//', v) is not None)
    check("the scan reads the stripped text, not the raw source",
          re.search(r'findall\(r"sql:\\s\*`\(\.\*\?\)`",\s*no_comments', v) is not None)

    # And the behaviour, through the same transformation the verifier applies.
    withprose = """
    /* Every dataset must carry sql: `a template literal` like this. */
    // another mention of sql: `not a dataset`
    export const QUERIES = { kpi: { sql: `SELECT 1` } };
    """
    n, _ = _scan(withprose)
    check("comments are excluded from the dataset count", n == 1, f"found {n}")


# --------------------------------------------------------------------------
# D7 / D10 -- rules that could not decide, and undocumented aggregation
# --------------------------------------------------------------------------
def test_composition_rules_can_decide() -> None:
    """D7: two rules failed to decide, and the fallback was to ask the user.

    Ordering by measured total cannot discriminate when every RPT_ view wraps the
    same fact -- they tie to the cent, always, for this generator. Selecting from
    `drill_paths` picks hierarchies with no frame and omits territory, the richest
    hierarchy in the reference model, which is absent from drill_paths entirely.
    """
    print("D7: the view-ordering and selection rules can decide")
    r = src("references", "composition-rules.md")
    check("ordering is by measured cardinality, not measured total",
          "measured axis cardinality" in r)
    check("the old tie-prone total ordering is gone",
          "in descending order of their measured total" not in r)
    check("it says why totals tie",
          re.search(r"wraps?\s+the\s+same\s+fact", r) is not None)
    check("selection comes from the frames that exist",
          "not from `drill_paths`" in r)
    check("the drill_paths-only selection rule is gone",
          not re.search(r"One view per hierarchy\*\* in `drill_paths`", r))
    check("a measurement is given, not left to judgement",
          "COUNT(DISTINCT" in r)


def test_component_aggregation_is_documented() -> None:
    """D10: the rules said aggregation was the page's job; three components do it.

    Pre-rolling for RankedBar, Pareto or Waterfall nests an Other (N) inside
    another Other (N). The only way to find out was to read the component source.
    """
    print("D10: per-component aggregation behaviour is documented")
    r = src("references", "composition-rules.md")
    check("the chart-selection table has an aggregation column",
          "Aggregates internally?" in r)
    check("it warns against pre-aggregating for those components",
          "Do not pre-aggregate" in r)

    # Derived from the source, so the doc cannot drift from the libraries.
    tsx = src("assets", "react_ui", "charts.tsx")
    aggregating = set()
    for block in re.split(r"(?=^export function )", tsx, flags=re.M):
        m = re.match(r"export function (\w+)", block)
        if m and ("rollup(" in block or "collapseTail(" in block):
            aggregating.add(m.group(1))
    # `count` is a formatter, not a chart.
    aggregating.discard("count")
    check("charts.tsx has components that aggregate internally", bool(aggregating),
          ", ".join(sorted(aggregating)))
    for comp in sorted(aggregating):
        row = re.search(rf"^\|[^|]*\|\s*`{comp}`[^|]*\|([^|]*)\|", r, re.M)
        check(f"  {comp} is marked as aggregating",
              row is not None and "Yes" in row.group(1),
              (row.group(1).strip() if row else "no table row found"))


# --------------------------------------------------------------------------
# D2 -- the documented dry-run undercounted the plan
# --------------------------------------------------------------------------
def test_documented_dry_run_includes_extract() -> None:
    """Without --extract the dry run reports 16 phases where the plan is 18.

    The two omitted phases include the most expensive phase in the build, so the
    confirmation gate understated exactly what it exists to state.
    """
    print("D2: the documented dry-run command matches the executed plan")
    w = src("references", "wizard.md")
    m = re.search(r"python3 pipeline/build\.py --paths <p>[^\n`]*--dry-run", w)
    check("wizard documents a dry-run command", m is not None)
    if m:
        check("the documented dry-run includes --extract", "--extract" in m.group(0),
              m.group(0))

    # The count difference is real, not just a doc issue: assert it.
    def phases(args: list[str]) -> int:
        out = subprocess.run([sys.executable, "pipeline/build.py", *args, "--dry-run"],
                             capture_output=True, text=True, cwd=ROOT)
        return len(re.findall(r"^\s+\d+\. \[", out.stdout, re.M))

    without = phases(["--paths", "1,4", "--deploy", "none"])
    with_x = phases(["--paths", "1,4", "--deploy", "none", "--extract", "/tmp/x.zip"])
    check("--extract adds phases to the plan", with_x > without,
          f"{without} without, {with_x} with")


# --------------------------------------------------------------------------
# D3 -- the wizard named inventory keys that do not exist
# --------------------------------------------------------------------------
def parsed_inventory_keys() -> set[str]:
    """Top-level keys of a genuinely parsed inventory.

    Built by running the real Cognos parser over the minimal fixture the adapter
    tests already use, then through the real inventory builder -- rather than by
    grepping for string literals, which misses keys the adapters add and would
    have passed while the documented keys were wrong.
    """
    import tempfile
    import textwrap

    sys.path.insert(0, ROOT)
    try:
        test_src = src("modules", "cognos", "test_cognos.py")
        i = test_src.index("_MINIMAL_MODEL = textwrap.dedent(")
        j = test_src.index("\n)", i) + 2
        ns: dict = {"textwrap": textwrap}
        exec(test_src[i:j], ns)

        from modules.cognos.parser import parse_framework_manager_model
        from modules.output.inventory import build_unified_inventory

        d = tempfile.mkdtemp(prefix="b2s-keys-")
        path = os.path.join(d, "model.xml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(ns["_MINIMAL_MODEL"])
        return set(build_unified_inventory(parse_framework_manager_model(path), "cognos"))
    finally:
        if ROOT in sys.path:
            sys.path.remove(ROOT)


def test_wizard_inventory_keys_exist() -> None:
    """Every inventory key the wizard names must be one the parser emits.

    The wizard named `measures`, `drill_paths` and `provenance`; the parse
    inventory has `metrics`, `hierarchies` and `source_analysis`. It was
    describing the app-stage inventory, which is a different file -- and the
    mismatch cost 128 seconds of grepping for keys that were not there.
    """
    print("D3: inventory keys named in the wizard exist in a parsed inventory")
    known = parsed_inventory_keys()
    check("a fixture inventory was built to check against", len(known) > 10,
          f"{len(known)} keys")

    w = src("references", "wizard.md")
    m = re.search(r"The parse-stage inventory's top-level keys are\n(.*?)\n\n", w, re.S)
    check("wizard lists the parse inventory's keys", m is not None)
    if not m:
        return
    named = set(re.findall(r"`([a-z_]+)`", m.group(1)))
    # These three were the defect: present in bim_inventory.json, absent here.
    for ghost in ("measures", "drill_paths", "provenance"):
        check(f"wizard no longer names `{ghost}` as a parse-inventory key",
              ghost not in named)
    missing = sorted(named - known)
    check("every key the wizard names is in a parsed inventory",
          not missing, "absent from a real parse: " + ", ".join(missing) if missing else "")
    check("the wizard's list is complete",
          not (known - named), "undocumented: " + ", ".join(sorted(known - named)))


# --------------------------------------------------------------------------
# D4 -- the wizard asked for a schema the build discarded
# --------------------------------------------------------------------------
def test_schema_answer_is_not_silently_discarded() -> None:
    print("D4: no inert schema question, and the gate names real schemas")
    w = src("references", "wizard.md")
    check("the wizard no longer asks for a database AND schema",
          "Which database and schema should this build into?" not in w)
    check("the gate template names the schemas that receive objects",
          "SALES_ANALYTICS" in w and "COMMON_ANALYTICS" in w and "KNOWLEDGE_BASE" in w)
    check("the gate no longer promises <DATABASE>.<SCHEMA>",
          "Will create in <DATABASE>.<SCHEMA>" not in w)

    # And the flag that could not be honoured now refuses rather than half-applying.
    rc = subprocess.run([sys.executable, "pipeline/build.py", "--paths", "1",
                         "--analytics-schema", "PUBLIC", "--dry-run"],
                        capture_output=True, text=True, cwd=ROOT)
    check("--analytics-schema rejects a value the DDL cannot honour",
          rc.returncode != 0, f"exit={rc.returncode}")
    check("and says why", "hardcodes" in (rc.stdout + rc.stderr))


# --------------------------------------------------------------------------
# D6 -- a phase note that disagreed with the DDL
# --------------------------------------------------------------------------
def test_reporting_view_count_is_derived() -> None:
    print("D6: the reporting-view count comes from the DDL, not a literal")
    b = src("pipeline", "build.py")
    check("build.py counts the views", "_count_views(" in b)
    check("the note is not a hardcoded number",
          not re.search(r'note="\d+ RPT_ views', b))

    ddl = src("pipeline", "sql", "45_reporting_views.sql")
    actual = len(re.findall(r"CREATE\s+OR\s+REPLACE\s+VIEW", ddl, re.I))
    out = subprocess.run([sys.executable, "pipeline/build.py", "--paths", "2", "--dry-run"],
                         capture_output=True, text=True, cwd=ROOT).stdout
    m = re.search(r"(\d+) RPT_ views", out)
    check("the printed count matches the DDL", m is not None and int(m.group(1)) == actual,
          f"DDL has {actual}, plan says {m.group(1) if m else 'nothing'}")


# --------------------------------------------------------------------------
# D8 -- nothing stopped a composed page restyling the locked libraries
# --------------------------------------------------------------------------
def _colour_literals(text: str) -> set[str]:
    hexes = {h.upper() for h in re.findall(r"#[0-9A-Fa-f]{6}\b", text)}
    rgbas = {re.sub(r"\s+", "", r).lower() for r in re.findall(r"rgba\([^)]*\)", text)}
    return hexes | rgbas


def library_palette() -> set[str]:
    palette: set[str] = set()
    for rel in ["assets/streamlit_ui", "assets/react_ui"]:
        base = os.path.join(ROOT, rel)
        for dirpath, _dirs, files in os.walk(base):
            for name in files:
                if name.endswith((".py", ".ts", ".tsx", ".css", ".toml")):
                    with open(os.path.join(dirpath, name), encoding="utf-8") as fh:
                        palette |= _colour_literals(fh.read())
    return palette


def test_composed_app_introduces_no_new_colour() -> None:
    """The headline rule -- never restyle the libraries -- had no enforcement.

    It was violated within minutes of the rule being read: a textColor of
    #1A1A2E, a colour existing nowhere in either library, went into
    .streamlit/config.toml. Caught only by grepping every hex by hand.
    """
    print("D8: composed app source uses only colours the libraries define")
    palette = library_palette()
    check("the libraries define a palette to check against", len(palette) > 5,
          f"{len(palette)} literals")

    scanned = 0
    offenders: list[str] = []
    for folder in ["pipeline/app_streamlit", "pipeline/app_react"]:
        base = os.path.join(ROOT, folder)
        if not os.path.isdir(base):
            continue
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in {"node_modules", ".next", "__pycache__"}]
            for name in files:
                if not name.endswith((".py", ".ts", ".tsx", ".css", ".toml")):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8", errors="replace") as fh:
                    found = _colour_literals(fh.read())
                scanned += 1
                for c in sorted(found - palette):
                    offenders.append("%s: %s" % (os.path.relpath(path, ROOT), c))

    if scanned == 0:
        print("       (no composed app source present -- nothing to scan)")
    check("no composed file introduces a colour the libraries do not define",
          not offenders, "; ".join(offenders[:5]))

    # The guard must have teeth even with no app composed, so prove it on a sample.
    planted = _colour_literals('textColor = "#1A1A2E"') - palette
    check("a colour outside the palette would be caught", bool(planted),
          str(sorted(planted)))


# --------------------------------------------------------------------------
# D9 / S3 -- no typecheck existed over the composed React app
# --------------------------------------------------------------------------
def test_composed_react_typechecks() -> None:
    """`tsc --noEmit` cost 3s and caught a defect nothing else would have.

    AreaGap has three non-optional label props that no reference file documents;
    the page shipped without them and only tsc said so. Skipped rather than
    failed when there is no composed app or no local tsc: the suite must stay
    runnable offline.
    """
    print("D9/S3: the composed React app typechecks when it exists")
    app = os.path.join(ROOT, "pipeline", "app_react")
    if not os.path.isdir(app):
        print("       (no pipeline/app_react/ -- nothing to typecheck)")
        check("typecheck is wired into the suite for when an app exists", True)
        return
    tsc = os.path.join(app, "node_modules", ".bin", "tsc")
    if not os.path.exists(tsc):
        print("       (no local tsc in pipeline/app_react/node_modules -- run npm install)")
        check("typecheck is wired into the suite for when an app exists", True)
        return
    proc = subprocess.run([tsc, "--noEmit"], capture_output=True, text=True, cwd=app)
    check("composed React app has no type errors", proc.returncode == 0,
          (proc.stdout or proc.stderr).strip()[:400])


# --------------------------------------------------------------------------
# D17 -- app source presence must be reported in every deploy mode
# --------------------------------------------------------------------------
def test_app_source_reported_in_every_deploy_mode() -> None:
    print("D17: app-source presence is reported even on --deploy none")
    b = src("pipeline", "build.py")
    check("build.py has a deploy-mode-independent status check",
          "def app_source_status(" in b)
    check("it is called from the run summary", "app_source_status(paths)" in b)
    check("it is not gated on the deploy phases being planned",
          "app_source_status" in b
          and not re.search(r'if "p24-\w+" in keys:[\s\S]{0,200}app_source_status', b))


# --------------------------------------------------------------------------
# D11 / D12 / D13 -- the React scaffold
# --------------------------------------------------------------------------
def test_react_scaffold_ships() -> None:
    print("D11/D12/D13: the React scaffold ships instead of being inferred")
    for rel, why in [
        ("assets/react_ui/queries.template.ts", "the queries.ts shape (D1)"),
        ("assets/react_ui/package.template.json", "pinned dependencies (D12)"),
        ("assets/react_ui/tailwind.config.ts", "the Tailwind dependency (D11)"),
        ("assets/react_ui/postcss.config.mjs", "the Tailwind dependency (D11)"),
        ("assets/react_ui/app/globals.css", "the Tailwind directives (D11)"),
        ("assets/react_ui/ui.tsx", "page chrome parity with ui.py (D13)"),
    ]:
        check(f"ships {rel} -- {why}", os.path.exists(os.path.join(ROOT, rel)))

    ui = src("assets", "react_ui", "ui.tsx")
    for comp in ["KpiCard", "KpiRow", "Section", "Provenance", "Guard"]:
        check(f"react_ui exports {comp}", f"export function {comp}" in ui)

    theme = src("assets", "react_ui", "theme.ts")
    check("theme.ts exports neutral chrome colours", "export const NEUTRAL" in theme)
    for key in ["line", "surface", "muted"]:
        check(f"  NEUTRAL.{key} exists", re.search(rf"\b{key}:", theme) is not None)

    pkg = json.loads(re.sub(r'"_comment":\s*\[[^\]]*\],', "",
                            src("assets", "react_ui", "package.template.json")))
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    check("every dependency is pinned exactly, no ranges",
          all(re.fullmatch(r"\d+\.\d+\.\d+", v) for v in deps.values()),
          str({k: v for k, v in deps.items() if not re.fullmatch(r"\d+\.\d+\.\d+", v)}))
    # The version that npm flagged on this run. Pinning it again would be a regression.
    check("next is not pinned to the version npm flagged as vulnerable",
          deps.get("next") != "15.1.6", f"next={deps.get('next')}")
    check("lib/queries.ts is in REACT_SOURCE so preflight catches it",
          '"lib/queries.ts"' in src("pipeline", "build.py"))


# --------------------------------------------------------------------------
# D16 -- the logger impersonated a different installed skill
# --------------------------------------------------------------------------
def test_logger_namespace_is_this_skill() -> None:
    print("D16: loggers do not carry another skill's name")
    lg = src("modules", "common", "logger.py")
    check("getLogger root is this skill", 'getLogger("bi_to_snowflake")' in lg)
    check("child loggers match", 'f"bi_to_snowflake.{name}"' in lg)
    check("no getLogger call uses the old name",
          not re.search(r'getLogger\(f?"semantic_extraction', lg))


# --------------------------------------------------------------------------
# D15 -- a reported path that did not resolve from where it was printed
# --------------------------------------------------------------------------
def test_timings_path_is_reported_relative_to_the_repo() -> None:
    print("D15: the timings path is printed as it can be found")
    b = src("pipeline", "build.py")
    check("the printed path is computed, not a literal",
          'os.path.relpath(timings_path' in b)
    check("no print statement hardcodes the wrong relative path",
          not re.search(r'print\([^)]*"\\nwrote out/build_timings\.json"', b))


def main() -> int:
    for fn in [
        test_generators_cannot_write_into_tracked_paths,
        test_generated_names_are_ignored_under_sql_too,
        test_leak_guard_covers_everything_committable,
        test_react_query_scanner_contract,
        test_react_scanner_ignores_its_own_prose,
        test_composition_rules_can_decide,
        test_component_aggregation_is_documented,
        test_documented_dry_run_includes_extract,
        test_wizard_inventory_keys_exist,
        test_schema_answer_is_not_silently_discarded,
        test_reporting_view_count_is_derived,
        test_composed_app_introduces_no_new_colour,
        test_composed_react_typechecks,
        test_app_source_reported_in_every_deploy_mode,
        test_react_scaffold_ships,
        test_logger_namespace_is_this_skill,
        test_timings_path_is_reported_relative_to_the_repo,
    ]:
        fn()
    print()
    if failures:
        print("%d check(s) failed:" % len(failures))
        for f in failures:
            print("  - " + f)
        return 1
    print("all measured-defect guards passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
