#!/usr/bin/env python3
"""Measure whether two composed dashboards are the same shape.

The skill generates pages agentically rather than copying them, which is the right
tradeoff for adapting to an unfamiliar model but introduces run-to-run variance. The
claim that a fixed input converges on a fixed layout is testable, so it should be
tested rather than asserted: fingerprint the reference app, fingerprint each generated
run, and diff.

A fingerprint is deliberately shallow. It records which components appear in which
view and how many times -- the things a viewer notices -- and ignores props, ordering
within a view, whitespace and naming. Two runs that pick the same charts for the same
views and wire different column labels into them look the same on a screen, and a
metric that flagged that as a difference would be noise.

Usage:

    # Establish the reference
    python3 tests/composition_fingerprint.py emit \\
        --kind react --path <app>/components/report.tsx -o reference.react.json

    # Fingerprint a generated run and compare
    python3 tests/composition_fingerprint.py emit \\
        --kind react --path <generated>/report.tsx -o run1.json
    python3 tests/composition_fingerprint.py compare reference.react.json run1.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

# Only components from the locked libraries count. A generated page that renders a
# bare <div> for layout is not making a visual choice; one that renders <Pareto> is.
REACT_COMPONENTS = {
    "Sparkline", "RankedBar", "TrendLines", "AreaGap", "Waterfall", "Heatmap",
    "Treemap", "Sunburst", "MixBar", "Pareto", "CompareBars", "Kpi",
}
STREAMLIT_CHARTS = {
    "ranked_bar", "trend", "grid", "area_gap", "waterfall", "heatmap",
    "treemap", "sunburst", "mix_bar", "pareto", "kpi_card",
}


def fingerprint_react(src: str) -> dict[str, dict[str, int]]:
    """Split on `view === "Name"` guards, then count library components in each block.

    Anything before the first guard is shared chrome -- the KPI band lives there in the
    reference app -- so it is recorded under a `_shared` key rather than discarded.
    """
    guards = [(m.start(), m.group(1)) for m in re.finditer(r'view === "([^"]+)"', src)]
    # Dedupe: the reference app names each view twice, once to gate a fetch and once to
    # gate the render. Keep the last occurrence of each, which is the render block.
    last: dict[str, int] = {}
    for pos, name in guards:
        last[name] = pos
    ordered = sorted(last.items(), key=lambda kv: kv[1])

    out: dict[str, dict[str, int]] = {}
    first = ordered[0][1] if ordered else len(src)
    shared = _count_react(src[:first])
    if shared:
        out["_shared"] = shared
    for i, (name, pos) in enumerate(ordered):
        end = ordered[i + 1][1] if i + 1 < len(ordered) else len(src)
        # Views with no library component are kept deliberately. The reference app's
        # "Fiscal calendar" renders a bespoke panel rather than a chart, and dropping it
        # would understate the view count and skew the view-overlap score.
        out[name] = _count_react(src[pos:end])
    return out


def _count_react(block: str) -> dict[str, int]:
    found: dict[str, int] = {}
    for m in re.finditer(r"<([A-Z]\w*)", block):
        n = m.group(1)
        if n in REACT_COMPONENTS:
            found[n] = found.get(n, 0) + 1
    return dict(sorted(found.items()))


def fingerprint_streamlit(src: str) -> dict[str, dict[str, int]]:
    """Split on `def page_*` and count chart calls in each page function."""
    defs = [(m.start(), m.group(1)) for m in re.finditer(r"^def (page_\w+)", src, re.M)]
    out: dict[str, dict[str, int]] = {}
    for i, (pos, name) in enumerate(defs):
        end = defs[i + 1][0] if i + 1 < len(defs) else len(src)
        block = src[pos:end]
        found: dict[str, int] = {}
        for fn in STREAMLIT_CHARTS:
            # Word-boundary call sites only, so `waterfall` in a comment or in
            # `waterfall_note` does not count.
            n = len(re.findall(rf"\b{fn}\s*\(", block))
            if n:
                found[fn] = n
        # Kept even when empty, for the same reason as the React side: a page that
        # renders prose or a bespoke panel is still a page, and dropping it understates
        # the view count.
        out[name] = dict(sorted(found.items()))
    return out


def _snake(name: str) -> str:
    """Canonical form, so a React name and a Streamlit name compare equal.

    `RankedBar` and `ranked_bar` are the same shape; `Overview` and `page_overview` are
    the same view. Without this, a cross-surface comparison scores 0% on naming alone
    and the parity claim between the two apps cannot be checked at all.
    """
    n = re.sub(r"^page_", "", name.strip())
    n = re.sub(r"[^A-Za-z0-9]+", "_", n)
    n = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", n)
    n = n.strip("_").lower()
    # Surface-specific synonyms for the same rendered thing.
    return {"kpi": "kpi_card", "trend_lines": "trend", "compare_bars": "compare_bars",
            "mix_seasonality": "mix", "lineage": "provenance"}.get(n, n)


def normalise(d: dict) -> dict:
    out: dict[str, dict[str, int]] = {}
    for view, comps in d.items():
        v = _snake(view)
        bucket = out.setdefault(v, {})
        for c, n in comps.items():
            k = _snake(c)
            bucket[k] = bucket.get(k, 0) + n
    return out


def compare(a: dict, b: dict) -> dict:
    """Jaccard on (view, component) pairs, plus the view-set overlap.

    Counts are compared as min/max rather than equality: two Treemaps against three is a
    smaller difference than a Treemap against none, and a hard equality test would score
    those the same.
    """
    def pairs(d: dict) -> dict[tuple[str, str], int]:
        return {(v, c): n for v, comps in d.items() for c, n in comps.items()}

    pa, pb = pairs(a), pairs(b)
    keys = set(pa) | set(pb)
    if not keys:
        return {"similarity": 1.0, "views": 1.0, "shared": 0, "only_a": [], "only_b": [], "count_diffs": []}

    overlap = 0.0
    count_diffs = []
    for k in keys:
        na, nb = pa.get(k, 0), pb.get(k, 0)
        if na and nb:
            overlap += min(na, nb) / max(na, nb)
            if na != nb:
                count_diffs.append({"view": k[0], "component": k[1], "a": na, "b": nb})

    va, vb = set(a), set(b)
    return {
        "similarity": round(overlap / len(keys), 4),
        "views": round(len(va & vb) / len(va | vb), 4) if (va | vb) else 1.0,
        "shared": int(sum(1 for k in keys if pa.get(k) and pb.get(k))),
        "only_a": sorted(f"{v}:{c}" for v, c in set(pa) - set(pb)),
        "only_b": sorted(f"{v}:{c}" for v, c in set(pb) - set(pa)),
        "count_diffs": count_diffs,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("emit", help="Write a fingerprint for one page file")
    e.add_argument("--kind", required=True, choices=["react", "streamlit"])
    e.add_argument("--path", required=True)
    e.add_argument("-o", "--output")

    c = sub.add_parser("compare", help="Compare two fingerprints")
    c.add_argument("a")
    c.add_argument("b")
    c.add_argument("--threshold", type=float, default=0.90,
                   help="Fail below this similarity (default 0.90)")
    c.add_argument("--normalise", action="store_true",
                   help="Canonicalise view and component names first. Required when "
                        "comparing a React fingerprint against a Streamlit one.")

    a = ap.parse_args(argv)

    if a.cmd == "emit":
        src = pathlib.Path(a.path).read_text()
        fp = fingerprint_react(src) if a.kind == "react" else fingerprint_streamlit(src)
        blob = {"kind": a.kind, "source": a.path, "composition": fp,
                "totals": {"views": len(fp),
                           "components": sum(sum(v.values()) for v in fp.values()),
                           "distinct": len({c for v in fp.values() for c in v})}}
        text = json.dumps(blob, indent=2)
        if a.output:
            pathlib.Path(a.output).write_text(text + "\n")
            print(f"{a.output}: {blob['totals']['views']} views, "
                  f"{blob['totals']['components']} components, "
                  f"{blob['totals']['distinct']} distinct shapes")
        else:
            print(text)
        return 0

    fa = json.loads(pathlib.Path(a.a).read_text())
    fb = json.loads(pathlib.Path(a.b).read_text())
    ca, cb = fa["composition"], fb["composition"]
    if a.normalise or fa.get("kind") != fb.get("kind"):
        # Auto-normalise across surfaces: comparing react against streamlit without it
        # reports 0% on naming alone, which is a misleading answer rather than a useful one.
        ca, cb = normalise(ca), normalise(cb)
        print("(names normalised)")
    r = compare(ca, cb)
    print(f"similarity   {r['similarity']:.1%}")
    print(f"view overlap {r['views']:.1%}")
    print(f"shared       {r['shared']} (view, component) pairs")
    for label, items in (("only in A", r["only_a"]), ("only in B", r["only_b"])):
        if items:
            print(f"{label}: {', '.join(items)}")
    for d in r["count_diffs"]:
        print(f"  count differs {d['view']}/{d['component']}: {d['a']} vs {d['b']}")
    ok = r["similarity"] >= a.threshold
    print(f"\n{'PASS' if ok else 'FAIL'} against threshold {a.threshold:.0%}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
