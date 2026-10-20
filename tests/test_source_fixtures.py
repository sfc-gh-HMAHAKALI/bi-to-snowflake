import pytest
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
from modules.looker.parser import parse_lookml_project
from modules.cognos.parser import parse_framework_manager_model
from modules.output.inventory import build_unified_inventory
from model_contract import resolve_inventory, osi
from test_model_contract import settings


def test_fictional_library_lookml(tmp_path):
    source = tmp_path / 'library'
    source.mkdir()
    (source / 'loans.view.lkml').write_text('''view: loans {
      sql_table_name: SOURCE_LIBRARY.PUBLIC.LOANS ;;
      dimension: loan_id { primary_key: yes type: number sql: ${TABLE}.LOAN_ID ;; }
      dimension: branch { type: string sql: ${TABLE}.BRANCH ;; }
      dimension: duration { type: number sql: ${TABLE}.DURATION ;; }
      measure: mean_duration { type: average sql: ${TABLE}.DURATION ;; }
    }''')
    parsed = parse_lookml_project(str(source))
    inventory = build_unified_inventory(parsed, 'looker')
    model = resolve_inventory(inventory, settings(tmp_path))
    assert not model['issues'], model['issues']
    output = osi(model)
    assert output['datasets'][0]['source'] == '"SOURCE_LIBRARY"."PUBLIC"."LOANS"'
    assert 'AVG(' in str(output['metrics'])


def test_fictional_weather_cognos(tmp_path):
    source = tmp_path / 'weather.xml'
    source.write_text('''<?xml version="1.0"?>
    <project><name>Fictional Weather</name><namespace><name>Import View</name>
    <querySubject><name>OBSERVATIONS</name>
      <queryItem><name>STATION</name><externalName>STATION</externalName><datatype>varchar</datatype><usage>attribute</usage></queryItem>
      <queryItem><name>TEMPERATURE</name><externalName>TEMPERATURE</externalName><datatype>double</datatype><usage>fact</usage><regularAggregate>average</regularAggregate><semiAggregate>average</semiAggregate></queryItem>
    </querySubject></namespace></project>''')
    parsed = parse_framework_manager_model(str(source))
    inventory = build_unified_inventory(parsed, 'cognos')
    conf = settings(tmp_path, source_mappings={'OBSERVATIONS': 'SOURCE_WEATHER.RAW.OBSERVATIONS'})
    model = resolve_inventory(inventory, conf)
    assert not model['issues'], model['issues']
    output = osi(model)
    assert output['datasets'][0]['source'] == '"SOURCE_WEATHER"."RAW"."OBSERVATIONS"'
    assert 'AVG(' in str(output['metrics'])
    assert 'LOANS' not in str(output)


@pytest.mark.parametrize('source_type', ['tableau', 'powerbi', 'denodo', 'businessobjects'])
def test_remaining_real_adapters(tmp_path, source_type):
    from modules.cli import _run_parser
    translations = {}
    mappings = {}
    if source_type == 'tableau':
        path = tmp_path / 'weather.tds'
        path.write_text('''<datasource name="WEATHER"><connection class="snowflake" dbname="RAW" schema="WEATHER">
        <relation name="READINGS" table="READINGS" type="table"/></connection>
        <column name="[STATION]" role="dimension" datatype="string"/>
        <column name="[TEMP]" role="measure" datatype="real" aggregation="average"/></datasource>''')
    elif source_type == 'powerbi':
        path = tmp_path / 'weather.bim'
        path.write_text(json.dumps({'model': {'tables': [{'name': 'Readings', 'columns': [
            {'name': 'Station', 'sourceColumn': 'STATION', 'dataType': 'string', 'summarizeBy': 'none'},
            {'name': 'Temperature', 'sourceColumn': 'TEMP', 'dataType': 'double', 'summarizeBy': 'average'}]}]}}))
        mappings = {'READINGS': 'RAW.WEATHER.READINGS'}
    elif source_type == 'denodo':
        path = tmp_path / 'weather.vql'
        path.write_text('CREATE OR REPLACE VIEW WEATHER AS SELECT STATION, AVG(TEMP) AS MEAN_TEMP FROM RAW.WEATHER.READINGS WHERE TEMP > 0 GROUP BY STATION;')
    else:
        path = tmp_path / 'weather.json'
        path.write_text(json.dumps({'dataFoundation': {'tables': [{'name': 'READINGS', 'sqlName': 'RAW.WEATHER.READINGS'}]},
            'businessLayer': {'objects': [
                {'name': 'Station', 'qualification': 'dimension', 'select': 'READINGS.STATION'},
                {'name': 'Mean temperature', 'qualification': 'measure', 'select': 'AVG(READINGS.TEMP)'}]}}))
    inventory = build_unified_inventory(_run_parser(str(path), source_type), source_type)
    conf = settings(tmp_path, source_mappings=mappings, translations=translations)
    model = resolve_inventory(inventory, conf)
    assert not model['issues'], model['issues']
    output = osi(model)
    assert 'AVG(' in str(output['metrics'])
    assert 'RAW' in output['datasets'][0]['source']
    if source_type == 'denodo': assert 'WHERE' in output['datasets'][0]['source']


