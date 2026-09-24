#!/usr/bin/env python3
"""Regression guards for the bugs found in a full end-to-end debug run.

Every check here corresponds to a failure that reported "ok" or reached
Snowflake before anyone noticed. They are static and cheap on purpose: each one
would have caught its bug before a phase ran, and several of these bugs only
surfaced at phase 10 or 15 of 18.

Run from the repo root.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPE = os.path.join(ROOT, "pipeline")

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


def src(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def defined_names(tree: ast.AST) -> set[str]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            out.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.arg):
            out.add(node.arg)
        elif isinstance(node, ast.Global):
            out.update(node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            out.add(node.name)
    return out


def test_no_undefined_module_names() -> None:
    """Catch the NameError class: naming/kb/db/extra referenced but never bound.

    Four separate phases shipped this exact bug (generate_physical_layer's
    `extra`, kb_to_inventory's `naming`, path3's `naming`), each firing minutes
    into a build.
    """
    print("undefined-name sweep across pipeline/")
    builtins = set(dir(__builtins__)) | {"__file__", "__name__", "__doc__"}
    for name in sorted(os.listdir(PIPE)):
        if not name.endswith(".py"):
            continue
        tree = ast.parse(src("pipeline", name), name)
        bound = defined_names(tree) | builtins
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        missing = sorted(used - bound)
        check(f"{name} binds every name it reads",
              not missing,
              "" if not missing else "unbound: " + ", ".join(missing))


def test_sql_templates_are_fstrings() -> None:
    """A {placeholder} in a plain string reaches Snowflake as literal text.

    kb_to_inventory had three of these and verify_deployment had one. They are
    indistinguishable from working code by eye.
    """
    print("SQL strings containing {placeholders} are f-strings")
    # Single braces only. A {{DB}} is the render_sql placeholder convention and
    # belongs in a plain string -- it is substituted by config.render_sql, not by
    # Python, which is the whole reason the convention uses doubled braces.
    pat = re.compile(r"(?<!\{)\{(kb|db|semantic_view|ANALYTICS|DB|scope_list|names)\}(?!\})")
    for name in sorted(os.listdir(PIPE)):
        if not name.endswith(".py"):
            continue
        tree = ast.parse(src("pipeline", name), name)
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if pat.search(node.value) and "SELECT" in node.value.upper():
                    bad.append(node.lineno)
        check(f"{name} has no un-interpolated SQL template",
              not bad, "" if not bad else "plain strings at line(s) " + str(bad))


def test_no_module_level_fstring_constants() -> None:
    """A module-level f-string is a frozen snapshot, not a template.

    TAG_DDL/GLOSSARY_DDL/GLOSSARY_LOAD baked in the default KB name at import,
    so rebinding the KB global in main() changed nothing -- and the first fix
    looked complete while half the file stayed broken.
    """
    print("no module-level f-string constants referencing templated globals")
    for name in sorted(os.listdir(PIPE)):
        if not name.endswith(".py"):
            continue
        tree = ast.parse(src("pipeline", name), name)
        bad = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.JoinedStr):
                names = {n.id for n in ast.walk(node.value)
                         if isinstance(n, ast.Name)}
                if names & {"KB", "DB", "ANALYTICS"}:
                    bad.append(node.lineno)
        check(f"{name} has no frozen f-string constant",
              not bad, "" if not bad else "line(s) " + str(bad))


def test_kb_global_is_rebound_where_declared() -> None:
    """Declaring KB = config.DEFAULT.kb without a rebind silently ignores --kb-database."""
    print("every module declaring a KB global rebinds it from the flags")
    for name in sorted(os.listdir(PIPE)):
        if not name.endswith(".py"):
            continue
        text = src("pipeline", name)
        if not re.search(r"^KB = config\.DEFAULT\.kb", text, re.M):
            continue
        # An AST check, not a substring: "global KB" also appears in the comment
        # explaining why the rebind is needed, so grepping for it passes against
        # the very bug it is meant to catch.
        tree = ast.parse(text, name)
        declared = any(isinstance(n, ast.Global) and "KB" in n.names
                       for n in ast.walk(tree))
        # Assigned somewhere inside a function body, which is the actual
        # requirement. Matching the right-hand side instead is too narrow:
        # KB = config.from_args(args).kb and KB = naming.kb are equivalent.
        assigned = any(
            any(isinstance(t, ast.Name) and t.id == "KB"
                for n in ast.walk(fn) if isinstance(n, ast.Assign)
                for t in n.targets)
            for fn in ast.walk(tree)
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        check(f"{name} rebinds KB in main()", declared and assigned,
              f"global={declared} assigned-in-function={assigned}")


def test_semantic_view_name_is_single_source() -> None:
    """The OSSIE document's name: field IS the created object.

    If it disagrees with Naming.semantic_view, the agent, the search service and
    every RPT_ view reference an object that was never created -- and none of
    that DDL validates the reference at creation time, so the build reports ok.
    """
    print("semantic view name comes from Naming, not from model_name directly")
    p3 = src("pipeline", "path3_ossie_semantic_view.py")
    check("path3 names the view from naming.semantic_view",
          "view_name = naming.semantic_view" in p3)
    check("path3 no longer passes args.model_name as the view name",
          "build_ossie_model(args.connection, args.database, args.model_name)" not in p3)
    check("path3 defaults --target-schema instead of sending None",
          "args.target_schema = naming.analytics" in p3)


def test_bridge_runs_after_the_semantic_view() -> None:
    """The bridge reads SEMANTIC_METRICS, so it must follow p3 and precede p2."""
    print("phase order: inventory bridge sits between path 3 and path 2")
    out = subprocess.run([sys.executable, "pipeline/build.py", "--paths", "4",
                          "--dry-run"], capture_output=True, text=True, cwd=ROOT)
    check("dry run succeeds", out.returncode == 0, out.stderr.strip()[-200:])
    lines = [l for l in out.stdout.splitlines() if re.match(r"\s+\d+\. \[", l)]

    def index_of(needle: str) -> int:
        for i, l in enumerate(lines):
            if needle in l:
                return i
        return -1

    bridge = index_of("app inventory")
    ossie = index_of("Ossie semantic view")
    rpt = index_of("Reporting views")
    check("bridge appears in the plan", bridge >= 0)
    check("bridge runs after the Ossie semantic view",
          ossie >= 0 and bridge > ossie, f"ossie={ossie} bridge={bridge}")
    check("bridge runs before the reporting views",
          rpt >= 0 and bridge < rpt, f"bridge={bridge} rpt={rpt}")


def test_row_access_policy_is_idempotent() -> None:
    """CREATE OR REPLACE on an attached policy is refused, so run two fails."""
    print("row access policy phase can run twice")
    rls = src("pipeline", "sql", "30_row_access_policy.sql")
    # Statements only. The comment above the fix names the same syntax, so a
    # substring search over the whole file passes even with the detach deleted.
    statements = "\n".join(l for l in rls.splitlines()
                          if not l.lstrip().startswith("--"))
    check("detaches before replacing",
          "DROP ALL ROW ACCESS POLICIES" in statements)
    check("detach precedes the CREATE OR REPLACE",
          "DROP ALL ROW ACCESS POLICIES" in statements
          and "CREATE OR REPLACE ROW ACCESS POLICY" in statements
          and statements.index("DROP ALL ROW ACCESS POLICIES")
          < statements.index("CREATE OR REPLACE ROW ACCESS POLICY"))
    check("demo principals are deleted before insert",
          "DELETE FROM" in statements and "LOAD_ID = 'demo-principals'" in statements)


def test_verify_deployment_honours_deploy_mode() -> None:
    """A --deploy none build creates no Streamlit object; probing it raises."""
    print("verify_deployment knows what was actually deployed")
    v = src("pipeline", "verify_deployment.py")
    check("accepts a --deploy flag", '"--deploy"' in v)
    check("Streamlit check is gated", "if WANT_STREAMLIT:" in v)
    check("React checks are gated", "if WANT_REACT:" in v)
    check("skips are reported", "skipped.append" in v and "SKIP" in v)
    check("no hardcoded model-specific figure",
          "37,711,379" not in v)
    check("no hardcoded metric count",
          "n == 98" not in v)


def test_core_columns_are_generated() -> None:
    """Nothing produced out/core_columns.json; main() just assumed it existed."""
    print("physical layer can build without a manual pre-step")
    g = src("pipeline", "generate_physical_layer.py")
    check("has a core-columns generator", "def fetch_core_columns" in g)
    check("generates the file when absent",
          "if not os.path.exists(args.columns)" in g)
    check("date DDL goes through placeholder rendering",
          "config.render_sql(data_date_dimension()" in g)
    check("no stale `extra` reference", not re.search(r"\+ extra\b", g))


def test_drill_paths_are_scoped() -> None:
    """Unscoped, this returns every named set in the whole source model."""
    print("drill paths are scoped to the in-scope entities")
    k = src("pipeline", "kb_to_inventory.py")
    check("hierarchy query filters on SOURCE_ENTITY",
          "l.SOURCE_ENTITY IN ({scope_list})" in k)
    check("each drill path carries its entity",
          '"source_entity": e' in k)


def test_charts_library_hardening() -> None:
    """Decimal * float raises, and grid() was the one function without a key."""
    print("chart library survives Decimal measures and accepts a key")
    c = src("assets", "streamlit_ui", "charts.py")
    check("has a numeric coercion helper", "def _num(" in c)
    check("no bare Decimal arithmetic on peak",
          not re.search(r"peak = max\((?!.*_num)", c))
    check("grid() accepts a key", re.search(r"def grid\([^)]*key:", c, re.S) is not None)
    check("download key uses it", 'key=f"dl_{key or title}"' in c)


def test_composition_rules_cover_the_silent_defects() -> None:
    print("composition rules encode the silent defects")
    r = src("references", "composition-rules.md")
    for phrase, why in [
        ("(unattributed)", "null bucket labelling"),
        ("cardinality", "constant-dimension axis"),
        ("title against the props", "unsupported titles"),
        ("paramstyle", "qmark placeholder trap"),
        ("Tie one chart total back to the KPI band", "KPI tie-out"),
    ]:
        check(f"documents {why}", phrase in r)


def test_dependencies_are_declared_and_asserted() -> None:
    """Ordering used to live only in list position, and that record was wrong.

    check_order() turns the dependency graph into something reviewable before a
    build starts, rather than something you infer from a printed plan.
    """
    print("phase dependencies are declared and checked at plan time")
    sys.path.insert(0, PIPE)
    try:
        import build as build_mod
    finally:
        sys.path.pop(0)

    check("Phase carries depends_on", "depends_on" in build_mod.Phase.__annotations__)
    check("build preflights the order", "check_order(plan)" in src("pipeline", "build.py"))

    for paths in ({1}, {2}, {3}, {4}, {1, 4}, {2, 3}):
        plan = build_mod.resolve(set(paths), False, True)
        problems = build_mod.check_order(plan)
        check(f"--paths {','.join(map(str, sorted(paths)))} satisfies its dependencies",
              not problems, "; ".join(problems))

    # And that the check is not vacuous.
    plan = build_mod.resolve({4}, False, True)
    bridge = next(p for p in plan if p.key == "bridge")
    plan.remove(bridge)
    plan.insert(0, bridge)
    check("check_order catches the bridge running too early",
          bool(build_mod.check_order(plan)))


def main() -> int:
    for fn in [
        test_no_undefined_module_names,
        test_sql_templates_are_fstrings,
        test_no_module_level_fstring_constants,
        test_kb_global_is_rebound_where_declared,
        test_semantic_view_name_is_single_source,
        test_bridge_runs_after_the_semantic_view,
        test_dependencies_are_declared_and_asserted,
        test_row_access_policy_is_idempotent,
        test_verify_deployment_honours_deploy_mode,
        test_core_columns_are_generated,
        test_drill_paths_are_scoped,
        test_charts_library_hardening,
        test_composition_rules_cover_the_silent_defects,
    ]:
        fn()
    print()
    if failures:
        print("%d check(s) failed:" % len(failures))
        for f in failures:
            print("  - " + f)
        return 1
    print("all regression guards passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
