"""Execute generated Streamlit UI with a fake read-only Snowflake connection."""
import sys
from pathlib import Path
import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
from artifacts import generate
from model_contract import resolve_inventory
from test_model_contract import fixture, settings


@pytest.mark.parametrize('state', ['success', 'empty', 'error'])
def test_generated_streamlit_states(tmp_path, monkeypatch, state):
    conf = settings(tmp_path, outputs=['streamlit'], connection='fictional', warehouse='COMPUTE', role='READER')
    artifacts = generate(resolve_inventory(fixture(), conf), conf)
    for name, text in artifacts.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    queries = []
    class Connection:
        def query(self, sql, ttl):
            queries.append(sql)
            assert ttl == 0
            if state == 'error': raise RuntimeError('Fictional query failure')
            if state == 'empty': return pd.DataFrame()
            return pd.DataFrame({'CATEGORY': ['A', 'B', None], 'LOANS_AVERAGE_VALUE': [0.3, -2, None]})
    monkeypatch.setattr(st, 'connection', lambda *args, **kwargs: Connection())
    app_dir = tmp_path / 'app_streamlit'
    monkeypatch.syspath_prepend(str(app_dir))
    app = AppTest.from_file(str(app_dir / 'app.py')).run()
    assert not app.exception
    if state == 'success':
        # Dict-valued selectors require the actual value rather than its label.
        import json
        runtime = json.loads((app_dir / 'runtime.json').read_text())
        app.selectbox[1].set_value(runtime['datasets'][0]['queries'][-1]).run()
        assert not app.exception and not app.error
        assert len(app.dataframe) == 1
        assert len(app.get('plotly_chart')) == 1
    elif state == 'empty': assert app.info
    else: assert app.error
    assert queries and all(sql.startswith('SELECT * FROM "WORK"."REPORTS".') for sql in queries)


def test_hosted_streamlit_chat_uses_caller_and_parameter_binding(tmp_path, monkeypatch):
    import json
    conf = settings(tmp_path, outputs=['streamlit'], chat=True, connection='fictional', warehouse='READ_WH',
                    role='APP_OWNER', deploy='streamlit', compute_pool='POOL', external_access_integrations=['PACKAGES'])
    artifacts = generate(resolve_inventory(fixture(), conf), conf)
    for name, text in artifacts.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    connections, queries = [], []
    class Connection:
        def query(self, sql, ttl, params=None):
            queries.append((sql, params))
            if params:
                return pd.DataFrame({'RESPONSE': [json.dumps({'content': [{'type': 'text', 'text': 'Fictional answer'}]})]})
            return pd.DataFrame({'LOANS_AVERAGE_VALUE': [2]})
    def connect(name, **kwargs):
        connections.append((name, kwargs))
        return Connection()
    monkeypatch.setattr(st, 'connection', connect)
    monkeypatch.syspath_prepend(str(tmp_path / 'hosted_streamlit'))
    app = AppTest.from_file(str(tmp_path / 'hosted_streamlit/app.py')).run()
    assert not app.exception
    app.chat_input[0].set_value("What is average duration?").run()
    assert not app.exception and not app.error
    assert all(name == 'snowflake-callers-rights' and not kwargs for name, kwargs in connections)
    assert queries[-1][1][0] == '"WORK"."REPORTS"."BI_ANALYST"'
    assert any(element.value == 'Fictional answer' for element in app.text)
