"""Plan cleanup only for objects recorded as created by a verified run.

No schema sweeps, CASCADE, database drops, or live execution. Review ownership
and dependencies in Snowflake before using the printed statements.
"""
import argparse
from pathlib import Path
import config
from build import verify_run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True)
    args = parser.parse_args(argv)
    try:
        manifest = verify_run(args.run_dir)
        plan = config.read_json(Path(args.run_dir) / 'deployment-plan.json')
        created = set(manifest.get('completed', []))
        print('REVIEW ONLY: verify current ownership and dependencies before dropping anything.')
        for obj in reversed(manifest.get('hosted_objects', [])):
            if obj['kind'] not in ('STREAMLIT', 'SERVICE'): raise ValueError('Unexpected hosted object kind')
            print('DROP ' + obj['kind'] + ' IF EXISTS ' + config.fqn(*config.qualified(obj['name'])) + ';')
        for obj in reversed(plan['objects']):
            if obj['name'] in created:
                parts = config.qualified(obj['name'])
                if obj['kind'] not in ('VIEW', 'SEMANTIC VIEW', 'TABLE', 'AGENT', 'CORTEX SEARCH SERVICE'):
                    raise ValueError('Unexpected object kind')
                print('DROP ' + obj['kind'] + ' IF EXISTS ' + config.fqn(*parts) + ';')
        if not created and not manifest.get('hosted_objects'): print('No objects recorded as created; nothing to drop.')
        return 0
    except (ValueError, OSError) as exc:
        print('ERROR:', exc)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
