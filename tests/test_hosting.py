import json
import os
from pathlib import Path
import pytest
import yaml
from test_model_contract import fixture, settings
from model_contract import resolve_inventory
from artifacts import generate
from app_backend import connection_params, execute, chat_payload, chat_response


def test_hosted_artifacts_are_complete_and_caller_scoped(tmp_path):
    conf = settings(tmp_path, outputs=['streamlit', 'react'], chat=True, glossary_search=True,
                    warehouse='READ_WH', connection='fictional', role='APP_OWNER', grant_role='READER',
                    deploy='both', compute_pool='APP_POOL', external_access_integrations=['PACKAGES'],
                    container_image='/work/reports/images/dashboard:test')
    artifacts = generate(resolve_inventory(fixture(), conf), conf)
    manifest = yaml.safe_load(artifacts['hosted_streamlit/snowflake.yml'])['entities']['report']
    assert all('hosted_streamlit/' + name in artifacts for name in manifest['artifacts'])
    assert 'snowflake-callers-rights' in artifacts['hosted_streamlit/app.py']
    assert 'connection_name=' not in artifacts['hosted_streamlit/app.py']
    spec = yaml.safe_load(artifacts['hosted_react/service-spec.yaml'])
    assert spec['capabilities']['securityContext']['executeAsCaller'] is True
    assert '0.0.0.0' in artifacts['hosted_react/package.json']
    assert 'Sf-Context' not in artifacts['hosted_react/components/Chat.tsx']
    assert 'GRANT SERVICE ROLE' in artifacts['caller-grants-review.sql']
    assert 'GRANT CALLER SELECT ON VIEW' in artifacts['caller-grants-review.sql']
    assert 'CREATE CORTEX SEARCH SERVICE' in artifacts['deploy.sql']
    spec = json.loads(artifacts['agent.json'])
    assert 'search_glossary' in spec['tool_resources']
    search_sql = next(item['sql'] for item in json.loads(artifacts['deployment-plan.json'])['statements'] if item['kind'] == 'CORTEX SEARCH SERVICE')
    assert 'TERM:expression' not in search_sql and 'TERM:source' not in search_sql


def test_hosting_requires_resources(tmp_path):
    with pytest.raises(ValueError, match='compute_pool'):
        settings(tmp_path, outputs=['react'], deploy='react').validate()


def test_caller_token_missing_never_falls_back(monkeypatch):
    monkeypatch.setenv('B2S_RUNTIME', 'spcs')
    with pytest.raises(ValueError, match='fallback'):
        connection_params({'connection': 'owner', 'warehouse': 'WH', 'role': 'OWNER'})


def test_caller_connection_omits_owner_role(monkeypatch):
    monkeypatch.setenv('B2S_RUNTIME', 'spcs')
    monkeypatch.setenv('SNOWFLAKE_HOST', 'fictional.snowflakecomputing.com')
    monkeypatch.setenv('SNOWFLAKE_ACCOUNT', 'fictional')
    monkeypatch.setattr(Path, 'read_text', lambda self: 'service-token')
    params = connection_params({'warehouse': 'WH', 'role': 'OWNER'}, 'viewer-token')
    assert params['token'] == 'service-token.viewer-token'
    assert 'role' not in params and 'connection_name' not in params


def test_backend_rejects_arbitrary_sql_before_connect():
    with pytest.raises(ValueError, match='Unknown'):
        execute({'datasets': []}, {'action': 'query', 'query': 'DROP TABLE X'}, connect=lambda **kw: pytest.fail('connected'))


def test_chat_uses_bindings_and_hides_thinking(monkeypatch):
    monkeypatch.delenv('B2S_RUNTIME', raising=False)
    seen = []
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params): seen.append((sql, params))
        def fetchone(self): return [json.dumps({'content': [{'type': 'thinking', 'thinking': {'text': 'private'}},
                                                             {'type': 'text', 'text': 'answer'}], 'warnings': [{'code': 'test'}]})]
    class Connection(Cursor):
        def cursor(self): return Cursor()
    question = "'; DROP TABLE X; --"
    result = execute({'agent': 'WORK.REPORTS.AGENT', 'connection': 'fictional', 'warehouse': 'WH', 'role': 'R'},
                     {'action': 'chat', 'question': question}, connect=lambda **kw: Connection())
    assert question not in seen[0][0]
    assert json.loads(seen[0][1][1])['messages'][0]['content'][0]['text'] == question
    assert result['text'] == 'answer' and result['warnings']
    with pytest.raises(ValueError): chat_payload('x' * 8001)
