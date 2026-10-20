# Guided workflow

Reuse choices already given. Ask only for unresolved inputs using ask_user_question, with at
most four related questions per call. Text inputs must have editable suggestions.

## 1. Inspect the source

Resolve source path and adapter (cognos, tableau, powerbi, looker, denodo, businessobjects).
Do not ask for the type again when the source establishes it. Parse locally; no account writes.
Explain extracted tables, dimensions, facts, metrics, filters, hierarchies and issues before
asking what to build. Keep source files private and leave archive extraction to the adapter.

## 2. Resolve configuration

Offer outputs independently: semantic view, agent, catalog, Streamlit, React. Record them in
`outputs`; use explicit names rather than legacy numeric paths. Agent/apps require semantic;
catalog alone does not. Reuse the prefix/model name supplied or suggest editable model-derived names.

Ask for the generated database and schema. ANALYTICS is only one editable suggestion. Accept
an existing schema or a new name; creating a new database/schema needs a separate approved action.
Ask separately for catalog destination if different. Source bindings are not output destinations.
Resolve unqualified/ambiguous sources through `source_mappings` with full Snowflake identifiers.
Quoted mixed-case names are supported; never strip their quotes or split them on a bare dot.

Use a private output directory per run. Supply connection, warehouse and role only when needed
for agent resources or future execution; do not assume a local connection name. Reuse existing
answers and show a compact resolved summary, not a repeated wizard.

## 3. Generate and review

```bash
python3 "$SKILL_DIR/pipeline/build.py" --config /path/to/run.json --dry-run
python3 "$SKILL_DIR/pipeline/build.py" --config /path/to/run.json
```

Or use `--guided` with a source/inventory. This terminal helper asks only for missing required
values. A CoCo-guided run should gather those through ask_user_question and pass explicit flags.
The two routes resolve through the same configuration model.

Show the exact source objects, semantic/catalog destinations, selected outputs, warehouse and
role. Explain any expanded dependencies. Report `issues.json` blockers with their source references.
Unsupported semantics are not optional decorations: do not remove filters, security or rollups
merely to get a green build. Correct the inventory/mappings with evidence, then generate a new run.

## 4. Execution boundary

Generation, local hosting, and account deployment are distinct. `--deploy none` does not authorize
backend writes. Without `--execute`, this orchestrator stays offline. Local apps query already
deployed Snowflake objects when opened. Non-local hosting generates caller-scoped artifacts with
explicit existing compute pool/warehouse/role, Streamlit package EAI and/or React container image.
It never falls back to localhost or claims that artifact generation created a hosted application.

After a user explicitly approves the reviewed backend plan:

```bash
python3 "$SKILL_DIR/pipeline/build.py" --resume /path/to/run --execute
```

The saved run must include connection, warehouse and role. Review grants separately. For hosting,
review `hosting-plan.json` using `pipeline/deploy_apps.py --run-dir RUN`; only after backend creation,
image readiness and separate approval add `--execute`. Verify two distinct viewers' allowed/denied
access. Do not run live actions as part of skill refactoring/tests.

Security mappings require the complete source-rule hash, exact principal, mapped Snowflake role,
full predicate and explicit `any_role` combination. Do not infer entitlement from column names.
`--chat` adds the agent dependency; `--glossary-search` adds catalog and indexes definitions only.
Explain Search owner-rights exposure and require description review before granting access.

## 5. Resume and handoff

```bash
python3 "$SKILL_DIR/pipeline/build.py" --resume /path/to/run
python3 "$SKILL_DIR/pipeline/verify_deployment.py" --run-dir /path/to/run
```

Changed source, mapping or destination invalidates artifacts. Generate into a new directory without
`--resume`; do not delete the previous run. A modified artifact fails its hash check. The verifier is
an offline integrity check, not proof of live SQL, agent responses or browser rendering.

Deliver clickable full file paths, selected outputs, tests actually run and unresolved limitations.
The synthesized explorer includes exact-grain ranked bars, tables and optional chat. Other chart
shapes require compatible grain and explicit composition; do not label it as a feature- or
pixel-equivalent replacement. Generate example questions from actual metric names and include
one out-of-scope question; do not reuse a previous model's fiscal assumptions.
