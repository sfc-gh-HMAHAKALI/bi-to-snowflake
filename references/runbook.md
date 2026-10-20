# Run and recovery guide

## Before generation

Keep the source export and private run configuration outside the repository. Select the correct
adapter; resolve source bindings independently from generated destinations. Run `--dry-run` with
the same inputs intended for generation. A nonzero exit indicates blockers, not a partial success.

## Inspect generated artifacts

- `resolved-config.json`: settings for this run; connection names, never credentials.
- `inventory.json`: source adapter output and source metadata.
- `model-contract.json`: resolved tables, fields, metrics, relationships and restrictions.
- `issues.json`: unresolved semantics with references for correction.
- `manifest.json`: fingerprint, file hashes, status and execution progress.
- `deployment-plan.json` and `deploy.sql`: selected backend writes, generated only when unblocked.
- `grants-review.sql`: optional privileges requiring separate review.

No physical source tables or demo rows are created. Existing objects are not replaced automatically.
Catalog generation reuses the KB loader offline to produce prefixed tables, consumer views and
batched MERGEs, plus a glossary table and ontology proposal. It does not annotate source objects
or certify definitions. Optional glossary Search indexes term names/definitions with owner rights.
Keep full KB/security metadata restricted to stewards; review the separate viewer-grants file.

`security-review.json` records reviewed row controls on generated secure views. Compare complete
source predicates and role combinations, including denied users; source hashes only detect drift.
`search-review.json` records indexed content and exposure. `hosting-plan.json` lists separate hosting
operations; `caller-grants-review.sql` must be reviewed by an administrator. Existing compute pools,
EAIs, image repositories and ordinary viewer privileges are prerequisites, not auto-created objects.

Use `deploy_apps.py --run-dir RUN` for an offline hosting preview, and add `--execute` only after
backend execution and explicit approval. Build/upload the React linux/amd64 image first. Streamlit
requires Snowflake CLI 3.14+ and package-access EAI. A created object still requires runtime,
caller-isolation and agent-answer validation; no live deployment was tested during this refactor.

## Recovery

Use `--resume RUN` only with unchanged configuration, source and artifacts. When the target or model
changes, choose a new output directory and regenerate. Prior runs remain untouched. Partial
account execution requires object/ownership reconciliation before further writes; automatic
mutation replay is refused. If execution
fails between a Snowflake commit and manifest update, the next run may refuse an existing object;
inspect it rather than treating absence from the manifest as permission to replace it.

`verify_deployment.py --run-dir RUN` checks file integrity offline. `verify_composition.py --run-dir RUN`
also regenerates and compares backend, app, hosting and grants artifacts. Neither checks the account.
`teardown.py --run-dir RUN` prints a review-only cleanup plan for recorded created objects. It never
sweeps schemas, drops databases, uses CASCADE or executes the plan. Verify current ownership first.

## Validation boundaries

Offline tests cover configuration, parsing and generation. Live semantic import/round-trip,
metric reconciliation, source access, row-level access tests and app rendering are separate checks.
Do not publish a demo or claim parity until the relevant checks have actually passed.
