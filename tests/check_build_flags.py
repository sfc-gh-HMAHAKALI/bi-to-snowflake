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


if __name__ == "__main__":
    test_dry_run_phase_counts()
    test_missing_app_source_is_caught_in_preflight()
