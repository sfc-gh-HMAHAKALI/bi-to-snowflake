"""Offline-first orchestrator. Execution is a separate, explicit action."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

import config
from model_contract import resolve_inventory
from artifacts import generate

ROOT = Path(__file__).resolve().parents[1]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    config.add_arguments(result)
    for name in ('connection', 'warehouse', 'role', 'grant_role', 'source_type', 'extract', 'inventory', 'output_dir', 'outputs', 'deploy', 'compute_pool', 'container_image'):
        result.add_argument('--' + name.replace('_', '-'), default=argparse.SUPPRESS)
    result.add_argument('--glossary-search', action=argparse.BooleanOptionalAction, default=argparse.SUPPRESS)
    result.add_argument('--chat', action=argparse.BooleanOptionalAction, default=argparse.SUPPRESS)
    result.add_argument('--config')
    result.add_argument('--resume', metavar='RUN_DIRECTORY')
    result.add_argument('--guided', action='store_true')
    result.add_argument('--dry-run', action='store_true', help='Inspect and print plan without writing files or connecting')
    result.add_argument('--execute', action='store_true', help='Execute reviewed backend plan; requires --resume')
    result.add_argument('--paths', help='Legacy alias: 1=catalog, 2=apps, 3=semantic+agent, 4=apps+agent')
    result.add_argument('--all', action='store_true')
    result.add_argument('--skip-deploy', action='store_true')
    result.add_argument('--skip-physical', action='store_true', help='Compatibility flag: sources are always read-only')
    return result


def load_source(settings):
    if settings.inventory:
        return config.read_json(settings.inventory)
    sys.path.insert(0, str(ROOT))
    from modules.cli import _run_parser
    from modules.output.inventory import build_unified_inventory
    parsed = _run_parser(settings.extract, settings.source_type)
    return build_unified_inventory(parsed, settings.source_type)


def atomic_json(path, value):
    with tempfile.NamedTemporaryFile('w', dir=path.parent, encoding='utf-8', delete=False) as handle:
        json.dump(value, handle, indent=2)
        temporary = Path(handle.name)
    temporary.replace(path)


def verify_run(directory):
    directory = Path(directory).resolve()
    manifest = config.read_json(directory / 'manifest.json')
    required = {'resolved-config.json', 'inventory.json', 'model-contract.json', 'issues.json'}
    if manifest.get('version') != 1 or not isinstance(manifest.get('artifacts'), dict):
        raise ValueError('Invalid run manifest')
    if not required.issubset(manifest['artifacts']):
        raise ValueError('Manifest omits required run artifacts')
    if manifest.get('status') != 'blocked' and 'deployment-plan.json' not in manifest['artifacts']:
        raise ValueError('Manifest omits the deployment plan')
    for name, expected in manifest['artifacts'].items():
        path = (directory / name).resolve()
        if not path.is_relative_to(directory) or not path.is_file():
            raise ValueError('Missing or unsafe artifact path: ' + name)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Artifact changed: ' + name + '; regenerate in a new output directory')
    fingerprint = digest({'config': config.read_json(directory / 'resolved-config.json'),
                          'inventory': config.read_json(directory / 'inventory.json')})
    if manifest.get('fingerprint') != fingerprint:
        raise ValueError('Saved configuration/input fingerprint mismatch')
    return manifest


def execute(directory, settings):
    manifest = verify_run(directory)
    if manifest['status'] not in ('generated', 'partially_executed'):
        raise ValueError('Run is not ready for execution (blocked, incomplete or already executed)')
    # DDL commits independently of the manifest. Do not replay mutations into
    # objects merely because a local file claims they are ours.
    if manifest['status'] == 'partially_executed':
        raise ValueError('Partial execution requires account-side object/ownership reconciliation before further writes')
    if not settings.connection or not settings.warehouse or not settings.role:
        raise ValueError('Execution requires explicit connection, warehouse and role in the saved configuration')
    plan = config.read_json(Path(directory) / 'deployment-plan.json')
    # The importer can replace a semantic object. Refuse any pre-existing target;
    # a rerun is not authorization to overwrite an object this run did not create.
    import snowflake.connector
    with snowflake.connector.connect(connection_name=settings.connection,
            warehouse=config.identifier(settings.warehouse), role=config.identifier(settings.role)) as connection:
        with connection.cursor() as cursor:
            completed = manifest.get('completed', [])
            for obj in plan['objects']:
                if obj['name'] in completed: continue
                database, schema, name = config.qualified(obj['name'])
                show_kind = {'SEMANTIC VIEW': 'SEMANTIC VIEWS', 'VIEW': 'VIEWS', 'TABLE': 'TABLES', 'AGENT': 'AGENTS', 'CORTEX SEARCH SERVICE': 'CORTEX SEARCH SERVICES'}[obj['kind']]
                cursor.execute(f'SHOW {show_kind} IN SCHEMA ' + config.fqn(database, schema))
                columns = [c[0].lower() for c in cursor.description]
                names = [dict(zip(columns, row)).get('name') for row in cursor.fetchall()]
                if name in names: raise ValueError('Target already exists; no replacement authorized: ' + obj['name'])
            for statement in plan['statements']:
                key = digest(statement)
                if key in manifest.get('completed_statements', []): continue
                manifest['status'] = 'partially_executed'
                atomic_json(Path(directory) / 'manifest.json', manifest)
                cursor.execute(statement['sql'])
                manifest.setdefault('completed_statements', []).append(key)
                if statement['kind'] in ('TABLE', 'VIEW', 'SEMANTIC VIEW', 'AGENT', 'CORTEX SEARCH SERVICE'):
                    manifest.setdefault('completed', []).append(statement['object'])
                manifest['status'] = 'partially_executed'
                atomic_json(Path(directory) / 'manifest.json', manifest)
    manifest['status'] = 'executed'
    atomic_json(Path(directory) / 'manifest.json', manifest)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        settings = config.resolve(args)
        if args.all: settings.outputs = ['semantic', 'agent', 'catalog', 'streamlit', 'react']
        if args.paths:
            mapping = {'1': ['catalog'], '2': ['streamlit', 'react'], '3': ['semantic', 'agent'], '4': ['streamlit', 'react', 'agent']}
            paths = args.paths.split(',')
            if set(paths) - set(mapping): raise ValueError('--paths accepts only 1,2,3,4')
            settings.outputs = sorted({item for path in paths for item in mapping[path]})
        if args.skip_deploy: settings.deploy = 'none'
        if args.guided: config.guided(settings)
        settings.validate()
        output = Path(settings.output_dir)
        if args.resume and output != Path(args.resume).expanduser().resolve():
            raise ValueError('--resume must name the saved output directory; relocation needs a new run')
        # Run output must never overwrite source, shipped code or another model's artifacts.
        relative_output = output.relative_to(ROOT) if output.is_relative_to(ROOT) else None
        if output == ROOT or (relative_output is not None and relative_output.parts[0] not in ('out', 'b2s-output')):
            raise ValueError('Use an external output directory or the ignored out/ or b2s-output/ folders')
        inventory = load_source(settings)
        fingerprint = digest({'config': settings.public_dict(), 'inventory': inventory})
        model = resolve_inventory(inventory, settings)
        print(json.dumps({'target': settings.naming.analytics, 'catalog': settings.naming.kb,
                          'sources': [t['source'] for t in model['tables']], 'outputs': settings.effective_outputs,
                          'warehouse': settings.warehouse, 'role': settings.role,
                          'blockers': model['issues'], 'operation': 'execute' if args.execute else 'offline generation'}, indent=2))
        if args.dry_run:
            if not model['issues']:
                planned = generate(model, settings, inventory)
                print(planned['deployment-plan.json'])
            return 2 if model['issues'] else 0
        if args.resume:
            previous = verify_run(args.resume)
            if previous['fingerprint'] != fingerprint:
                raise ValueError('Run configuration or source changed: semantic, reporting, agent, app and catalog artifacts are stale. Generate into a new --output-dir without --resume; prior artifacts are untouched.')
            if args.execute: execute(output, settings)
            print('Run verified: ' + str(output))
            return 2 if model['issues'] else 0
        if args.execute: raise ValueError('Generate and review first, then --resume RUN_DIRECTORY --execute')
        if output.exists() and any(output.iterdir()):
            raise ValueError('Output directory is not empty. Use --resume or choose a new directory; nothing overwritten')
        artifacts = {'resolved-config.json': json.dumps(settings.public_dict(), indent=2),
                     'inventory.json': json.dumps(inventory, indent=2),
                     'model-contract.json': json.dumps(model, indent=2),
                     'issues.json': json.dumps(model['issues'], indent=2)}
        if not model['issues']:
            artifacts.update(generate(model, settings, inventory))
        output.mkdir(parents=True, exist_ok=True)
        for name, text in artifacts.items():
            path = output / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
        manifest = {'version': 1, 'fingerprint': fingerprint, 'status': 'blocked' if model['issues'] else 'generated',
                    'artifacts': {name: hashlib.sha256(text.encode()).hexdigest() for name, text in artifacts.items()},
                    'completed': [], 'completed_statements': []}
        atomic_json(output / 'manifest.json', manifest)
        print(('Blocked; inspect issues.json in ' if model['issues'] else 'Generated, not deployed: ') + str(output))
        return 2 if model['issues'] else 0
    except (ValueError, KeyError, OSError, TypeError) as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
