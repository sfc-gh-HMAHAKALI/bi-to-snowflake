import copy
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
import config
from model_contract import resolve_inventory, osi, expression, ModelError


def fixture(domain='LIBRARY'):
    table = 'LOANS' if domain == 'LIBRARY' else 'OBSERVATIONS'
    measure = 'DURATION' if domain == 'LIBRARY' else 'TEMPERATURE'
    return {'source_type': 'fictional', 'tables': [{'name': table, 'physical_table': f'RAW.{domain}.{table}'}],
            'dimensions': [{'name': 'ID', 'table': table, 'expr': 'ID', 'data_type': 'NUMBER', 'primary_key': True},
                           {'name': 'CATEGORY', 'table': table, 'expr': 'CATEGORY', 'data_type': 'VARCHAR'}],
            'facts': [{'name': measure, 'table': table, 'expr': measure, 'data_type': 'NUMBER'}],
            'metrics': [{'name': 'AVERAGE_VALUE', 'table': table, 'expr': f'AVG({measure})'}]}


def settings(tmp_path, **kwargs):
    return config.RunConfig(database='WORK', analytics_schema='REPORTS', inventory='fictional.json', output_dir=str(tmp_path), **kwargs)


@pytest.mark.parametrize('domain', ['LIBRARY', 'WEATHER'])
def test_model_without_embedded_entities(tmp_path, domain):
    model = resolve_inventory(fixture(domain), settings(tmp_path))
    assert not model['issues']
    output = osi(model)
    assert len(output['datasets']) == 1
    assert output['datasets'][0]['source'].startswith('"RAW".')
    assert 'AVG(' in output['metrics'][0]['expression']['dialects'][0]['expression']
    assert 'SUM(' not in str(output)


def test_mapping_preserves_quoted_source(tmp_path):
    conf = settings(tmp_path, source_mappings={'LOANS': '"Source.Db"."Mixed.Schema"."Borrowing"'})
    model = resolve_inventory(fixture(), conf)
    assert model['tables'][0]['source'] == '"Source.Db"."Mixed.Schema"."Borrowing"'


@pytest.mark.parametrize('kind,value', [
    ('security_rules', [{'principal': 'fictional', 'expression': 'REGION=1'}]),
    ('aggregation_rules', [{'regular_aggregate': 'sum', 'rollup_aggregate': 'last'}]),
    ('filters', [{'name': 'calendar restriction', 'expression': 'year > 2020', 'scope': 'model'}]),
    ('relationships', [{'left_table': 'LOANS', 'right_table': 'LOANS'}]),
])
def test_unsupported_semantics_block(tmp_path, kind, value):
    inventory = fixture()
    inventory[kind] = value
    model = resolve_inventory(inventory, settings(tmp_path))
    assert model['issues']
    with pytest.raises(ModelError): osi(model)


@pytest.mark.parametrize('text', ['SUM(x); DROP TABLE t', 'SYSTEM$SEND_EMAIL(x)', 'SELECT x FROM t', 'SUM(other.x)', '${value}', 'AVG(x) OVER ()'])
def test_expression_boundary(text):
    with pytest.raises(ModelError): expression(text, 'LOANS', {'X': 'X'}, True)


def test_no_invented_primary_key(tmp_path):
    inventory = fixture()
    inventory['dimensions'][0].pop('primary_key')
    assert not resolve_inventory(inventory, settings(tmp_path))['tables'][0]['primary_key']


def test_reviewed_filter_kept(tmp_path):
    inventory = fixture()
    inventory['filters'] = [{'table': 'LOANS', 'expr': 'DURATION > 0'}]
    model = resolve_inventory(inventory, settings(tmp_path))
    assert not model['issues']
    assert 'WHERE ("DURATION" > 0)' in osi(model)['datasets'][0]['source']
