"""KnowledgeBaseLoader must write into the KB database, not the analytics one.

01_kb_tables.sql creates KNOWLEDGE_BASE in {{KB_DB}}. The two are commonly
different databases (BI2SF vs BI2SF_KB by default), so
a loader wired to the wrong one fails on its first MERGE with "does not exist"
-- or worse, if the two names happen to coincide, silently writes to the wrong
place with no error at all.
"""

import re
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline"))


def test_loader_targets_kb_database_not_analytics_database():
    from kb_loader import KnowledgeBaseLoader

    loader = KnowledgeBaseLoader(
        runner=None, source_system="cognos", source_model="M", load_id="L",
        schema_map={}, kb_database="SALESKB",
    )
    assert loader.target_database == "SALESKB", (
        "loader targets %r, expected the KB database SALESKB" % loader.target_database
    )

    statements = []
    loader.runner = type("R", (), {
        "run_statements": lambda self, stmts, label: statements.extend(stmts)
    })()
    loader._merge("KB_TERM", ["TERM_ID"], ["TERM_ID", "NAME"],
                  [["'t1'", "'Sales'"]], "terms")
    sql = statements[0]
    assert "MERGE INTO SALESKB.KNOWLEDGE_BASE.KB_TERM" in sql, sql[:150]
    print("  loader MERGEs into the KB database (SALESKB), not the analytics one")


def test_main_wires_kb_database_flag():
    """The call site in main() must pass --kb-database through, not --database."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "pipeline", "kb_loader.py")).read()
    call = src[src.index("loader = KnowledgeBaseLoader("):src.index("loader.load_source_model")]
    assert "args.kb_database" in call, "call site does not pass args.kb_database:\n%s" % call
    assert "args.database,\n    )" not in call, "call site still passes args.database as the KB target"
    print("  main() wires args.kb_database into the loader")




def test_path1_honours_kb_naming_flags():
    """Path 1's DDL must follow --kb-database/--kb-schema, not the defaults.

    Two ways this broke: the module global KB was never rebound from the flags,
    and the DDL blocks were import-time f-strings that a rebind could not reach.
    """
    import argparse
    import config
    import path1_catalog_glossary as P

    ap = argparse.ArgumentParser()
    config.add_arguments(ap)
    args = ap.parse_args(["--kb-database", "CUSTOMER_KB", "--kb-schema", "GOVERNED"])
    P.KB = config.from_args(args).kb
    assert P.KB == "CUSTOMER_KB.GOVERNED", P.KB

    for fn in ("tag_ddl", "glossary_ddl", "glossary_load"):
        out = getattr(P, fn)()
        assert "CUSTOMER_KB.GOVERNED" in out, "%s ignored the naming flags" % fn
        assert "BI2SF_KB" not in out, "%s baked in the default KB database" % fn

    # An import-time f-string constant would silently reintroduce the bug.
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "pipeline", "path1_catalog_glossary.py")).read()
    assert not re.search(r'^[A-Z_]+ = f"""', src, re.M), \
        "an import-time f-string constant is back; it cannot see the naming flags"
    print("  path 1 DDL honours --kb-database/--kb-schema")


def test_merge_uses_a_values_list_not_a_union_all_chain() -> None:
    """The MERGE source shape is a compile-time cost, and it dominated the phase.

    Measured on the same 500 rows into the same table: the 500-way UNION ALL form
    spent 1.87s compiling and 0.57s executing; one VALUES row-constructor list
    spent 0.43s and 0.30s. Compile is 77% of the cost and scales with the length
    of the SQL text, so the knowledge-base phase was mostly Snowflake parsing a
    ~97KB statement, once per batch, 88 times over.
    """
    import kb_loader as K

    captured: list[tuple[str, list[str]]] = []

    class Fake:
        def run_statements(self, stmts, label): captured.append((label, stmts))
        def run_file(self, sql, label):
            raise AssertionError("the KB load must pass statements as a list")
        def close(self): pass

    ldr = K.KnowledgeBaseLoader(Fake(), "cognos", "M", "load1", {}, "MYKB")
    rows = [[K._sql_str("v%d" % i), K._sql_str("x")] for i in range(1100)]
    ldr._merge("KB_THING", ["C1"], ["C1", "C2"], rows, "thing")

    stmts = [s for _, batch in captured for s in batch]
    assert len(stmts) == 3, "1100 rows at BATCH=500 should be 3 statements, got %d" % len(stmts)
    for s in stmts:
        assert "FROM VALUES" in s, "MERGE source is not a VALUES list"
        assert "UNION ALL" not in s, "the UNION ALL chain is back; it costs 4x to compile"
        # Idempotency is the property that makes a re-run safe. Unchanged.
        assert s.startswith("MERGE INTO"), "no longer a MERGE"
        assert "ON t.C1 = s.C1" in s, "natural-key ON clause changed"
        assert "WHEN MATCHED THEN UPDATE" in s and "WHEN NOT MATCHED THEN INSERT" in s
        assert "MYKB.KNOWLEDGE_BASE.KB_THING" in s, "wrong target database"
    print("  MERGE uses one VALUES list, keeps MERGE-on-natural-key semantics")


