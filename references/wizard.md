# The guided run

Two rounds of questions with a cheap parse between them, then one costed confirmation before
anything is created. The parse sits in the middle on purpose: round 2 asks about a model you
have already read, so the counts shown are real rather than promised.

Never skip to the build. A user who wanted one semantic view does not want two dashboards.

## Naming parameters are not answers to the wizard

**A request that supplies a database, KB database, prefix or connection has answered
*where things are named*. It has not answered *what to build* or *where it is hosted*.**
Run both rounds and the confirmation gate anyway.

This has gone wrong in a real run and it was expensive. The request named a database,
a KB database, a prefix and a connection, and said "including building and deploying
the React app". That was read as "deploy to Snowflake" when it meant "get the React
app built and running" — so `--deploy all` ran unasked, created a compute pool, a
`STREAMLIT` object and an `APPLICATION SERVICE`, and spent about 290 seconds of remote
build time on infrastructure nobody had asked for. All of it then had to be torn down.

The failure was not a missing rule; it was treating supplied parameters as consent for
adjacent decisions. So:

> The only wizard question a user's earlier message can pre-answer is one they answered
> **in the same terms the wizard uses**. "Deploy the React app" is not an answer to
> "where should the dashboards be hosted?", because it does not distinguish running it
> locally from creating an App Runtime service. If a choice costs money, creates
> account-level objects, or is hard to reverse, ask — even if you believe you know.

Deploying is all three.

## Round 1 -- the source

```
ask_user_question:
  - header: "BI tool"
    question: "What kind of BI source are we starting from?"
    options:
      - label: "IBM Cognos Framework Manager"
        description: "A model.xml, a .cpf, or the zip it downloaded as."
      - label: "Tableau"
        description: "twb, twbx, tds or tdsx."
      - label: "Power BI"
        description: "pbit or pbix."
      - label: "Looker"
        description: "LookML project directory."
      - label: "Denodo"
        description: "VQL export."
      - label: "SAP BusinessObjects"
        description: "unv or unx universe."
  - header: "File"
    question: "Where is the file?"
    type: text
    defaultValue: "<best guess from the working directory, or ~/Downloads>"
```

Glob for the likely file first and offer what you find as the default. Asking for a path the
user has to go and look up is worse than a wrong guess they can correct.

## Profile -- read the model before asking anything else

```bash
cd "$SKILL_DIR"   # the directory holding SKILL.md; do not hardcode the skill name
python3 -m modules.cli parse --type <type> "<path>" -o /tmp/b2s/inventory.json
python3 pipeline/describe_model.py --inventory /tmp/b2s/inventory.json \
        --source "<path>" --out /tmp/b2s/model-overview.md
```

**Give the user that Markdown file now, before round 2.** Tell them the full path and
offer to open it. This is the first thing they get back after pointing at their model,
it takes about two seconds to produce, and it needs no Snowflake connection at all.

It is also the answer to a question they will otherwise ask during the build: the
knowledge base load is the longest phase, around two minutes, and a user with nothing to
read will watch a progress log. Handing them a description of their own model turns that
wait into something useful — and it lets them check what was found *before* agreeing to
build anything on top of it.

The digest covers the counts below plus the things worth knowing that a count cannot
carry: which data sources already point at Snowflake, how many objects are fiscal-year
copies of each other, how many security filters there are and how few distinct *shapes*
they reduce to, and what is flagged with the reasons grouped. Read it yourself before
round 2 — it is the same material round 2 and the confirmation gate are built from.

`build.py` regenerates it as its own `describe` phase, so a direct CLI run still produces
one. But that phase runs after three Snowflake DDL phases, so waiting for it means the
user has already committed to the build. In a guided run, produce it here.

Then read the counts out of the inventory. The parse-stage inventory's top-level keys are
`dimensions`, `metrics`, `hierarchies`, `tables`, `facts`, `relationships`,
`aggregation_rules`, `security_rules`, `grain_declarations`, `dashboards`, `worksheets`,
`filters`, `flagged`, `errors`, `complexity_summary`, `source_analysis`, `source_type` and
`snowflake_target`. These are what round 2 and the confirmation are built from.

