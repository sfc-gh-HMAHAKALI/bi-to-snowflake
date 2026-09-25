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
2. **One view per categorical axis that has a frame to draw.** Take the axes carried by the
   `RPT_` views you actually have, keep those whose measured cardinality is 3 or more, and
   name each after its business label, not the column: `Territory`, not `TERRITORY_LEVEL1`.
3. **Mix and seasonality** -- only if a date dimension exists with at least twelve distinct
   periods. Below twelve, seasonality is noise and the view is dropped.
4. **Provenance** -- always last. Where the numbers come from, which view, which rights.

**Select from the frames that exist, not from `drill_paths`.** An earlier version of this
rule said "one view per `drill_paths` hierarchy with two or more levels", and on the
reference model that selects the wrong set entirely: the six declared drill paths were
`Alphabet Grouping`, `Calender Time`, `Customer Class`, `Customer Segment`,
`Customer Tier 2` and `Time Cube`, while the frames actually built were territory, product,
customer and period. Territory does not appear in `drill_paths` at all and is by far the
richest hierarchy in the model — 301 distinct level-4 values, zero nulls. Following the old
rule literally would have built tabs for hierarchies with no frame behind them and omitted
the best one.

**Order hierarchy views by measured axis cardinality, descending — not by measured total.**
The old rule ranked by total, which cannot discriminate here: every `RPT_` view wraps the
same fact, so territory, product, customer and period all total the identical figure to the
cent. That is not a quirk of one model; it is true whenever the views wrap one fact, which
for this generator is always. Cardinality is the tie-break that actually orders them, and it
is stable between runs. Measure it — do not estimate it:

```sql
SELECT COUNT(DISTINCT <axis>) FROM <DB>.ANALYTICS.<RPT_ view>;
```

Cardinality also decides whether an axis is worth a chart at all, which is the same
measurement the constant-dimension rule below depends on. Take both from one pass.

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

**A sparkline needs the measure to exist in the period frame, which is not implied by the
measure existing.** The band is selected by magnitude across all `RPT_` views, so a card
can legitimately carry a measure that the *time* frame does not expose. On the reference
model `SALES_QUANTITY_TOTAL` is the second card by the `kind_of()` rule, but
`RPT_SALES_BY_PERIOD` has no quantity column at all — only `RPT_SALES_BY_PRODUCT` carries
it, and that view has no time column. So that one tile has a correct value and no
sparkline. Check the column is present in the period frame before promising a trend, and
leave the sparkline off when it is not. Do not substitute a different measure's series to
fill the space, and do not drop the card: the value is real even when its history is not
available. Say so in the Provenance view rather than leaving a viewer to wonder why three
tiles have a line and one does not.

## Chart selection

Match on data shape. The left column is the only question to ask.

| Data shape | Component | Aggregates internally? | Notes |
|---|---|---|---|
| Measure over ordered time | `TrendLines` / `trend` | No — pass rows as given | Always on the Overview if time exists. |
| Ranked categories, one measure | `RankedBar` / `ranked_bar` | **Yes — groups and folds the tail** | Horizontal. Bars beat angles for comparison. |
| Two measures over time, gap matters | `AreaGap` / `area_gap` | No | Order is load-bearing: the fill goes to the previous series. `AreaGap` also requires `lowerLabel`, `upperLabel` and `gapLabel` — all three non-optional. |
| Period-to-period change, one measure | `Waterfall` / `waterfall` | **Yes — groups** | See the partial-period rule below. |
| Category by period, one measure | `Heatmap` / `heatmap` | **Yes — groups, both libraries** | Needs an explicit legend range or it renders as a bare gradient. |
| Part-to-whole over time | `MixBar` / `mix_bar` | **Yes — groups, both libraries** | Stacked to 100%. |
| Concentration in a ranked set | `Pareto` / `pareto` | **Yes — groups** | Cumulative axis; the 80% line only when in range. |
| Two-level hierarchy, one measure | `Treemap` / `treemap` | No | Key nodes by full path or repeated children merge. |
| Three-plus-level hierarchy | `Sunburst` / `sunburst` | No | Same keying rule. |
| Two measures across categories | `CompareBars` | No | Grouped, not stacked. |
| Row-level detail, or the user asked for a table | `grid` | No | Always exportable. |

