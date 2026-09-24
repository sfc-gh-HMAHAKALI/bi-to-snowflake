"""No file this repository would commit may carry a customer identifier.

This repository was scrubbed once, of one engagement's database names, business-unit
abbreviation, email domain and Cognos group paths. A grep in the suite is what keeps
them from drifting back in through a copied snippet.

**Scans everything `git add -A` would stage**, not just what is already tracked. That
distinction is the whole point. Two phases generate DDL from the knowledge base, and
until recently they wrote it over the tracked templates in ``pipeline/sql/`` -- so after
any real build the working tree held 552 lines of one customer's database, schema and
table names, one ``git add -A`` away from being published. The generators now write to
``pipeline/out/`` instead, but a guard that only reads ``git ls-files`` would not have
caught the old behaviour and would not catch a stray ``--out-dir sql`` reintroducing it.
Anything ignored by ``.gitignore`` is out of scope by definition: it cannot be committed.

Excluded on purpose, and only these two:

* This file, which has to contain the patterns it bans.
* The ``name:`` line of ``SKILL.md``'s frontmatter. Installing this skill under a
  second name to test it against a live model is the supported way to try a
  change without disturbing the installed copy, and that rename is the one place
  the engagement's name legitimately appears in a working tree. It is the skill's
  own install identity, not customer content, and it is never what leaks. Without
  this the suite cannot pass in the very clone you would use to validate a fix.

No exception is made for a configured database or object prefix. Those are the
identifiers most worth catching, and the composed app source that necessarily
carries them is ignored by ``.gitignore`` instead -- out of scope because it
cannot be committed, which is the honest reason, rather than allowlisted while
still staged.
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

# The skill's own install name, in SKILL.md frontmatter. Anchored to `name:` at the
# start of a line so it cannot excuse a customer identifier anywhere else in the
# file -- the description, the trigger list and the body are all still scanned.
RENAME_LINE = re.compile(r"^name:\s*\S+\s*$")

# Max offending lines to print per file. A generated file carries hundreds of hits and
# they are all the same problem; listing 15 of them buried the one hit in SKILL.md that
# a reader needed to see. Report per file, with a count.
PER_FILE = 3


def is_local_rename(rel: str, line: str) -> bool:
    """True for the one line that names the skill's own installed copy."""
    return rel == "SKILL.md" and bool(RENAME_LINE.match(line))


def flags(rel: str, line: str) -> bool:
    """Whether this line, in this file, counts as a customer identifier.

    The single decision point, so a test can assert on behaviour rather than on
    the presence of string literals in this file. A syntactic test -- "no banned
    name appears in quotes here" -- is evaded by any allowlist assembled from
    fragments, and an allowlist is the tempting wrong fix for the composed app
    source naming its own target database.
    """
    if is_local_rename(rel, line):
        return False
    return bool(BANNED.search(line) or SPU.search(line))


def staged_set(root: str) -> list[str]:
    """Every path `git add -A` would stage: tracked plus untracked-not-ignored."""
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root, capture_output=True, text=True, check=True).stdout
    return [p for p in out.split("\n") if p]


def main() -> int:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    files = staged_set(root)
    by_file: dict[str, list[str]] = {}
    for rel in files:
        if rel == SELF:
            continue
        try:
            text = open(os.path.join(root, rel), encoding="utf-8").read()
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        for i, line in enumerate(text.split("\n"), 1):
            if flags(rel, line):
                by_file.setdefault(rel, []).append("%d: %s" % (i, line.strip()[:70]))

    if by_file:
        total = sum(len(v) for v in by_file.values())
        print("  FAIL %d customer identifier(s) across %d file(s):"
              % (total, len(by_file)))
        for rel in sorted(by_file):
            hits = by_file[rel]
            print("      %s (%d)" % (rel, len(hits)))
            for h in hits[:PER_FILE]:
                print("        " + h)
            if len(hits) > PER_FILE:
                print("        ... and %d more in this file" % (len(hits) - PER_FILE))
        return 1

    print("  %d committable files, none carry a customer identifier" % len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
