import copy
import json
import pytest
from test_model_contract import fixture, settings
from model_contract import resolve_inventory
from security_contract import rule_digest
from artifacts import generate


def protected(tmp_path):
    inventory = fixture()
    rule = {'principal': 'library-readers', 'entities': ['LOANS'],
            'expression': "CATEGORY = 'PUBLIC' AND DURATION > 0"}
    inventory['security_rules'] = [rule]
    conf = settings(tmp_path, security={'security_rules:0': {
        'source_hash': rule_digest(rule), 'principal': 'library-readers',
        'role': 'LIBRARY_READER', 'predicate': rule['expression'], 'combination': 'any_role'}})
    return inventory, conf


def test_security_preserves_boolean_groups_and_no_source_mutation(tmp_path):
    inventory, conf = protected(tmp_path)
    model = resolve_inventory(inventory, conf)
    assert not model['issues'], model['issues']
    artifacts = generate(model, conf, inventory)
    sql = artifacts['deploy.sql']
    assert 'CREATE SECURE VIEW' in sql
    assert "IS_ROLE_IN_SESSION('LIBRARY_READER')" in sql
    assert 'AND "DURATION" > 0' in sql
    assert 'ALTER TABLE' not in sql and 'CREATE OR REPLACE' not in sql
    assert 'BI_SEC_LOANS' in artifacts['semantic.ossie.yaml']
    assert json.loads(artifacts['deployment-plan.json'])['source_objects'] == ['"RAW"."LIBRARY"."LOANS"']
    assert model['tables'][0]['source'] == '"RAW"."LIBRARY"."LOANS"'


@pytest.mark.parametrize('change', ['hash', 'principal', 'scope', 'predicate', 'combination', 'unmapped'])
def test_incomplete_security_blocks(tmp_path, change):
    inventory, conf = protected(tmp_path)
    mapping = conf.security['security_rules:0']
    if change == 'hash': inventory['security_rules'][0]['expression'] += ' OR TRUE'
    if change == 'principal': mapping['principal'] = 'someone-else'
    if change == 'scope':
        inventory['security_rules'][0]['entities'].append('OTHER')
        mapping['source_hash'] = rule_digest(inventory['security_rules'][0])
    if change == 'predicate': mapping['predicate'] = 'TRUE'
    if change == 'combination': mapping['combination'] = 'guessed'
    if change == 'unmapped': conf.security.clear()
    assert resolve_inventory(inventory, conf)['issues']
