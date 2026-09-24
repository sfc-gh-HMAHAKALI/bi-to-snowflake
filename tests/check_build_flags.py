"""Test pipeline/build.py CLI flags and dry-run phase resolution."""

import subprocess
import sys


def test_build_dry_runs():
    tests = [
        (["--paths", "4", "--deploy", "all", "--dry-run"], 17, ["Deploy the Streamlit", "Deploy the React"]),
        (["--paths", "4", "--deploy", "both", "--dry-run"], 17, ["Deploy the Streamlit", "Deploy the React"]),
        (["--paths", "4", "--deploy", "streamlit", "--dry-run"], 15, ["Deploy the Streamlit"]),
        (["--paths", "4", "--deploy", "react", "--dry-run"], 16, ["Deploy the React"]),
        (["--paths", "4", "--deploy", "none", "--dry-run"], 14, []),
        (["--paths", "4", "--deploy", "local", "--dry-run"], 14, []),
        (["--paths", "1", "--dry-run"], 9, ["Tag taxonomy", "Horizon comments"]),
        (["--paths", "3", "--dry-run"], 12, ["Ossie semantic view", "Cortex Agent"]),
    ]

    for cmd, expected_len, expected_phases in tests:
        res = subprocess.run(["python3", "pipeline/build.py"] + cmd, capture_output=True, text=True,
                             cwd=sys.path[0] + "/..")
        assert res.returncode == 0, f"Failed: {cmd}\n{res.stderr}"
        lines = [l for l in res.stdout.splitlines() if l.strip().startswith(tuple(f"{i}." for i in range(1, 30)))]
        assert len(lines) == expected_len, f"Expected {expected_len} phases for {cmd}, got {len(lines)}:\n{res.stdout}"
        for ep in expected_phases:
            assert any(ep in l for l in lines), f"Missing phase {ep} in {lines}"
        if not any("Deploy the Streamlit" in ep for ep in expected_phases):
            assert not any("Deploy the Streamlit" in l for l in lines), f"Unexpected Streamlit deploy in {lines}"
        if not any("Deploy the React" in ep for ep in expected_phases):
            assert not any("Deploy the React" in l for l in lines), f"Unexpected React deploy in {lines}"

    print("  all 8 build dry-run configurations resolved correctly")


if __name__ == "__main__":
    test_build_dry_runs()
