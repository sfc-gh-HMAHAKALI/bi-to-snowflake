# BI to Snowflake

Inspect a local BI export, resolve its Snowflake bindings, and generate a reviewable run.
The six source adapters are vendored; install Python dependencies from `requirements.txt`.
Generation is offline. It does not provision compute, load demo rows or modify source tables.

## Quick start

Use the directory containing this README as `SKILL_DIR`.

```bash
python3 "$SKILL_DIR/pipeline/build.py" --guided --inventory /path/to/inventory.json
```

Or provide everything without prompts:

```bash
python3 "$SKILL_DIR/pipeline/build.py" \
  --extract /path/to/model.xml --source-type cognos \
  --database BI_WORK --analytics-schema REPORTING \
  --kb-database BI_METADATA --kb-schema CATALOG \
  --prefix LIBRARY --model-name OPERATIONS \
  --output-dir /path/to/private/run-001 --outputs semantic,catalog
```

If source references are not three-part Snowflake names, use `--config` with explicit mappings:

```json
{
  "version": 1,
  "inventory": "/path/to/inventory.json",
  "database": "BI_WORK",
  "analytics_schema": "REPORTING",
  "output_dir": "/path/to/private/run-001",
  "outputs": ["semantic", "catalog"],
  "source_mappings": {"LOANS": "SOURCE_LIBRARY.RAW.LOANS"}
}
```

```bash
python3 "$SKILL_DIR/pipeline/build.py" --config /path/to/run.json --dry-run
python3 "$SKILL_DIR/pipeline/build.py" --config /path/to/run.json
python3 "$SKILL_DIR/pipeline/verify_deployment.py" --run-dir /path/to/private/run-001
```

`--dry-run` prints the resolved plan and blockers, with no output writes or Snowflake calls.
A blocked generation writes `issues.json`, the inventory and model contract, but no deploy SQL.

## Why ANALYTICS was restricted

At the original public tip `ef61642`, `pipeline/config.py:15-19` described the choice not to
template schema names. Commit `3d2c2da34d756821b584d53dfce8a6137ea14fb2` added a guard at
`config.py:265-280`: Python accepted a custom schema while SQL still wrote to ANALYTICS.
Rejecting the input prevented a split deployment but did not make the pipeline portable.
The original wizard reinforced that restriction at lines 194-199. The new shared configuration
and generated identifier sites replace that restriction; ANALYTICS is not a required destination.

## Configuration

Precedence: explicit flags > configuration/naming file > explicitly resumed saved configuration > documented defaults.
Explicit flags win even when their value equals a default. Database, governed schema, source and
output directory are required. ANALYTICS is only an editable guided suggestion. Existing and new
schema names are accepted; schema/database creation remains a separate explicit operation.

Source mappings never inherit the output database/schema. The catalog has an independent
`kb_database`/`kb_schema`. Prefix and model name control generated object names. Quoted identifiers
retain case and embedded dots: `--analytics-schema '"Mixed.Schema"'`.
Connection, warehouse and execution role have no machine-specific defaults. Agent generation
requires a warehouse. Optional `grant_role` produces a separately reviewed grants file, not an
implicit privilege change. Do not put credentials in configuration files.

Select `semantic`, `agent`, `catalog`, `streamlit`, `react` with `--outputs` (comma-separated).
An agent or app adds the semantic dependency; catalog-only does not create an agent.
Legacy `--paths` aliases remain: 1=catalog, 2=both apps, 3=semantic+agent, 4=both apps+agent.
`--skip-physical` is accepted; source tables are always read-only.

## Runs and execution

Each output directory stores resolved configuration, input snapshot, model contract, issues,
artifact hashes and status. Use `--resume /path/to/run` to verify an unchanged run. A changed
source, destination or configuration invalidates reuse and requires a new output directory.
No old files are deleted or overwritten.

Backend execution is opt-in **after review**, using `--resume RUN --execute`. The saved configuration
must include connection, warehouse and role. Existing targets are rejected, not replaced. This path
has not been validated against a live account in this refactor. Review `deploy.sql` and validate
semantic importer compatibility before production use. Generated files are not proof of deployment.

Local app hosting still queries Snowflake. The generated Streamlit and React previews provide dataset, breakdown and metric selection,
ranked charts and exact rows. Their pre-aggregated chart mode preserves averages, ratios, nulls
and negative values without summing or folding categories; legacy chart defaults are unchanged.
React binds localhost and calls a fixed-query Python connector backend; never expose that local
single-user preview publicly. `--chat` adds independent-question chat to either app and includes
its agent dependency. Questions use bound parameters, and tool-unavailable warnings are surfaced.

`--deploy streamlit|react|both` prepares separate hosted variants, not a live deployment. Hosted
Streamlit uses the container runtime and `snowflake-callers-rights`; React/SPCS requires an ingress
caller token and has no service-owner fallback. Supply `compute_pool`, `warehouse`, `role` and,
for Streamlit, `external_access_integrations`; React requires an existing repository image path in
`container_image`. No pool, integration or repository is created automatically. Service names
must satisfy DNS-label constraints; ordinary generated objects retain quoted-identifier support.
Review the prepared hosting operations with:

