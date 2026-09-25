"""Test pipeline/build.py CLI flags, dry-run phase resolution, and the
composed-app preflight."""

import os
import re
import inspect
import ast
import subprocess
import sys
import io
import tempfile
import contextlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build(args):
    return subprocess.run(["python3", "pipeline/build.py"] + args,
                          capture_output=True, text=True, cwd=ROOT)


def phase_lines(stdout):
    return [l for l in stdout.splitlines()
            if l.strip().startswith(tuple("%d." % i for i in range(1, 30)))]


def test_dry_run_phase_counts():
    """--deploy decides which app surfaces are in the plan, nothing else."""
    cases = [
        (["--paths", "4", "--deploy", "all"], 17, ["Deploy the Streamlit", "Deploy the React"]),
        (["--paths", "4", "--deploy", "both"], 17, ["Deploy the Streamlit", "Deploy the React"]),
        (["--paths", "4", "--deploy", "streamlit"], 15, ["Deploy the Streamlit"]),
        (["--paths", "4", "--deploy", "react"], 16, ["Deploy the React"]),
        (["--paths", "4", "--deploy", "none"], 14, []),
        (["--paths", "4", "--deploy", "local"], 14, []),
        (["--paths", "1"], 9, ["Tag taxonomy", "Horizon comments"]),
        (["--paths", "3"], 12, ["Ossie semantic view", "Cortex Agent"]),
    ]
    for args, want, must_have in cases:
        res = build(args + ["--dry-run"])
        assert res.returncode == 0, "failed: %s\n%s" % (args, res.stderr)
        lines = phase_lines(res.stdout)
        assert len(lines) == want, \
            "expected %d phases for %s, got %d:\n%s" % (want, args, len(lines), res.stdout)
        for frag in must_have:
            assert any(frag in l for l in lines), "missing %r in %s" % (frag, args)
        for frag in ("Deploy the Streamlit", "Deploy the React"):
            if frag not in must_have:
                assert not any(frag in l for l in lines), \
                    "%s should not appear with %s" % (frag, args)
    print("  8 dry-run configurations resolve correctly")


def test_missing_app_source_is_caught_in_preflight():
    """A deploy phase with no composed app must fail before anything is created.

    This arranges its own fixture rather than asserting on ambient absence. The
    earlier version counted complaints from a real `build.py` run in this
    checkout, which is only meaningful while `pipeline/app_streamlit/` and
    `pipeline/app_react/` happen not to exist. Compose an app -- which any path
    2/4 run requires -- and the assertion flipped, the preflight then ran on into
    a live connectivity check, and the suite reported a failure that blamed the
    user's connection. So the suite passed on a pristine clone and failed after a
    successful build, which is exactly backwards.
    """
    import tempfile

    sys.path.insert(0, os.path.join(ROOT, "pipeline"))
    try:
        import build as B
    finally:
        sys.path.pop(0)

    real_here = B.HERE
    expect = {"all": 2, "streamlit": 1, "react": 1, "none": 0}
    try:
        # (i) Nothing composed: every mode planning a deploy must complain.
        empty = tempfile.mkdtemp(prefix="b2s-empty-")
        B.HERE = empty
        for mode, want in expect.items():
            plan = B.resolve({4}, False, False, deploy=mode)
            n = len(B.missing_app_source(plan))
            assert n == want, \
                "empty tree, --deploy %s: expected %d complaints, got %d" % (mode, want, n)

        # (ii) Everything composed: no mode may complain.
        full = tempfile.mkdtemp(prefix="b2s-full-")
        for folder, names in (("app_streamlit", B.STREAMLIT_SOURCE),
                              ("app_react", B.REACT_SOURCE)):
            for name in names:
                p = os.path.join(full, folder, name)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                open(p, "w").close()
        B.HERE = full
        for mode in expect:
            plan = B.resolve({4}, False, False, deploy=mode)
            problems = B.missing_app_source(plan)
            assert not problems, \
                "fully composed, --deploy %s still complained: %s" % (mode, problems)
    finally:
        B.HERE = real_here

    # End to end, but only the assertion that holds in either state: a build that
    # plans no deploy must never complain about app source.
    res = build(["--paths", "4", "--deploy", "none", "--skip-physical",
                 "--connection", "__none__"])
    assert res.stdout.count("app source is not composed") == 0, \
        "--deploy none complained about app source:\n%s" % res.stdout
    print("  composed-app preflight fires per surface, from a fixture, not ambient state")




def test_skip_deploy_alias_matches_deploy_none():
    """--skip-deploy is the name people guess first; it must not be an error."""
    a = build(["--paths", "4", "--skip-deploy", "--dry-run"])
    b = build(["--paths", "4", "--deploy", "none", "--dry-run"])
    assert a.returncode == 0, "--skip-deploy rejected:\n%s" % a.stderr
    assert phase_lines(a.stdout) == phase_lines(b.stdout), \
        "--skip-deploy and --deploy none planned differently"
    print("  --skip-deploy is accepted and matches --deploy none")


def test_local_dashboards_keep_what_they_read():
    """--deploy none must keep the bridge and the RPT_ views; --paths 3 drops both.

    That is why --paths 3 is the wrong way to avoid the deploys: the apps would
    come up with nothing to read.
    """
    none4 = build(["--paths", "4", "--deploy", "none", "--dry-run"]).stdout
    assert "app inventory" in none4, "--deploy none dropped the app inventory bridge"
    assert "Reporting views" in none4, "--deploy none dropped the RPT_ views"
    for frag in ("Deploy the Streamlit", "Deploy the React"):
        assert frag not in none4, "%s survived --deploy none" % frag

    p3 = build(["--paths", "3", "--dry-run"]).stdout
    assert "app inventory" not in p3 and "Reporting views" not in p3, \
        "--paths 3 now includes app phases; the documented trap is stale"
    print("  --deploy none keeps the bridge and RPT_ views that --paths 3 drops")




def test_skill_dir_defaults_to_this_repo():
    """Extraction must run the vendored modules/, not a sibling skill's copy.

    The default pointed at ~/.snowflake/cortex/skills/semantic-extraction, so
    `python -m modules.cli` loaded that skill's parser. It worked wherever the
    sibling happened to be installed and current, and diverged everywhere else --
    including silently missing archive support the vendored parser has.
    """
    sys.path.insert(0, os.path.join(ROOT, "pipeline"))
    import build as B

    assert B.SKILL_ROOT == ROOT, \
        "SKILL_ROOT is %s, expected the repo root %s" % (B.SKILL_ROOT, ROOT)
    assert os.path.isfile(os.path.join(B.SKILL_ROOT, "modules", "cognos", "parser.py")), \
        "the resolved skill dir has no vendored parser"

    src = open(os.path.join(ROOT, "pipeline", "build.py")).read()
    assert "skills/semantic-extraction" not in src, \
        "build.py still hardcodes a sibling skill path"

    # The guard must reject a directory with no modules/ tree rather than
    # failing later inside a subprocess.
    ok, msg = B.run_extract("ignored.xml", "/tmp/b2s_probe/inv.json", "/tmp")
    assert ok is False and "modules/cognos/parser.py" in msg, \
        "run_extract did not reject a skill dir without modules/: %r" % msg
    print("  --skill-dir defaults to this repo and rejects a dir with no modules/")


