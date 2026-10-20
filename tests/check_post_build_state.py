"""No customer-name exceptions, including skill renames or split literals."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import check_identifiers as guard


def test_no_content_exceptions():
    marker = 'FICTIONAL_PRIVATE_MARKER'
    for path in ('SKILL.md', 'pipeline/build.py', 'sample.sql'):
        assert guard.flags(path, 'name: ' + marker, (marker,))
    assert marker in guard.folded_strings('value = "FICTIONAL_" + "PRIVATE_MARKER"')


def test_negative_control():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / 'repo'
        root.mkdir()
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        deny = Path(temporary) / 'deny.txt'
        deny.write_text('FICTIONAL_PRIVATE_MARKER\n')
        probe = root / 'fictional_private_probe.txt'
        assert not probe.exists()
        try:
            probe.write_text('FICTIONAL_PRIVATE_MARKER')
            result = subprocess.run([sys.executable, str(guard.ROOT / 'tests/check_identifiers.py'), '--root', str(root), '--deny-terms-file', str(deny)], capture_output=True, text=True)
            assert result.returncode == 1
            assert 'fictional_private_probe.txt' in result.stdout
            assert 'FICTIONAL_PRIVATE_MARKER' not in result.stdout
        finally:
            probe.unlink(missing_ok=True)


def test_hardcoding_negative_control():
    root = Path(__file__).resolve().parents[1]
    probe = root / 'tests' / 'fictional_schema_probe.sql'
    assert not probe.exists()
    try:
        probe.write_text(''.join(['USE SCHEMA {{DB}}.', 'ANALYTICS;']))
        result = subprocess.run([sys.executable, str(root / 'tests/check_identifiers.py')], capture_output=True, text=True)
        assert result.returncode == 1 and probe.name in result.stdout
    finally:
        probe.unlink(missing_ok=True)


if __name__ == '__main__':
    test_hardcoding_negative_control()
    test_no_content_exceptions()
    test_negative_control()
    print('Sanitization negative controls passed; mutations removed.')