```bash
python3 "$SKILL_DIR/pipeline/deploy_apps.py" --run-dir /path/to/private/run-001
# Only after backend execution, image preparation, grant review and explicit hosting approval:
python3 "$SKILL_DIR/pipeline/deploy_apps.py" --run-dir /path/to/private/run-001 --execute
```

The hosting runner rejects existing targets and partial-run replay, checks packaged files, and
records created objects after account verification. Creation is not runtime validation: verify
image/pool readiness, caller grants, two viewers with different access and agent answers before
production use. The Docker image itself has not been built or deployed during this refactor.

## Semantic boundaries

Source-derived scalar fields and declared aggregates are supported through a bounded SQL parser.
Names are not evidence of keys, aggregation or fiscal calendars. Explicit source keys and reviewed
join column/cardinality mappings are required. Unsupported expressions, source flags, ambiguous
filters and grain-dependent rollups block generation rather than disappear.
`translations` and `relationship_mappings` use references printed in `issues.json`.

All six adapters have real-parser positive fixtures. Coverage is bounded, not universal parity:
Cognos physical fields and declared aggregates; LookML scalar fields and declared measures;
Tableau single-table fields and explicit aggregates; Power BI TOM source columns and explicit
aggregates; Denodo single-table SELECT/WHERE/GROUP BY; BusinessObjects unambiguous table-bound
expressions. Tableau calculations and DAX measures require reviewed translations. Complex
relational transforms, ambiguous filter scope, bidirectional joins, object-level security and
unknown fiscal/rollup behavior remain blockers with original metadata retained.

### Reviewed row security

`security` maps each `security_rules:N` reference to exactly `source_hash`, `principal`, `role`,
`predicate` and `combination: "any_role"`. Compute the hash using
`pipeline/security_contract.py:rule_digest` over the complete inventory rule, then review its
principal and full Boolean predicate. The generator creates secure input views using
`IS_ROLE_IN_SESSION` and the reviewed predicate; unmatched roles receive no rows. Rules for one
entity are unioned only under that explicit any-role contract. Original source objects and their
privileges remain unchanged. A source hash proves review freshness, **not semantic equivalence**:
compare allowed/denied rows and role combinations against the original before deployment.
Missing rules, changed hashes, ambiguous entity scope and constant bypass predicates block.

The customer-specific sales model, synthetic loaders, fiscal calendar and bypass policy have been
removed. Old demos cannot be rerun without a source model and explicit mappings. The reusable
KB utilities remain reusable. Catalog generation emits prefixed KB tables, batched MERGEs and
consumer views through the existing loader, alongside glossary/ontology artifacts. It does not
automatically annotate or certify source tables. `--glossary-search` adds a definitions-only
Cortex Search service in the configured KB destination, plus a search tool when an agent is
selected. Search uses owner rights: review definitions before granting service access. Complete
KB source/security metadata is not included in default viewer grants; steward access is separate.

The legacy `modules.cli seed-data` utility is separately selected, never called by the build.
It requires explicit destination database/schema, rejects known source schemas, prefixes tables
with `SYNTHETIC_`, and uses create-only DDL. Its heuristic sample values are not model semantics.
Do not use synthetic data as evidence of source parity.

## Verification and confidentiality

```bash
bash "$SKILL_DIR/tests/run_all.sh"
python3 "$SKILL_DIR/tests/check_identifiers.py" --deny-terms-file /private/customer-deny-terms.txt
```

Offline tests deny network connections and Snowflake CLI subprocesses. Generated Streamlit
success/empty/error states and caller-scoped chat are tested headlessly with fake data. Both local
and hosted fictional React variants pass
TypeScript checks, a production build and executable chart-behavior tests. Run those separately:
`python3 tests/check_generated_apps.py --install --hosted` (downloads public dependencies; no live queries).
These tests do not establish browser fidelity or live Snowflake correctness. Sanitization scans tracked
and untracked committable paths, including literal-concatenated Python strings. Private deny terms
stay outside this repository. Binary files cause a review failure rather than being silently skipped.
Generic checks alone do not certify customer neutrality. Git history is unchanged and may retain
removed content; do not publish this work as a fully scrubbed history.

## Refactor validation record

The approved local implementation was checked with **143 passing offline tests**, the KB loader,
placeholder/library guards and deliberate sanitization failure controls. Generated local and hosted
React variants passed TypeScript and production builds; four Streamlit AppTests covered ordinary
states and caller-scoped chat. Reference fingerprint self-comparisons are checker sanity tests,
not fidelity evidence.

The current scan covered **135 committable paths**, including untracked additions, with zero generic
or supplied private-deny-term matches and zero binary/archive review gaps. A separate URL/email
review retained vendor API endpoints, documentation namespaces, example domains and repository
provenance. No release assets were returned by the repository release listing. These checks are
bounded evidence, not a guarantee that every possible confidential identifier is known.

Historical exposure is separate: the earlier targeted history audit found legacy matches in
42 commits across nine paths. This change does **not** rewrite that history or change visibility.
Live SQL compilation/importer round-trip, image build/upload, real caller isolation, agent answers,
and browser fidelity were not run; they remain deployment acceptance checks, not claimed results.
The installed skill and Snowflake account were not modified.

See `references/wizard.md` for guided operation and `references/runbook.md` for recovery.
