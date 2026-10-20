"""Review or execute prepared hosting after a separately verified backend run."""
import argparse
import json
from pathlib import Path
import subprocess
import config
from build import verify_run, atomic_json


def run(directory, execute=False):
    directory = Path(directory).resolve()
    manifest = verify_run(directory)
    settings = config.RunConfig(**config.read_json(directory / 'resolved-config.json')).validate()
    plan = config.read_json(directory / 'hosting-plan.json')
    print(json.dumps(plan, indent=2))
    if not execute: return plan
    from verify_composition import verify
    verify(directory)
    if manifest['status'] != 'executed': raise ValueError('Execute and verify the backend before hosting')
    if manifest.get('hosting_status'): raise ValueError('Existing/partial hosting requires ownership reconciliation; no automatic replacement')
    if not settings.connection: raise ValueError('Hosting requires an explicit saved connection')
    for operation in plan['operations']:
        app_dir = directory / operation['directory']
        if not app_dir.resolve().is_relative_to(directory): raise ValueError('Unsafe hosting directory')
        if operation['kind'] == 'STREAMLIT':
            import yaml
            entity = yaml.safe_load((app_dir / 'snowflake.yml').read_text())['entities']['report']
            for filename in entity['artifacts']:
                path = (app_dir / filename).resolve()
                if not path.is_relative_to(app_dir) or not path.is_file(): raise ValueError('Missing hosted artifact')
    import snowflake.connector
    with snowflake.connector.connect(connection_name=settings.connection,
            warehouse=config.identifier(settings.warehouse), role=config.identifier(settings.role)) as connection:
        with connection.cursor() as cursor:
            cursor.execute('DESCRIBE COMPUTE POOL ' + config.quote(config.identifier(settings.compute_pool)))
            for operation in plan['operations']:
                database, schema, name = config.qualified(operation['name'])
                kind = {'STREAMLIT': 'STREAMLITS', 'SERVICE': 'SERVICES'}[operation['kind']]
                cursor.execute('SHOW ' + kind + ' IN SCHEMA ' + config.fqn(database, schema))
                columns = [column[0].lower() for column in cursor.description]
                if name in [dict(zip(columns, row)).get('name') for row in cursor.fetchall()]:
                    raise ValueError('Hosted target exists; replacement not authorized')
            # Persist intent before the first independently committed external action.
            manifest['hosting_status'] = 'in_progress'
            atomic_json(directory / 'manifest.json', manifest)
            for operation in plan['operations']:
                if operation['kind'] == 'STREAMLIT':
                    subprocess.run(operation['command'], cwd=directory / operation['directory'], check=True)
                else:
                    cursor.execute((directory / operation['sql_file']).read_text())
                cursor.execute(operation['verify_sql'])
                if not cursor.fetchall(): raise ValueError('Hosted object verification returned no rows')
                manifest.setdefault('hosted_objects', []).append({'kind': operation['kind'], 'name': operation['name']})
                atomic_json(directory / 'manifest.json', manifest)
    manifest['hosting_status'] = 'created_requires_runtime_validation'
    atomic_json(directory / 'manifest.json', manifest)
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    try:
        run(args.run_dir, args.execute)
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print('ERROR:', exc)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