def test_the_deploy_cli_is_a_preflight_check_not_a_phase_failure() -> None:
    """A missing App Runtime CLI must stop the build before it creates anything.

    The App Runtime commands only exist from CLI 3.15, and the deploy phases run
    last. So on a 3.14 machine the old behaviour built the entire backend -- forty
    minutes of it -- and only then failed on a prerequisite that was true or false
    at second zero and that nothing the build does can change. That makes it a
    precondition, and preconditions belong in the preflight.
    """
    sys.path.insert(0, os.path.join(ROOT, "pipeline"))
    try:
        import build as B
    finally:
        sys.path.pop(0)

    real = B.find_snow_cli
    try:
        B.find_snow_cli = lambda: ("", "")          # simulate a 3.14-only machine
        expect_complaint = {"all": True, "streamlit": True, "react": True, "none": False}
        for mode, should in expect_complaint.items():
            plan = B.resolve({4}, False, False, deploy=mode)
            got = bool(B.missing_cli_for_deploy(plan))
            assert got is should, (
                "--deploy %s: expected complaint=%s, got %s" % (mode, should, got))
        # The message has to name the fix, or it is just a refusal.
        msg = B.missing_cli_for_deploy(B.resolve({4}, False, False, deploy="all"))[0]
        for needed in ("3.15", "pip install -U snowflake-cli", "--deploy none"):
            assert needed in msg, "the message never mentions %r" % needed

        B.find_snow_cli = lambda: ("/somewhere/snow", "Snowflake CLI version: 3.28.0")
        assert not B.missing_cli_for_deploy(B.resolve({4}, False, False, deploy="all")), \
            "a machine with a 3.15+ CLI was still refused"
    finally:
        B.find_snow_cli = real

    # And it must actually be wired into the preflight, not merely defined.
    src = open(os.path.join(ROOT, "pipeline", "build.py"), encoding="utf-8").read()
    assert "issues += missing_cli_for_deploy(plan)" in src, \
        "missing_cli_for_deploy is never called from the preflight"
    print("  a deploy-capable CLI is a preflight precondition, with the fix named")


def test_the_build_says_which_snow_it_will_use() -> None:
    """An old `snow --version` is not on its own a reason to upgrade.

    A machine commonly has two: a conda one first on PATH and a newer pip one
    elsewhere. find_snow_cli() searches past the first, so `snow --version` can
    report 3.14 while the build happily uses 3.28. Reporting only the version would
    leave that trap intact, so the report names the binary too, and says explicitly
    when the one on PATH is not the one being used.
    """
    sys.path.insert(0, os.path.join(ROOT, "pipeline"))
    try:
        import build as B
    finally:
        sys.path.pop(0)

    real = B.find_snow_cli
    try:
        B.find_snow_cli = lambda: ("/opt/new/snow", "Snowflake CLI version: 3.28.0")
        report = B.deploy_cli_report(B.resolve({4}, False, False, deploy="all"))
        assert "3.28.0" in report, "the version is not reported"
        assert "/opt/new/snow" in report, \
            "the report names no binary, so a PATH mismatch stays invisible"
        # Silent when nothing is being deployed.
        assert B.deploy_cli_report(B.resolve({4}, False, False, deploy="none")) == "", \
            "a backend-only build should not talk about the deploy CLI"
    finally:
        B.find_snow_cli = real

    src = open(os.path.join(ROOT, "pipeline", "build.py"), encoding="utf-8").read()
    assert "cli_report = deploy_cli_report(plan)" in src, \
        "deploy_cli_report is never printed"
    print("  the build names the snow binary it will use, not just a version")


def test_the_model_digest_runs_between_the_parse_and_the_load() -> None:
    """The description must land in the gap it exists to fill.

    The parse takes about two seconds and the knowledge base load about two minutes.
    So a digest of the model is only useful if it is written after the parse -- when
    the facts exist -- and before the load, when the user is about to have nothing to
    do. Written after the load it is a report nobody was waiting for.

    It must also stream, because the message that matters is the file path; captured,
    the user is handed a document and never told where it is.
    """
    sys.path.insert(0, os.path.join(ROOT, "pipeline"))
    try:
        import build as B
    finally:
        sys.path.pop(0)

    plan = B.resolve({1, 2, 3, 4}, True, True, deploy="none")
    keys = [p.key for p in plan]
    assert "describe" in keys, "the model digest phase is not in a full plan"
    assert keys.index("extract") < keys.index("describe") < keys.index("kb-load"), \
        "digest must sit between the parse and the load, got: %s" % keys[:6]

    desc = next(p for p in plan if p.key == "describe")
    assert "extract" in desc.depends_on, \
        "the digest reads the inventory but does not declare the dependency"
    assert desc.stream, \
        "the digest does not stream, so the path it prints is captured and lost"

    # Nothing to describe without a parse.
    assert "describe" not in [p.key for p in B.resolve({1, 2, 3, 4}, False, True, deploy="none")], \
        "the digest runs even when no model was parsed"

    # And it must actually be handed the inventory and the original filename. Read a
    # window after the lookup rather than slicing to the first "]", which lands
    # inside plan[desc_idx] and made an earlier version of this assertion misfire.
    src = open(os.path.join(ROOT, "pipeline", "build.py"), encoding="utf-8").read()
    block = src[src.index('desc_idx = next('):][:600]
    for flag in ("--inventory", "--source"):
        assert flag in block, "the digest phase is never passed %s" % flag
    print("  the model digest runs between parse and load, streams, and gets its inputs")


def test_the_digest_reports_only_what_the_parse_found() -> None:
    """Every number in the digest must come from the inventory.

    This document is read before anyone looks at the data, and it is used to decide
    what to migrate. So an inflated count, a rounded-up total or a section invented
    to look thorough would do real harm. Rendered from a deliberately small inventory
    and checked for claims it has no basis for.
    """
    sys.path.insert(0, os.path.join(ROOT, "pipeline"))
    try:
        import describe_model as D
    finally:
        sys.path.pop(0)

    tiny = {
        "source_type": "cognos",
        "tables": [{"name": "T1"}, {"name": "T2"}],
        "dimensions": [{"name": "C%d" % i} for i in range(7)],
        "metrics": [], "facts": [], "relationships": [], "hierarchies": [],
        "grain_declarations": [], "filters": [], "security_rules": [],
        "dashboards": [], "worksheets": [], "errors": [],
        "complexity_summary": {"simple": 5, "needs_translation": 2, "manual_required": 1},
        "flagged": [{"name": "X", "reason": "because", "expression": "1+1"}],
        "source_analysis": {"model_name": "Tiny", "clones": {}, "data_sources": {},
                            "security_summary": {}},
    }
    md = D.render(tiny, "/somewhere/Tiny.zip")

    assert "Tiny" in md and "Tiny.zip" in md, "the model and source file are not named"
    assert "| Tables and views | 2 |" in md, "table count wrong or missing"
    assert "| Fields | 7 |" in md, "field count wrong or missing"
    # Empty collections must be omitted, not rendered as zero.
    for absent in ("| Measures | 0 |", "| Joins | 0 |", "| Security filters | 0 |"):
        assert absent not in md, "an empty section was rendered as a zero: %s" % absent
    # Sections with no data must not appear at all.
    for heading in ("## Where the data already lives", "## What repeats",
                    "## How access is controlled"):
        assert heading not in md, "%s was rendered with no data behind it" % heading
    # The complexity line must add up to what was given, not to a nicer number.
    assert "Of 8 expressions" in md, "expression total is not the sum of the parse"
    assert "**5 translate directly**" in md and "2 need translation" in md
    # A Framework Manager model has no reports; say so rather than implying failure.
    assert "## What is not in this file" in md, \
        "a model with no dashboards should explain why, not stay silent"

    # Now the opposite: with data present, the sections must appear.
    rich = dict(tiny)
    rich["source_analysis"] = {
        "model_name": "Rich",
        "clones": {"fiscal_year_object_families": {"FACT_BIG": ["FY22", "FY23", "FY24"],
                                                   "AM": ["FY23"]},
                   "fiscal_year_cloned_objects": 4, "calculation_total": 10,
                   "calculation_clone_ratio": 2.0, "package_total": 5,
                   "package_role_based": 4},
        "data_sources": {"sources": [{"name": "S", "platform": "snowflake",
                                      "query_subjects_using": 3}]},
        "security_summary": {"filter_total": 100, "distinct_principals": 9,
                             "distinct_shapes": 1,
                             "shapes": [{"columns": ["ROLE"], "filter_count": 100}]},
    }
    md2 = D.render(rich, "/x/Rich.zip")
    for heading in ("## Where the data already lives", "## What repeats",
                    "## How access is controlled"):
        assert heading in md2, "%s missing when the data is present" % heading
    # The clone example must be the illustrative family, not the alphabetically first.
    assert "`FACT_BIG`" in md2, \
        "the clone example picked the alphabetically first family, which illustrates nothing"
    assert "100" in md2 and "1 shapes" not in md2.replace("**1 shapes**", ""), \
        "shape count rendering is wrong"
    print("  the digest reports only parsed facts, and omits sections it has no data for")


