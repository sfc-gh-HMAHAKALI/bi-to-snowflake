"""No tracked file may carry a customer identifier.

This repository was scrubbed once, of one engagement's database names, business-unit
abbreviation, email domain and Cognos group paths. A grep in the suite is what keeps
them from drifting back in through a copied snippet.

Excluded on purpose: this file, which has to contain the patterns it bans, and the
upstream attribution URL in VENDOR.md, which names the repository this code was
vendored from rather than any customer.
"""
import os
import re
import subprocess
import sys

BANNED = re.compile(r"(?i)resmed|sfsenorthamerica|demo_hmahakali|himavarsh")
# Word-boundary SPU, plus the object-prefix form. Deliberately case-sensitive on the
# prefix form so it cannot be confused with unrelated lowercase text.
SPU = re.compile(r"\bSPU\b|SPU_")

SELF = "tests/check_identifiers.py"


def main() -> int:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    files = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True,
                           text=True, check=True).stdout.split()
    bad = []
    for rel in files:
        if rel == SELF:
            continue
        try:
            text = open(os.path.join(root, rel), encoding="utf-8").read()
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        for i, line in enumerate(text.split("\n"), 1):
            if BANNED.search(line) or SPU.search(line):
                bad.append("%s:%d %s" % (rel, i, line.strip()[:70]))
    if bad:
        print("  FAIL %d customer identifier(s):" % len(bad))
        for b in bad[:15]:
            print("     ", b)
        return 1
    print("  %d tracked files, none carry a customer identifier" % len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
