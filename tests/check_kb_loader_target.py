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
        "run_file": lambda self, sql, label: statements.append(sql)
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


if __name__ == "__main__":
    test_loader_targets_kb_database_not_analytics_database()
    test_main_wires_kb_database_flag()
    test_path1_honours_kb_naming_flags()
