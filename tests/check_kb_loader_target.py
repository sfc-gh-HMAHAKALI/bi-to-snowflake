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
SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def test_variant_and_array_columns_never_appear_inside_the_values_clause() -> None:
    """A VALUES row constructor takes literals. PARSE_JSON in one is rejected.

    This shipped broken. The VALUES reshape was measured on a synthetic batch of
    plain string columns, so it never rendered a VARIANT or an ARRAY, and the
    first real model it touched failed at once:

        002014 (22000): SQL compilation error:
        Invalid expression [PARSE_JSON('{...}')] in VALUES clause

    The values travel as plain strings and are converted in the SELECT that wraps
    the VALUES list. So no PARSE_JSON may appear between `FROM VALUES` and the
    closing alias, and the conversion must appear in the select list instead.
    """
    import kb_loader as K

    captured: list[list[str]] = []

    class Fake:
        def run_statements(self, stmts, label): captured.append(stmts)
        def run_file(self, sql, label):
            raise AssertionError("the KB load must pass statements as a list")
        def close(self): pass

    ldr = K.KnowledgeBaseLoader(Fake(), "cognos", "M", "load1", {}, "MYKB")
    # One of each shape the renderers can produce, including the empty array and
    # the None cases, which take different branches.
    rows = [[K._sql_str("k%d" % i),
             K._sql_variant({"depth": i, "note": "it's quoted"}),
             K._sql_array(["a", "b"]) if i % 2 else K._sql_array(None),
             K._sql_variant(None)]
            for i in range(20)]
    ldr._merge("KB_THING", ["C1"], ["C1", "C2", "C3", "C4"], rows, "thing")

    stmts = [s for batch in captured for s in batch]
    assert stmts, "no statement was produced"
    for s in stmts:
        assert "FROM VALUES" in s, "MERGE source is not a VALUES list"
        head, rest = s.split("FROM VALUES", 1)
        body = rest.split("AS v(", 1)[0]
        assert "PARSE_JSON" not in body, (
            "PARSE_JSON is inside the VALUES clause; Snowflake rejects it:\n%s"
            % body[:400])
        assert "ARRAY_CONSTRUCT" not in body, (
            "ARRAY_CONSTRUCT is inside the VALUES clause:\n%s" % body[:400])
        assert "PARSE_JSON(v2)" in head, \
            "the VARIANT column is no longer rebuilt in the select list"
        assert "PARSE_JSON(v3)::ARRAY" in head, \
            "the ARRAY column is no longer rebuilt in the select list"
        assert "v1," in head, "the plain literal column should pass through unwrapped"
    print("  VARIANT and ARRAY values travel as literals and convert in the SELECT")


def test_an_unrecognised_expression_falls_back_instead_of_emitting_bad_sql() -> None:
    """Anything the hoist cannot express must go back to the UNION ALL form.

    The fallback is the whole reason this is safe to ship. A value shape nobody
    anticipated -- a function call, a column reference, a cast this code does not
    know -- must produce slower SQL, never invalid SQL, and never SQL that means
    something subtly different.
    """
    import kb_loader as K

    # Refuses what it cannot express.
    assert K._hoist_expressions([["CURRENT_TIMESTAMP()"]], 1) is None, \
        "an unknown expression was hoisted into a VALUES clause anyway"
    # Refuses to wrap a plain string in PARSE_JSON, which would fail at runtime.
    mixed = [[K._sql_variant({"a": 1})], [K._sql_str("not json")]]
    assert K._hoist_expressions(mixed, 1) is None, \
        "a column mixing JSON and plain text was hoisted; PARSE_JSON would fail on it"
    # NULL coexists with JSON, because PARSE_JSON(NULL) is NULL either way.
    ok = K._hoist_expressions([[K._sql_variant({"a": 1})], [K._sql_variant(None)]], 1)
    assert ok is not None and ok[1] == ["PARSE_JSON(v1)"], \
        "NULL should not block hoisting a JSON column: %r" % (ok,)

    captured: list[list[str]] = []

    class Fake:
        def run_statements(self, stmts, label): captured.append(stmts)
        def run_file(self, sql, label): raise AssertionError("must be a list")
        def close(self): pass

    ldr = K.KnowledgeBaseLoader(Fake(), "cognos", "M", "load1", {}, "MYKB")
    ldr._merge("KB_THING", ["C1"], ["C1", "C2"],
               [[K._sql_str("k"), "CURRENT_TIMESTAMP()"]], "thing")
    s = captured[0][0]
    assert "UNION ALL" in s or "SELECT " in s, "no fallback source was emitted"
    assert "FROM VALUES" not in s, \
        "an unhoistable batch still used a VALUES clause:\n%s" % s[:400]
    print("  unhoistable batches fall back to SELECT/UNION ALL, never invalid SQL")


