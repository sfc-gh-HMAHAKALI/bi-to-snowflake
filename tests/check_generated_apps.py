"""Generate fictional apps and validate React types, build and chart behavior.

Dependency installation is opt-in. It downloads public packages, never uploads
source exports or account data. No application server or Snowflake query runs.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pipeline'))
sys.path.insert(0, str(ROOT / 'tests'))
from test_model_contract import fixture, settings
from model_contract import resolve_inventory
from artifacts import generate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true')
    parser.add_argument('--hosted', action='store_true', help='Also typecheck/build the caller-scoped React artifact')
    args = parser.parse_args()
    directory = ROOT / 'out' / 'app-validation'
    conf = settings(directory, outputs=['react', 'streamlit'], chat=True, warehouse='READ_WH',
                    connection='fictional', role='APP_OWNER',
                    deploy='both' if args.hosted else 'none', compute_pool='APP_POOL',
                    external_access_integrations=['PACKAGES'], container_image='/work/reports/images/report:test')
    artifacts = generate(resolve_inventory(fixture(), conf), conf)
    for name, content in artifacts.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    react = directory / 'app_react'
    env = {**os.environ, 'NEXT_TELEMETRY_DISABLED': '1'}
    if args.install:
        subprocess.run(['npm', 'install', '--ignore-scripts', '--no-audit', '--no-fund'], cwd=react, env=env, check=True)
    subprocess.run([sys.executable, '-m', 'pytest', '-q', str(ROOT / 'tests/test_generated_streamlit.py')],
                   env={**env, 'PYTHONPATH': str(ROOT / 'tests/offline')}, check=True)
    if not (react / 'node_modules/typescript').exists():
        raise SystemExit('Dependencies missing. Run this fixture-only check with --install.')
    # Compile the real shipped TS module; only UI imports are stubbed. The
    # exported helper and default aggregation execute unchanged in Node.
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const ts = require('typescript');
const source = fs.readFileSync('lib/charts.tsx', 'utf8');
const javascript = ts.transpileModule(source, {compilerOptions: {
  module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020
}}).outputText;
const context = {exports: {}, require: () => ({}), Map, Set};
vm.runInNewContext(javascript, context);
const rankedData = context.exports.rankedData;
const rows = Array.from({length: 12}, (_, index) => ({label: String(index), metric: index / 10}));
rows[0].metric = null; rows[1].metric = -2;
const actual = rankedData(rows, 'label', 'metric', true);
assert.equal(actual.length, 12);
for (const row of rows) assert.equal(actual.find(item => item.name === row.label).value, row.metric);
assert(!actual.some(item => item.name.startsWith('Other')));
assert.throws(() => rankedData([{label:'A',metric:0.3},{label:'A',metric:0.8}], 'label', 'metric', true), /one row/);
assert.throws(() => rankedData([{label:'A',metric:'bad'}], 'label', 'metric', true), /numeric/);
assert.equal(rankedData([{label:'A',metric:0.3},{label:'A',metric:0.8}], 'label', 'metric')[0].value, 1.1);
assert.equal(rankedData(rows, 'label', 'metric').length, 8);
console.log('React chart behavior: passed; nulls, negatives, ratios, tails, duplicates and legacy aggregation');
'''
    subprocess.run(['node', '-e', script], cwd=react, env=env, check=True)
    subprocess.run(['npm', 'run', 'typecheck'], cwd=react, env=env, check=True)
    subprocess.run(['npm', 'run', 'build'], cwd=react, env=env, check=True)
    if args.hosted:
        hosted = directory / 'hosted_react'
        # Turbopack refuses node_modules symlinks outside its application root.
        import shutil
        dependencies = hosted / 'node_modules'
        if dependencies.is_symlink(): dependencies.unlink()
        if not dependencies.exists(): shutil.copytree(react / 'node_modules', dependencies, symlinks=True)
        subprocess.run(['npm', 'run', 'typecheck'], cwd=hosted, env=env, check=True)
        subprocess.run(['npm', 'run', 'build'], cwd=hosted, env={**env, 'B2S_RUNTIME': 'spcs'}, check=True)
    print('Generated React typecheck/build passed. No live Snowflake or browser validation.')


if __name__ == '__main__':
    main()
