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

    The app source is composed per model and is not in the repo, so in a clean
    checkout every mode that plans a deploy should say so up front. --deploy none
    plans no deploy, so it must stay silent about app source.
    """
    expect = {"all": 2, "streamlit": 1, "react": 1, "none": 0}
    for mode, want in expect.items():
        res = build(["--paths", "4", "--deploy", mode, "--skip-physical",
                     "--connection", "__none__"])
        n = res.stdout.count("app source is not composed")
        assert n == want, \
            "--deploy %s: expected %d app-source complaints, got %d:\n%s" % (
                mode, want, n, res.stdout)
        if want:
            assert "Nothing executed" in res.stdout, \
                "--deploy %s complained but did not stop" % mode
    print("  composed-app preflight fires per surface, and not for --deploy none")




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


if __name__ == "__main__":
    test_skill_dir_defaults_to_this_repo()
    test_dry_run_phase_counts()
    test_missing_app_source_is_caught_in_preflight()
    test_skip_deploy_alias_matches_deploy_none()
    test_local_dashboards_keep_what_they_read()
