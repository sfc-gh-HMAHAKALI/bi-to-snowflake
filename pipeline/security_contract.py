"""Reviewed, source-bound row restrictions on generated secure views only."""
import hashlib
import json
import config
from sqlglot import exp
import sqlglot


def rule_digest(rule):
    return hashlib.sha256(json.dumps(rule, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def resolve(inventory, settings, tables, validate_expression):
    rules = inventory.get('security_rules', [])
    mappings = settings.security
    issues, resolved = [], []
    expected = {f'security_rules:{index}' for index in range(len(rules))}
    for reference in set(mappings) - expected:
        issues.append((reference, 'Security mapping does not identify a source rule'))
    protected = set()
    for index, rule in enumerate(rules):
        reference = f'security_rules:{index}'
        mapping = mappings.get(reference, {})
        try:
            if not isinstance(mapping, dict) or set(mapping) != {'source_hash', 'principal', 'role', 'predicate', 'combination'}:
                raise ValueError('Supply source_hash, principal, role, predicate and combination=any_role for every source rule')
            if mapping['source_hash'] != rule_digest(rule):
                raise ValueError('Security source hash changed; review the complete source rule again')
            if not rule.get('principal') or mapping['principal'] != rule['principal']:
                raise ValueError('Principal mapping must match the declared source principal exactly')
            if mapping['combination'] != 'any_role':
                raise ValueError('Only explicitly reviewed any-role union semantics are implemented')
            entities = rule.get('entities') or ([rule['entity']] if rule.get('entity') else [])
            if len(entities) != 1:
                raise ValueError('Security predicate must have exactly one entity scope')
            table = next((item for item in tables if item['source_name'] == entities[0]), None)
            if table is None: raise ValueError('Security entity has no resolved source binding')
            fields = {}
            for field in table['fields']:
                for column in sqlglot.parse_one(field['expr'], read='snowflake').find_all(exp.Column):
                    fields[column.name] = column.name
            predicate = validate_expression(mapping['predicate'], table['name'], fields)
            tree = sqlglot.parse_one(predicate, read='snowflake')
            if not isinstance(tree, (exp.And, exp.Or, exp.Not, exp.EQ, exp.NEQ, exp.GT, exp.GTE,
                                     exp.LT, exp.LTE, exp.In, exp.Between, exp.Is, exp.Paren)):
                raise ValueError('Security mapping requires a Boolean row predicate, not a constant bypass')
            if not list(tree.find_all(exp.Column)):
                raise ValueError('Security predicate must restrict declared row columns')
            role = config.identifier(mapping['role'])
            resolved.append({'reference': reference, 'table': table['name'], 'role': role,
                             'predicate': predicate, 'source_hash': mapping['source_hash']})
            protected.add(table['source_name'])
        except (ValueError, TypeError, KeyError) as exc:
            issues.append((reference, str(exc)))
    for table in inventory.get('tables', []):
        if table.get('security_filter_count') and table['name'] not in protected:
            issues.append(('security_rules', 'Protected source entity has no complete executable security mapping'))
    return resolved, issues


def predicate_for(rules, table):
    predicates = ["(IS_ROLE_IN_SESSION(" + config.literal(rule['role']) + ") AND (" + rule['predicate'] + "))"
                  for rule in rules if rule['table'] == table]
    return ' OR '.join(predicates) if predicates else None
