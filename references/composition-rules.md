# Composition rules

How a generated page decides what to show. These rules exist so that two runs over the same
model produce the same layout -- not because the model is copied, but because the rules are
specific enough that the *data* determines the answer.

Read this before generating any page. If a rule does not decide a case, say so and ask;
do not improvise, because an improvised choice is the thing that varies between runs.

## What you may not do

1. **Never restyle the component libraries.** No colours, fonts, margins, axis config or
   chart code in a generated page. Everything visual comes from `assets/react_ui/` or
   `assets/streamlit_ui/`. A page composes; it does not draw.
2. **Never issue a query per chart.** Fetch a frame per view, then aggregate client-side.
   The reference app holds first paint to two fact queries with ten chart shapes on top.
3. **Never invent a metric.** Every number resolves through the semantic view or an `RPT_`
   wrapper over it. If a page needs a figure no view exposes, add a view; do not compute it
   in the page.

## Inputs

From `inventory.json`, produced by `kb_to_inventory.py`:

| Key | Shape | Use |
|---|---|---|
| `dimensions[]` | `name, label, table, data_type, description, source_entity` | candidate axes and filters |
| `measures[]` | `name, label, aggregation, description, format, decimals, currency, certified` | candidate values |
| `drill_paths[]` | `dimension, hierarchy, levels` | view derivation, treemap and sunburst levels |
| `tables[]` | `name, snowflake_name, kind, source_entity` | provenance |
| `semantic_view` | fully qualified name | what every `RPT_` view wraps |

Two properties of real inventories to plan around, both true of the reference model:

- **`aggregation` may be uniformly `sum`.** All 98 measures in the reference model are
  `sum`. So aggregation cannot rank measures.
- **`certified` may be uniformly false.** It is for all 98. So certification cannot pick the
  KPI band either.

Both mean magnitude has to be **measured, not inferred**: run one `SELECT` of candidate
measure totals against the semantic view before composing, and rank on the result. That
query is part of composition, not of page render.

## Deriving views

A view is a page tab. Derive them in this order and stop at eight.

1. **Overview** -- always first, always present.
2. **One view per hierarchy** in `drill_paths` that has two or more levels and whose root
   dimension appears in a fact-joined table. Name it after the hierarchy's business label,
   not the column: `Territory`, not `TERRITORY_LEVEL1`.
3. **Mix and seasonality** -- only if a date dimension exists with at least twelve distinct
   periods. Below twelve, seasonality is noise and the view is dropped.
4. **Provenance** -- always last. Where the numbers come from, which view, which rights.

Order is fixed: Overview, then hierarchy views in descending order of their measured total,
then Mix and seasonality, then Provenance. Ranking hierarchy views by measured magnitude is
what keeps tab order stable between runs.

## The KPI band

Four cards, never three, never five. Wrapping past four costs a row of vertical space worth
more than a fifth number.

Selection, in order:
1. The largest additive measure by measured total.
2. The largest additive measure whose `kind_of()` differs from the first -- so a currency
   figure sits beside a count rather than beside another currency.
3. The next largest additive measure.
4. A derived comparison if one is available: period-over-period delta, or attainment against
   a target measure if the model declares one. Otherwise the next largest measure.

Each card carries a delta against the prior comparable period and a twelve-point sparkline
when a date dimension exists. `higher_is_better` follows `kind_of()`: costs and returns are
inverted, everything else is not.

## Chart selection

Match on data shape. The left column is the only question to ask.

| Data shape | Component | Notes |
|---|---|---|
| Measure over ordered time | `TrendLines` / `trend` | Always on the Overview if time exists. |
| Ranked categories, one measure | `RankedBar` / `ranked_bar` | Horizontal. Bars beat angles for comparison. |
| Two measures over time, gap matters | `AreaGap` / `area_gap` | Order is load-bearing: the fill goes to the previous series. |
| Period-to-period change, one measure | `Waterfall` / `waterfall` | See the partial-period rule below. |
| Category by period, one measure | `Heatmap` / `heatmap` | Needs an explicit legend range or it renders as a bare gradient. |
| Part-to-whole over time | `MixBar` / `mix_bar` | Stacked to 100%. |
| Concentration in a ranked set | `Pareto` / `pareto` | Cumulative axis; the 80% line only when in range. |
| Two-level hierarchy, one measure | `Treemap` / `treemap` | Key nodes by full path or repeated children merge. |
| Three-plus-level hierarchy | `Sunburst` / `sunburst` | Same keying rule. |
| Two measures across categories | `CompareBars` | Grouped, not stacked. |
| Row-level detail, or the user asked for a table | `grid` | Always exportable. |

Never a pie or donut for share of total. `RankedBar` answers the same question more
accurately, and the library deliberately ships no pie.

## Rules that prevent specific defects

Each of these exists because its absence produced a visible bug in the reference build.

- **Fold category tails past eight** into `Other (N)`, keeping the count in the label.
  Dropping the tail silently makes the total wrong; keeping thirteen legend entries wraps
  the legend over the bars.
- **Never compare a partial period against a complete one.** For any period-over-period
  visual, count distinct sub-periods per period and compare only the two most recent
  *complete* ones. Name the excluded partial period in the subtitle. The reference build
  drew a 75M collapse before this rule existed; it was three months against twelve.
- **Cumulative axes scale to the data**, not to 100. Use `ceil(max * 1.15 / 5) * 5`, capped
  at 105. A cumulative curve reaching 24% on an axis to 100 is unreadable.
- **Charts need a height floor of 140px**, not 220. One bar in a 220px panel looks broken.
- **Semi-additive measures are never summed across time.** Where `IS_SEMI_ADDITIVE` is set,
  or `rollupAggregate` differs from `regularAggregate`, take the last value per period and
  sum only across non-time dimensions. Note: no measure in the reference model triggers
  this -- 0 of 4,070 rules -- so the rule is untested against real data and exists for
  models that do declare it.

## Honest labelling

If a figure is measured on a table that is not in `tables[]` -- a companion built to
demonstrate a behaviour the source model does not contain -- the page must say so where the
figure appears. The reference build needed this for four of six cube behaviours. Silence
here reads as a claim.

## When the rules do not fit

If the model has no date dimension, no hierarchy with two levels, or fewer than four
measures, the derived layout degenerates. Do not pad it with repeated charts. Build what the
data supports, and state in the Provenance view which views were not generated and why.