Note the two inventories differ. This file has `metrics` and `hierarchies`; the app-stage
`bim_inventory.json` that `kb_to_inventory.py` writes later has `measures` and
`drill_paths`. Reading the wrong set costs a few minutes of grepping for keys that are not
there.

Report anything surprising at this point rather than after the build:

- **No date dimension.** Mix and seasonality will not be generated, and KPI sparklines are
  not possible. Say so now.
- **Fewer than four measures.** The KPI band degrades. Say so now.
- **No hierarchy with two or more levels.** Treemap, sunburst and per-hierarchy views all
  drop out, which is most of the dashboard.
- **`dashboards` is `0`.** For Cognos this is always true and is not a defect: Framework
  Manager is a modelling layer and declares no reports, charts or dashboards at all. Any
  reporting pages are therefore our construction. Never describe the result as a
  reproduction of existing reports. (Test `dashboards == 0` — the parse inventory carries no
  `provenance` block, so there is no synthesised flag to read.)

### Pass the file as it came

Every adapter takes the archive directly. A Cognos export that downloaded as
`Sales DMR Model.zip`, a `.twbx`, a `.pbix` -- hand the path straight to
`--extract`. The parser extracts into a fresh temporary directory of its own,
finds the model file inside however deeply it is nested, and rejects members
that try to escape the directory.

**Do not add a shell unzip step**, and never put a delete in front of one. A
command like `rm -rf ./*` needs the user's approval before it runs, and they
have no way to tell from the command text that the directory you just made is
empty -- so they decline, and the build stalls on its first step. There is
nothing to clean up in the first place: use the path the user gave you.


## Round 2 -- what to build

```
ask_user_question:
  - header: "Outputs"
    question: "What should this run produce?"
    multiSelect: true
    options:
      - label: "Ossie semantic view"
        description: "Portable YAML plus a deployed SEMANTIC VIEW. Everything else depends on this."
      - label: "Cortex Agent"
        description: "Plain-English questions over the semantic view, with a SQL bridge both apps can call."
      - label: "Streamlit dashboard"
        description: "Multi-page report on a compute pool, generated against the locked library."
      - label: "React dashboard"
        description: "Next.js and ECharts on App Runtime. Same component vocabulary as Streamlit."
      - label: "Horizon catalog and glossary"
        description: "Object comments, tag taxonomy, business glossary, ontology handoff."
  - header: "Target"
    question: "Which database should this build into?"
    type: text
    defaultValue: "<from inventory snowflake_target's database, else BI2SF>"
  - header: "Naming"
    question: "What should we call everything? This prefix goes in front of every object -- the semantic view, agent, Streamlit app and React app -- so two models can share one account."
    type: text
    defaultValue: "<derive from model name: if the model is 'Regional Sales Analysis', suggest RSA. Short, uppercase, no spaces.>"
  - header: "Model name"
    question: "What should the semantic view be called? (The prefix above is added automatically, so 'SALES_BOOKINGS' with prefix 'RSA' creates RSA_SALES_BOOKINGS.)"
    type: text
    defaultValue: "<derive from model subject: SALES_BOOKINGS, REVENUE, PIPELINE -- whatever the model is about, in one or two words, uppercase with underscores.>"
```

**Derive the defaults from the model.** The inventory has the model name and subject matter. Use
them — if the model is named "Regional Sales Analysis", default to prefix `RSA`, model name
`SALES_BOOKINGS`. The user can change either or both; whatever they type becomes `--prefix` and
`--model-name`.

**Show what the names will be before the confirmation gate.** After the user answers, compute
the derived names and show them:

```
With prefix "RSA" and model name "SALES_BOOKINGS" in database "ACME_SALES":

  Semantic view:  ACME_SALES.ANALYTICS.RSA_SALES_BOOKINGS
  Cortex Agent:   ACME_SALES.ANALYTICS.RSA_ANALYST
  Search service: ACME_SALES.ANALYTICS.RSA_GLOSSARY_SEARCH
  Streamlit app:  ACME_SALES.ANALYTICS.RSA_REPORT
  React app:      ACME_SALES_RSA_REPORT  (APPLICATION SERVICE)
  Knowledge base: ACME_SALES_KB.KNOWLEDGE_BASE
```

