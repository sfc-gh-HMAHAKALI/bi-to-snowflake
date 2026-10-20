"""Validate source inventory without inventing model semantics.

SQLGlot parses expressions; only a deliberately bounded scalar/aggregate grammar
is accepted automatically. Unsupported constructs remain in the issue register.
"""
from __future__ import annotations

import copy
import re
import sqlglot
from sqlglot import exp
import config


class ModelError(ValueError):
    pass


def alias(value):
    result = re.sub(r'[^A-Za-z0-9_]', '_', value).strip('_').upper()
    if not result or result[0].isdigit(): result = 'T_' + result
    config.quote(result)
    return result


def expression(text, table, fields=None, metric=False):
    if not isinstance(text, str) or not text.strip():
        raise ModelError('Missing expression; supply a reviewed translation')
    if any(token in text for token in (';', '--', '/*', '${', '{measure}', '{date_column}', '#')):
        raise ModelError('Unresolved source syntax or multiple statements')
    try:
        trees = sqlglot.parse(text, read='snowflake')
        if len(trees) != 1 or trees[0] is None: raise ModelError('Expected one expression')
        tree = trees[0]
    except sqlglot.errors.ParseError as exc:
        raise ModelError('Expression requires a reviewed Snowflake translation') from exc
    allowed = {'Column', 'Identifier', 'Literal', 'Null', 'Boolean', 'Star', 'Sum', 'Avg', 'Count',
               'Min', 'Max', 'Distinct', 'Add', 'Sub', 'Mul', 'Div', 'Neg', 'Paren', 'EQ', 'NEQ',
               'GT', 'GTE', 'LT', 'LTE', 'And', 'Or', 'Not', 'Is', 'In', 'Between', 'Case', 'If',
               'Coalesce', 'Nullif', 'CurrentDate', 'Cast', 'DataType', 'DataTypeParam'}
    if any(type(node).__name__ not in allowed for node in tree.walk()):
        raise ModelError('Unsupported SQL construct; inspect rather than approximate it')
    if metric and not any(isinstance(node, exp.AggFunc) for node in tree.walk()):
        raise ModelError('Metric has no declared aggregate')
    if not metric and any(isinstance(node, exp.AggFunc) for node in tree.walk()):
        raise ModelError('Aggregate expression cannot be a row-level field or filter')
    if metric:
        for aggregate in tree.find_all(exp.AggFunc):
            if isinstance(aggregate.this, exp.Star):
                raise ModelError('COUNT(*) needs an explicit non-null field binding to its dataset')
            if aggregate.find_ancestor(exp.AggFunc):
                raise ModelError('Nested aggregates require grain-aware translation')
        for column in tree.find_all(exp.Column):
            if not column.find_ancestor(exp.AggFunc):
                raise ModelError('Metric contains a nonaggregated field')
    for column in tree.find_all(exp.Column):
        if column.table and column.table.upper() != table.upper():
            raise ModelError('Cross-table expression requires explicit semantic binding')
        if column.db or column.catalog:
            raise ModelError('Expression must use the logical table, not an external SQL reference')
        name = column.name if column.this.args.get('quoted') else column.name.upper()
        if fields is not None:
            if name not in fields: raise ModelError(f'Unknown field {name!r}')
            name = fields[name]
        column.set('this', exp.to_identifier(name, quoted=True))
        column.set('table', exp.to_identifier(table, quoted=True) if metric else None)
    return tree.sql(dialect='snowflake')


def source_expression(item, table):
    text = item.get('expr', '')
    original = item.get('original') or {}
    # Exact single physical-column references only; no regex translation of calculations.
    if original.get('external_name') and not original.get('expression'):
        return config.quote(original['external_name'])
    match = re.fullmatch(r'(?:\[[^\]]+\]\.)*\[([^\]]+)\]', text)
    if match:
        components = re.findall(r'\[([^\]]+)\]', text)
        if len(components) > 1 and components[-2] != table:
            raise ModelError('Cross-entity source reference requires explicit translation')
        return config.quote(match[1])
    # The inventory may qualify fields with its full physical table name.
    if text.startswith(table + '.'):
        return text[len(table) + 1:]
    return text


