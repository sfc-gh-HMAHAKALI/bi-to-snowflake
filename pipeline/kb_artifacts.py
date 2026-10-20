"""Reuse the relational KB schema and loader to generate offline load plans."""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import sqlglot
from sqlglot import exp
import config
from kb_loader import KnowledgeBaseLoader

ROOT = Path(__file__).resolve().parent


def normalized_inventory(model, original=None):
    inventory = copy.deepcopy(original or {})
    inventory.update(source_type=model['source_type'], tables=[], dimensions=[], facts=[], metrics=[])
    for table in model['tables']:
        inventory['tables'].append({'name': table['source_name'], 'database': table['source_parts'][0],
            'schema': table['source_parts'][1], 'physical_name': table['source_parts'][2],
            'source_alias': table['source_name'], 'description': table['description'],
            'is_passthrough': not table['filters'], 'complexity': 'simple',
            'sql': ('SELECT * FROM ' + table['source'] + ' WHERE ' +
                    ' AND '.join('(' + predicate + ')' for predicate in table['filters'])) if table['filters'] else ''})
        for field in table['fields']:
            parsed = sqlglot.parse_one(field['expr'], read='snowflake')
            physical = parsed.name if isinstance(parsed, exp.Column) else None
            item = {'name': field['name'], 'table': table['source_name'], 'expr': field['expr'],
                    'data_type': field['data_type'], 'description': field['description'],
                    'complexity': 'simple', 'original': {'expression': field['expr'],
                        'external_name': physical, 'is_calculated': physical is None,
                        'usage': 'identifier' if field['name'] in table['primary_key'] else 'attribute'}}
            inventory['dimensions' if field['dimension'] else 'facts'].append(item)
        for metric in table['metrics']:
            inventory['metrics'].append({'name': table['name'] + '_' + metric['name'],
                'table': table['source_name'], 'expr': metric['expr'], 'description': metric['description'],
                'complexity': 'simple', 'original': {'expression': metric['expr']}})
    inventory['hierarchies'] = model['hierarchies']
    # Original source records remain in inventory.json. Do not fabricate source
    # join/security records from a lossy projection of the semantic graph.
    return inventory


def generate_kb(model, settings, original=None):
    naming = settings.naming
    captured = []
    class Collector:
        def run_statements(self, statements, label): captured.extend(statements)
    inventory = normalized_inventory(model, original)
    load_id = hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()
    loader = KnowledgeBaseLoader(Collector(), model['source_type'], model['label'], load_id,
        {table['source_name']: table['source_parts'][1] for table in model['tables']},
        config.quote(naming.kb_database), kb_schema=config.quote(naming.kb_schema))
    loader.load_source_model(inventory, {}, '')
    loader.load_entities(inventory, {})
    loader.load_terms(inventory)
    loader.load_metrics(inventory)
    loader.load_hierarchies(inventory)
    loader.load_grain(inventory)
    loader.load_aggregation_rules(inventory)
    loader.load_relationships(inventory)
    loader.load_security(inventory, [])
    loader.load_lineage(inventory)
    loader.load_issues(inventory, {})
    # Parse the shipped schema rather than splitting SQL on semicolons in comments.
    schema = config.render_sql((ROOT / 'sql/01_kb_tables.sql').read_text(), naming)
    objects = []
    statements = []
    names = {}
    for tree in sqlglot.parse(schema, read='snowflake'):
        if not isinstance(tree, exp.Create) or tree.args.get('kind') != 'TABLE': continue
        target = tree.this.this
        source_name = target.name
        target_name = naming.prefix + '_' + source_name
        target_fqn = config.fqn(naming.kb_database, naming.kb_schema, target_name)
        names[source_name] = target_fqn
        target.replace(exp.to_table(target_fqn, dialect='snowflake'))
        # The run executor checks targets first and records only new objects.
        tree.set('exists', False)
        objects.append({'kind': 'TABLE', 'name': target_fqn})
        statements.append({'object': target_fqn, 'kind': 'TABLE', 'sql': tree.sql(dialect='snowflake') + ';'})
    for sql in captured:
        tree = sqlglot.parse_one(sql, read='snowflake')
        if not isinstance(tree, exp.Merge) or tree.this.name not in names:
            raise ValueError('Unexpected KB loader target')
        target_fqn = names[tree.this.name]
        target = exp.to_table(target_fqn, dialect='snowflake')
        target.set('alias', tree.this.args.get('alias'))
        tree.set('this', target)
        statements.append({'object': target_fqn, 'kind': 'MERGE', 'sql': tree.sql(dialect='snowflake') + ';'})
    views_sql = config.render_sql((ROOT / 'sql/02_kb_views.sql').read_text(), naming)
    views = [tree for tree in sqlglot.parse(views_sql, read='snowflake')
             if isinstance(tree, exp.Create) and tree.args.get('kind') == 'VIEW']
    for tree in views:
        names[tree.this.name] = config.fqn(naming.kb_database, naming.kb_schema, naming.prefix + '_' + tree.this.name)
    for tree in views:
        target_fqn = names[tree.this.name]
        for table in list(tree.find_all(exp.Table)):
            if table.name not in names: raise ValueError('Unexpected KB consumer dependency: ' + table.name)
            replacement = exp.to_table(names[table.name], dialect='snowflake')
            replacement.set('alias', table.args.get('alias'))
            table.replace(replacement)
        tree.set('replace', False)
        objects.append({'kind': 'VIEW', 'name': target_fqn})
        statements.append({'object': target_fqn, 'kind': 'VIEW', 'sql': tree.sql(dialect='snowflake') + ';'})
    return objects, statements, loader.stats