This is not decoration — it is the naming preview that prevents "why is it called BI_REPORT?"
after a twelve-minute build. The fully-qualified names also tell the user where to find
everything, which is worth more than the names themselves.

If the user says "just use the defaults" or "that looks fine", proceed. If they change a name,
update both the `--prefix` and `--model-name` flags accordingly and recompute the preview.

**The human-readable label** (`--model-label`) defaults to the model name from the inventory
with normal casing and is shown in object comments, the Streamlit page title, and the React
window title. It is a separate flag because it contains spaces and mixed case, and it is not
asked as a question — it is derived from the model name. If the user changes the prefix or
model name to something that no longer matches the model, update the label to match what they
typed.

**Ask for the database only, never the schema.** The schema names are fixed: the DDL in
`pipeline/sql/` hardcodes `ANALYTICS`, `SALES_ANALYTICS`, `COMMON_ANALYTICS` and
`KNOWLEDGE_BASE`, and `--analytics-schema` now rejects any other value rather than applying
it to half the pipeline. Asking "which database and schema?" produced an answer that was
silently discarded and then a confirmation gate that named `PUBLIC`, where nothing was ever
created. One inert question is worse than one fewer question.

```yaml
  - header: "Deploy"
    question: "How should the dashboards run?"
    defaultAnswer: "Run both locally"
    options:
      - label: "Run both locally"
        description: "Default. Generate the code and run it on your machine against Snowflake. Nothing is hosted, nothing to tear down."
      - label: "Host Streamlit on Snowflake, React locally"
        description: "Adds a compute pool and a STREAMLIT object. React renders identically on localhost and starts in seconds."
      - label: "Host both on Snowflake"
        description: "Adds an APPLICATION SERVICE and a Docker build on top of the above. Slowest and the most to remove afterwards."
```

Only ask the deploy question if a dashboard was selected.

**The answer to this question is a `--deploy` value. It is not a path.** "Run locally",
"local", "localhost", "deploy locally", "run it on my machine" and "nothing hosted" are
all the same answer and all mean `--deploy none`. They are not answers to the outputs
question and must not be looked up in the path table below — a run that tried to resolve
"deploy locally" as a path reported that it was not one, then proceeded anyway, which is
the worst of both outcomes: the user was told their answer was invalid and it was also
not honoured as stated.

| If the user says | Use |
|---|---|
| run locally, local, localhost, on my machine, nothing hosted, don't deploy | `--deploy none` |
| Streamlit on Snowflake, host the Streamlit one, deploy Streamlit only | `--deploy streamlit` |
| host both, deploy everything, put it all on Snowflake, SPCS | `--deploy all` |

If the phrasing is genuinely ambiguous between hosting and generating, ask — hosting
creates account-level objects and costs money, so it is never the thing to assume.

## Output to path mapping

Selections resolve onto `pipeline/build.py` paths. Dependencies expand automatically --
path 4 pulls in 2 and 3 -- so pass what the user asked for and let `expand()` do the rest.

| Selection | Path / flag |
|---|---|
| Ossie semantic view | `--paths 3` |
| Cortex Agent | `--paths 3` |
| Horizon catalog and glossary | `--paths 1` |
| Streamlit or React dashboard | `--paths 2` |
| A dashboard **and** an agent | `--paths 4` |
| *the deploy answer* | **not a path** -- it sets `--deploy`, see the table above |

That last row exists because its absence caused a real failure. Only the **outputs**
answer maps onto paths. The deploy answer is a separate flag, and an agent that looks it
up here finds nothing and concludes the user gave an invalid answer. If a wizard answer
has no row in this table, that means it belongs to a different flag, not that it was
wrong.

A dashboard plus an agent is path 4 rather than `2,3` because an embedded chat panel that
cannot see the report's filter state is a different product. Path 4 builds them together.

### Deploy mapping

