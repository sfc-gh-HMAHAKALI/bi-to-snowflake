# bi-to-snowflake

Takes a BI model file and builds the Snowflake layer from it in one guided run: knowledge
base, Ossie-compliant semantic view, Cortex Agent, Horizon catalog and glossary, and a
Streamlit or React dashboard.

Self-contained. Source parsing, both dashboard component libraries and the Snowflake build
are vendored here; nothing else needs installing. See `VENDOR.md` for where each tree came
from and how to re-sync it.

## Quick start

```bash
cd "$SKILL_DIR"   # the directory holding SKILL.md; do not hardcode the skill name
python3 -m pip install -r requirements.txt          # only if a parse fails

# 1. Read the model
python3 -m modules.cli parse --type cognos /path/to/model.xml -o /tmp/inventory.json

# 2. See the plan without touching anything
python3 pipeline/build.py --paths 4 --dry-run

# 3. Build (choose --deploy all, streamlit, or none for local dev)
python3 pipeline/build.py --paths 4 --deploy streamlit \
  --extract /path/to/model.xml --connection <name>
```

`--extract` and `parse` both accept the export as it came: a `model.xml`, the
project directory, or the `.zip`/`.cpf` it downloaded as. Archives are extracted
into a fresh temporary directory with zip-slip and zip-bomb guards, so there is
no unzip step to run first.

## Docs

| File | Read it when |
|---|---|
| `SKILL.md` | Entry point. Layout and the one rule that matters. |
| `references/wizard.md` | Running a guided build. The two question rounds, the output-to-path mapping, teardown. |
| `references/composition-rules.md` | Before generating any page. How a layout is decided. |
| `references/runbook.md` | Before demoing this live. Pre-warm, timings, fallbacks, hard questions. |
| `VENDOR.md` | Changing anything under `modules/` or `assets/`. |

## The idea

Dashboards are **generated, not copied** -- but they are generated against a component
library that ships fixed. Palette, axes, KPI card, and ten chart shapes live in
`assets/react_ui/` and `assets/streamlit_ui/` and are never restyled by a generated page. A
page chooses which component goes where and wires the model's fields into it.

That split is what lets a generated dashboard look like a hand-built one. It also makes the
output reproducible enough to demo: `references/composition-rules.md` is written so the
*data* decides the layout, and `tests/composition_fingerprint.py` measures whether two runs
over the same model actually agree.

## Tests

```bash
./tests/run_all.sh
```

No Snowflake connection needed. Covers Python syntax, the 41 Cognos adapter tests, that both
libraries still expose every shape, that the composition rules cite only components that
exist, that the reference fingerprints self-compare at 100%, and that teardown refuses to act
without `--execute`. Both library checks are verified by negative control.

## Naming

Every object name this pipeline creates comes from `pipeline/config.py`. Nothing is
hardcoded to one account, and the DDL in `pipeline/sql/` carries `{{NAME}}`
placeholders that are substituted at run time.

```bash
python3 pipeline/build.py --paths 4 \
  --database MY_ANALYTICS --kb-database MY_ANALYTICS_KB --prefix ACME \
  --model-label "Acme Sales Model" \
  --extract /path/to/model.xml --connection <name>
```

| Flag | Default | What it names |
|---|---|---|
| `--database` | `BI2SF` | Target database for the analytics layer |
| `--kb-database` | `BI2SF_KB` | Database holding the knowledge base |
| `--kb-schema` | `KNOWLEDGE_BASE` | Schema holding the knowledge base |
| `--analytics-schema` | `ANALYTICS` | Schema for the governed views and agent |
| `--prefix` | `BI` | Prefixes every created object, so two models can share an account |
| `--model-name` | `SALES_BOOKINGS` | Semantic view name, after the prefix |
| `--model-label` | `BI Model` | Human label used in comments and tag values |
| `--naming-file` | - | Read any of the above from JSON; explicit flags still win |

`build.py` forwards these to every child script, so a run cannot write half its
objects into one database and half into another. `render_sql` raises on an unknown
placeholder rather than passing it through, because an unsubstituted `{{NAME}}`
would reach Snowflake as an invalid identifier and fail a phase after earlier
statements had already committed.
