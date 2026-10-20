---
name: bi-to-snowflake
description: >
  Inspect BI model exports from Cognos Framework Manager, Tableau, Power BI, Looker,
  Denodo or SAP BusinessObjects and generate source-derived Snowflake artifacts.
  Use for BI migration, model.xml, semantic extraction and BI-to-Snowflake workflows.
---

# BI to Snowflake

## Setup

Use the directory containing this file as `SKILL_DIR`; do not assume an installation name.
Python 3.11+, the selected adapter dependencies and `requirements.txt` are prerequisites.
No connection is required for inspection or generation. Do not install into an unrelated
project environment or change the user's active connection silently.

## Workflow

1. Read `references/wizard.md`. Reuse the source path/type and choices already supplied.
2. Parse the source using the appropriate vendored adapter. Describe the inventory's
   `tables`, `dimensions`, `facts`, `metrics`, `filters`, `hierarchies` and reported issues.
   A model definition is not an existing dashboard; label synthesized output honestly.
3. Resolve source mappings separately from output locations. Ask for missing database,
   schema and output directory with editable suggestions. ANALYTICS is optional.
4. Generate a dry-run plan with `pipeline/build.py`; show actual destinations, dependencies
   and blockers. Preserve source filters, aggregation, calendar, grain and security intent.
5. Generate locally after configuration is resolved. Never claim a blocked model is complete.
6. Verify saved artifact hashes and run applicable offline tests. Inspect generated SQL and
   semantic output separately from live validation. Ask before live execution.
7. Deliver full artifact paths, validation results and remaining limitations. Do not open
   reports, publish content, update the installed skill or commit without authorization.

## Stopping points

- Missing source mappings, unsupported expressions, ambiguous grain/joins, or unresolved security.
- Before account writes, source annotations, grants, hosting or cleanup. Supplying a warehouse
  or destination is not deployment consent.
- Before replacing existing targets; the executor rejects them rather than assuming ownership.

## Outputs

A private run directory with resolved configuration, inventory, model contract, issue register,
manifest and selected generated artifacts. `semantic` selects OSI, `agent` adds an agent resource,
`catalog` emits prefixed relational KB tables/views, glossary and ontology proposals. App selections
emit source-derived previews and reporting queries; `--chat` adds the agent dependency and
parameterized independent-question chat. `--glossary-search` adds definitions-only Search.

The current local previews compose the locked ranked-bar library and exact-grain tables.
Use its explicit pre-aggregated mode for query results; default chart behavior still aggregates.
Hosted variants are generated with `--deploy streamlit|react|both` and explicit existing compute
resources. Use `pipeline/deploy_apps.py --run-dir RUN` to review and, only after separate approval,
add `--execute`. Both hosted runtimes query as the viewer without owner fallback. Missing resources
are blockers, not an invitation to invent names or create integrations.

Row security requires explicit principal/predicate mappings bound to complete source-rule hashes.
Review `security-review.json`; a matching hash does not prove equivalent permissions. Unsupported
security scope, calculations and grain remain blockers. Review Search definitions for metadata
exposure and keep raw KB/security records steward-only. Preserve component styling and existing
chart defaults. Unsupported chart shapes are not auto-composed; never substitute sum-of-averages.
The output is a synthesized explorer, not a pixel-equivalent copy of the source dashboard.

See README for CLI configuration and compatibility changes. Historical customer material may
remain in Git history even after working-tree sanitization; publishing needs a separate review.