| Deploy selection | `--deploy` flag | What deploys to Snowflake | What runs locally |
|---|---|---|---|
| Run both locally **(default)** | `--deploy none` (or `local`) | Backend only (views, semantic view, agent) | Streamlit (`streamlit run pipeline/app_streamlit/app.py`) and React (`cd pipeline/app_react && npm run dev`) |
| Host Streamlit on Snowflake, React locally | `--deploy streamlit` | Streamlit report (container runtime) | React (`cd pipeline/app_react && npm install && npm run dev`) |
| Host both on Snowflake | `--deploy all` | Streamlit + React App Runtime service | None needed |

**The labels in this table are the labels the wizard offers, word for word.** They were
once different -- the question said "Both local / Nothing deployed" while this table said
something else -- and two names for one choice is how a valid answer ends up looking
invalid.

**`--deploy` defaults to `none`.** Deploying creates a compute pool, a `STREAMLIT`
object and an `APPLICATION SERVICE`; it costs money, it is the slowest part of the
build, and it is the least reversible. So it is never the default and always an
explicit choice. `--skip-deploy` is accepted as an alias for `--deploy none`.

This used to default to `all` while this table marked a different row "Recommended" —
the default and the recommendation disagreed, and the default was the option hardest to
undo. Local-first is also simply better for this workflow: a React app on localhost
renders identically and starts in seconds, which is what anyone iterating on a
composition actually wants.

### A first run cannot deploy in one command

`--deploy all` on a fresh model **cannot satisfy its own preflight**, and this is not a
bug to work around — it is inherent. The preflight refuses to start when
`pipeline/app_react/` and `pipeline/app_streamlit/` are missing, but composing those
needs `pipeline/out/bim_inventory.json`, which the `bridge` phase writes near the end of
the backend build. The app source cannot exist before the run that produces its input.

So the run shape is three steps, and the third is opt-in:

```bash
# 1. Backend, through the bridge phase. This is the whole build for most purposes.
python3 pipeline/build.py --extract "<model>" --all --deploy none

# 2. Compose both surfaces against the measured inventory (see composition-rules.md).
#    Nothing to run here -- this is the authoring step.

# 3. Only if the user asked for hosting on Snowflake:
python3 pipeline/build.py --paths 4 --deploy streamlit --skip-physical
```

Step 3 takes `--skip-physical` because the tables already exist and rebuilding them
would discard the catalog comments and tags path 1 wrote onto them.


**Local hosting is not local data.** `--deploy` only decides where the two apps are
*hosted*. The semantic view, row access policy, Cortex Search, the agent, the SQL
bridge and the `RPT_` views are Snowflake objects either way, and a dashboard on
localhost queries them over a connection. Say this plainly if a user asks for
"local instead of Snowflake": the apps can run anywhere, the governed layer cannot.

**Do not reach for `--paths 3` to get local dashboards.** It looks like it avoids the
deploy phases, and it does, but it also drops the app inventory bridge and the seven
`RPT_` views -- the two things a dashboard actually reads. The apps would come up
empty. `--paths 4 --deploy none` is the correct combination: every phase a dashboard
depends on, and neither app surface.

### Where the composed app has to live

The app source is not in this repo: it is composed per model against the locked
libraries. Write it to the paths the deploy phases read, or they will not find it:

| Surface | Compose into | Must contain |
|---|---|---|
| Streamlit | `pipeline/app_streamlit/` | `app.py`, `data.py`, `pages_impl.py`, `metrics.py`, `pyproject.toml`, `bim_ui/` (all six library modules), `.streamlit/config.toml` |
| React | `pipeline/app_react/` | `app.yml`, `package.json`, `lib/queries.ts`, `lib/theme.ts`, `lib/charts.tsx`, `lib/ui.tsx`, `tailwind.config.ts`, `postcss.config.mjs`, `app/globals.css`, plus the Next.js tree |

**The Streamlit layout, which is as fixed as the React one.** `bim_ui/` is a *verbatim
copy* of `assets/streamlit_ui/`; the files beside it are the page's own layer. Both a
root `metrics.py` and a `bim_ui/metrics.py` exist and they are different files — this
caught a real run out, and two runs inferring it differently is exactly the variance the
locked-library rule exists to remove.

