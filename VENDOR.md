# Vendored code: provenance and re-sync

Everything in this file records code that was **copied in** rather than imported, so a
self-contained Cortex Extension share works in any account with no sibling-skill or
git-branch prerequisites. The cost of vendoring is drift. This file is how drift stays
visible.

Vendored on **2026-09-23**.

## modules/ -- BI source parsing

| | |
|---|---|
| From | `semantic-extraction` |
| Commit | `115bf8274895a913cacc45596564577a73610227` (`main`) |
| Repo | https://github.com/sfc-gh-HMAHAKALI/SemanticExtratcor.git |
| What | the whole `modules/` tree: 51 files, 23,850 lines |
| Tests | `modules/cognos/test_cognos.py`, 41 passing at time of vendoring |

The whole tree was taken rather than a hand-picked subset. `modules/cognos/` depends only
on `modules/common/`, so a subset was feasible, but taking the tree keeps imports intact
and brings the other five adapters (Tableau, Power BI, Looker, Denodo, SAP BusinessObjects)
along at no extra cost.

The Cognos adapter was merged from `feat/cognos-adapter` into `main` immediately before
vendoring, specifically so this record points at a released state rather than a branch.
Before that merge, `main` was missing 3,161 lines across 11 files -- not only
`modules/cognos/` but `modules/output/inventory.py` and the `modules/cli.py` wiring.

**Re-sync:** `rsync -a --exclude='__pycache__' --exclude='*.pyc' <se>/modules/ modules/`
then run `python3 -m pytest modules/cognos/test_cognos.py`.

## assets/react_ui/ -- React component library

| | |
|---|---|
| From | A customer engagement's React app (not redistributed) |
| What | `lib/theme.ts` (59 lines), `components/charts.tsx` (891 lines) |
| Upstream | none -- this code was in no skill |

`bi-modernization` ships `react-app/SKILL.md` (instructions) but **no React component
library**, so there was nothing upstream to vendor. These two files are the source of the
visual identity: palette, series order, colour scales, and 14 exports -- `usd`, `pct`,
`count`, `Sparkline`, `RankedBar`, `TrendLines`, `AreaGap`, `Waterfall`, `Heatmap`,
`Treemap`, `Sunburst`, `MixBar`, `Pareto`, `CompareBars`.

`charts.tsx` imports `echarts-for-react` and `@/lib/theme`. A scaffolded app must therefore
place `theme.ts` at `lib/theme.ts` and keep the `@/*` tsconfig path alias, or the import
breaks.

**Re-sync:** this is now the canonical copy. Changes should be made here and propagated
outward, not the reverse.

## assets/streamlit_ui/ -- Streamlit component library

| | |
|---|---|
| From | A customer engagement's Streamlit app (not redistributed) |
| What | `bim_ui/__init__.py` as `ui.py`, `bim_ui/compat.py`, `bim_ui/filters.py`, `metrics.py`, and the chart half of `pages_impl.py` as `charts.py` |

`charts.py` is lines 1-674 of `pages_impl.py` -- everything above `page_overview`. That was
a clean cut: the library half references no page state and issues no queries. Imports were
rewritten from `import bim_ui` / `import metrics` to package-relative form; nothing else
changed. It carries all ten shapes: `ranked_bar`, `trend`, `grid`, `area_gap`, `waterfall`,
`heatmap`, `treemap`, `sunburst`, `mix_bar`, `pareto`.

### Why not `bi-modernization/assets/bim_ui/`

That library was evaluated and **not used**. It is richer -- 13 files, 4,559 lines against
these 613 -- but it is a *different* library, not an older version of this one. All three
shared filenames differ substantially:

| file | upstream | here | differing lines |
|---|---|---|---|
| `__init__.py` | 125 | 227 | 298 |
| `compat.py` | 371 | 204 | 455 |
| `filters.py` | 640 | 182 | 734 |

An earlier draft of the plan described that copy as a "diverged fork" of upstream and
proposed reconciling the two. That was wrong: they are parallel implementations that share
some names. There is nothing to reconcile.

The deciding argument is the requirement. This skill exists to reproduce a specific
dashboard, and that dashboard was built on this code. Mixing two design systems -- two
palettes, two KPI renderers, two filter models -- produces an app that looks like neither.
So the proven code is the basis, and upstream's extra modules (`chart_spec.py`,
`charts_altair.py`, `grid.py`, `provenance.py`, `states.py`) are deliberately left behind
rather than grafted on.

Worth revisiting if upstream's library ever gains something these ten shapes cannot express.

## pipeline/ -- the Snowflake build

| | |
|---|---|
| From | A customer engagement's pipeline (not redistributed) |
| What | `build.py` (phase graph, `--dry-run`, timing instrumentation), `sql/` (20 DDL files), `kb_loader.py`, `kb_to_inventory.py`, `generate_physical_layer.py`, `path1_catalog_glossary.py`, `path3_ossie_semantic_view.py`, `agent_client.py`, `verify_deployment.py` |
| Upstream | none -- untracked, never committed anywhere |

3,741 lines of Python plus the DDL. This is the only copy; it had no version control before
this repo. Carried engagement-specific literals at the time, which the naming work in
`pipeline/config.py` since replaced
parameterises.