def test_statements_are_never_reparsed_out_of_a_joined_string() -> None:
    """Splitting a joined SQL string on ";\\n" is unsafe, and looked equivalent.

    ``_sql_str`` escapes backslashes and single quotes but leaves newlines intact.
    So any source value containing a semicolon at the end of a line -- and this
    model family carries 25,398 Cognos expressions -- would be cut in half and
    sent to Snowflake as invalid SQL. Passing a list and never re-parsing removes
    the whole class of bug, so the statement count must be unaffected by content.
    """
    import kb_loader as K

    captured: list[list[str]] = []

    class Fake:
        def run_statements(self, stmts, label): captured.append(stmts)
        def run_file(self, sql, label):
            raise AssertionError("the KB load must pass statements as a list")
        def close(self): pass

    nasty = "line one;\nline two;\nstill the same value"
    ldr = K.KnowledgeBaseLoader(Fake(), "cognos", "M", "load1", {}, "MYKB")
    ldr._merge("KB_THING", ["C1"], ["C1", "C2"],
               [[K._sql_str("k1"), K._sql_str(nasty)]], "thing")

    stmts = [s for batch in captured for s in batch]
    assert len(stmts) == 1, \
        "one row produced %d statements; the SQL is being re-parsed" % len(stmts)
    assert "line one;" in stmts[0] and "still the same value" in stmts[0], \
        "the embedded semicolon-newline did not survive intact"
    print("  a value containing ';\\n' still yields exactly one statement")


def test_both_runners_share_an_interface_and_the_cli_remains_a_fallback() -> None:
    """One persistent session, without losing the reason the CLI was chosen.

    Shelling out to `snow sql` means the loader inherits the operator's configured
    auth and holds no credentials -- a good property worth keeping. The connector
    path reads the same connections.toml via connection_name, so it is the same
    credentials with the per-call process spawn and login removed (~4s each, ~14
    times per load). If the connector is missing, the CLI must still work.
    """
    import kb_loader as K

    for cls in (K.SnowSqlRunner, K.ConnectorRunner):
        for method in ("run_statements", "run_file", "close"):
            assert callable(getattr(cls, method, None)), \
                "%s is missing %s" % (cls.__name__, method)

    assert isinstance(K.make_runner("whatever", prefer_cli=True), K.SnowSqlRunner), \
        "--use-cli must force the subprocess path"

    # With the connector unavailable, make_runner must fall back rather than raise.
    real = K.ConnectorRunner.__init__

    def boom(self, connection):
        raise ImportError("no connector here")

    K.ConnectorRunner.__init__ = boom
    try:
        got = K.make_runner("whatever")
        assert isinstance(got, K.SnowSqlRunner), \
            "a missing connector must fall back to the CLI, not fail the load"
    finally:
        K.ConnectorRunner.__init__ = real

    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "pipeline", "kb_loader.py")).read()
    assert "--use-cli" in src, "the escape hatch back to the CLI is undocumented"
    assert "runner.close()" in src, "the session is never released"
    print("  connector session by default, CLI fallback, --use-cli escape hatch")


if __name__ == "__main__":
    test_loader_targets_kb_database_not_analytics_database()
    test_main_wires_kb_database_flag()
    test_path1_honours_kb_naming_flags()
    test_merge_uses_a_values_list_not_a_union_all_chain()
    test_statements_are_never_reparsed_out_of_a_joined_string()
    test_both_runners_share_an_interface_and_the_cli_remains_a_fallback()