| Path | What it is | Comes from |
|---|---|---|
| `bim_ui/__init__.py` | Re-exports the library surface. Does `from . import charts, compat, filters, metrics, ui` — so **all six modules must be present** or the app raises `ImportError` on its first line. | copy of `assets/streamlit_ui/` |
| `bim_ui/{charts,compat,filters,metrics,ui}.py` | The locked component library. Never edited, never restyled, never partially copied. | copy of `assets/streamlit_ui/` |
| `metrics.py` (root) | The **page's** formatting and safe arithmetic. A different file from `bim_ui/metrics.py`, and not a substitute for it. | written per model |
| `app.py` | Entry point and page registration. | written per model |
| `data.py` | Every query, one function per dataset. | written per model |
| `pages_impl.py` | Rendering, calling into `bim_ui`. | written per model |
| `.streamlit/config.toml` | Theme. Must carry no off-palette colour. | written per model |

Copying only part of `bim_ui/` fails in the worst available way: the `STREAMLIT` object
is created, `SHOW STREAMLITS` looks healthy, and it breaks only when a person opens it.
The upload list in `run_app_deploy` is now derived from the library directory rather
than hand-written, so a module added to `assets/streamlit_ui/` is staged automatically —
but a composition that copies three of six files is still wrong and the preflight is
what catches it.

**The React component library goes in `lib/`, not the app root.** This is fixed by the
library's own imports and is not a matter of taste: `charts.tsx` imports
`@/lib/theme` while `ui.tsx` imports `./theme`, and the only layout satisfying both is
`lib/theme.ts` beside `lib/charts.tsx` and `lib/ui.tsx`. `tailwind.config.ts` already
globs `./lib/**/*.{ts,tsx}`, which confirms it. Copying the three files to the app root
instead fails the build outright with
`Module not found: Can't resolve '@/lib/theme'` — one wasted build cycle, and the error
names a path that exists nowhere, so it reads like a broken asset rather than a
misplacement.

**Give the React page a background from the palette.** `app/globals.css` deliberately
defines no colours, and Tailwind's preflight sets no `body` background, so a composed
React app inherits the operating system's dark mode while the Streamlit surface pins
light through `.streamlit/config.toml`. The two surfaces are supposed to look identical;
without this they do not, and the failure only shows on a machine set to dark mode, which
is how it survives review. `theme.ts` already exports `NEUTRAL.wash` described as "page
background behind the panels" — apply it in `app/layout.tsx`:

```tsx
<body style={{ background: NEUTRAL.wash, color: NEUTRAL.ink }}>{children}</body>
```

That wires the locked theme in rather than inventing a colour, so it stays inside the
never-restyle rule and past the colour guard.

**The React files composition has to write, and what each must do.** Eight assets ship;
everything else below is written per run, which is where run-to-run variance comes from.
Naming them does not make them generated code — it stops each run re-deciding the same
questions differently.

| File | Must do |
|---|---|
| `lib/snowflake.ts` | One `query()` over the SQL REST API. **No driver** — `package.template.json` pins none, and adding one means choosing a version the template declined to pin. Must follow result partitions, and must coerce numeric columns, because the API returns every value as a string and client-side aggregation would otherwise concatenate. |
| `app/api/data/route.ts` | One route returning **every** frame, fetched concurrently. Not a route per panel: first paint is one round trip. |
| `app/api/ask/route.ts` | Call the bridge, then re-run the returned statement for its rows, accepting only `SELECT`/`WITH` and degrading to no table. Escape quotes in the question. |
| `components/report.tsx` | `"use client"`. The views, the KPI band, null labelling, and the agent panel. Composes library components; contains no chart code. |
| `app/layout.tsx` | Import `globals.css` once; set the `NEUTRAL.wash` background above. |
| `app/page.tsx` | Render the report. Nothing else. |
| `next.config.ts` | `output: "standalone"`, and `transpilePackages: ["echarts", "echarts-for-react"]` — the wrapper is CJS and the interop fails on the server render path without it. |
| `tsconfig.json` | `paths: {"@/*": ["./*"]}`, `strict`. Expect `next build` to rewrite `jsx` to `react-jsx` and add `.next` types; that rewrite is normal, not a defect. |
| `app.yml` | The App Runtime service spec. No credential: the mounted OAuth token is what `lib/snowflake.ts` prefers when it exists. |

