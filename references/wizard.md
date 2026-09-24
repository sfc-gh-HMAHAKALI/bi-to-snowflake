# The guided run

Two rounds of questions with a cheap parse between them, then one costed confirmation before
anything is created. The parse sits in the middle on purpose: round 2 asks about a model you
have already read, so the counts shown are real rather than promised.

Never skip to the build. A user who wanted one semantic view does not want two dashboards.

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
```

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
```

**Ask for the database only, never the schema.** The schema names are fixed: the DDL in
`pipeline/sql/` hardcodes `ANALYTICS`, `SALES_ANALYTICS`, `COMMON_ANALYTICS` and
`KNOWLEDGE_BASE`, and `--analytics-schema` now rejects any other value rather than applying
it to half the pipeline. Asking "which database and schema?" produced an answer that was
silently discarded and then a confirmation gate that named `PUBLIC`, where nothing was ever
created. One inert question is worse than one fewer question.

```yaml
  - header: "Deploy"
    question: "How should the dashboards run?"
    options:
      - label: "Streamlit deployed, React on localhost"
        description: "Fastest. React renders identically and starts in seconds. Recommended for a live walkthrough."
      - label: "Both deployed to Snowflake"
        description: "Full SPCS deploy. Adds roughly 95 seconds and a Docker build."
      - label: "Both local / Nothing deployed"
        description: "Generate and verify the code; run dashboards locally against Snowflake."
```

Only ask the deploy question if a dashboard was selected.

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

A dashboard plus an agent is path 4 rather than `2,3` because an embedded chat panel that
cannot see the report's filter state is a different product. Path 4 builds them together.

### Deploy mapping

| Deploy selection | `--deploy` flag | What deploys to Snowflake | What runs locally |
|---|---|---|---|
| Streamlit deployed, React on localhost | `--deploy streamlit` | Streamlit report (container runtime) | React (`cd pipeline/app_react && npm install && npm run dev`) |
| Both deployed to Snowflake | `--deploy all` | Streamlit + React App Runtime service | None needed |
| Both local / Nothing deployed | `--deploy none` (or `local`) | Backend only (views, semantic view, agent) | Streamlit (`streamlit run pipeline/app_streamlit/app.py`) and React (`cd pipeline/app_react && npm run dev`) |

If omitted, `--deploy` defaults to `all`. `--skip-deploy` is accepted as an alias for
`--deploy none`.

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
| Streamlit | `pipeline/app_streamlit/` | `app.py`, `data.py`, `pages_impl.py`, `metrics.py`, `pyproject.toml`, `bim_ui/{__init__,compat,filters}.py`, `.streamlit/config.toml` |
| React | `pipeline/app_react/` | `app.yml`, `package.json`, `lib/queries.ts`, `tailwind.config.ts`, `postcss.config.mjs`, `app/globals.css`, plus the Next.js tree |

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

  <one line per selected output, naming the object>

Plan: <N> phases, roughly <N> minutes cold.
Proceed?
```

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

## After the build

Report what was created with fully qualified names and URLs, then run the verifier:

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