def test_run_app_deploy_does_not_shadow_the_config_module() -> None:
    """A local name must not shadow an imported module it later calls.

    `config = [".streamlit/config.toml"]` shadowed the `config` module imported at the
    top of build.py, and the shadow bit about thirty lines later at
    `config.render_sql(...)` with `AttributeError: 'list' object has no attribute
    'render_sql'`. The damage is in the timing: the compute pool, the stage and all
    nine PUTs had already succeeded, so the phase failed having done nearly all its
    work, and the error named a list with nothing in it pointing at deployment.

    Checked with the AST rather than a grep, so any local rebinding of an imported
    module name in this file fails, not just this one spelling of it.
    """
    path = os.path.join(ROOT, "pipeline", "build.py")
    tree = ast.parse(open(path, encoding="utf-8").read())

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.add((a.asname or a.name).split(".")[0])

    offenders = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        # Does this function also *call* an attribute of the name it rebinds?
        called = {n.value.id for n in ast.walk(fn)
                  if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id in imported and t.id in called:
                        offenders.append("%s() rebinds imported module %r and still "
                                         "calls %s.<attr>" % (fn.name, t.id, t.id))
    assert not offenders, "module shadowing in build.py:\n  " + "\n  ".join(offenders)
    print("  no function shadows an imported module it goes on to call")


def test_the_streamlit_deploy_stages_the_whole_locked_library() -> None:
    """Every module the library's __init__ imports must reach the stage.

    `assets/streamlit_ui/__init__.py` does `from . import charts, compat, filters,
    metrics, ui`, but only __init__, compat and filters were being staged -- and
    STREAMLIT_SOURCE, which drives the preflight, listed the same three. So the
    preflight passed, the STREAMLIT object was created, SHOW STREAMLITS looked
    healthy, and the app raised ImportError on its first line when somebody opened it.

    Both lists must now be derived from the library directory, so adding a module
    cannot leave them behind.
    """
    sys.path.insert(0, os.path.join(ROOT, "pipeline"))
    try:
        import build as B
    finally:
        sys.path.pop(0)

    lib = os.path.join(ROOT, "assets", "streamlit_ui")
    on_disk = sorted(f for f in os.listdir(lib) if f.endswith(".py"))
    assert sorted(B.STREAMLIT_LIB_MODULES) == on_disk, (
        "the library module list does not match the library: %s vs %s"
        % (sorted(B.STREAMLIT_LIB_MODULES), on_disk))
    assert B.STREAMLIT_LIB_MODULES[0] == "__init__.py", \
        "__init__.py must be staged first so the package imports cleanly"

    # Whatever __init__ imports must be in the list.
    init = open(os.path.join(lib, "__init__.py"), encoding="utf-8").read()
    m = re.search(r"from \.\s+import\s+([^\n#]+)", init)
    assert m, "could not find the `from . import ...` line in the library __init__"
    needed = ["%s.py" % n.strip() for n in m.group(1).split(",") if n.strip()]
    for mod in needed:
        assert mod in B.STREAMLIT_LIB_MODULES, \
            "__init__ imports %s but it is not staged -- the deployed app will ImportError" % mod
        assert "bim_ui/%s" % mod in B.STREAMLIT_SOURCE, \
            "%s is staged but the preflight does not require it" % mod

    # And the deploy function must build its upload list from the derived one.
    src = inspect.getsource(B.run_app_deploy)
    assert "STREAMLIT_LIB_MODULES" in src, \
        "run_app_deploy hand-writes its bim_ui list again; it will drift again"
    print("  the Streamlit deploy stages all %d library modules, derived not listed"
          % len(B.STREAMLIT_LIB_MODULES))


def test_deploying_is_never_the_default() -> None:
    """--deploy must default to none, and the docs must agree with the code.

    It defaulted to `all` while wizard.md marked a different row "Recommended" -- the
    default and the recommendation disagreed, and the default was the option that
    creates a compute pool, a STREAMLIT and an APPLICATION SERVICE, costs money and is
    hardest to reverse. A real run deployed all of that unasked and had to tear it down.
    """
    sys.path.insert(0, os.path.join(ROOT, "pipeline"))
    try:
        import build as B
    finally:
        sys.path.pop(0)

    src = open(os.path.join(ROOT, "pipeline", "build.py"), encoding="utf-8").read()
    i = src.index('ap.add_argument("--deploy"')
    decl = src[i:src.index("ap.add_argument", i + 10)]
    assert 'default="none"' in decl, \
        "--deploy does not default to none; deploying must be an explicit choice"

    # A default plan must create no app surfaces at all.
    plan = B.resolve({1, 2, 3, 4}, True, True, deploy="none")
    assert not [p for p in plan if p.kind in ("app", "react")], \
        "the default plan still contains deploy phases"

    doc = open(os.path.join(ROOT, "references", "wizard.md"), encoding="utf-8").read()
    assert "defaults to `none`" in doc, "wizard.md does not document the none default"
    assert "defaults to `all`" not in doc, \
        "wizard.md still claims --deploy defaults to all"
    print("  --deploy defaults to none, in the code and in the wizard")


def test_the_wizard_forbids_treating_parameters_as_consent() -> None:
    """Supplying a database name is not consent to deploy.

    The rule that was missing. A request naming a database, prefix and connection was
    read as having pre-answered the wizard, including the hosting question it never
    mentioned. wizard.md said "Never skip to the build" but only described what to ask,
    never when the wizard may be bypassed -- which is never.
    """
    doc = open(os.path.join(ROOT, "references", "wizard.md"), encoding="utf-8").read()
    assert "Naming parameters are not answers to the wizard" in doc, \
        "wizard.md has no rule about parameters not implying consent"
    for idea in ("same terms the wizard uses", "hard to reverse"):
        assert idea in doc, "the rule does not explain %r" % idea
    print("  the wizard states that supplied parameters do not pre-answer it")


def test_the_three_step_run_shape_is_documented() -> None:
    """A first run cannot deploy in one command, and the docs must say so.

    The preflight needs composed app source; composing it needs
    pipeline/out/bim_inventory.json, which the bridge phase writes late in the backend
    build. So `--deploy all` on a fresh model can never satisfy its own preflight, and
    the real sequence is backend, compose, then deploy. This had to be worked out by
    reading preflight() -- nothing documented it.
    """
    sys.path.insert(0, os.path.join(ROOT, "pipeline"))
    try:
        import build as B
    finally:
        sys.path.pop(0)

    # The dependency that makes it inherent: the bridge phase runs, and the deploy
    # phases come after it, yet the preflight gates on app source before anything runs.
    plan = B.resolve({1, 2, 3, 4}, True, True, deploy="all")
    keys = [p.key for p in plan]
    assert "bridge" in keys, "the inventory-producing phase is missing from a full plan"
    deploys = [i for i, k in enumerate(keys) if plan[i].kind in ("app", "react")]
    assert deploys and min(deploys) > keys.index("bridge"), \
        "deploy phases no longer come after the bridge; re-check this reasoning"

    doc = open(os.path.join(ROOT, "references", "wizard.md"), encoding="utf-8").read()
    assert "cannot satisfy its own preflight" in doc, \
        "wizard.md does not explain that a first run cannot deploy in one command"
    assert "--skip-physical" in doc, \
        "the three-step shape omits --skip-physical, which step 3 needs"
    print("  the three-step run shape is documented, with why step 3 is separate")


def test_the_streamlit_layout_is_pinned_like_the_react_one() -> None:
    """bim_ui/ is a verbatim copy, and root metrics.py is a different file.

    The React layout was pinned precisely and the Streamlit one was not, even though
    both a root metrics.py and a bim_ui/metrics.py exist and must differ. That had to
    be inferred, and two runs will infer it differently.
    """
    doc = open(os.path.join(ROOT, "references", "wizard.md"), encoding="utf-8").read()
    assert "verbatim" in doc and "bim_ui/" in doc, \
        "wizard.md does not say bim_ui/ is a verbatim copy of the library"
    assert "bim_ui/metrics.py" in doc and "A different file" in doc, \
        "wizard.md does not distinguish root metrics.py from bim_ui/metrics.py"
    assert "all six modules must be present" in doc.replace("**", ""), \
        "wizard.md does not state that a partial bim_ui/ copy breaks the app"
    print("  the Streamlit layout is pinned, including the two metrics.py files")


def test_the_deploy_question_and_its_mapping_use_the_same_labels() -> None:
    """A valid answer must not be able to look invalid.

    The wizard offered "Both local / Nothing deployed" while the mapping table named the
    same choice differently, and the deploy answer had no row in the path table at all.
    So selecting local produced "that's not one of the paths" followed by the agent doing
    it anyway -- the user was told their answer was invalid AND it was not honoured as
    stated. Two names for one choice is the mechanism; a missing row is the trigger.

    Rather than assert exact strings, this reads the labels out of the wizard's Round 2
    deploy block and requires every one of them to appear in the mapping table. They
    cannot drift apart without failing.
    """
    doc = open(os.path.join(ROOT, "references", "wizard.md"), encoding="utf-8").read()

    block = doc[doc.index('header: "Deploy"'):]
    block = block[:block.index("```")]
    labels = re.findall(r'- label: "([^"]+)"', block)
    assert len(labels) == 3, "expected three deploy options, found %d: %s" % (len(labels), labels)

    mapping = doc[doc.index("### Deploy mapping"):]
    mapping = mapping[:mapping.index("\n**") if "\n**" in mapping else len(mapping)]
    for label in labels:
        assert label in mapping, (
            "the wizard offers %r but the deploy mapping table never names it, so the "
            "answer cannot be resolved" % label)

    # The default must be the local option, matching --deploy's argparse default.
    assert 'defaultAnswer: "Run both locally"' in doc, \
        "the deploy question does not pre-select the local default"
    local = [l for l in labels if "local" in l.lower()]
    assert local, "no deploy option mentions running locally"
    assert "**(default)**" in mapping, "the mapping table marks no default"
    default_row = next(l for l in mapping.splitlines() if "**(default)**" in l)
    assert "`--deploy none`" in default_row, \
        "the row marked default is not --deploy none: %s" % default_row[:90]
    assert "Recommended" not in block, \
        "a deploy option is still marked Recommended, which competes with the default"
    print("  the deploy options, the mapping table and the default all agree")


def test_the_deploy_answer_is_not_treated_as_a_path() -> None:
    """The path table must say that the deploy answer belongs to a different flag.

    An agent reading the deploy answer looked it up in the outputs-to-paths table, found
    nothing, and concluded the user had given an invalid answer. A table that lists only
    some answers needs to say so, or absence reads as rejection.
    """
    doc = open(os.path.join(ROOT, "references", "wizard.md"), encoding="utf-8").read()
    table = doc[doc.index("## Output to path mapping"):]
    table = table[:table.index("### Deploy mapping")]
    assert "not a path" in table, \
        "the path table does not say the deploy answer is not a path"
    assert "--deploy" in table, "the path table never points at the --deploy flag"

    # And the natural phrasings must be recognised somewhere in the wizard.
    for phrase in ("run locally", "localhost", "deploy locally"):
        assert phrase in doc.lower(), \
            "the wizard does not recognise %r as a way of asking for local" % phrase
    print("  the deploy answer is documented as a flag, not a path, with its synonyms")


def test_the_parse_creates_its_output_directory() -> None:
    """The first documented command of a guided run must work on a fresh machine.

    `modules.cli parse ... -o /tmp/b2s/inventory.json` failed with FileNotFoundError
    whenever /tmp/b2s did not already exist -- which is every machine on its first run.
    Worse, it surfaces as a parse error report, so it reads as "your model could not be
    parsed" when the parse succeeded and only the write failed. build.py never hit it
    because run_extract makes the directory first; only the documented command did.
    """
    import tempfile
    sys.path.insert(0, ROOT)
    try:
        from modules.output.inventory import save_inventory
    finally:
        sys.path.pop(0)

    base = tempfile.mkdtemp(prefix="b2s-nodir-")
    nested = os.path.join(base, "does", "not", "exist", "inventory.json")
    save_inventory({"source_type": "cognos"}, nested)
    assert os.path.isfile(nested), "save_inventory did not create its parent directory"
    print("  the parse creates its output directory instead of reporting a parse failure")


def test_the_wizard_hands_over_the_digest_before_asking_what_to_build() -> None:
    """The model description must be offered at the profile step, not mid-build.

    It was implemented only as build phase 5, behind three Snowflake DDL phases, and
    neither SKILL.md nor wizard.md mentioned it at all -- so an agent following the
    documented flow never produced it and the user never saw one. The point of the
    document is to be the first thing handed back after pointing at a model, and to be
    readable before committing to a build.
    """
    wiz = open(os.path.join(ROOT, "references", "wizard.md"), encoding="utf-8").read()
    skill = open(os.path.join(ROOT, "SKILL.md"), encoding="utf-8").read()

    for name, doc in (("wizard.md", wiz), ("SKILL.md", skill)):
        assert "describe_model.py" in doc, \
            "%s never tells the agent to produce the model description" % name

    # In the wizard it must come before round 2, not after.
    profile_at = wiz.index("describe_model.py")
    round2_at = wiz.index("## Round 2")
    assert profile_at < round2_at, \
        "the digest is documented after round 2; it exists to inform round 2"

    # And the agent must be told to hand it over, not just generate it.
    assert "Give the user that Markdown file now" in wiz, \
        "the wizard generates the digest but never says to show it to the user"

    # The build phase stays, as the fallback for a direct CLI run.
    sys.path.insert(0, os.path.join(ROOT, "pipeline"))
    try:
        import build as B
    finally:
        sys.path.pop(0)
    assert "describe" in [p.key for p in B.resolve({1, 2, 3, 4}, True, True, deploy="none")], \
        "the build no longer regenerates the digest for non-wizard runs"
    print("  the digest is handed over at the profile step, before round 2")


def test_the_digest_does_not_claim_a_build_is_running_when_none_is() -> None:
    """One document, two moments -- the framing must match which one it is.

    Produced at the wizard step it said "the knowledge base load is running while you
    read this", which is simply untrue before anything has been created and undermines
    the one thing the document is for: being trustworthy about the model.
    """
    sys.path.insert(0, os.path.join(ROOT, "pipeline"))
    try:
        import describe_model as D
    finally:
        sys.path.pop(0)

    tiny = {"source_type": "cognos", "tables": [{"name": "T"}],
            "dimensions": [], "metrics": [], "facts": [], "relationships": [],
            "hierarchies": [], "grain_declarations": [], "filters": [],
            "security_rules": [], "dashboards": [], "worksheets": [], "errors": [],
            "complexity_summary": {}, "flagged": [],
            "source_analysis": {"model_name": "T", "clones": {}, "data_sources": {},
                                "security_summary": {}}}

    profile = D.render(tiny, "/x/T.zip", stage="profile")
    assert "Nothing has been built yet" in profile, \
        "the profile-stage digest does not say that nothing exists yet"
    assert "load is running while you read this" not in profile, \
        "the profile-stage digest claims a build is in progress when none is"

    during = D.render(tiny, "/x/T.zip", stage="build")
    assert "load is running while you read this" in during, \
        "the build-stage digest lost its framing"

    # Default must be the wizard stage, since that is now the primary path.
    assert D.render(tiny, "/x/T.zip") == profile, \
        "render() defaults to the build framing; the wizard path is the common one"

    # And build.py must pass the build stage explicitly.
    src = open(os.path.join(ROOT, "pipeline", "build.py"), encoding="utf-8").read()
    assert '"--stage", "build"' in src, \
        "build.py does not tell describe_model.py it is running mid-build"
    print("  the digest's framing matches when it was produced, and defaults to profile")


def test_the_wizard_forbids_polling_for_progress() -> None:
    """The streamed narration is the progress report; guessing at it invents numbers.

    An observed run polled row counts and announced "Security mappings now loading
    (888 of 19,921)" -- the mapping table's FINAL count against the source model's
    access-rule count, two unrelated numbers dressed as a ratio. It then decided the
    static count meant the load had moved on, and kept polling a finished table.
    """
    wiz = open(os.path.join(ROOT, "references", "wizard.md"), encoding="utf-8").read()
    # Collapse wrapping: the rule spans lines, and a heading must not satisfy the check.
    low = " ".join(wiz.lower().split())
    body = " ".join(l for l in low.split("### ") if not l.startswith("the build narrates"))
    assert "rows to infer progress" in low, \
        "wizard.md does not forbid counting rows to infer build progress"
    assert "do not poll" in body or "do not open a second connection" in low, \
        "the no-polling rule survives only as a heading, not as an instruction"
    assert "888" in wiz and "19,921" in wiz, \
        "the concrete invented-ratio example is gone; the rule reads as abstract advice"
    assert "relay those lines" in low, \
        "wizard.md does not say what to do instead of polling"
    b = open(os.path.join(ROOT, "pipeline", "build.py"), encoding="utf-8").read()
    # The rule is worthless if the phases stopped streaming. Bound each declaration by
    # the next Phase( rather than the first "),": depends_on=("extract",), contains one.
    for phase in ("describe", "kb-load"):
        decl = b[b.index('Phase("%s"' % phase) + 1:]
        nxt = decl.find("\n    Phase(")
        decl = decl[:nxt] if nxt != -1 else decl[:decl.find("\n]")]
        assert "stream=True" in decl, (
            "wizard.md tells the agent to read streamed output, but the %s phase "
            "does not stream" % phase)
    print("  the wizard relays streamed progress instead of inventing it from row counts")


def test_the_build_writes_a_log_the_user_can_read_while_it_runs() -> None:
    """Streaming to stdout reaches nobody when the build runs inside a tool call.

    A measured 7.7-minute run produced every line of narration correctly and showed the
    user none of it: stdout was captured and returned on exit. The log file is the only
    channel that works for both a human at a terminal and an agent driving the build,
    and it only works if it is flushed per line and its path is given out up front.
    """
    src = open(os.path.join(ROOT, "pipeline", "build.py"), encoding="utf-8").read()
    assert "build-log.txt" in src, "the build writes no readable log"

    sys.path.insert(0, os.path.join(ROOT, "pipeline"))
    try:
        import build as B
    finally:
        sys.path.pop(0)

    # (i) Readable before close, and it grows -- not buffered until exit.
    probe = os.path.join(tempfile.mkdtemp(), "live.txt")
    tee = B._Tee(io.StringIO(), probe)
    try:
        tee.write("phase 1\n")
        assert open(probe, encoding="utf-8").read() == "phase 1\n", \
            "the log is not flushed per line, so it is empty while the build runs"
        tee.write("phase 2\n")
        assert open(probe, encoding="utf-8").read().count("\n") == 2, \
            "the log does not grow during the run"
    finally:
        tee.close()

    # (ii) The path is announced before any work, so it is in the launch message.
    plan_pos = src.index('print("BUILD PLAN")')
    assert src.index('print("Live log: %s" % log_path)') < plan_pos, \
        "the log path is printed after the plan; it must come first, before any work"

    # (iii) And the wizard requires handing it over rather than only relaying prose.
    wiz = " ".join(open(os.path.join(ROOT, "references", "wizard.md"),
                        encoding="utf-8").read().lower().split())
    assert "build-log.txt" in wiz, \
        "wizard.md never names the log file, so the user is never given the path"
    assert "message where you launch the build" in wiz, \
        "wizard.md does not require the path in the message that starts the build"
    assert "not treat relaying as a substitute" in wiz, \
        "wizard.md lets prose summaries stand in for the log path"
    print("  the build logs to a file, flushed per line, and hands over the path first")


def test_composed_apps_are_handed_over_not_auditioned() -> None:
    """Learnings belong in the composition rules, not in a post-hoc inspection.

    A measured run spent ~10 minutes after composition on `npm run build`, dev servers,
    re-querying totals the backend verifier had already checked, and reading composed
    files back. It found nothing the rules had not already prevented. The user waited
    ten minutes to be told it was fine, when a caveat plus CoCo in their hands is both
    faster and more useful -- a runtime error pasted back is a sixty-second fix.
    """
    comp = " ".join(open(os.path.join(ROOT, "references", "composition-rules.md"),
                         encoding="utf-8").read().lower().split())
    wiz = " ".join(open(os.path.join(ROOT, "references", "wizard.md"),
                        encoding="utf-8").read().lower().split())
    skill = " ".join(open(os.path.join(ROOT, "SKILL.md"), encoding="utf-8")
                     .read().lower().split())

    # (i) The rule exists, in the composition doc and in both entry points.
    for name, doc in (("composition-rules.md", comp), ("wizard.md", wiz),
                      ("SKILL.md", skill)):
        assert "audition" in doc, (
            "%s does not tell the agent to hand the apps over rather than verify "
            "them; the silence is what the ten-minute self-audit filled" % name)

    # (ii) The expensive things are named as forbidden. Naming them matters: a general
    # "do not over-verify" is advice, and advice loses to the urge to check.
    for banned in ("npm run build", "next dev", "streamlit run", "browser"):
        assert banned in comp, \
            "composition-rules.md does not explicitly rule out %r after composing" % banned

    # (iii) The cheap checks that DO stay, because they catch the ImportError class of
    # defect that looks healthy from SHOW STREAMLITS.
    assert "py_compile" in comp, "the one cheap check worth keeping is not named"
    assert "streamlit_source" in comp, \
        "the file-completeness check is not named as the check that stays"

    # (iv) The caveat is required, not optional. Handing over silently is the other
    # failure mode: the user then discovers a runtime error with no idea it was expected.
    for phrase in ("generated, not tested", "paste the error back"):
        assert phrase in comp, \
            "composition-rules.md does not require telling the user %r" % phrase
    assert "generated rather than tested" in wiz and "generated rather than tested" in skill, \
        "the entry points do not carry the caveat, so it depends on reading the rules doc"

    # (v) And no unbounded waiting.
    assert "do not wait forever" in comp, \
        "composition-rules.md does not bound how long the agent waits on a command"
    print("  composed apps are handed over with a caveat, not auditioned for ten minutes")


def test_the_wizard_asks_how_to_name_the_objects() -> None:
    """The naming system already supports --prefix and --model-name, and every
    derived name (agent, semantic view, streamlit, etc.) cascades. But the wizard
    never asked -- so everything was called BI_REPORT and BI_ANALYST unless the
    user happened to know the flags existed. The question is the last missing piece.
    """
    wiz = open(os.path.join(ROOT, "references", "wizard.md"), encoding="utf-8").read()
    low = wiz.lower()

    # (i) The wizard asks for a prefix and a model name.
    assert "--prefix" in wiz, "wizard.md never mentions --prefix, the primary naming knob"
    assert "--model-name" in wiz, "wizard.md never mentions --model-name"
    assert "naming" in low and "prefix" in low, \
        "the naming question is gone from the wizard"

    # (ii) A preview of the derived names is shown before the confirmation gate.
    gate_pos = low.index("confirmation gate")
    assert "semantic view" in low[gate_pos:] and "agent" in low[gate_pos:], \
        "the confirmation gate does not show what the objects will be called"
    assert "prefix" in low[gate_pos:] and "analytics" in low[gate_pos:], \
        "the confirmation gate does not include the fully-qualified names"

    # (iii) The defaults are derived from the model, not hardcoded.
    assert "derive from model" in low, \
        "the naming defaults are hardcoded instead of derived from the model"

    # (iv) The model label is derived, not asked as a separate question.
    assert "--model-label" in wiz, "the human-readable label is not mentioned"
    assert "derived from the model name" in low, \
        "the label is asked as a question rather than derived from the model name"

    print("  the wizard asks how to name objects, shows a preview, derives defaults from the model")


def test_the_confirmation_is_a_button_not_a_text_prompt() -> None:
    """The confirmation gate is a yes/no decision. A text prompt invites a paragraph."""
    wiz = open(os.path.join(ROOT, "references", "wizard.md"), encoding="utf-8").read()
    low = wiz.lower()
    assert "ask_user_question" in low[low.index("confirmation gate"):], \
        "the confirmation gate is not presented as a radio-button question"
    assert '"proceed"' in low, \
        "there is no 'Proceed' option in the confirmation question"
    assert '"change something"' in low, \
        "there is no 'Change something' option — the user can only accept, not revise"
    print("  the confirmation gate is a radio button, not a text prompt")


def test_the_backend_is_handed_over_with_clickable_urls() -> None:
    """The backend finishes minutes before the dashboards. Handing it over with URLs
    lets the user explore the agent and semantic view while composition runs."""
    wiz = open(os.path.join(ROOT, "references", "wizard.md"), encoding="utf-8").read()
    low = wiz.lower()

    # (i) The URL patterns are documented.
    assert "app.snowflake.com" in wiz, \
        "wizard.md does not include the Snowsight URL pattern"
    assert "semantic-view" in low, \
        "the semantic view URL pattern is missing"
    assert "#/agents/" in low, \
        "the agent playground URL pattern is missing"

    # (ii) The handoff happens before composition, not after.
    assert "while i compose" in low or "while you compose" in low or "while the" in low, \
        "the handoff does not say the user gets the backend while composition runs"

    # (iii) The org/account lookup is documented.
    assert "current_organization_name" in low, \
        "wizard.md does not say how to get the org name for the URL"
    print("  the backend is handed over with clickable Snowsight URLs before composition")


def test_naming_rejects_values_that_cannot_be_identifiers() -> None:
    """Now that the wizard asks for the prefix, whatever the user types reaches the DDL.

    Observed: a verifier run reported the agent procedure as ASK__ANALYST and the
    conclusion drawn was "a naming mismatch in the verifier". It was not -- an empty
    prefix had been accepted silently, and a doubled underscore is a legal identifier,
    so the build succeeded and only the verifier noticed. Values with spaces, hyphens
    or a leading digit are worse: they are not legal identifiers at all and fail
    mid-build with a SQL syntax error, after earlier phases have committed.
    """
    sys.path.insert(0, os.path.join(ROOT, "pipeline"))
    try:
        import config as C
    finally:
        sys.path.pop(0)

    for bad in ("", "   ", "bi model", "spu-dmr", "2024", "a;DROP", "x" * 250):
        for fieldname in ("prefix", "model_name"):
            try:
                C.Naming(**{fieldname: bad})
            except ValueError:
                pass
            else:
                raise AssertionError(
                    "Naming accepted %s=%r, which cannot be an unquoted Snowflake "
                    "identifier and will fail mid-build" % (fieldname, bad))

    # Legal values still work, and fold to upper case so the name reported matches
    # the name Snowflake actually creates.
    n = C.Naming(prefix="rsa", model_name="sales_bookings", database="acme_sales")
    assert n.prefix == "RSA" and n.model_name == "SALES_BOOKINGS", \
        "a lower-case prefix is not folded, so the object and the report disagree"
    assert n.ask_procedure == "ASK_RSA_ANALYST", \
        "the derived procedure name is wrong: %r" % n.ask_procedure
    assert "__" not in n.ask_procedure, "a doubled underscore survived validation"

    # And a typo is reported as one line, not a dataclass traceback.
    src = open(os.path.join(ROOT, "pipeline", "config.py"), encoding="utf-8").read()
    assert "raise SystemExit(str(exc))" in src, \
        "a bad --prefix raises a ValueError traceback, which reads as a broken pipeline"
    print("  naming rejects unusable identifiers and folds case, with a one-line error")


def test_v10_defects() -> None:
    """The four v10 findings that were real, verified against the code.

    Findings 4 and 5 are not here. 4 claimed no phase creates the row-access bypass
    role; 30_row_access_policy.sql:36 creates it and grants it to SYSADMIN, which
    ACCOUNTADMIN inherits -- the empty-territory symptom it was diagnosing is
    finding 3. 5 blamed the verifier's name derivation for ASK__ANALYST; the cause
    was an empty --prefix accepted silently, fixed by validation in config.py.
    """
    comp = open(os.path.join(ROOT, "references", "composition-rules.md"),
                encoding="utf-8").read()
    low = " ".join(comp.lower().split())
    src = open(os.path.join(ROOT, "pipeline", "build.py"), encoding="utf-8").read()

    # F1: set_page_config must precede the bim_ui import, not just the first widget.
    # Scope every assertion to the rule's own bullet: "set_page_config" and
    # "st.connection" both occur elsewhere in this file, so a doc-wide search passes
    # even after the rule itself has been deleted.
    assert "## Runtime traps" in comp, "the runtime-traps section is gone"
    traps = comp[comp.index("## Runtime traps"):]
    bullets = traps.split("\n- ")
    page = [b for b in bullets if "set_page_config" in b]
    assert page, "the set_page_config ordering trap is undocumented"
    rule = " ".join(page[0].lower().split())
    assert "must come before" in rule, \
        "the rule does not state the ordering as a requirement"
    assert "from bim_ui import" in rule, \
        "the rule does not say set_page_config must precede the bim_ui import"
    assert "_in_snowsight" in rule and "st.connection" in rule, (
        "the rule does not explain WHY importing the library is a Streamlit call, "
        "so a reader has no reason to believe it")
    assert rule.index("set_page_config(page_title") < rule.index("from bim_ui import kpi"), \
        "the worked example shows the imports before set_page_config"

    # F2: no nested expanders.
    exp = [b for b in bullets if "expander" in b]
    assert exp, "the nested-expander trap is undocumented"
    assert "never nest" in " ".join(exp[0].lower().split()), \
        "the nested-expander rule is not stated as a prohibition"

    # F3: a phase that succeeds having inserted nothing must fail.
    assert "expect_rows" in src, "Phase has no expect_rows, so an empty insert stays green"
    assert "TERRITORY_SITE_SALES_PERSON" in src, \
        "the territory table is not declared as a table that must not be empty"
    assert "empty_expected_tables" in src, "nothing checks the declared tables"
    # The check must actually gate on the phase's own result, and must run before
    # success is recorded. A text-position assertion alone is satisfied by a branch
    # that has been disabled, so assert the live condition.
    assert "if ok and p.expect_rows:" in src, \
        "the empty-table check is not gated on the phase succeeding with expect_rows"
    check_pos = src.index("empty_expected_tables(p, args.connection)")
    append_pos = src.index('results.append({"phase"')
    assert check_pos < append_pos, \
        "the row check runs after the phase is recorded as successful"
    assert src[check_pos:append_pos].count("ok = False") == 1, \
        "an empty declared table does not flip the phase to failed"

    # F6: a bad connection name is caught in preflight, not by the first phase.
    assert "def connection_problem" in src, "nothing validates the connection name"
    assert "connection_problem(connection)" in src, \
        "connection_problem is defined but never called from preflight"
    assert "snow\", \"connection\", \"list\"" in src or "'connection', 'list'" in src, \
        "the check does not read the configured connections, so it cannot list them"
    print("  the four real v10 defects are guarded (F1 page-config, F2 expanders, "
          "F3 empty inserts, F6 connection)")


def test_the_slowest_phase_does_not_look_like_a_hang() -> None:
    """v10: "the log hung up a few times and then suddenly jumped with many lines."

    The Horizon phase is the slowest in the build at ~200s, because object comments
    and tag assignments are hundreds of serial COMMENT ON / ALTER SET TAG statements.
    It printed one line per batch AFTER the batch, and the phase did not stream, so
    its output was captured and arrived all at once at the end -- silence, then a
    burst. Both halves have to be fixed: announcing before the work is useless if the
    announcement is still buffered.
    """
    b = open(os.path.join(ROOT, "pipeline", "build.py"), encoding="utf-8").read()
    decl = b[b.index('Phase("p1-catalog"') + 1:]
    nxt = decl.find("\n    Phase(")
    decl = decl[:nxt] if nxt != -1 else decl
    assert "stream=True" in decl, \
        "the Horizon phase does not stream, so its progress arrives only at the end"

    p1 = open(os.path.join(ROOT, "pipeline", "path1_catalog_glossary.py"),
              encoding="utf-8").read()
    run_sql = p1[p1.index("def run_sql("):p1.index("def query_json(")]
    announce = [l for l in run_sql.splitlines() if ": running" in l]
    assert announce, \
        "the batch is not announced before it runs, so the wait stays unexplained"
    assert "flush=True" in announce[0], (
        "the announcement is buffered, so streaming the phase still shows nothing "
        "until the batch finishes -- which is the whole defect")
    done = [l for l in run_sql.splitlines() if ": ok" in l]
    assert done and "flush=True" in done[0], "the completion line is buffered"
    call = run_sql.index("subprocess.run(")
    assert run_sql.index(announce[0]) < call, \
        "the announcement is printed after the work it announces"
    assert run_sql.index(done[0]) > call, \
        "the completion line no longer follows the work"
    print("  the slowest phase announces each batch before running it, unbuffered")


def test_catalog_comments_and_tags_are_batched_per_table() -> None:
    """The Horizon phase's cost is statement COUNT, not statement length.

    Each statement is a serial round trip of roughly 0.67s, so one COMMENT ON COLUMN
    per column made this the slowest phase in the build at ~200s. Snowflake accepts a
    list of column clauses in a single ALTER TABLE -- verified against a live 100
    column table, 12.6KB in one statement, all 100 comments applied -- so the same
    work is one round trip per table.
    """
    src = open(os.path.join(ROOT, "pipeline", "path1_catalog_glossary.py"),
               encoding="utf-8").read()

    # Test what the code EMITS, not what it mentions: the explanatory comment above
    # the batching names the old per-column form, and a naive source scan for that
    # string fails on the very comment that documents the fix.
    emitted = "\n".join(l for l in src.splitlines()
                        if not l.lstrip().startswith("#"))
    assert "COMMENT ON COLUMN" not in emitted, \
        "column comments are still emitted one statement per column"
    assert 'MODIFY COLUMN "' not in emitted, \
        "column tags are still emitted one statement per column"
    for shape in ("ALTER TABLE %s ALTER", "ALTER TABLE %s MODIFY"):
        assert shape in src, "the batched %r form is missing" % shape

    sys.path.insert(0, os.path.join(ROOT, "pipeline"))
    try:
        import path1_catalog_glossary as P
    finally:
        sys.path.pop(0)

    # Bounded, so one wide table cannot build a single unbounded statement whose
    # failure names no column.
    assert 1 < P._CLAUSES_PER_STATEMENT <= 250, \
        "the clause cap is missing or implausible: %r" % P._CLAUSES_PER_STATEMENT
    assert len(list(P._chunks(["x"] * 25, P._CLAUSES_PER_STATEMENT))) == 1, \
        "a normal-width table should still be a single round trip"
    n = P._CLAUSES_PER_STATEMENT * 2 + 1
    assert len(list(P._chunks(["x"] * n, P._CLAUSES_PER_STATEMENT))) == 3, \
        "chunking does not split a table wider than the cap"
    # No clause may be dropped or duplicated by the chunking.
    items = [str(i) for i in range(257)]
    flat = [c for batch in P._chunks(items, P._CLAUSES_PER_STATEMENT) for c in batch]
    assert flat == items, "chunking loses or reorders column clauses"
    print("  catalog comments and tags batch per table, bounded, losing no clause")


def test_composed_column_references_are_checkable_and_checked() -> None:
    """v11 bug 1: composed charts named columns the reporting views do not expose.

    FISCAL_QUARTER_LABEL, SALES_AMOUNT, BOOKED_AMOUNT, GPC1_DESCRIPTION, TERRITORY_L1
    against real columns FISCAL_QUARTER_YEAR, SALES_AMOUNT_TOTAL, BOOKED_AMOUNT_TOTAL,
    GPC1, TERRITORY_LEVEL1. Nothing raised -- the query succeeded, the component got a
    key that was not in the row, and every chart rendered empty. A rule alone cannot
    fix this; the checker can, because the views declare their columns statically.
    """
    sys.path.insert(0, os.path.join(ROOT, "pipeline"))
    try:
        import verify_composition as V
    finally:
        sys.path.pop(0)

    sql = open(os.path.join(ROOT, "pipeline", "sql", "45_reporting_views.sql"),
               encoding="utf-8").read()
    views = V.view_columns(sql)
    declared = re.findall(r"CREATE OR REPLACE VIEW\s+(RPT_\w+)", sql)
    assert set(views) == set(declared), (
        "the parser misses reporting views, so real columns would be reported as "
        "unknown: %s" % (set(declared) - set(views)))
    known = set().union(*views.values())

    # The real names must be recognised, and the fabricated ones must not be.
    for real in ("FISCAL_QUARTER_YEAR", "SALES_AMOUNT_TOTAL", "BOOKED_AMOUNT_TOTAL",
                 "GPC1", "TERRITORY_LEVEL1", "STRANDED_USD"):
        assert real in known, "%s is a real column but the parser missed it" % real
    for fake in ("FISCAL_QUARTER_LABEL", "SALES_AMOUNT", "BOOKED_AMOUNT",
                 "GPC1_DESCRIPTION", "TERRITORY_L1"):
        assert fake not in known, \
            "%s does not exist but the parser accepts it, so the check is inert" % fake

    # End to end on the actual defect, including that a valid reference passes.
    tmp = tempfile.mkdtemp()
    react = os.path.join(tmp, "app_react")
    os.makedirs(react)
    with open(os.path.join(react, "report.tsx"), "w", encoding="utf-8") as fh:
        fh.write('<LineChart xKey="FISCAL_QUARTER_LABEL" yKey="SALES_AMOUNT_TOTAL" />\n')
    with contextlib.redirect_stdout(io.StringIO()):
        rc = V.main(["--app-react", react, "--app-streamlit", os.path.join(tmp, "none")])
    assert rc == 1, "the checker passed a fabricated column name"
    with open(os.path.join(react, "report.tsx"), "w", encoding="utf-8") as fh:
        fh.write('<LineChart xKey="FISCAL_QUARTER_YEAR" yKey="SALES_AMOUNT_TOTAL" />\n')
    with contextlib.redirect_stdout(io.StringIO()):
        rc = V.main(["--app-react", react, "--app-streamlit", os.path.join(tmp, "none")])
    assert rc == 0, "the checker rejects valid column names"

    # And the rules must actually tell composition to run it.
    comp = " ".join(open(os.path.join(ROOT, "references", "composition-rules.md"),
                         encoding="utf-8").read().lower().split())
    assert "verify_composition.py" in comp, \
        "composition-rules.md never tells anyone to run the checker"
    print("  composed column references are validated against the reporting views")


def test_the_app_wiring_rules_are_documented() -> None:
    """v11 bugs 2-6: each one silent or fatal on first run, none a judgement call."""
    comp = " ".join(open(os.path.join(ROOT, "references", "composition-rules.md"),
                         encoding="utf-8").read().lower().split())
    for phrase, why in (
        ("use `call`, not `select`",
         "bug 2: SELECT on the ASK procedure fails as an unknown UDF"),
        ("variant object, not a string",
         "bug 4: str(row[0]) yields raw JSON and the markdown regex finds nothing"),
        ('do not use `st.connection("snowflake")`',
         "bug 3: it resolves the connection named 'default' and the app dies at import"),
        (".env.local",
         "bug 6: the React app needs account, warehouse and a token to start"),
        ("render_analyst()",
         "bug 5: the agent panel was buried at the bottom of the Provenance tab"),
    ):
        assert phrase in comp, "composition-rules.md does not cover %s" % why
    print("  the CALL, VARIANT, local-connection, env and analyst-tab rules are stated")


def test_the_model_specific_boundary_is_documented_and_accurate() -> None:
    """Which layers generalise is a fact about the code, so assert it against the code.

    The knowledge base, physical layer and semantic view are derived from whatever
    model is parsed. The reporting views, row access policy and demo data are written
    for a sales-and-bookings shape. Stating that honestly is the difference between
    "point it at any model" and "point it at any model, then write one SQL file".

    Also guards the reverse drift: if someone generalises the reporting views, this
    test fails and the claim has to be updated rather than silently going stale.
    """
    skill = open(os.path.join(ROOT, "SKILL.md"), encoding="utf-8").read()
    assert "What generalises, and what does not" in skill, \
        "SKILL.md does not say which layers are model-specific"

    shape = re.compile(r"TERRITORY_LEVEL|GPC\d|SALES_AMOUNT|BOOKED|FISCAL_")
    counts = {}
    for name in ("45_reporting_views", "30_row_access_policy",
                 "12_dimension_data", "13_fact_data"):
        path = os.path.join(ROOT, "pipeline", "sql", name + ".sql")
        counts[name] = len(shape.findall(open(path, encoding="utf-8").read()))
        assert counts[name] > 0, (
            "%s no longer carries model-shape references -- if it was generalised, "
            "update the boundary table in SKILL.md" % name)
        assert name in skill, "%s is model-specific but not listed in SKILL.md" % name

    # The generated layers must stay generated, or the documented split is a lie.
    # path1 (catalog) is the one that genuinely derives everything from the KB.
    p1 = open(os.path.join(ROOT, "pipeline", "path1_catalog_glossary.py"),
              encoding="utf-8").read()
    assert "CORE_ENTITIES" not in p1, \
        "the catalog generator now depends on the hardcoded entity list"

    # The semantic view and physical layer are NOT model-independent, and SKILL.md
    # must keep saying so. CORE_ENTITIES is the single pivot, and the physical layer
    # imports it -- an easy thing to forget when describing the boundary.
    p3 = open(os.path.join(ROOT, "pipeline", "path3_ossie_semantic_view.py"),
              encoding="utf-8").read()
    assert "CORE_ENTITIES" in p3, "CORE_ENTITIES is gone; update the boundary table"
    phys = open(os.path.join(ROOT, "pipeline", "generate_physical_layer.py"),
                encoding="utf-8").read()
    if "CORE_ENTITIES" in phys:
        assert "imports `CORE_ENTITIES`" in skill or "imports that same list" in skill, (
            "generate_physical_layer.py imports CORE_ENTITIES, so the physical layer "
            "is scoped to the reference star schema -- SKILL.md must not present it "
            "as model-independent")
    assert "CORE_ENTITIES" in skill, \
        "SKILL.md does not name CORE_ENTITIES, the single pivot for generalising"
    assert "KB_TERM" in phys, "the physical layer no longer derives columns from KB_TERM"

    # And the checker must not hardcode this model's columns in its logic. Its
    # docstring names them to explain the observed defect, which is fine; the code
    # must derive everything from the views file.
    vc = open(os.path.join(ROOT, "pipeline", "verify_composition.py"),
              encoding="utf-8").read()
    body = vc.split('"""', 2)[2] if vc.count('"""') >= 2 else vc
    code = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("#"))
    assert not shape.search(code), (
        "verify_composition.py hardcodes this model's column names, so it would not "
        "generalise to another subject area")

    # The empty-table failure must not assert a cause that only this model has.
    b = open(os.path.join(ROOT, "pipeline", "build.py"), encoding="utf-8").read()
    msg_start = b.index("reported success but left these empty")
    msg = b[msg_start:msg_start + 1200]
    assert not shape.search(msg), (
        "the empty-table message names this model's columns; the generic mechanism "
        "should defer to the phase's own note for model-specific hints")
    print("  the model-specific boundary is documented and matches the code")