**Auth in `lib/snowflake.ts`: two paths, probed in this order.** The mounted OAuth token at
`/snowflake/session/token`, present when running as an App Runtime service and re-read per
request because it rotates; then `SNOWFLAKE_PAT` from the environment, which is the local
path. Both hit the same endpoint and differ only in
`X-Snowflake-Authorization-Token-Type` (`OAUTH` vs `PROGRAMMATIC_ACCESS_TOKEN`). Surface
which path was used in the UI — on a local run "it returned rows" is not evidence the
deployed path works, and the two are easy to confuse when both succeed. A connection
already using `authenticator = "programmatic_access_token"` has a token file to point
`SNOWFLAKE_PAT` at, so a local run needs no new credential minted.

**`lib/queries.ts` has a required shape, not just a required name.** Every dataset must carry
a `sql:` property holding a backtick template literal, and every object reference must
resolve inside `${DB}.${SCHEMA}`. `verify_deployment.py` scans for exactly that. A file
holding the same SQL under bare dataset keys is semantically identical and structurally
wrong — the scan finds zero datasets and fails. Copy
`assets/react_ui/queries.template.ts` and replace the bodies; do not invent the shape.

**The React library requires Tailwind.** `assets/react_ui/charts.tsx` uses Tailwind utility
classes in nine places for its empty states, so without it those elements render unstyled —
and the empty state is exactly what a demo hits when filters exclude everything. Copy
`assets/react_ui/tailwind.config.ts`, `postcss.config.mjs` and `app/globals.css` alongside
the app; they are shipped so the dependency is not a matter of inference.

**Copy `assets/react_ui/package.template.json` rather than choosing versions.** Composing a
`package.json` from memory once picked `next@15.1.6`, which `npm install` immediately
flagged as carrying a security vulnerability.

`build.py` preflights this list whenever a deploy phase is in the plan, so a missing
file is one message before anything is created rather than a failure at phase 15.

**`--deploy none` skips that preflight**, because the deploy phases that trigger it are
not in the plan. So on a local-only build you get no warning that the app source was
never composed -- and `streamlit run pipeline/app_streamlit/app.py` then fails with a
plain "no such file", which reads like a broken skill rather than a step not yet done.
If you chose local dashboards, check the two directories yourself before telling anyone
the build finished:

```
ls pipeline/app_streamlit/app.py pipeline/app_react/package.json
```

Composition is real work that happens on every run, not a one-time skill fix. Absent
directories mean it has not happened yet.

## The confirmation gate

Show this before creating anything. It is the last point at which a wrong target schema
costs nothing.

```
From <file name> (<source_type>):
  <N> dimensions, <N> metrics, <N> hierarchies across <N> tables
  <note any degradation found during profiling>

Will create in <DATABASE>:
  ANALYTICS         <the governed views, the semantic view, the agent, the bridge>
  SALES_ANALYTICS   <fact and territory tables>
  COMMON_ANALYTICS  <shared dimensions, including the date dimension>
and in <KB_DATABASE>:
  KNOWLEDGE_BASE    <the 14 knowledge-base tables and their views>

Objects:
  Semantic view:  <DATABASE>.ANALYTICS.<PREFIX>_<MODEL_NAME>
  Cortex Agent:   <DATABASE>.ANALYTICS.<PREFIX>_ANALYST
  Search service: <DATABASE>.ANALYTICS.<PREFIX>_GLOSSARY_SEARCH
  Streamlit app:  <DATABASE>.ANALYTICS.<PREFIX>_REPORT
  React app:      <DATABASE>_<PREFIX>_REPORT  (APPLICATION SERVICE)

Plan: <N> phases, roughly <N> minutes cold.
Proceed?
```

**Use a radio button, not a text prompt.** The confirmation gate is a yes/no decision, not a
free-form answer. Present it with `ask_user_question`:

```yaml
ask_user_question:
  - header: "Confirm"
    question: "<the full confirmation summary above, as the question text>"
    options:
      - label: "Proceed"
        description: "Start the build with the settings shown above."
      - label: "Change something"
        description: "Go back and change the database, naming, paths or deploy mode."
```

