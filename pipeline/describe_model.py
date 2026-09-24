#!/usr/bin/env python3
"""Write a human-readable digest of the parsed BI model.

Runs straight after the parse and before the knowledge base load, which is the
longest phase in the build. That ordering is the whole point: the parse takes about
two seconds and the load about two minutes, so there is a window where the user has
nothing to do and everything interesting is already known. Rather than leave them
watching a progress log, hand them something worth reading about their own model.

Everything here is derived from the inventory. Nothing is inferred, estimated or
rounded up: if a number is not in the parse output it does not appear. A digest that
flatters the model would be worse than none, because the first thing it is used for
is deciding what to migrate.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

log = logging.getLogger("describe_model")

# Sections are suppressed rather than shown empty. A Framework Manager model has no
# dashboards, and a heading reading "Dashboards: 0" invites the reader to think
# something failed when in fact that content lives in the Content Store and was
# never in this file. Silence is more honest than a zero.
MIN_TO_REPORT = 1


def _fmt(n: int) -> str:
    return format(n, ",")


def _counts(inv: dict) -> list[tuple[str, int, str]]:
    """The scale table: label, count, and what the thing actually is."""
    rows = [
        ("Tables and views", len(inv.get("tables") or []),
         "query subjects the model exposed to report authors"),
        ("Fields", len(inv.get("dimensions") or []),
         "every column, with its datatype and usage"),
        ("Measures", len(inv.get("metrics") or []),
         "numeric facts, with the aggregation each is allowed to use"),
        ("Facts", len(inv.get("facts") or []),
         "measure/table pairings"),
        ("Joins", len(inv.get("relationships") or []),
         "with cardinality, so fan-out can be detected"),
        ("Drill paths", len(inv.get("hierarchies") or []),
         "hierarchies such as territory or product"),
        ("Declared grains", len(inv.get("grain_declarations") or []),
         "determinants -- what makes a fact row unique"),
        ("Filters", len(inv.get("filters") or []),
         "reusable filter definitions"),
        ("Security filters", len(inv.get("security_rules") or []),
         "row-level rules bound to groups"),
    ]
    return [r for r in rows if r[1] >= MIN_TO_REPORT]


def _where_the_data_lives(sa: dict) -> list[str]:
    out: list[str] = []
    sources = ((sa.get("data_sources") or {}).get("sources")) or []
    if not sources:
        return out
    on_sf = [s for s in sources if (s.get("platform") or "").lower() == "snowflake"]
    out.append("## Where the data already lives\n")
    if on_sf and len(on_sf) == len(sources):
        out.append(
            "Every data source in this model already points at Snowflake. The tables are "
            "where they need to be; what is being rebuilt is the **meaning** layered over "
            "them -- the names, the formulas, the joins and the access rules.\n")
    elif on_sf:
        out.append(
            "%d of %d data sources already point at Snowflake.\n"
            % (len(on_sf), len(sources)))
    out.append("| Data source | Platform | Query subjects using it |")
    out.append("|---|---|---:|")
    for s in sorted(sources, key=lambda x: -(x.get("query_subjects_using") or 0)):
        out.append("| `%s` | %s | %s |" % (
            s.get("name", "?"), s.get("platform") or "not declared",
            _fmt(s.get("query_subjects_using") or 0)))
    out.append("")
    return out


def _what_repeats(clones: dict) -> list[str]:
    out: list[str] = []
    fams = clones.get("fiscal_year_object_families") or {}
    cloned = clones.get("fiscal_year_cloned_objects") or 0
    calc_total = clones.get("calculation_total") or 0
    ratio = clones.get("calculation_clone_ratio")
    pkg_total = clones.get("package_total") or 0
    pkg_role = clones.get("package_role_based") or 0
    if not (fams or cloned or calc_total or pkg_total):
        return out

    out.append("## What repeats\n")
    out.append(
        "Models of this age accumulate copies. These are counts, not criticism -- "
        "they are the clearest measure of how much of the model is one idea stated "
        "many times, and therefore how much simpler the Snowflake version can be.\n")
    if fams and cloned:
        # The most-cloned family, tie-broken by the longer name, which in practice
        # picks something recognisable like FACT_SALES_SUMMARY over a two-letter
        # abbreviation. Alphabetical order picked "AM", which illustrates nothing.
        example = max(fams.items(), key=lambda kv: (len(kv[1]), len(kv[0])), default=None)
        out.append(
            "- **%s objects are fiscal-year copies**, across %d families. "
            "A family is one logical object cloned per year."
            % (_fmt(cloned), len(fams)))
        if example:
            name, years = example
            out.append("  For example `%s` exists as %s."
                       % (name, ", ".join("`%s`" % y for y in sorted(years))))
        out.append(
            "  In Snowflake these collapse into one object with a fiscal-year column.")
    if calc_total and ratio:
        canonical = clones.get("calculation_canonical_patterns")
        tail = (" -- roughly %s distinct patterns" % _fmt(canonical)) if canonical else ""
        out.append(
            "- **%s calculations, at a clone ratio of %s**%s. The same expression "
            "restated per year or per territory." % (_fmt(calc_total), ratio, tail))
    if pkg_total:
        share = " (%d of them role-based)" % pkg_role if pkg_role else ""
        out.append(
            "- **%s packages**%s. Packages are what report authors actually built "
            "against, so this is the best available proxy for how many reports exist."
            % (_fmt(pkg_total), share))
    out.append("")
    return out


def _how_access_works(sa: dict) -> list[str]:
    ss = sa.get("security_summary") or {}
    total = ss.get("filter_total") or 0
    shapes = ss.get("shapes") or []
    if not (total and shapes):
        return []
    out = ["## How access is controlled\n"]
    out.append(
        "%s row-level security filters, for %s distinct groups. That sounds like a "
        "migration in itself, but they reduce to **%d shapes** -- the filters differ "
        "only in the values they compare against:\n"
        % (_fmt(total), _fmt(ss.get("distinct_principals") or 0), len(shapes)))
    out.append("| Filtered on | Filters |")
    out.append("|---|---:|")
    for s in shapes:
        out.append("| %s | %s |" % (
            ", ".join("`%s`" % c for c in s.get("columns") or []),
            _fmt(s.get("filter_count") or 0)))
    out.append("")
    out.append(
        "One row access policy per shape reproduces all %s, driven by a mapping table "
        "rather than %s hand-written predicates.\n" % (_fmt(total), _fmt(total)))
    return out


def _needs_a_look(inv: dict) -> list[str]:
    flagged = inv.get("flagged") or []
    comp = inv.get("complexity_summary") or {}
    if not (flagged or comp):
        return []
    out = ["## What needs a human eye\n"]
    if comp:
        simple = comp.get("simple") or 0
        trans = comp.get("needs_translation") or 0
        manual = comp.get("manual_required") or 0
        total = simple + trans + manual
        if total:
            out.append(
                "Of %s expressions: **%s translate directly**, %s need translation, "
                "%s need a person.\n" % (_fmt(total), _fmt(simple), _fmt(trans), _fmt(manual)))
    if flagged:
        out.append(
            "%s items are flagged. These are not errors -- they are places where the "
            "Cognos semantics do not have a single obvious SQL equivalent, so a choice "
            "gets made and should be checked. The most common reasons:\n"
            % _fmt(len(flagged)))
        reasons: dict[str, int] = {}
        for f in flagged:
            r = (f.get("reason") or "unspecified").strip()
            reasons[r] = reasons.get(r, 0) + 1
        for r, n in sorted(reasons.items(), key=lambda kv: -kv[1])[:5]:
            out.append("- **%s x** %s" % (_fmt(n), r[:160]))
        out.append("")
        first = flagged[0]
        if first.get("name"):
            out.append("An example, `%s`:\n" % first["name"])
            expr = (first.get("expression") or "").strip()
            if expr:
                out.append("```")
                out.append(expr[:400] + (" ..." if len(expr) > 400 else ""))
                out.append("```")
            out.append("")
    return out


def render(inv: dict, source_path: str) -> str:
    sa = inv.get("source_analysis") or {}
    clones = sa.get("clones") or {}
    name = sa.get("model_name") or inv.get("source_model") or "your model"
    tool = (inv.get("source_type") or "BI").replace("_", " ").title()

    out: list[str] = []
    out.append("# %s\n" % name)
    out.append(
        "Parsed from `%s` -- a %s model. Nothing has been changed in the source; this "
        "is a read of what is in it.\n"
        % (os.path.basename(source_path) if source_path else "your model file", tool))
    out.append(
        "The knowledge base load is running while you read this. It is the longest step "
        "in the build, and everything below is already in hand, so this is a good moment "
        "to check that what was found matches what you expected.\n")

    errors = inv.get("errors") or []
    if errors:
        out.append("> **%s parse error(s)** were recorded. See the end of this file.\n"
                   % _fmt(len(errors)))

    out.append("## At a glance\n")
    out.append("| | Count | What it is |")
    out.append("|---|---:|---|")
    for label, n, desc in _counts(inv):
        out.append("| %s | %s | %s |" % (label, _fmt(n), desc))
    out.append("")

    out.extend(_where_the_data_lives(sa))
    out.extend(_what_repeats(clones))
    out.extend(_how_access_works(sa))
    out.extend(_needs_a_look(inv))

    if not (inv.get("dashboards") or inv.get("worksheets")):
        out.append("## What is not in this file\n")
        out.append(
            "No reports, charts or layouts. A Framework Manager model is the metadata "
            "layer only -- report specifications live in the Cognos Content Store, not "
            "here. So this build can reproduce every field, formula, join and access "
            "rule, but it cannot know which of them any particular report used. If you "
            "can export the report specifications, that gap closes.\n")

    out.append("## What happens next\n")
    out.append(
        "1. **Knowledge base** -- everything above, written into queryable tables. Running now.\n"
        "2. **Physical layer** -- tables and views generated from those definitions.\n"
        "3. **Semantic view** -- the model a Cortex agent reasons over.\n"
        "4. **Governed views and access policies** -- including the security shapes above.\n"
        "5. **Catalog** -- descriptions and tags on every object.\n"
        "6. **Agent and apps** -- ask questions in English; Streamlit and React front ends.\n")

    if errors:
        out.append("## Parse errors\n")
        for e in errors[:20]:
            out.append("- `%s`" % json.dumps(e)[:220])
        out.append("")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Describe a parsed BI model in Markdown")
    ap.add_argument("--inventory", required=True, help="Unified inventory JSON")
    # The inventory does not record where it came from, and naming the inventory
    # file in a document written for the user is meaningless to them -- they gave us
    # a .zip, not an inv.json.
    ap.add_argument("--source", default="", help="The original BI model file, for the heading")
    ap.add_argument("--out", help="Where to write the digest (default: alongside the inventory)")
    ap.add_argument("--connection", help="Unused; accepted so the build can pass it")
    config.add_arguments(ap)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")

    with open(args.inventory, encoding="utf-8") as fh:
        inv = json.load(fh)

    dest = args.out or os.path.join(os.path.dirname(os.path.abspath(args.inventory)),
                                    "model-overview.md")
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(render(inv, args.source))

    log.info("Wrote a description of your model while the rest of the build runs:")
    log.info("  %s", dest)
    log.info("  Open it now -- the next phase takes about two minutes.")
    print(json.dumps({"status": "ok", "overview": dest}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
