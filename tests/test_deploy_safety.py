"""Execute only against fake connectors; prove mutation gates and recovery state."""
import json
from pathlib import Path
import pytest
from build import main, verify_run
from deploy_apps import run
from test_portable_build import arguments


def prepared(tmp_path):
    args = arguments(tmp_path)
    args += ['--deploy', 'react', '--compute-pool', 'POOL', '--container-image', '/work/apps/images/report:test']
    assert main(args) == 0
    directory = tmp_path / 'run_LIBRARY'
    path = directory / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['status'] = 'executed'
    path.write_text(json.dumps(manifest))
    return directory


@pytest.mark.parametrize('failure', ['none', 'existing', 'denied', 'create', 'verify'])
def test_hosting_state_and_no_overwrite(tmp_path, monkeypatch, failure):
    import snowflake.connector
    directory = prepared(tmp_path)
    seen = []
    class Cursor:
        description = [('name',)]
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql):
            seen.append(sql)
            if failure == 'denied' and sql.startswith('SHOW '): raise RuntimeError('denied')
            if sql.startswith('CREATE '):
                assert json.loads((directory / 'manifest.json').read_text())['hosting_status'] == 'in_progress'
                if failure == 'create': raise RuntimeError('failed')
        def fetchall(self):
            if seen[-1].startswith('SHOW SERVICES'):
                return [('BI_MODEL_REPORT',)] if failure == 'existing' else []
            return [] if failure == 'verify' else [('ready',)]
    class Connection(Cursor):
        def cursor(self): return Cursor()
    monkeypatch.setattr(snowflake.connector, 'connect', lambda **kwargs: Connection())
    if failure == 'none':
        run(directory, True)
        manifest = verify_run(directory)
        assert manifest['hosting_status'] == 'created_requires_runtime_validation'
        assert len(manifest['hosted_objects']) == 1
    else:
        with pytest.raises((ValueError, RuntimeError)): run(directory, True)
        manifest = verify_run(directory)
        if failure in ('existing', 'denied'):
            assert not any(sql.startswith('CREATE ') for sql in seen)
            assert not manifest.get('hosting_status')
        else:
            assert manifest['hosting_status'] == 'in_progress'
            with pytest.raises(ValueError, match='reconciliation'): run(directory, True)


def test_hosting_review_stays_offline(tmp_path, monkeypatch):
    import snowflake.connector
    directory = prepared(tmp_path)
    monkeypatch.setattr(snowflake.connector, 'connect', lambda **kwargs: pytest.fail('connected'))
    assert run(directory)['status'] == 'prepared_not_deployed'


def test_synthetic_requires_separate_explicit_destination(tmp_path):
    from modules.output.sample_data import generate_seed_sql
    from test_model_contract import fixture
    inventory = fixture()
    with pytest.raises(ValueError, match='explicit'): generate_seed_sql(inventory, include_all=True)
    with pytest.raises(ValueError, match='source schema'):
        generate_seed_sql(inventory, include_all=True, destination_database='RAW', destination_schema='LIBRARY')
    result = generate_seed_sql(inventory, include_all=True, destination_database='"Demo.Db"', destination_schema='"Sample.Schema"')
    sql = '\n'.join(result['statements'])
    assert 'CREATE TABLE "Demo.Db"."Sample.Schema"."SYNTHETIC_LOANS"' in sql
    assert 'OR REPLACE' not in sql and 'FROM "RAW"' not in sql
