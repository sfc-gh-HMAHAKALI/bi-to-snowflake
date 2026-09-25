---
name: bi-to-snowflake
description: >
  Takes a BI source file -- IBM Cognos Framework Manager, Tableau, Power BI, Looker,
  Denodo, or SAP BusinessObjects -- and builds the Snowflake layer from it in one guided
  run: knowledge base, Ossie-compliant semantic view, Cortex Agent, Horizon catalog and
  glossary, and a Streamlit or React dashboard generated against a locked component
  library. Self-contained; no sibling skills required. Use when a user points at a BI
  model file and wants it rebuilt on Snowflake, or asks what is inside one.
  Triggers: cognos, framework manager, model.xml, BI migration, rebuild my dashboard on
  snowflake, semantic view from BI file, recreate cognos in snowflake, bi to snowflake.
---

# BI to Snowflake

One guided run: a BI model file in, a governed Snowflake layer and a working dashboard out.

Self-contained. Source parsing, the dashboard component libraries and the Snowflake build
are all vendored here -- see `VENDOR.md` for provenance and re-sync. Nothing else needs to
be installed.

## Setup

Every command below runs from the directory holding this `SKILL.md` — the skill you just
loaded, whatever it is named. Set `SKILL_DIR` to that path once and use it throughout.

```bash
SKILL_DIR="$HOME/.snowflake/cortex/skills/<this-skill-name>"   # e.g. bi-to-snowflake
cd "$SKILL_DIR"
```

**Do not hardcode `bi-to-snowflake`.** This skill gets cloned and renamed for testing, and a
hardcoded name silently runs a *different* installed skill whose vendored code has drifted —
which cost one debug session an afternoon of chasing a parse failure that was really a wrong
working directory. `pipeline/build.py --skill-dir` defaults to its own repo root for the same
reason.

If a parse fails on a missing module:

```bash
python3 -m pip install -r requirements.txt
```

## Layout

| Path | What |
|---|---|
| `modules/` | BI source parsing. Six adapters, one per BI tool. |
| `assets/react_ui/` | Locked React component library -- `theme.ts`, `charts.tsx`, 14 exports. |
| `assets/streamlit_ui/` | Locked Streamlit component library -- palette, KPI card, ten chart shapes. |
| `pipeline/` | The Snowflake build: phase graph in `build.py`, 20 DDL files in `sql/`. |
| `references/` | Composition rules, per-adapter notes, example prompts. |

## The one rule that matters

**Never restyle the component libraries, and never write chart code in a generated page.**

The libraries in `assets/` are fixed. Generated pages compose against them -- choosing which
component goes where and wiring the customer's fields into it. That is what makes two runs
over the same model produce the same look, and it is the whole reason a generated dashboard
can match one built by hand.

Composition choices are governed by `references/composition-rules.md`, not by taste.

**Composed apps are handed over, not auditioned.** The rules in that file exist so the
known defects never reach the page; they are not a checklist to re-verify afterwards.
Check that the source files exist and that each Streamlit page compiles, then deliver,
saying plainly that the apps are generated rather than tested and that a runtime error
pasted back into CoCo is a quick fix. The backend verifier covers the views, semantic
view, agent and grants -- that is the part that is actually checked.

## What generalises, and what does not

Worth being exact about. The parse is genuinely general across six BI tools; the build
on top of it is not general all the way up, and the boundary is narrower than it looks.

**Model-independent — works on any parsed model with no edits:**

| Layer | Derived from |
|---|---|
| Parse and inventory | the source file; Cognos, Tableau, Power BI, Looker, Denodo, SAP BO |
| Knowledge base schema and load | source-agnostic, keyed `(SOURCE_SYSTEM, SOURCE_MODEL, SOURCE_OBJECT_ID)` |
| Horizon catalog: comments, tags, glossary, ontology | generated from the knowledge base; no entity names hardcoded |
| Naming, connection preflight, progress narration | model-independent |
| `verify_composition.py` | parses whatever the reporting views declare |

**Bound to the reference model's star schema.** One list is the pivot:
`CORE_ENTITIES` in `pipeline/path3_ossie_semantic_view.py` names six tables
(`FACT_SALES_SUMMARY`, `FACT_BOOKED_SUMMARY`, `DIM_TIME`, `DIM_US_PROD_CUSTOMERS`,
`TERRITORY_SITE_SALES_PERSON`, `EDW_ORA_COA_PROD_GFP_W_NORA`).
`generate_physical_layer.py` **imports that same list**, so both the physical layer and
the semantic view are scoped to those six entities, and everything downstream inherits
it:

| File | What is model-specific |
|---|---|
| `path3_ossie_semantic_view.py` | `CORE_ENTITIES`, per-entity dimension allowlists, filters, grain keys |
| `generate_physical_layer.py` | imports `CORE_ENTITIES`; columns themselves come from `KB_TERM` |
| `sql/45_reporting_views.sql` | static; names `SALES_AMOUNT_TOTAL`, `GPC1`-`GPC4`, `TERRITORY_LEVEL1`-`4`, a July-June fiscal calendar |
| `sql/30_row_access_policy.sql` | territory-hierarchy policy |
| `sql/12_dimension_data.sql`, `sql/13_fact_data.sql` | demo data for this shape; skipped by `--skip-physical` |

So a different subject area — HR, supply chain — parses correctly, loads a complete and
useful knowledge base, and gets a full Horizon catalog and glossary. It does **not** get
a working semantic view, reporting views or dashboards without editing that list and
writing its own reporting views.

**This is the skill's main generalisation debt, and it is not hidden behind the
Cognos adapter — it is in the semantic layer.** Deriving `CORE_ENTITIES` from
`KB_GRAIN` and `KB_RELATIONSHIP` (the facts and the dimensions they join to are both
already recorded) and generating the reporting views from `KB_METRIC` and
`KB_HIERARCHY` would remove it. Nothing about the knowledge base blocks that; the
information is there.

## Workflow

The guided wizard, the output selector and the phase graph are documented in
`references/wizard.md`. Load it before starting a run.

**The first thing to hand back, before any questions about what to build, is a
description of their model.** Parse the file and write the digest -- about two seconds,
no Snowflake connection needed -- then give them the path:

```bash
python3 -m modules.cli parse --type <type> "<path>" -o /tmp/b2s/inventory.json
python3 pipeline/describe_model.py --inventory /tmp/b2s/inventory.json \
        --source "<path>" --out /tmp/b2s/model-overview.md
```

It tells them what is in their model, what repeats, how access is controlled and what
needs a human eye. It also gives them something to read during the knowledge base load,
which is the longest phase of the build. Do not wait for the build to produce it: the
`describe` phase runs after three Snowflake DDL phases, so by then they have already
committed.

When a run finishes, offer a few prompts from `references/example-prompts.md`.
A user handed a new agent with no idea what to ask it concludes it does not
work.
