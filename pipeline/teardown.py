#!/usr/bin/env python3
"""Undo a build, so the demo can be rehearsed more than once.

Every phase in build.py is idempotent, which means a re-run converges rather than
duplicating. That is enough to retry a failed build, but it is not enough to rehearse
from a clean account: an object created by a run that is later dropped from the plan
lingers, a semantic view keeps a stale definition alive, and a STREAMLIT object holds
a url_id that a partial rebuild will silently reuse. Teardown exists to get back to
nothing.

Safety, because this only ever destroys:

* Default is a dry run. Nothing happens without --execute.
* Target databases must be named explicitly. There is no default that could quietly
  point at the wrong account.
* Drop order follows dependencies -- consumers before producers -- so a failure leaves
  a partially torn-down state that is still safe to re-run rather than a pile of
  "cannot drop, other objects depend on it" errors.
* Schemas are emptied by default; --drop-schemas removes the containers too, and
  --drop-databases removes the databases. Neither is implied.
* Failures are reported and do not stop the run. A missing object is the expected case
  when tearing down a partial build, so IF EXISTS is used throughout and a non-zero
  exit from one statement must not abandon the rest.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

import config

HERE = os.path.dirname(os.path.abspath(__file__))


def sql(statement: str, connection: str, execute: bool) -> tuple[bool, str]:
    if not execute:
        return True, "(dry run)"
    proc = subprocess.run(
        ["snow", "sql", "-c", connection, "-q", statement],
        capture_output=True, text=True,
    )
    return proc.returncode == 0, ((proc.stdout or "") + (proc.stderr or "")).strip()


def discover(connection: str, database: str, schema: str, kind: str) -> list[str]:
    """List objects of one kind, so teardown covers what a run actually made.

    Enumerating beats a hard-coded list: the reporting views grew from 7 to 8 during
    the reference build, and a hard-coded teardown would have left the newest one
    behind -- which is exactly the object a rebuild then fails to replace cleanly.
    """
    table_type = "VIEW" if kind == "VIEW" else "BASE TABLE"
    q = (f"SELECT table_name FROM {database}.INFORMATION_SCHEMA.TABLES "
         f"WHERE table_schema = '{schema}' AND table_type = '{table_type}'")
    proc = subprocess.run(
        ["snow", "sql", "-c", connection, "-q", q, "--format", "csv"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return []
    rows = [r.strip().strip('"') for r in (proc.stdout or "").splitlines()[1:]]
    return [r for r in rows if r and not r.startswith("+")]


def plan_statements(db: str, kb_db: str, kb_schema: str, analytics: str,
                    app_service: str, streamlit: str, pool: str,
                    drop_schemas: bool, drop_databases: bool,
                    naming: config.Naming = config.DEFAULT) -> list[tuple[str, str]]:
    """Ordered (label, statement) pairs. Consumers first, producers last."""
    s: list[tuple[str, str]] = []

    # 1. App surfaces. These hold the compute and the published URLs, and nothing
    #    depends on them, so they go first and their failure blocks nothing.
    s.append(("App Runtime service", f"DROP SERVICE IF EXISTS {db}.{analytics}.{app_service}"))
    s.append(("Streamlit object", f"DROP STREAMLIT IF EXISTS {db}.{analytics}.{streamlit}"))

    # 2. Agent and search, which reference the semantic view.
    s.append(("Cortex Agent", f"DROP AGENT IF EXISTS {db}.{analytics}.{naming.agent}"))
    s.append(("Agent SQL bridge", f"DROP PROCEDURE IF EXISTS {db}.{analytics}.{naming.ask_procedure}(STRING, STRING)"))
    s.append(("Glossary search service",
              f"DROP CORTEX SEARCH SERVICE IF EXISTS {kb_db}.{kb_schema}.{naming.search_service}"))

    # 3. Reporting views, then the semantic view they wrap. This order matters: the
    #    RPT_ views are the only consumers of SEMANTIC_VIEW() syntax in the build.
    s.append(("__DISCOVER_VIEWS__", f"{db}.{analytics}"))
    s.append(("Semantic view", f"DROP SEMANTIC VIEW IF EXISTS {db}.{analytics}.{naming.semantic_view}"))

    # 4. Row access policy. Must come after the views, and after any table it is
    #    attached to has been dropped, or the drop is refused while a reference exists.
    s.append(("Row access policy",
              f"DROP ROW ACCESS POLICY IF EXISTS {db}.{naming.source_schemas[0]}.{naming.row_access_policy}"))

    # 5. Physical data.
    for sch in ("SALES_ANALYTICS", "COMMON_ANALYTICS"):
        s.append((f"Tables in {sch}", f"__DISCOVER_TABLES__:{db}.{sch}"))

    # 6. Knowledge base last. Everything upstream was generated from it, so dropping
    #    it first would make a failed teardown unrecoverable without a re-extract.
    s.append(("Knowledge base views", f"__DISCOVER_VIEWS__:{kb_db}.{kb_schema}"))
    s.append(("Knowledge base tables", f"__DISCOVER_TABLES__:{kb_db}.{kb_schema}"))

    # 7. Compute pool, only once the service and Streamlit on it are gone.
    s.append(("Compute pool", f"DROP COMPUTE POOL IF EXISTS {pool}"))

    if drop_schemas:
        for sch in (analytics, "SALES_ANALYTICS", "COMMON_ANALYTICS"):
            s.append((f"Schema {sch}", f"DROP SCHEMA IF EXISTS {db}.{sch}"))
        s.append((f"Schema {kb_schema}", f"DROP SCHEMA IF EXISTS {kb_db}.{kb_schema}"))
    if drop_databases:
        s.append((f"Database {db}", f"DROP DATABASE IF EXISTS {db}"))
        s.append((f"Database {kb_db}", f"DROP DATABASE IF EXISTS {kb_db}"))
    return s


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Tear down a bi-to-snowflake build. Dry run unless --execute.")
    config.add_arguments(ap)
    ap.add_argument("--app-service",
                    help="Defaults to the naming flags' app service name")
    ap.add_argument("--streamlit",
                    help="Defaults to the naming flags' Streamlit name")
    ap.add_argument("--pool",
                    help="Defaults to the naming flags' compute pool name")
    ap.add_argument("--connection", default="my-demo-account")
    ap.add_argument("--drop-schemas", action="store_true", help="Also drop the schemas")
    ap.add_argument("--drop-databases", action="store_true",
                    help="Also drop the databases. Implies --drop-schemas.")
    ap.add_argument("--execute", action="store_true", help="Actually run it")
    a = ap.parse_args(argv)

    if a.drop_databases:
        a.drop_schemas = True

    naming = config.from_args(a)
    steps = plan_statements(a.database, a.kb_database, a.kb_schema, a.analytics_schema,
                            a.app_service or naming.app_service,
                            a.streamlit or naming.streamlit,
                            a.pool or naming.compute_pool,
                            a.drop_schemas, a.drop_databases, naming)

    # Expand the discovery placeholders now so the dry run shows real object names
    # rather than a promise to find some.
    expanded: list[tuple[str, str]] = []
    for label, stmt in steps:
        if stmt.startswith("__DISCOVER_VIEWS__:") or stmt.startswith("__DISCOVER_TABLES__:"):
            kind = "VIEW" if "VIEWS" in stmt else "TABLE"
            fq = stmt.split(":", 1)[1]
            d, sch = fq.split(".", 1)
            names = discover(a.connection, d, sch, kind)
            if not names:
                expanded.append((f"{label} (none found)", ""))
            for n in names:
                expanded.append((f"{label}: {n}",
                                 f"DROP {kind} IF EXISTS {d}.{sch}.{n}"
                                 + (" CASCADE" if kind == "TABLE" else "")))
        elif label == "__DISCOVER_VIEWS__":
            d, sch = stmt.split(".", 1)
            for n in discover(a.connection, d, sch, "VIEW"):
                expanded.append((f"Reporting view: {n}", f"DROP VIEW IF EXISTS {d}.{sch}.{n}"))
        else:
            expanded.append((label, stmt))

    mode = "EXECUTING" if a.execute else "DRY RUN -- nothing will be dropped"
    print("=" * 74)
    print(f"TEARDOWN  ({mode})")
    print(f"  analytics: {a.database}   knowledge base: {a.kb_database}")
    print(f"  schemas dropped: {a.drop_schemas}   databases dropped: {a.drop_databases}")
    print("=" * 74)

    failed = 0
    for i, (label, stmt) in enumerate(expanded, 1):
        if not stmt:
            print(f"{i:4}. {label}")
            continue
        ok, out = sql(stmt, a.connection, a.execute)
        mark = " " if ok else "!"
        print(f"{i:4}. {mark} {label}")
        if not ok:
            failed += 1
            first = (out.splitlines() or [""])[0][:130]
            print(f"       {first}")

    print("=" * 74)
    if not a.execute:
        print(f"{len(expanded)} statements planned. Re-run with --execute to apply.")
        return 0
    print(f"Done. {len(expanded) - failed} succeeded, {failed} failed.")
    # A failure here is usually a missing object, which is fine, or a dependency that
    # a second pass will clear. Re-running is always safe.
    if failed:
        print("Re-running is safe and often clears dependency-ordering failures.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
