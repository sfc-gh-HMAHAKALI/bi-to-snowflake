"""App composition contract derived from the same resolved semantic model."""
from __future__ import annotations
import config


def build(model, settings):
    naming = settings.naming
    dashboards = []
    for table in model['tables']:
        if not table['metrics']: continue
        dashboards.append({'name': table['name'], 'synthesised': True,
                           'view': config.fqn(naming.database, naming.analytics_schema,
                                               naming.prefix + '_RPT_' + table['name'] + '_TOTAL'),
                           'dimensions': [f['name'] for f in table['fields'] if f['dimension']],
                           'metrics': [m['name'] for m in table['metrics']],
                           'aggregation': 'query at requested grain; never sum displayed averages/ratios'})
    return {'source_type': model['source_type'], 'source_model': model['label'],
            'snowflake_target': {'database': naming.database, 'schema': naming.analytics_schema},
            'semantic_view': config.fqn(naming.database, naming.analytics_schema, naming.semantic_view),
            'dashboards': dashboards, 'tables': model['tables'], 'drill_paths': model['hierarchies'],
            'provenance': {'dashboards_synthesised': True, 'runtime_validated': False}}


if __name__ == '__main__':
    from build import main
    raise SystemExit(main())
