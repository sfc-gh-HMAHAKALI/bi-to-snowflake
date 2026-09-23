#!/usr/bin/env python3
"""Assert both component libraries still expose every shape the rules depend on.

A generated page composes against these names. If a refactor renames or drops one, the
failure surfaces as a broken generated app rather than as a library error, so it is
worth catching here.
"""
from __future__ import annotations

import pathlib
import re
import sys

SHAPES_PY = {"ranked_bar", "trend", "grid", "area_gap", "waterfall", "heatmap",
             "treemap", "sunburst", "mix_bar", "pareto"}
SHAPES_TS = {"RankedBar", "TrendLines", "AreaGap", "Waterfall", "Heatmap",
             "Treemap", "Sunburst", "MixBar", "Pareto", "CompareBars"}
HERE = pathlib.Path(__file__).resolve().parent.parent


def main() -> int:
    sys.path.insert(0, str(HERE))
    from assets.streamlit_ui import charts  # noqa: E402

    have_py = {n for n in SHAPES_PY if callable(getattr(charts, n, None))}
    missing_py = sorted(SHAPES_PY - have_py)

    ts = (HERE / "assets" / "react_ui" / "charts.tsx").read_text()
    have_ts = set(re.findall(r"^export function (\w+)", ts, re.M))
    missing_ts = sorted(SHAPES_TS - have_ts)

    # The KPI card and sparkline are not chart shapes but every generated page uses them.
    for name, src in (("kpi_card", dir(__import__("assets.streamlit_ui", fromlist=["ui"]).ui)),
                      ("Sparkline", have_ts)):
        if name not in src:
            missing_py.append(name) if name.islower() else missing_ts.append(name)

    if missing_py or missing_ts:
        if missing_py:
            print(f"  MISSING streamlit: {missing_py}")
        if missing_ts:
            print(f"  MISSING react: {missing_ts}")
        return 1

    print(f"  streamlit {len(have_py)} shapes, react {len(have_ts)} exports")
    return 0


if __name__ == "__main__":
    sys.exit(main())