if __name__ == "__main__":
    test_skill_dir_defaults_to_this_repo()
    test_dry_run_phase_counts()
    test_missing_app_source_is_caught_in_preflight()
    test_skip_deploy_alias_matches_deploy_none()
    test_local_dashboards_keep_what_they_read()
    test_the_deploy_cli_is_a_preflight_check_not_a_phase_failure()
    test_the_build_says_which_snow_it_will_use()
    test_the_model_digest_runs_between_the_parse_and_the_load()
    test_the_digest_reports_only_what_the_parse_found()
    test_run_app_deploy_does_not_shadow_the_config_module()
    test_the_streamlit_deploy_stages_the_whole_locked_library()
    test_deploying_is_never_the_default()
    test_the_wizard_forbids_treating_parameters_as_consent()
    test_the_three_step_run_shape_is_documented()
    test_the_streamlit_layout_is_pinned_like_the_react_one()
    test_the_deploy_question_and_its_mapping_use_the_same_labels()
    test_the_deploy_answer_is_not_treated_as_a_path()
    test_the_parse_creates_its_output_directory()
    test_the_wizard_hands_over_the_digest_before_asking_what_to_build()
    test_the_digest_does_not_claim_a_build_is_running_when_none_is()
    test_the_wizard_forbids_polling_for_progress()
    test_the_build_writes_a_log_the_user_can_read_while_it_runs()
    test_composed_apps_are_handed_over_not_auditioned()
    test_the_wizard_asks_how_to_name_the_objects()
    test_the_confirmation_is_a_button_not_a_text_prompt()
    test_the_backend_is_handed_over_with_clickable_urls()
    test_naming_rejects_values_that_cannot_be_identifiers()
    test_v10_defects()
    test_the_slowest_phase_does_not_look_like_a_hang()
    test_catalog_comments_and_tags_are_batched_per_table()
    test_composed_column_references_are_checkable_and_checked()
    test_the_app_wiring_rules_are_documented()
    test_the_model_specific_boundary_is_documented_and_accurate()