def test_reported_row_counts_cover_every_batch_not_just_the_last() -> None:
    """The row count in the summary must be the whole input, not the last batch.

    A multi-batch table reported the size of its final batch: 19,051 rows came back
    as 51, 4,070 as 70, 4,550 as 50 -- every error an exact multiple of BATCH, which
    is the tell. The cause was one line in _merge assigning to `rows`, the function's
    own parameter, inside the per-batch loop, so `total = len(rows)` afterwards saw
    only what was left there. The data written was always correct, which is why
    nothing else caught it: only the number we reported was wrong, and it under-
    reported, so it never looked like a duplicate-row problem.

    Asserted with more rows than one batch holds, because with a single batch the
    bug is invisible -- the last batch is the whole input.
    """
    import kb_loader as K

    class Fake:
        def run_statements(self, stmts, label): pass
        def run_file(self, sql, label): raise AssertionError("must be a list")
        def close(self): pass

    ldr = K.KnowledgeBaseLoader(Fake(), "cognos", "M", "load1", {}, "MYKB")
    n = K.KnowledgeBaseLoader.BATCH * 3 + 7          # deliberately not a multiple
    rows = [[K._sql_str("k%d" % i), K._sql_variant({"i": i})] for i in range(n)]
    ldr._merge("KB_THING", ["C1"], ["C1", "C2"], rows, "thing")
    assert ldr.stats["KB_THING"] == n, (
        "reported %d rows for an input of %d -- the count is per-batch, not total"
        % (ldr.stats["KB_THING"], n))

    # And the loader must not have mutated the caller's list.
    assert len(rows) == n, "_merge rebound or truncated the rows it was given"
    print("  reported row counts span every batch, and the input list is untouched")


def test_the_knowledge_base_load_narrates_itself() -> None:
    """The longest phase must explain itself while it runs.

    Two minutes of a silent terminal reads as a hang. Three things have to hold:
    every table the loader writes has a plain-language description; the table is
    announced *before* it is written, not only after (the slowest table takes over a
    minute on its own, so completion-only reporting leaves exactly the gap this
    exists to close); and build.py streams the phase rather than capturing it, since
    narration nobody sees is worse than none.
    """
    import kb_loader as K

    # (i) Every table the loader writes is described.
    src = open(os.path.join(SRC, "pipeline", "kb_loader.py"), encoding="utf-8").read()
    written = set(re.findall(r'self\._merge\(\s*"([A-Z_]+)"', src))
    assert written, "no _merge calls found -- has the loader been restructured?"
    undocumented = sorted(written - set(K.TABLE_PURPOSE))
    assert not undocumented, (
        "these knowledge base tables have no description in TABLE_PURPOSE, so the "
        "load would narrate them as bare identifiers: %s" % undocumented)
    stale = sorted(set(K.TABLE_PURPOSE) - written)
    assert not stale, "TABLE_PURPOSE describes tables the loader no longer writes: %s" % stale

    # (ii) Announced before the write, not only on completion.
    said: list[str] = []
    prog = K.LoadProgress(emit=lambda fmt, *a: said.append(fmt % a))
    prog.opening("M", "cognos")
    assert any("13 tables" in s or "tables describing" in s for s in said), \
        "the opening does not say what is being built"

    class Fake:
        def run_statements(self, stmts, label): pass
        def run_file(self, sql, label): raise AssertionError("must be a list")
        def close(self): pass

    said.clear()
    ldr = K.KnowledgeBaseLoader(Fake(), "cognos", "M", "l", {}, "MYKB", progress=prog)
    ldr._merge("KB_TERM", ["C1"], ["C1"], [[K._sql_str("a")]], "terms")
    joined = "\n".join(said)
    assert "KB_TERM" in joined, "the table was never announced"
    assert K.TABLE_PURPOSE["KB_TERM"] in joined, "the description was not printed"
    first_announce = next(i for i, s in enumerate(said) if "KB_TERM" in s)
    first_result = next((i for i, s in enumerate(said) if "rows" in s), len(said))
    assert first_announce < first_result, \
        "the table is only reported after it is written -- the wait stays silent"

    # (iii) build.py streams this phase.
    b = open(os.path.join(SRC, "pipeline", "build.py"), encoding="utf-8").read()
    assert "stream: bool = False" in b, "Phase has no stream flag"
    assert "stream=p.stream" in b, "the flag is never passed to run_py"
    kb_decl = b[b.index('Phase("kb-load"'):]
    kb_decl = kb_decl[:kb_decl.index("),")]
    assert "stream=True" in kb_decl, \
        "the knowledge base load does not stream, so its narration is captured and lost"
    print("  the load names every table, announces it before writing, and streams")


if __name__ == "__main__":
    test_loader_targets_kb_database_not_analytics_database()
    test_main_wires_kb_database_flag()
    test_path1_honours_kb_naming_flags()
    test_merge_uses_a_values_list_not_a_union_all_chain()
    test_variant_and_array_columns_never_appear_inside_the_values_clause()
    test_an_unrecognised_expression_falls_back_instead_of_emitting_bad_sql()
    test_statements_are_never_reparsed_out_of_a_joined_string()
    test_both_runners_share_an_interface_and_the_cli_remains_a_fallback()
    test_reported_row_counts_cover_every_batch_not_just_the_last()
    test_the_knowledge_base_load_narrates_itself()
