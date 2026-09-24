"""Test pipeline/build.py CLI flags, dry-run phase resolution, and the
composed-app preflight."""

import os
import subprocess
import sys

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


if __name__ == "__main__":
    test_skill_dir_defaults_to_this_repo()
    test_dry_run_phase_counts()
    test_missing_app_source_is_caught_in_preflight()
    test_skip_deploy_alias_matches_deploy_none()
    test_local_dashboards_keep_what_they_read()
    test_the_deploy_cli_is_a_preflight_check_not_a_phase_failure()
    test_the_build_says_which_snow_it_will_use()
