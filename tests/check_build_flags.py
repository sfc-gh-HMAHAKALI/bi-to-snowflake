"""Behavioral replacement for legacy schema-rejection and phase-count checks."""
import subprocess
import sys
from pathlib import Path

if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    raise SystemExit(subprocess.call([sys.executable, '-m', 'pytest', '-q',
        str(root / 'tests/test_config.py'), str(root / 'tests/test_portable_build.py'),
        str(root / 'tests/test_source_fixtures.py')]))
