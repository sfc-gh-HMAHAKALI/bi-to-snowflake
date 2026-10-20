"""Verify generated query/runtime contracts against the saved resolved model."""
from __future__ import annotations
import argparse
from pathlib import Path
import config
from build import verify_run
from artifacts import generate


def verify(directory):
    directory = Path(directory).resolve()
    manifest = verify_run(directory)
    if manifest['status'] == 'blocked':
        raise ValueError('Blocked runs have no deployable composition')
    settings = config.RunConfig(**config.read_json(directory / 'resolved-config.json')).validate()
    model = config.read_json(directory / 'model-contract.json')
    inventory = config.read_json(directory / 'inventory.json')
    expected = generate(model, settings, inventory)
    names = list(expected)
    count = 0
    for name in names:
        if name not in expected: continue
        path = directory / name
        if not path.is_file() or path.read_text(encoding='utf-8') != expected[name]:
            raise ValueError('Composition differs from the resolved contract: ' + name)
        count += 1
    return count


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True)
    args = parser.parse_args(argv)
    try:
        count = verify(args.run_dir)
        print(f'{count} composition artifacts match the model; no live queries performed.')
        return 0
    except (ValueError, KeyError, OSError, TypeError) as exc:
        print('ERROR:', exc)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
