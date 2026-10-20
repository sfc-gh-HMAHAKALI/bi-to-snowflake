"""Safety and portability checks independent of the retired phase graph."""
import copy
import json
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
from artifacts import generate
from build import main, verify_run
from model_contract import expression, resolve_inventory, ModelError, source_expression
from test_model_contract import fixture, settings
from test_portable_build import arguments


@pytest.mark.parametrize('sql', ['SUM(X) + X', 'SUM(AVG(X))', 'COUNT(*)'])
def test_invalid_metric_grain_rejected(sql):
    with pytest.raises(ModelError): expression(sql, 'LOANS', {'X': 'X'}, True)


def test_row_determinant_supplies_declared_key(tmp_path):
    inventory = fixture()
    inventory['dimensions'][0].pop('primary_key')
    inventory['grain_declarations'] = [{'entity': 'LOANS', 'is_row_grain': True, 'key_columns': ['ID']}]
    model = resolve_inventory(inventory, settings(tmp_path))
    assert not model['issues'] and model['tables'][0]['primary_key'] == ['ID']


def test_grouped_determinant_is_not_ignored(tmp_path):
    inventory = fixture()
    inventory['grain_declarations'] = [{'entity': 'LOANS', 'groupable': True, 'key_columns': ['CATEGORY']}]
    assert resolve_inventory(inventory, settings(tmp_path))['issues']


def test_explicit_translation_does_not_evaluate_rejected_source(tmp_path):
    inventory = fixture()
    inventory['facts'][0]['expr'] = '[Import View].[OTHER].[DURATION]'
    conf = settings(tmp_path, translations={'facts:0': 'DURATION'})
    assert not resolve_inventory(inventory, conf)['issues']


def test_typo_in_translation_reference_blocks(tmp_path):
    assert resolve_inventory(fixture(), settings(tmp_path, translations={'fact:0': 'DURATION'}))['issues']


def test_cross_entity_cognos_expression_not_stripped():
    with pytest.raises(ModelError):
        source_expression({'expr': '[Import View].[OTHER].[DURATION]'}, 'LOANS')


@pytest.mark.parametrize('key', ['required_access_grants', 'sql_always_having', 'sql_where'])
def test_source_metadata_restrictions_block(tmp_path, key):
    inventory = fixture()
    inventory['source_metadata'] = {'views': [{'source_definition': {key: ['restricted']}}]}
    assert resolve_inventory(inventory, settings(tmp_path))['issues']


def test_destination_source_collision_rejected(tmp_path):
    inventory = fixture()
    inventory['tables'][0]['physical_table'] = 'WORK.REPORTS.BI_MODEL'
    model = resolve_inventory(inventory, settings(tmp_path))
    with pytest.raises(ValueError, match='collides'): generate(model, settings(tmp_path))


def test_reporting_name_collision_rejected(tmp_path):
    inventory = fixture()
    inventory['dimensions'][1].update(name='TOTAL', expr='TOTAL')
    conf = settings(tmp_path, outputs=['streamlit'])
    with pytest.raises(ValueError, match='collision'): generate(resolve_inventory(inventory, conf), conf)


def test_nested_out_directory_cannot_bypass_source_guard(tmp_path):
    from build import ROOT
    args = arguments(tmp_path)
    args[args.index('--output-dir') + 1] = str(ROOT / 'pipeline' / 'out')
    assert main(args) == 2


def test_manifest_missing_required_hash_rejected(tmp_path):
    assert main(arguments(tmp_path)) == 0
    directory = tmp_path / 'run_LIBRARY'
    path = directory / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['artifacts'].pop('deployment-plan.json')
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='omits'): verify_run(directory)


def test_partial_execution_requires_reconciliation(tmp_path):
    from build import execute
    import config
    assert main(arguments(tmp_path)) == 0
    directory = tmp_path / 'run_LIBRARY'
    path = directory / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['status'] = 'partially_executed'
    path.write_text(json.dumps(manifest))
    conf = config.RunConfig(**config.read_json(directory / 'resolved-config.json'))
    with pytest.raises(ValueError, match='reconciliation'):
        execute(directory, conf)


def test_source_change_invalidates_resume(tmp_path):
    assert main(arguments(tmp_path)) == 0
    source = tmp_path / 'LIBRARY.json'
    inventory = json.loads(source.read_text())
    inventory['metrics'][0]['expr'] = 'MAX(DURATION)'
    source.write_text(json.dumps(inventory))
    assert main(['--resume', str(tmp_path / 'run_LIBRARY')]) == 2


def test_kb_plan_preserves_source_and_destination(tmp_path):
    import config
    import sqlglot
    from sqlglot import exp
    conf = settings(tmp_path, outputs=['catalog'], kb_database='"Metadata.Db"', kb_schema='"Catalog.Space"')
    generated = generate(resolve_inventory(fixture(), conf), conf, fixture())
    plan = json.loads(generated['deployment-plan.json'])
    assert all(config.qualified(obj['name'])[:2] == ('Metadata.Db', 'Catalog.Space') for obj in plan['objects'])
    merges = [item for item in plan['statements'] if item['kind'] == 'MERGE']
    assert merges
    for item in merges:
        tree = sqlglot.parse_one(item['sql'], read='snowflake')
        assert isinstance(tree, exp.Merge)
        assert tree.this.catalog == 'Metadata.Db' and tree.this.db == 'Catalog.Space'
    entities = next(item['sql'] for item in merges if item['object'].endswith('"BI_KB_ENTITY"'))
    assert "'RAW'" in entities and "'LIBRARY'" in entities and "'LOANS'" in entities
    assert not any(item['kind'] in ('DATABASE', 'SCHEMA') for item in plan['objects'])


def test_app_inventory_references_created_views(tmp_path):
    conf = settings(tmp_path, outputs=['react'])
    artifacts = generate(resolve_inventory(fixture(), conf), conf)
    objects = {item['name'] for item in json.loads(artifacts['deployment-plan.json'])['objects']}
    assert all(item['view'] in objects for item in json.loads(artifacts['app-inventory.json'])['dashboards'])


def test_generated_assets_are_complete(tmp_path):
    conf = settings(tmp_path, outputs=['react', 'streamlit'])
    artifacts = generate(resolve_inventory(fixture(), conf), conf)
    root = Path(__file__).resolve().parents[1]
    for path in (root / 'assets/streamlit_ui').glob('*.py'):
        assert artifacts['app_streamlit/bim_ui/' + path.name] == path.read_text()
    for name in ('charts.tsx', 'theme.ts', 'ui.tsx'):
        assert artifacts['app_react/lib/' + name] == (root / 'assets/react_ui' / name).read_text()
    assert 'preAggregated' in artifacts['app_react/components/Chart.tsx']
    assert 'pre_aggregated=True' in artifacts['app_streamlit/app.py']