If they pick "Change something", ask what — do not re-run the whole wizard.

Name the schemas that actually receive objects, not a schema the user typed. The gate's
whole purpose is to state the cost and the location before anything is created, so a wrong
location here is worse than no gate.

Get the plan and the phase count from the build itself rather than estimating:

```bash
python3 pipeline/build.py --paths <p> [--deploy <mode>] --extract "<path>" --dry-run
```

**Include `--extract`** if the run will extract — and it will, on a first build. Without it
the dry run reports 16 phases where the real plan is 18, because `--extract` injects
*Parse the BI model* and *Load the knowledge base*. The second of those was measured at
413s, the single most expensive phase in the build, so omitting the flag understates both
the phase count and the duration at the exact moment the gate exists to state them.

Timing to quote, measured on the reference model: the knowledge base, views, bridge and
reporting views total about 42 seconds. A Streamlit deploy adds 62, caller grants 6, a React
deploy 93. Extraction, knowledge-base load and page composition sit on top, so a cold run
with everything is **6 to 10 minutes**. Warm, with the compute pool already active and image
layers cached, it is considerably less.

## Running it

```bash
python3 pipeline/build.py --paths <p> [--deploy <mode>] \
  --extract "<path>" --connection <name> --inventory /tmp/b2s/inventory.json
```

Every phase is idempotent: DDL is `CREATE OR REPLACE`, the knowledge-base load is a `MERGE`
on the source key, data loads `TRUNCATE` before `INSERT`. Re-running converges rather than
duplicating, which is what makes it safe to demo live and safe to retry after a failure.

Two things to avoid:

- **Never `CREATE OR REPLACE STREAMLIT`.** It mints a new `url_id` and breaks any published
  link. Redeploy by copying files into the live version location instead.
- **Re-run caller grants after adding any `RPT_` view.** `GRANT ... CALLER ... ON ALL VIEWS
  IN SCHEMA` is point-in-time and does not cover views created later. `ON FUTURE VIEWS` is
  rejected outright.

### Give the user the log path in the message that starts the build

The build prints its first line before doing any work:

```
Live log: <skill>/pipeline/out/build-log.txt
```

**Put that path in the message where you launch the build, and say they can open it
while it runs.** It is written line by line and flushed, so it is readable mid-run.

This is the only channel that reaches the user. Streaming to stdout serves a person
who typed the command themselves; when the build runs inside a single tool call the
output is captured and returned only on exit, so a measured 7.7-minute run showed the
user four sentences of agent prose and none of the narration -- the plain-language
table descriptions, the row counts, the elapsed times were all produced correctly and
all sat in a pipe until the build was over. The file is what makes them visible while
the build is the thing that is happening.

Relay the milestones as well, but do not treat relaying as a substitute for the path.

### The build narrates itself -- do not poll Snowflake to guess its progress

The `describe` and `kb-load` phases stream their output line by line. The knowledge base
load announces every table before it writes it, in plain language, with a running row
count and elapsed time. **Relay those lines. Do not open a second connection and count
rows to infer progress.**

Polling produces confident nonsense. An observed run reported *"Security mappings now
loading (888 of 19,921)"* -- 888 is the **final** row count of the access-group mapping
table, and 19,921 is the number of access rules in the source model. Two unrelated
numbers presented as a ratio, so the same line then read *"row count is static at 888,
the load likely moved on"* and several more polling cycles were spent on a table that
had already finished. The streamed narration said so directly.

The access-rule table is the long one -- roughly seventy seconds, tens of thousands of
rows, loaded in batches. Silence there is normal and the narration says as much in its
opening. Waiting is the correct behaviour.

## After the build

### Hand over the backend with clickable links before composing

The backend build finishes minutes before the dashboards are composed. **Give the user
the live objects immediately, with Snowsight URLs, so they can explore the agent and
semantic view while you compose the apps.** This is the most valuable handoff in the
whole run — the backend is verified, the agent works, and the user is otherwise waiting.

Build the Snowsight URLs from `CURRENT_ORGANIZATION_NAME()` and `CURRENT_ACCOUNT_NAME()`:

