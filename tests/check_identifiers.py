"""Scan committable paths and content without embedding real customer names.

Supply private deny terms through B2S_DENY_TERMS_FILE (one term per line).
The file must be outside the repository. Matches are reported by path/category,
not by quoting the confidential text. Binary files require manual review.
"""
from __future__ import annotations
import argparse
import ast
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
GENERIC = [
    ('local-home', re.compile(r'/(?:Users|home)/[A-Za-z0-9_.-]+/')),
    ('fixed-connection', re.compile(r'default\s*=\s*[\"\']my-demo-account[\"\']')),
    ('fixed-schema', re.compile(r'\{\{DB\}\}\.ANALYTICS\b')),
    ('private-key', re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')),
]


def staged_set(root):
    return sorted(set(subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
        cwd=root, capture_output=True, check=True).stdout.decode().split('\0')) - {''})


def flags(rel, text, terms=()):
    if any(term.casefold() in (rel + '\n' + text).casefold() for term in terms): return True
    return any(pattern.search(text) for _, pattern in GENERIC)


def folded_strings(text):
    """Catch literal concatenation in Python fixtures as well as visible text."""
    try: tree = ast.parse(text)
    except SyntaxError: return ''
    def fold(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str): return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = fold(node.left), fold(node.right)
            if left is not None and right is not None: return left + right
        return None
    return '\n'.join(value for node in ast.walk(tree) if (value := fold(node)) is not None)


def audit(root, terms=()):
    findings = []
    binaries = []
    files = staged_set(root)
    for relative in files:
        path = Path(root) / relative
        if not path.exists(): continue
        if path.is_symlink():
            findings.append((relative, 'symlink requires review'))
            continue
        try: text = path.read_bytes().decode('utf-8')
        except UnicodeDecodeError:
            binaries.append(relative)
            continue
        combined = text + ('\n' + folded_strings(text) if path.suffix == '.py' else '')
        if flags(relative, combined, terms): findings.append((relative, 'identifier/environment match'))
    return files, findings, binaries


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=str(ROOT))
    parser.add_argument('--deny-terms-file', default=os.environ.get('B2S_DENY_TERMS_FILE'))
    args = parser.parse_args(argv)
    terms = []
    if args.deny_terms_file:
        path = Path(args.deny_terms_file).expanduser().resolve()
        if path.is_relative_to(Path(args.root).resolve()):
            parser.error('Private deny terms must live outside the repository')
        terms = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    files, findings, binaries = audit(args.root, terms)
    for path, category in findings: print(f'FAIL {path}: {category}')
    for path in binaries: print(f'REVIEW {path}: binary/archive content not certified')
    print(f'{len(files)} committable paths; {len(findings)} findings; {len(binaries)} binary review gaps')
    if not terms: print('Generic checks only: supply a private deny-terms file for customer-specific coverage.')
    return 1 if findings or binaries else 0


if __name__ == '__main__':
    raise SystemExit(main())
