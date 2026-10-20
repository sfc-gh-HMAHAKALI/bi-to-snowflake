"""Verify a saved run's integrity offline; no authentication on import."""
import argparse
import json
from pathlib import Path
from build import verify_run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True)
    args = parser.parse_args(argv)
    try:
        manifest = verify_run(args.run_dir)
        print(json.dumps({'status': manifest['status'], 'artifacts_checked': len(manifest['artifacts']),
                          'live_sql_validated': False, 'app_runtime_validated': False}, indent=2))
        return 0 if manifest['status'] != 'blocked' else 2
    except (ValueError, OSError) as exc:
        print('ERROR:', exc)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
