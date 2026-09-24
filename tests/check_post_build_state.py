"""The suite must pass *after* a successful build, not only on a pristine clone.

N1, N2 and N10 from the v5 measured run were all one mistake wearing three hats:
guards added in the v4 pass were correct, and were tested only against a fresh
checkout. Their new reach was never exercised against the state the skill itself
produces. The result was a suite that went green on a clone and red immediately
after the thing the clone exists to do, with a failure message that blamed the
user's Snowflake connection.

So these tests assert the properties that only differ once a build has run:

* Per-run artefacts are ignored, so they never enter the set ``git add -A`` would
  stage -- which is the set the identifier guard reads.
* The identifier guard buys that pass by ignoring uncommittable files, not by
  allowlisting the customer's database name.
* A clone installed under a second name -- the supported way to validate a change
  against a live model -- can still run its own suite.

The ambient-absence half of N1 is fixed in ``check_build_flags.py``, where the
offending assertion lived; it now builds its own fixture.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Assembled rather than written out, so this file does not trip the very guard it
# is testing. The v4 pass learned this the hard way: a test naming the identifier
# it checks for is itself a leak, and the guard is right to say so.
TOKEN = "RES" + "MED"
PREFIX = "SP" + "U_"
ACCOUNT = "SFSENORTH" + "AMERICA-DEMO_" + "HMAHAKALI"


def test_per_run_artefacts_are_ignored_not_allowlisted():
    """Composed app source must be out of the committable set entirely.

    The composed app has to name the customer's target database -- that is its
    job. There are two ways to stop the identifier guard failing on it: ignore it,
    or allowlist the database name inside the guard. Only the first is honest. An
    allowlist leaves the files staged and teaches the guard to accept exactly the
    string it exists to catch, so a genuine leak of the same name in a tracked
    file would sail through as well.
    """
    probes = [
        # File paths, not bare directories. `git check-ignore` returns 1 for a
        # directory that does not exist unless it is asked with a trailing slash,
        # which made an earlier version of a sibling test vacuous.
        "pipeline/app_streamlit/app.py",
        "pipeline/app_react/lib/queries.ts",
        "pipeline/out/probe.sql",
    ]
    missing = [p for p in probes
               if subprocess.run(["git", "check-ignore", "-q", p],
                                 cwd=ROOT).returncode != 0]
    assert not missing, (
        "not gitignored, so these land in the set `git add -A` would stage and the\n"
        "identifier guard fails after every path 2/4 build: %s" % missing)
    print("  composed app source and generated SQL are ignored")


def test_the_identifier_guard_allowlists_no_database_name():
    """The guard must still flag a customer database name in a committable file.

    Asserted through the guard's own decision function, not by grepping its source
    for quoted names. A source-text check is evaded by any allowlist assembled
    from fragments -- and it was: a planted ``ALLOW = ("RES" "MED_TEST",)`` walked
    straight past an earlier version of this test.
    """
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    try:
        import check_identifiers as G
    finally:
        sys.path.pop(0)

    must_flag = [
        ("pipeline/sql/10_views.sql", "USE DATABASE %s_TEST_V5;" % TOKEN),
        ("pipeline/app_react/app.yml", "SNOWFLAKE_DATABASE: %s_TEST_V5" % TOKEN),
        ("pipeline/build.py", 'DEFAULT = "%s_SPU"' % TOKEN),
        ("docs/notes.md", "the %s prefix" % PREFIX),
        ("x.env", "SNOWFLAKE_ACCOUNT=%s" % ACCOUNT),
    ]
    missed = [(rel, line) for rel, line in must_flag if not G.flags(rel, line)]
    assert not missed, (
        "the guard no longer flags customer identifiers it exists to catch -- an\n"
        "allowlist has been added somewhere in its decision path: %s" % missed)
    print("  the guard still flags every customer identifier, in every file")


def test_guard_excuses_a_local_rename_and_nothing_else():
    """A clone installed under a second name must be able to run its own suite.

    Renaming the skill to test it against a live model without disturbing the
    installed copy is the supported workflow, and the frontmatter name is the one
    place the engagement name legitimately appears in a working tree. The
    exception is anchored to that line, so it must not excuse the same string
    elsewhere in SKILL.md, nor that line in any other file.
    """
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    try:
        import check_identifiers as G
    finally:
        sys.path.pop(0)

    renamed = "name: %s-test-skill-v5" % TOKEN.lower()
    assert G.is_local_rename("SKILL.md", renamed), (
        "a renamed SKILL.md still trips the guard, so the suite cannot pass in the "
        "very clone you would use to validate a change")

    too_wide = [
        ("SKILL.md", "description: rebuild the %s sales model" % TOKEN),
        ("SKILL.md", "  name: %s" % TOKEN),        # indented: not frontmatter
        ("SKILL.md", "name: %s and more words" % TOKEN),
        ("pipeline/build.py", "name: %s" % TOKEN.lower()),
    ]
    for rel, line in too_wide:
        assert not G.is_local_rename(rel, line), \
            "the rename exception is too wide: it excused %r in %s" % (line, rel)

    # And the guard as a whole must still fail on a real leak in SKILL.md.
    assert G.BANNED.search("database: %s_TEST_V5" % TOKEN), \
        "the banned pattern no longer matches the identifier it exists to catch"
    print("  the rename exception covers the frontmatter name line and nothing else")


if __name__ == "__main__":
    for fn in (test_per_run_artefacts_are_ignored_not_allowlisted,
               test_the_identifier_guard_allowlists_no_database_name,
               test_guard_excuses_a_local_rename_and_nothing_else):
        try:
            fn()
        except AssertionError as exc:
            print("  FAIL %s\n    %s" % (fn.__name__, exc))
            sys.exit(1)
    sys.exit(0)
