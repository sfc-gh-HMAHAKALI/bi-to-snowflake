# Example prompts

## Start a run

- Inspect my Cognos model.xml and report unsupported expressions before building anything.
- Generate only a semantic definition from this LookML project into my chosen reporting schema.
- Use the source mappings in my configuration; do not move or create source tables.
- Generate a catalog glossary in a separate database from the semantic view.
- Show the resolved destinations before any deployment.

## Ask the generated agent

Choose actual names from model-contract.json. Do not reuse questions from a different model.

- What is <declared metric> by <declared dimension>?
- Which definition and source object support that number?
- Does the source define the fiscal period I asked for, or is more information needed?
- Can you answer a question about a measure absent from this model? Explain what is missing.

Only describe a glossary-search tool, chart tool or app chat panel if it actually exists in the
selected artifacts and has been deployed and tested. An agent specification alone is not evidence
that it can answer a question correctly.