**Do not pre-aggregate for a component that aggregates itself.** The tail-folding rule below
reads as something the page does, and for most components it is — but `RankedBar` calls
`collapseTail` internally and `Pareto` and `Waterfall` call `rollup` internally, in both
libraries. Pre-rolling and pre-folding before handing rows to those three nests an
`Other (N)` inside another `Other (N)`. The column above is the authority; it was derived by
reading the component source, which is what the first composition had to do.

Note the React and Streamlit columns agree throughout. An earlier version of this table
claimed `Heatmap` and `MixBar` aggregate in Streamlit but not in React; that was wrong.
Both React components accumulate into a cell map with `+=` — the `cells.set` calls in
`charts.tsx` — exactly as their Streamlit counterparts do. The error was harmless in one
direction, since passing raw rows to something that groups is correct either way, but it
would have justified pre-aggregating for React, which nests an `Other (N)` inside another
`Other (N)`. Read the component source when this column matters; that is how both the
original column and this correction were derived.

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
- **Label the null bucket, never drop it.** Both chart libraries' `rollup()` skip an empty,
  `null` or `undefined` key by design, so a dimension with unattributed rows produces bars
  that quietly exclude them and stop agreeing with the KPI band above. Map null to
  `(unattributed)` *before* handing rows to any chart, and state the share in the subtitle.
  In the reference build 33.2% of revenue ($93.1M of $280.1M) arrived with `CUSTOMER_NAME`,
  `CUSTOMER_TIER1` and `STATE` all null; territory and product were clean, which is exactly
  why spot-checking two dimensions gave false confidence. This is the "fold tails into
  `Other (N)`" rule arriving through a different door: dropping nulls makes the total wrong
  just as silently as dropping a tail.
- **Tie one chart total back to the KPI band, on every page.** Sum the ranked view's measure
  and assert it equals the KPI. This is the single cheapest check that catches both of the
  rules above, and it is what caught the null bucket.
- **Measure a candidate axis's cardinality before composing against it.** A ranked bar over a
  dimension with one distinct value is a full-width bar restating the KPI and answering
  nothing -- `CUSTOMER_COUNTRY` has cardinality 1 in the reference model, and both apps were
  first composed with that mistake. One category is not a chart, and two rarely is. Rank by
  something with at least three.
- **Check the title against the props, every time.** Two panels shipped titles their data
  could not support: "Product mix by fiscal year" built from a view with no time column at
  all, and "Sales by region and fiscal year" with no region in it. A title written for the
  intended chart rather than the frame actually passed in is worse than a missing chart,
  because it is quotable.

## Runtime traps a composed app hits every time

These are not judgement calls. Both cost a working app in the reference build, and both
pass every local test.

- **The Snowflake SQL REST API paginates, and the first response is not the whole
  result.** This bites the React surface every run, because `package.template.json`
  pins no Snowflake driver — so the composed `lib/snowflake.ts` talks to
  `/api/v2/statements` over `fetch`, and a `POST` returns only the first partition.
  Read `resultSetMetaData.partitionInfo` and fetch
  `/api/v2/statements/{statementHandle}?partition=N` for every partition after the
  first. Measured: `RPT_SALES_DETAIL` came back as **812 of 3,198 rows**, which
  understated every total on the Mix page while the app looked entirely healthy —
  no error, no empty state, just quietly wrong numbers. The cheap catch is the
  tie-back rule below: sum the detail frame and assert it equals the KPI. That is
  what surfaced it.

- **Do not name the agent bridge from memory.** The procedure is generated with the
  run's prefix and is not the name composition tends to guess: on the reference
  build it is `ASK_RT6_ANALYST(QUESTION, CONTEXT)`, not `<PREFIX>_ASK_AGENT`. Read
  it rather than assuming, or the app's chat panel fails at the first question with
  "unknown function":

  ```sql
  SELECT PROCEDURE_NAME, ARGUMENT_SIGNATURE
    FROM <DB>.INFORMATION_SCHEMA.PROCEDURES
   WHERE PROCEDURE_SCHEMA = 'ANALYTICS';
  ```