def test_powerbi_security_survives_parser(tmp_path):
    from modules.powerbi.parser import extract_semantics
    source = {'roles': [{'name': 'Station readers', 'tablePermissions': [{'name': 'Readings', 'filterExpression': '[STATION] = "A"'}]}]}
    inventory = build_unified_inventory(extract_semantics(source), 'powerbi')
    assert inventory['security_rules'][0]['expression'] == '[STATION] = "A"'
    assert inventory['source_metadata']['source_definition'] == source


def test_source_restrictions_are_not_lost(tmp_path):
    from modules.cli import _run_parser
    path = tmp_path / 'restricted.tds'
    path.write_text('''<datasource name="READINGS"><connection dbname="RAW" schema="WEATHER">
      <relation name="READINGS" table="READINGS" type="table"/></connection>
      <column name="[TEMP]" role="measure" datatype="real" aggregation="average"/>
      <filter column="[TEMP]"><groupfilter function="member" member="1"/></filter></datasource>''')
    inventory = build_unified_inventory(_run_parser(str(path), 'tableau'), 'tableau')
    assert 'groupfilter' in inventory['source_metadata']['source_filters'][0]
    assert resolve_inventory(inventory, settings(tmp_path))['issues']
    path = tmp_path / 'restricted.vql'
    path.write_text('CREATE OR REPLACE VIEW X AS SELECT STATION, AVG(TEMP) AS VALUE FROM RAW.WEATHER.READINGS GROUP BY STATION HAVING AVG(TEMP) > 10;')
    inventory = build_unified_inventory(_run_parser(str(path), 'denodo'), 'denodo')
    assert resolve_inventory(inventory, settings(tmp_path))['issues']
    path = tmp_path / 'restricted.json'
    path.write_text(json.dumps({'dataFoundation': {'tables': []}, 'businessLayer': {},
                                'securityProfiles': [{'name': 'restricted'}]}))
    inventory = build_unified_inventory(_run_parser(str(path), 'businessobjects'), 'businessobjects')
    assert any('securityProfiles' in issue['reference'] for issue in resolve_inventory(inventory, settings(tmp_path))['issues'])


def test_looker_top_level_access_grants_survive(tmp_path):
    source = tmp_path / 'project'
    source.mkdir()
    (source / 'access.model.lkml').write_text('access_grant: restricted { user_attribute: collection allowed_values: ["public"] }')
    inventory = build_unified_inventory(parse_lookml_project(str(source)), 'looker')
    assert inventory['source_metadata']['source_files'][0]['definition']['access_grants']
    assert any('access_grants' in issue['reference'] for issue in resolve_inventory(inventory, settings(tmp_path))['issues'])


def test_merged_sources_retain_restrictions():
    from modules.output.inventory import merge_inventories
    merged = merge_inventories([{'source_metadata': {'sql_where': 'PRIVATE = 1'}, 'tables': [{'name': 'X'}]},
                                {'tables': [{'name': 'X'}]}])
    assert merged['errors']
    assert merged['source_metadata']['inputs'][0]['sql_where']
