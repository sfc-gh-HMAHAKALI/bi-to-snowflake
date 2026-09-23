# Demo runbook

The narrative: open with the finished dashboard, show the skill, hit build, walk the HTML
while it runs, come back to something that looks like what the room already saw.

This file is what makes that survivable. Read it the day before, not the hour before.

## Prerequisite that blocks everything

**The source `model.xml` must be on disk.** Point `--extract` at wherever you keep it
and no longer exists; it cannot be regenerated from the knowledge base, because extraction
runs one way. Nothing here -- rehearsal, convergence measurement, or the live run -- is
possible until it is supplied again.

Once you have it, put it somewhere permanent, not `/tmp`. That is how the first copy was lost.

## The day before

### 1. Rehearse a full cold run, twice

```bash
cd "$HOME/.snowflake/cortex/skills/bi-to-snowflake"

# Clean slate
python3 pipeline/teardown.py --database $DB --kb-database $KB_DB \
  --connection my-demo-account                      # dry run, read it
python3 pipeline/teardown.py --database $DB --kb-database $KB_DB \
  --connection my-demo-account --execute

# Cold build
time python3 pipeline/build.py --paths 4 --requirements \
  --extract /path/to/model.xml --connection my-demo-account

# Second run, to prove idempotency: same command, should converge not duplicate
time python3 pipeline/build.py --paths 4 --requirements \
  --extract /path/to/model.xml --connection my-demo-account
```

Record the cold number. Expect **6 to 10 minutes**; the phases measured on the reference
model total 203 seconds, of which the two deploys are 155, and extraction, knowledge-base
load and page composition sit on top. The second run should be markedly faster.

### 2. Measure convergence -- the number that decides whether the opener matches

```bash
# Fingerprint each generated run
python3 tests/composition_fingerprint.py emit --kind react \
  --path <generated>/components/report.tsx -o /tmp/run1.json
# ... repeat for run 2 and run 3, then
python3 tests/composition_fingerprint.py compare /tmp/run1.json /tmp/run2.json
python3 tests/composition_fingerprint.py compare /tmp/run1.json tests/reference/react.json
```

Read the similarity figure honestly. Above 90% the layouts are the same dashboard with
cosmetic differences. Below that, the composition rules are too loose somewhere: find the
disagreement in the `only in A` / `only in B` lines and tighten
`references/composition-rules.md` until runs stop disagreeing. That is the fix -- not
re-rolling until you get a good one.

### 3. Pre-warm, or the first two minutes are dead air

```sql
ALTER COMPUTE POOL <DB>_<PREFIX>_SIS_POOL RESUME;
-- confirm ACTIVE, not STARTING, before you stop watching
SHOW COMPUTE POOLS LIKE '<DB>_<PREFIX>_SIS_POOL';
```

Auto-suspend is already 7200s on both the compute pool and the application service. That was
raised from 300s after a live demo hit `ERROR_SERVICE_UNAVAILABLE` mid-`SUSPENDING`: a
service on its way down does not cleanly wake. Do not lower it.

A Docker layer cache from a recent deploy takes roughly a minute off the React build. If you
rehearsed the day before, you have one.

### 4. Record a fallback

Capture the rehearsal run on video. If minute four goes quiet you want a cut, not an apology.
Decide in advance what you say and switch to.

## The run itself

Recommended answers at the wizard:

| Question | Answer | Why |
|---|---|---|
| BI tool | IBM Cognos Framework Manager | The only adapter rehearsed through the full build. |
| Outputs | Semantic view, Cortex Agent, React dashboard, Requirements scorecard | Path 4 plus the scorecard. Drop Streamlit to save 62 seconds unless you plan to show both. |
| Deploy | Streamlit deployed, React on localhost | Saves ~95 seconds and a Docker build. React renders identically. |

Show `--dry-run` first. It prints the plan and changes nothing, which makes the point that
this is a defined pipeline rather than improvisation:

```bash
python3 pipeline/build.py --paths 4 --requirements --dry-run
```

18 phases for that selection. Then run it for real and switch to the HTML.

## What to say while it builds

Phase order matches the story, so narrate in order:

1. **Knowledge base** -- the model is read once into 14 tables. Everything downstream is
   generated from those, not from the file.
2. **Physical layer** -- the schema is emitted from `KB_TERM`, not hand-written.
3. **Semantic view** -- created by Snowflake's Ossie importer and exported back to YAML, so
   portability is demonstrated rather than claimed.
4. **Row access policy** -- 19,051 Cognos security filters collapse to one policy.
5. **Agent and SQL bridge** -- one entry point both apps and any JDBC client can call.
6. **Reporting views** -- because `SEMANTIC_VIEW()` is its own query syntax that the Node
   driver cannot speak.
7. **Caller grants** -- then the apps.

## Two things to be honest about when asked

**"Is this reproducing our reports?"** No, and it cannot. Framework Manager is a modelling
layer: it declares query subjects, determinants, hierarchies and aggregation rules, and it
declares no reports, charts or dashboards at all. Those live in Cognos Analytics report
specifications, which were not in the file. The dashboards are designed from the model's
semantics, not cloned.

**"Are those numbers from our data?"** For the dashboards, yes. For the cube behaviours in
the requirements scorecard, only E5 is measured entirely on extracted model tables. E1, E3
and E4 join the real sales fact to a purpose-built companion, and E2 and E6 run wholly on
built tables -- because the reference subject area has six tables and contains no forecast,
currency conversion, supplier or stock snapshot. Those were the examples the requirements
themselves named. Section E of the overview HTML states this per row; do not soften it.

A related finding worth volunteering: **none of the 4,070 aggregation rules in the model is
semi-additive.** The Sum-and-Last behaviour E6 asks about is not currently used anywhere in
this subject area.

## If it fails mid-run

Every phase is idempotent -- DDL is `CREATE OR REPLACE`, the knowledge-base load is a `MERGE`
on the source key, data loads `TRUNCATE` before `INSERT`. Re-run the same command; it
converges rather than duplicating.

| Symptom | Cause |
|---|---|
| `does not exist or not authorized` on every app query | Caller grants phase did not run, or an `RPT_` view was added after it. Re-run `sql/51_caller_grants.sql`. |
| `ERROR_SERVICE_UNAVAILABLE` | Service was mid-`SUSPENDING`. Wait, then resume. |
| Streamlit shows old code after a redeploy | `PUT` to the source stage does not update the app. Copy files into the live version location. |
| React deploy reverts a manual `ALTER` | `snow app deploy` re-applies `app.yml`. Change the file, not the object. |

Never `CREATE OR REPLACE STREAMLIT`. It mints a new `url_id` and breaks the published link
you opened the meeting with.

## After

```bash
python3 pipeline/verify_deployment.py --connection my-demo-account
```

Then report what exists, with fully qualified names and URLs.
