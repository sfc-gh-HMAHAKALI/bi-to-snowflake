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

## Workflow

The guided wizard, the output selector and the phase graph are documented in
`references/wizard.md`. Load it before starting a run.

When a run finishes, offer a few prompts from `references/example-prompts.md`.
A user handed a new agent with no idea what to ask it concludes it does not
work.
