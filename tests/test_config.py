import argparse
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
import config


def test_identifiers():
    assert config.identifier('reporting') == 'REPORTING'
    assert config.identifier('"Mixed.Schema"') == 'Mixed.Schema'
    assert config.qualified('"Db.One"."Reports"."T"') == ('Db.One', 'Reports', 'T')
    assert config.fqn('Db.One', 'Reports', 'a"b') == '"Db.One"."Reports"."a""b"'
    for bad in ('', 'a;drop', 'a b', '"unterminated', 'x' * 256):
        with pytest.raises(ValueError): config.identifier(bad)


def test_explicit_default_overrides_file(tmp_path):
    path = tmp_path / 'names.json'
    path.write_text(json.dumps({'analytics_schema': 'CUSTOM', 'prefix': 'OTHER'}))
    parser = argparse.ArgumentParser()
    config.add_arguments(parser)
    result = config.from_args(parser.parse_args(['--naming-file', str(path), '--analytics-schema', 'ANALYTICS', '--prefix', 'BI']))
    assert result.analytics_schema == 'ANALYTICS'
    assert result.prefix == 'BI'


def test_guided_and_noninteractive_equivalent(tmp_path):
    answers = iter(['WORK', 'REPORTS', str(tmp_path), '/fictional/inventory.json'])
    guided = config.guided(config.RunConfig(), lambda _: next(answers)).validate()
    direct = config.RunConfig(database='WORK', analytics_schema='REPORTS', output_dir=str(tmp_path), inventory='/fictional/inventory.json').validate()
    assert guided == direct


def test_resume_override_precedence(tmp_path):
    (tmp_path / 'resolved-config.json').write_text(json.dumps({'database': 'OLD', 'analytics_schema': 'OLD', 'prefix': 'X'}))
    override = tmp_path / 'new.json'
    override.write_text(json.dumps({'database': 'NEW', 'prefix': 'Y'}))
    result = config.resolve(argparse.Namespace(resume=str(tmp_path), config=str(override), prefix='BI'))
    assert (result.database, result.analytics_schema, result.prefix) == ('NEW', 'OLD', 'BI')


def test_quoted_forwarding():
    naming = config.Naming(database='"Mixed"', analytics_schema='"Target.One"', prefix='"Mi"')
    parser = argparse.ArgumentParser()
    config.add_arguments(parser)
    assert config.from_args(parser.parse_args(config.forward_flags(naming))) == naming


def test_required_settings_and_outputs(tmp_path):
    with pytest.raises(ValueError, match='database'): config.RunConfig().validate()
    with pytest.raises(ValueError, match='outputs'):
        config.RunConfig(database='D', analytics_schema='S', output_dir=str(tmp_path), inventory='x', outputs=['typo']).validate()
    assert config.RunConfig(outputs=['agent']).effective_outputs == ['agent', 'semantic']
    assert config.RunConfig(outputs=['catalog']).effective_outputs == ['catalog']