```
https://app.snowflake.com/<ORG>/<ACCT>/#/data/databases/<DB>/schemas/ANALYTICS/semantic-view/<SEMANTIC_VIEW>
```

For the agent, the playground URL is:
```
https://app.snowflake.com/<ORG>/<ACCT>/#/agents/<DB>.ANALYTICS.<AGENT>
```

**Hand them over in a message like this:**

> The backend is live. While I compose the dashboards, you can explore:
>
> - **[Cortex Agent playground](url)** — ask it a question (try: "What were total sales this fiscal year?")
> - **[Semantic view](url)** — the YAML and deployed view
> - **Glossary search** — `<DB>.ANALYTICS.<SEARCH_SERVICE>`
> - **8 reporting views** in `<DB>.ANALYTICS` (all `RPT_` prefixed)
>
> I'm composing the Streamlit and React dashboards now. Both are generated, not tested —
> a first run may hit a runtime error. Paste it back and I'll fix it.

Getting the org and account is one SQL call — `SELECT CURRENT_ORGANIZATION_NAME(), CURRENT_ACCOUNT_NAME()`.
You already have a connection open. This adds under a second to the handoff and gives the user something
real to work with instead of waiting.

Then run the verifier:

```bash
python3 pipeline/verify_deployment.py --connection <name> \
  --database <DB> --kb-database <KB_DB> --prefix <PREFIX> --deploy <mode>
```

**Pass the same namespace flags the build used.** Without them the verifier checks the
*default* database, which either passes against somebody else's objects or fails against
nothing — both confusing, neither about the build just run. `--deploy` matters too: on
`--deploy none` the Streamlit and App Runtime checks are reported as skipped instead of
failing on objects that were never meant to exist.

**Use `--format json` when checking whether an object exists.** `snow sql -q "show cortex
search services in account"` truncates the name column to fit the terminal, so a grep for a
service name finds nothing and the object looks missing when it is not. That cost a detour
believing a phase had produced nothing despite reporting `ok`:

```bash
snow sql -q "show cortex search services in account" --format json -c <name>
```

Read `references/composition-rules.md` before generating any page. The libraries in
`assets/` are fixed; pages compose against them and never restyle them.

**Then hand the apps over. Do not audition them.** See "Hand the app over" in
`composition-rules.md`: the only checks are that every file in `STREAMLIT_SOURCE` and `REACT_SOURCE` exists and that each composed Streamlit page compiles. No `npm run
build`, no dev server, no browser, no re-querying numbers the verifier already checked.
Say plainly that the apps are generated rather than tested, that a first run may hit a
runtime error, and that pasting the error back into CoCo fixes it against the composed
source. A ten-minute self-audit that finds nothing is worse than a caveat.

Then give the user something to ask. Pick a handful from
`references/example-prompts.md` -- a couple of number questions, one definition
question, and at least one of the two that the agent should decline to answer as
asked, because a generated agent that fabricates a plausible number is worse than
no agent and that is the fastest way to show it does not. Do not paste the whole
file.

## Rehearsing more than once

Idempotency makes a re-run safe, but it does not get you back to a clean account: an object
created by one run and dropped from the next plan lingers, and a `STREAMLIT` object holds a
`url_id` that a partial rebuild quietly reuses. Teardown exists for that.

```bash
# Dry run. Always do this first -- it prints every statement and changes nothing.
python3 pipeline/teardown.py --database <DB> --kb-database <KB_DB> --connection <name>

# Apply. Empties the schemas but keeps the containers.
python3 pipeline/teardown.py --database <DB> --kb-database <KB_DB> \
  --connection <name> --execute

# Also remove schemas, or schemas and databases.
  ... --execute --drop-schemas
  ... --execute --drop-databases
```

It enumerates objects from `INFORMATION_SCHEMA` rather than working from a hard-coded list,
which matters: the reference build's reporting views grew from seven to eight late on, and a
fixed list would have left the newest one behind -- precisely the object a rebuild then fails
to replace cleanly. Drop order runs consumers before producers, and the knowledge base goes
last so a failed teardown stays recoverable without re-extracting.

Failures do not stop the run. A missing object is the expected case on a partial build, and
re-running is always safe.