- **Never hardcode a bind placeholder.** `snowflake-connector-python` defaults to
  `paramstyle = "pyformat"`, so `%s` works from a REPL, from a script, and from `data.py`
  imported directly. But `bim_ui/compat.py` probes `st.connection(...)` at import time to
  detect Snowsight, and constructing a Snowpark session sets
  `snowflake.connector.paramstyle = "qmark"` *for the whole process* -- after which every
  `%s` in the app stops binding. It surfaces as a SQL *syntax* error
  (`unexpected '%'`), not a binding error, from a UI shim that has nothing to do with
  query parameters. Resolve it at call time:

  ```python
  ph = "?" if snowflake.connector.paramstyle == "qmark" else "%s"
  cur.execute(f"CALL {ASK_PROC}({ph}, {ph})", (question, ASK_CONTEXT))
  ```

- **Render the agent's markdown.** The agent replies in markdown. Streamlit gets that free
  from `st.markdown`; a React `whitespace-pre-wrap` div shows `**$92,322,705**` verbatim and
  reads as a data bug. Render a known subset (bold, dash bullets) into React elements. Do
  not use `dangerouslySetInnerHTML` -- then nothing the agent returns can inject markup, and
  no dependency is added.

- **The agent's result table does not survive the bridge.** `33_agent_sql_bridge.sql::_parse`
  returns `{answer, sql, error}`: it reads `sql` off the `tool_result` event but drops that
  event's row payload, so an answer ending "...in descending order:" is followed by nothing.
  Until the bridge returns `rows` too, re-run the returned SQL in the app and render it --
  accepting only a statement that begins `SELECT` or `WITH`, and degrading to no table on
  failure. The statement was generated
  against the semantic view and the territory row access policy applies to the re-run
  exactly as it did to the agent.

- **Do not run `next build` while `next dev` is running.** Both write the same `.next`
  directory and the result is a `MODULE_NOT_FOUND` through `webpack-runtime.js` with nothing
  wrong in the source. Stop the dev server, `rm -rf .next`, rebuild.

## Hand the app over -- do not audition it

The two sections above exist so the defects they describe never reach the composed app.
That is where the learnings belong: as rules applied while writing the page, not as an
inspection afterwards. Once the pages are written, **hand them over.**

A measured run spent roughly ten minutes after composition on `npm run build`, starting
dev servers, re-querying numbers that had already been verified by the backend verifier,
and reading composed files back. It found nothing that the rules above had not already
prevented, and the user waited ten minutes for a clean bill of health on something they
could have had in their hands.

**Do exactly these checks, and no others:**

| Check | Cost | Why it stays |
|---|---|---|
| Every file in `STREAMLIT_SOURCE` and `REACT_SOURCE` exists | instant, already in preflight | a missing library module is an `ImportError` on line 1 while `SHOW STREAMLITS` looks healthy |
| `python3 -m py_compile` on each composed Streamlit page | under a second | a syntax error is not something the user should discover |

**Do none of these:** `npm run build`, `next dev`, `streamlit run`, opening a browser,
screenshotting, re-running agent questions to confirm answers, re-querying totals the
backend verifier already checked, or reading composed files back to confirm what was
just written.

### Say plainly that it may break

Hand over with the caveat, not after eliminating the need for one. The user has CoCo
open; a runtime error pasted back is a sixty-second fix, and far cheaper than the agent
guessing at which errors might occur. Tell them:

- The backend is verified -- the views, semantic view, agent and grants were all checked.
- The two apps are **generated, not tested**. They may hit a runtime error on first run.
- Paste the error back into CoCo and it will be fixed against the composed source.
- The likely candidates are known and listed in "Runtime traps" above -- a wrong column
  name, an import path, or a chart that got an empty frame.

Both surfaces are source in the workspace, so a fix is an edit and a refresh, not a
rebuild.

### Do not wait forever

If a command has to be waited on, wait on it once with a bounded sleep and then report.
Do not loop `sleep`/check indefinitely, and do not re-run a check hoping for a different
answer. The build's own log (`pipeline/out/build-log.txt`) is the progress report -- give
the user the path and let them watch it rather than narrating from repeated polls.

## Honest labelling

If a figure is not measured through the semantic view -- a count read from the knowledge
base, or anything stated rather than queried -- the page must say so where the figure
appears. Silence here reads as a claim.

## When the rules do not fit

If the model has no date dimension, no hierarchy with two levels, or fewer than four
measures, the derived layout degenerates. Do not pad it with repeated charts. Build what the
data supports, and state in the Provenance view which views were not generated and why.