def resolve_inventory(inventory, settings):
    """Return a serializable model contract and complete, stable issue references."""
    inventory = copy.deepcopy(inventory)
    issues = []
    def issue(ref, message):
        issues.append({'reference': ref, 'message': message, 'severity': 'blocker'})
    raw_metadata = inventory.get('source_metadata') or {}
    def inspect_restrictions(value, path='source_metadata'):
        restricted = {'required_access_grants', 'access_grants', 'sql_always_having',
                      'sql_where', 'sql_table_name_where', 'always_join', 'source_filters',
                      'custom_relations', 'securityProfiles', 'dataSecurityProfiles',
                      'businessSecurityProfiles', 'objectLevelSecurity', 'tableRestrictions',
                      'calculationGroup', 'calculationGroups'}
        if isinstance(value, dict):
            for key, child in value.items():
                if inventory.get('source_type') == 'looker' and key in ('sql_always_where', 'always_filter', 'access_filters', 'conditionally_filter') and child:
                    issue(path + ':' + key, 'Preserve the complete LookML restriction scope')
                if key in restricted and child:
                    issue(path + ':' + key, 'Source restriction requires an explicit scope-preserving implementation')
                inspect_restrictions(child, path + ':' + key)
        elif isinstance(value, list):
            for index, child in enumerate(value): inspect_restrictions(child, path + ':' + str(index))
    inspect_restrictions(raw_metadata)
    # Normalizers do not yet lower all source restrictions. Preserve and block
    # them rather than treating absence from inventory.filters as no restriction.
    for index, explore in enumerate(raw_metadata.get('explores', [])):
        if any(explore.get(key) for key in ('sql_always_where', 'always_filter', 'access_filters', 'conditionally_filter')):
            issue(f'source_metadata:explores:{index}', 'Explore filters/security require explicit scope-preserving translation')
    for index, view in enumerate(raw_metadata.get('views', [])):
        if view.get('derived_table') or view.get('dimension_groups') or view.get('parameters') or view.get('extends'):
            issue(f'source_metadata:views:{index}', 'Derived tables, time groups or parameters need explicit translation')
        for metric_index, measure in enumerate(view.get('measures', [])):
            if measure.get('filters') or measure.get('sql_distinct_key'):
                issue(f'source_metadata:views:{index}:measures:{metric_index}', 'Filtered/distinct-key measures cannot be simplified to plain aggregates')
    if inventory.get('errors'): issue('errors', 'Source parser reported errors; inspect inventory.errors')
    if inventory.get('flagged'): issue('flagged', 'Source flags require review; preserve them in a corrected inventory')
    if inventory.get('source_type') == 'businessobjects':
        for key in ('contexts', 'derived_tables'):
            if raw_metadata.get(key): issue('source_metadata:' + key, 'BusinessObjects context/derived table requires relational translation')
    if inventory.get('source_type') == 'powerbi':
        source_definition = raw_metadata.get('source_definition', {})
        for role in source_definition.get('roles', []):
            for permission in role.get('tablePermissions', []):
                if permission.get('columnPermissions') or permission.get('metadataPermission'):
                    issue('source_metadata:roles', 'Object-level security cannot be represented as a row filter')
    known_references = {f'{kind}:{index}' for kind in ('dimensions', 'facts', 'metrics', 'filters')
                        for index, _ in enumerate(inventory.get(kind, []))}
    for reference in set(settings.translations) - known_references:
        issue(reference, 'Translation does not identify a source item; check its reference')
    relationship_refs = {f'relationships:{index}' for index, _ in enumerate(inventory.get('relationships', []))}
    for reference in set(settings.relationship_mappings) - relationship_refs:
        issue(reference, 'Relationship mapping does not identify a source relationship')
    if inventory.get('source_type') not in ('cognos', 'looker', 'tableau', 'powerbi', 'denodo', 'businessobjects', 'fictional'):
        issue('source_type', 'Resolve each source separately before composing a multi-source model')
    for index, rule in enumerate(inventory.get('aggregation_rules', [])):
        regular = str(rule.get('regular_aggregate') or rule.get('aggregate') or '').lower()
        rollup = str(rule.get('rollup_aggregate') or '').lower()
        if rule.get('semi_additive') or rule.get('is_semi_additive') or (rollup and rollup not in ('automatic', regular)):
            issue(f'aggregation_rules:{index}', 'Semi-additive/rollup behavior needs explicit grain-aware translation')

    tables = []
    table_names = {}
    for index, raw in enumerate(inventory.get('tables', [])):
        name = raw.get('name', '')
        ref = f'tables:{index}'
        if not name:
            issue(ref, 'Source table has no name')
            continue
        logical = alias(name)
        if logical in table_names.values():
            issue(ref, 'Logical name collision; choose distinct source aliases')
            continue
        table_names[name] = logical
        physical = settings.source_mappings.get(name)
        if not physical:
            physical = raw.get('physical_table') or raw.get('physical_name') or ''
        try:
            parts = config.qualified(physical)
        except ValueError:
            if raw.get('database') and raw.get('schema') and physical:
                try: parts = (config.identifier(raw['database']), config.identifier(raw['schema']), config.identifier(physical))
                except ValueError:
                    issue(ref, 'Invalid source binding; set source_mappings using a three-part name')
                    continue
            else:
                issue(ref, 'Set source_mappings[' + name + '] to an explicit DATABASE.SCHEMA.OBJECT')
                continue
        if raw.get('sql') and not raw.get('is_passthrough'):
            issue(ref + ':sql', 'Derived source query needs review; mapping a table does not preserve its transformations')
        entity_filters = raw.get('entity_filters') or []
        declared_filters = {f.get('expression') or f.get('expr') for f in inventory.get('filters', [])
                            if (f.get('table') or f.get('entity')) == name}
        if any((f.get('expression') or f.get('expr')) not in declared_filters for f in entity_filters):
            issue(ref + ':filters', 'Entity filters are absent from normalized filters; preserve their scope before generation')
        table = {'name': logical, 'source_name': name, 'source': config.fqn(*parts), 'source_parts': list(parts),
                 'description': raw.get('description', ''), 'fields': [], 'metrics': [], 'filters': [], 'primary_key': []}
        field_map = {}
        physical_fields = {}
        for kind in ('dimensions', 'facts'):
            for item_index, item in enumerate(inventory.get(kind, [])):
                if item.get('table') not in (name, raw.get('view_name')): continue
                field_ref = f'{kind}:{item_index}'
                try:
                    field_name = alias(item['name'])
                    if field_name in field_map: raise ModelError('Duplicate logical field name')
                    text = settings.translations[field_ref] if field_ref in settings.translations else source_expression(item, name)
                    if item.get('complexity') in ('manual_required', 'needs_translation') and field_ref not in settings.translations:
                        raise ModelError('Source expression needs reviewed translation')
                    text = expression(text, logical)
                    field_map[field_name] = field_name
                    parsed_field = sqlglot.parse_one(text, read='snowflake')
                    if isinstance(parsed_field, exp.Column):
                        physical_fields.setdefault(parsed_field.name, []).append(field_name)
                    data_type = str(item.get('data_type') or '').upper()
                    table['fields'].append({'name': field_name, 'expr': text, 'data_type': data_type,
                                            'dimension': kind == 'dimensions', 'description': item.get('description', ''),
                                            'reference': field_ref})
                    if item.get('primary_key') is True: table['primary_key'].append(field_name)
                    original = item.get('original') or {}
                    aggregate = original.get('regular_aggregate') or original.get('aggregate') or original.get('summarize_by')
                    if kind == 'facts' and aggregate:
                        aggregate = str(aggregate).upper()
                        aggregate = {'TOTAL': 'SUM', 'AVERAGE': 'AVG'}.get(aggregate, aggregate)
                        if aggregate not in ('SUM', 'AVG', 'MIN', 'MAX', 'COUNT'):
                            raise ModelError('Unsupported fact aggregate; no SUM fallback')
                        table['metrics'].append({'name': field_name + '_' + aggregate,
                                                 'expr': f'{aggregate}({config.fqn(logical, field_name)})',
                                                 'description': item.get('description', ''), 'reference': field_ref})
                except (ValueError, KeyError) as exc:
                    issue(field_ref, str(exc))
        for item_index, item in enumerate(inventory.get('metrics', [])):
            if item.get('table') not in (name, raw.get('view_name')): continue
            metric_ref = f'metrics:{item_index}'
            try:
                text = settings.translations[metric_ref] if metric_ref in settings.translations else source_expression(item, name)
                original = item.get('original') or {}
                if inventory.get('source_type') == 'looker' and metric_ref not in settings.translations:
                    aggregate = str(original.get('type') or '').upper()
                    aggregate = {'AVERAGE': 'AVG'}.get(aggregate, aggregate)
                    if aggregate == 'COUNT' and not text: text = 'COUNT(*)'
                    elif aggregate in ('SUM', 'AVG', 'MIN', 'MAX', 'COUNT'):
                        text = aggregate + '(' + text + ')'
                    else: raise ModelError('Looker measure type requires explicit translation')
                if inventory.get('source_type') in ('powerbi', 'tableau') and metric_ref not in settings.translations:
                    raise ModelError('Source formula requires a reviewed Snowflake expression; dialect similarity is not equivalence')
                if item.get('complexity') in ('manual_required', 'needs_translation') and metric_ref not in settings.translations:
                    raise ModelError('Metric requires reviewed translation')
                bindings = {**field_map, **{key: names[0] for key, names in physical_fields.items() if len(names) == 1}}
                table['metrics'].append({'name': alias(item['name']), 'expr': expression(text, logical, bindings, True),
                                         'description': item.get('description', ''), 'reference': metric_ref})
            except (ValueError, KeyError) as exc: issue(metric_ref, str(exc))
        for grain_index, grain in enumerate(inventory.get('grain_declarations', [])):
            if grain.get('entity') != name or not grain.get('is_row_grain'): continue
            keys = [alias(key) for key in grain.get('key_columns', [])]
            if not keys or any(key not in field_map for key in keys):
                issue(f'grain_declarations:{grain_index}', 'Row determinant references unresolved fields')
            elif table['primary_key'] and set(table['primary_key']) != set(keys):
                issue(f'grain_declarations:{grain_index}', 'Conflicting source-declared row keys')
            else:
                table['primary_key'] = keys
        metric_names = [m['name'] for m in table['metrics']]
        if len(set(metric_names)) != len(metric_names): issue(ref, 'Duplicate metric names')
        if not table['fields']: issue(ref, 'No resolved fields')
        tables.append(table)

    for kind in ('dimensions', 'facts', 'metrics'):
        for index, item in enumerate(inventory.get(kind, [])):
            if not any(item.get('table') in (t.get('name'), t.get('view_name')) for t in inventory.get('tables', [])):
                issue(f'{kind}:{index}', 'Field/metric has no unambiguous table binding')

    for index, filt in enumerate(inventory.get('filters', [])):
        ref = f'filters:{index}'
        entity = filt.get('table') or filt.get('entity')
        target = next((t for t in tables if t['source_name'] == entity), None)
        if target is None:
            issue(ref, 'Filter has no unambiguous entity scope')
            continue
        try:
            text = settings.translations.get(ref, filt.get('expr') or filt.get('expression'))
            # A filter is evaluated against source columns, not renamed semantic fields.
            physical = {}
            for field in target['fields']:
                parsed = sqlglot.parse_one(field['expr'], read='snowflake')
                for col in parsed.find_all(exp.Column): physical[col.name] = col.name
            target['filters'].append(expression(text, target['name'], physical))
        except ValueError as exc: issue(ref, str(exc))

    relationships = []
    by_name = {t['name']: t for t in tables}
    for index, raw in enumerate(inventory.get('relationships', [])):
        if raw.get('is_active') is False: continue
        ref = f'relationships:{index}'
        rel = {**raw, **settings.relationship_mappings.get(ref, {})}
        join_type = str(rel.get('join_type') or '').lower().replace(' ', '_')
        if join_type not in ('', 'left', 'left_outer'):
            issue(ref, 'This OSI path cannot preserve the declared join type; do not silently replace it')
            continue
        left = table_names.get(rel.get('left_table'))
        right = table_names.get(rel.get('right_table'))
        cardinality = rel.get('relationship_type') or rel.get('cardinality')
        if rel.get('cross_filter_behavior') not in (None, '', 'oneDirection'):
            issue(ref, 'Bidirectional relationship filtering needs a scope-preserving translation')
            continue
        cardinality = {'many:one': 'many_to_one', 'one:many': 'one_to_many', 'one:one': 'one_to_one',
                       'n-to-1': 'many_to_one', '1-to-n': 'one_to_many', '1-to-1': 'one_to_one'}.get(cardinality, cardinality)
        if left not in by_name or right not in by_name or cardinality not in ('many_to_one', 'one_to_many', 'one_to_one'):
            issue(ref, 'Resolve endpoints and explicit one-side cardinality')
            continue
        left_columns = rel.get('left_columns') or [rel.get('left_column')]
        right_columns = rel.get('right_columns') or [rel.get('right_column')]
        if not isinstance(left_columns, list) or not isinstance(right_columns, list):
            issue(ref, 'Join columns must be lists')
            continue
        if not all(left_columns + right_columns) or len(left_columns) != len(right_columns):
            issue(ref, 'Resolve all equality join column pairs')
            continue
        left_columns, right_columns = [alias(c) for c in left_columns], [alias(c) for c in right_columns]
        if cardinality == 'one_to_many':
            if join_type:
                issue(ref, 'Reversing this outer join changes its preserved side; explicit role/grain translation is required')
                continue
            left, right, left_columns, right_columns = right, left, right_columns, left_columns
        if any(c not in {f['name'] for f in by_name[left]['fields']} for c in left_columns) or any(c not in {f['name'] for f in by_name[right]['fields']} for c in right_columns):
            issue(ref, 'Join references unresolved fields')
            continue
        if cardinality == 'one_to_one' and set(left_columns) != set(by_name[left]['primary_key']):
            issue(ref, 'One-to-one join needs source-declared keys on both sides')
            continue
        if set(right_columns) != set(by_name[right]['primary_key']):
            issue(ref, 'One-side join columns must match a source-declared primary key')
            continue
        if rel.get('condition') and ref not in settings.relationship_mappings:
            issue(ref, 'Review full join predicate before reducing it to equality column pairs')
            continue
        relationships.append({'name': alias(rel.get('name') or f'JOIN_{index}'), 'from': left, 'to': right,
                              'from_columns': left_columns, 'to_columns': right_columns})
    # Grain declarations are never replaced by an identifier-name heuristic.
    for index, grain in enumerate(inventory.get('grain_declarations', [])):
        if grain.get('groupable') or grain.get('group_by') or grain.get('groupBy') or grain.get('is_group_by'):
            issue(f'grain_declarations:{index}', 'Grouped determinant requires grain-aware translation')
    # Reject cycles and alternate join paths rather than giving the importer an
    # ambiguous graph. A reviewed model can introduce explicit role-playing aliases.
    parent = {table['name']: table['name'] for table in tables}
    def root(name):
        while parent[name] != name: name = parent[name]
        return name
    for relationship in relationships:
        left, right = root(relationship['from']), root(relationship['to'])
        if left == right:
            issue('relationships', 'Cycle/alternate join path requires explicit role-playing aliases')
        else:
            parent[left] = right
    if not tables: issue('tables', 'No resolved source tables')
    if not any(t['metrics'] for t in tables) and set(settings.effective_outputs) - {'catalog'}:
        issue('metrics', 'No declared aggregate metrics; supply source definitions, not guessed SUMs')
    from security_contract import resolve as resolve_security
    security_rules, security_issues = resolve_security(inventory, settings, tables, expression)
    for reference, message in security_issues: issue(reference, message)
    return {'version': 1, 'name': settings.naming.semantic_view, 'label': settings.model_label,
            'security': security_rules,
            'source_type': inventory.get('source_type'), 'tables': tables, 'relationships': relationships,
            'hierarchies': inventory.get('hierarchies', []), 'issues': issues,
            'source_metadata': {key: inventory.get(key, []) for key in ('grain_declarations', 'aggregation_rules', 'security_rules', 'flagged')}}


def osi(model):
    if model['issues']: raise ModelError('Resolve all model issues before semantic generation')
    def expr(text): return {'dialects': [{'dialect': 'SNOWFLAKE', 'expression': text}]}
    datasets = []
    metrics = []
    for table in model['tables']:
        source = table['source']
        if table['filters']:
            source = 'SELECT * FROM ' + source + ' WHERE ' + ' AND '.join('(' + f + ')' for f in table['filters'])
        fields = []
        for item in table['fields']:
            field = {'name': item['name'], 'expression': expr(item['expr']), 'description': item['description']}
            if item['dimension']:
                field['dimension'] = {'is_time': item['data_type'].startswith(('DATE', 'TIME'))}
            fields.append(field)
        dataset = {'name': table['name'], 'source': source, 'fields': fields, 'description': table['description']}
        if table['primary_key']: dataset['primary_key'] = table['primary_key']
        datasets.append(dataset)
        for metric in table['metrics']:
            metrics.append({'name': table['name'] + '_' + metric['name'], 'expression': expr(metric['expr']), 'description': metric['description']})
    return {'version': '0.1.1', 'name': model['name'], 'description': model['label'],
            'datasets': datasets, 'relationships': model['relationships'], 'metrics': metrics}
