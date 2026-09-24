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
cd "$HOME/.snowflake/cortex/skills/bi-to-snowflake"
python3 -m modules.cli parse --type <type> "<path>" -o /tmp/b2s/inventory.json
```

Then read the counts out of the inventory: `dimensions`, `measures`, `drill_paths`, `tables`,
and `provenance`. These are what round 2 and the confirmation are built from.

Report anything surprising at this point rather than after the build:

- **No date dimension.** Mix and seasonality will not be generated, and KPI sparklines are
  not possible. Say so now.
- **Fewer than four measures.** The KPI band degrades. Say so now.
- **No hierarchy with two or more levels.** Treemap, sunburst and per-hierarchy views all
  drop out, which is most of the dashboard.
- **`dashboards` marked synthesised in `provenance`.** For Cognos this is always true and is
  not a defect: Framework Manager is a modelling layer and declares no reports, charts or
  dashboards at all. Never describe the result as a reproduction of existing reports.

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
    question: "Which database and schema should this build into?"
    type: text
    defaultValue: "<from inventory snowflake_target, else DB.SCHEMA>"
  - header: "Deploy"
    question: "How should the dashboards run?"
    options:
      - label: "Streamlit deployed, React on localhost"
        description: "Fastest. React renders identically and starts in seconds. Recommended for a live walkthrough."
      - label: "Both deployed to Snowflake"
        description: "Full SPCS deploy. Adds roughly 95 seconds and a Docker build."
      - label: "Nothing deployed"
        description: "Generate and verify the code only."
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
cannot see the report's filter state is a different product. Path 4 deploys them together.

## The confirmation gate

Show this before creating anything. It is the last point at which a wrong target schema
costs nothing.

```
From <file name> (<source_type>):
  <N> dimensions, <N> measures, <N> drill paths across <N> tables
  <note any degradation found during profiling>

Will create in <DATABASE>.<SCHEMA>:
  <one line per selected output, naming the object>

Plan: <N> phases, roughly <N> minutes cold.
Proceed?
```

Get the plan and the phase count from the build itself rather than estimating:

```bash
python3 pipeline/build.py --paths <p> --dry-run
```

Timing to quote, measured on the reference model: the knowledge base, views, bridge and
reporting views total about 42 seconds. A Streamlit deploy adds 62, caller grants 6, a React
deploy 93. Extraction, knowledge-base load and page composition sit on top, so a cold run
with everything is **6 to 10 minutes**. Warm, with the compute pool already active and image
layers cached, it is considerably less.

## Running it

```bash
python3 pipeline/build.py --paths <p> \
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
python3 pipeline/verify_deployment.py --connection <name>
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
