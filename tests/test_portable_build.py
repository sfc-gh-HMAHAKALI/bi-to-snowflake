import json
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
from build import main, verify_run
from artifacts import generate
from model_contract import resolve_inventory
from test_model_contract import fixture, settings


def arguments(tmp_path, domain='LIBRARY', target='REPORTS'):
    source = tmp_path / (domain + '.json')
    source.write_text(json.dumps(fixture(domain)))
    return ['--inventory', str(source), '--database', 'WORK_' + domain, '--analytics-schema', target,
            '--output-dir', str(tmp_path / ('run_' + domain)), '--outputs', 'semantic,catalog,streamlit,react,agent',
            '--warehouse', 'COMPUTE', '--role', 'BUILDER', '--connection', 'fictional']


@pytest.mark.parametrize('domain,target', [('LIBRARY', 'MART'), ('WEATHER', '"Mixed.Schema"')])
def test_end_to_end_artifacts(tmp_path, domain, target):
    args = arguments(tmp_path, domain, target)
    assert main(args) == 0
    directory = tmp_path / ('run_' + domain)
    manifest = verify_run(directory)
    assert manifest['status'] == 'generated'
    plan = json.loads((directory / 'deployment-plan.json').read_text())
    assert all('ANALYTICS' not in obj['name'] for obj in plan['objects'])
    assert all('RAW' not in obj['name'] for obj in plan['objects'])
    assert (directory / 'app_streamlit/app.py').exists()
    assert (directory / 'app_react/app/page.tsx').exists()
    compile((directory / 'app_streamlit/app.py').read_text(), 'app.py', 'exec')
    assert main(['--resume', str(directory)]) == 0
    from verify_composition import verify
    assert verify(directory) > 0
    previous = (directory / 'deployment-plan.json').read_bytes()
    assert main(['--resume', str(directory), '--analytics-schema', 'DIFFERENT']) == 2
    assert (directory / 'deployment-plan.json').read_bytes() == previous


def test_integrity_negative_control(tmp_path):
    assert main(arguments(tmp_path)) == 0
    directory = tmp_path / 'run_LIBRARY'
    (directory / 'deploy.sql').write_text('SELECT * FROM WRONG.TARGET.OBJECT;')
    with pytest.raises(ValueError, match='changed'): verify_run(directory)


def test_dry_run_and_invalid_config_write_nothing(tmp_path):
    args = arguments(tmp_path)
    assert main([*args, '--dry-run']) == 0
    assert not (tmp_path / 'run_LIBRARY').exists()
    assert main(['--database', 'WORK']) == 2


def test_existing_directory_not_overwritten(tmp_path):
    args = arguments(tmp_path)
    directory = tmp_path / 'run_LIBRARY'
    directory.mkdir()
    (directory / 'keep.txt').write_text('keep')
    assert main(args) == 2
    assert (directory / 'keep.txt').read_text() == 'keep'


def test_security_blocks_artifact_generation(tmp_path):
    args = arguments(tmp_path)
    source = tmp_path / 'LIBRARY.json'
    inv = json.loads(source.read_text())
    inv['security_rules'] = [{'principal': 'fictional-team'}]
    source.write_text(json.dumps(inv))
    assert main(args) == 2
    directory = tmp_path / 'run_LIBRARY'
    assert (directory / 'issues.json').exists()
    assert not (directory / 'deploy.sql').exists()
    assert main(['--resume', str(directory), '--execute']) == 2


def test_catalog_does_not_create_agent_or_semantic(tmp_path):
    conf = settings(tmp_path, outputs=['catalog'])
    generated = generate(resolve_inventory(fixture(), conf), conf)
    assert 'agent.json' not in generated and 'semantic.ossie.yaml' not in generated
    objects = json.loads(generated['deployment-plan.json'])['objects']
    assert len(objects) > 1 and all(obj['kind'] in ('TABLE', 'VIEW') for obj in objects)
    assert any(obj['name'].endswith('"BI_KB_V_GLOSSARY"') for obj in objects)
    import sqlglot
    from sqlglot import exp
    for statement in json.loads(generated['deployment-plan.json'])['statements']:
        if statement['kind'] != 'VIEW': continue
        tree = sqlglot.parse_one(statement['sql'], read='snowflake')
        assert not tree.args.get('replace')
        assert all(table.catalog == 'WORK' and table.db == 'KNOWLEDGE_BASE'
                   and table.name.startswith('BI_KB_') for table in tree.find_all(exp.Table))
    assert 'kb-load-summary.json' in generated
