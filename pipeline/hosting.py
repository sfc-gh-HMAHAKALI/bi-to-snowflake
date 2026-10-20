"""Prepare runtime-specific hosting artifacts without uploading or provisioning."""
import json
from pathlib import Path
import yaml
import config


def generate_hosting(settings, datasets, artifacts, objects):
    hosted = settings.hosted_outputs
    if not hosted: return {}
    settings.validate()
    naming = settings.naming
    owner = config.quote(config.identifier(settings.role))
    warehouse = config.quote(config.identifier(settings.warehouse))
    output = {}
    operations = []
    grants = [f'GRANT CALLER USAGE ON WAREHOUSE {warehouse} TO ROLE {owner};']
    scopes = {config.qualified(obj['name'])[:2] for obj in objects}
    for database, schema in sorted(scopes):
        grants.extend([f'GRANT CALLER USAGE ON DATABASE {config.quote(database)} TO ROLE {owner};',
                       f'GRANT CALLER USAGE ON SCHEMA {config.fqn(database, schema)} TO ROLE {owner};'])
    for obj in objects:
        if obj['kind'] not in ('VIEW', 'SEMANTIC VIEW', 'AGENT', 'CORTEX SEARCH SERVICE'): continue
        privilege = 'SELECT' if obj['kind'] in ('VIEW', 'SEMANTIC VIEW') else 'USAGE'
        grants.append(f'GRANT CALLER {privilege} ON {obj["kind"]} {obj["name"]} TO ROLE {owner};')
    if 'streamlit' in hosted:
        # Same charts and query contract; identity is deliberately NOT a named
        # developer connection in the hosted runtime.
        app = artifacts['app_streamlit/app.py']
        start = app.index('if not runtime["connection"]')
        stop = app.index('if not runtime["datasets"]:', start)
        app = app[:start] + 'connection = st.connection("snowflake-callers-rights")\n' + app[stop:]
        app = app.replace('    connection = st.connection("snowflake", connection_name=runtime["connection"],\n                               warehouse=runtime["warehouse"], role=runtime["role"])\n', '')
        app = app.replace("your connection's access", "the signed-in viewer's access")
        output['hosted_streamlit/app.py'] = app
        for path, content in artifacts.items():
            if path.startswith('app_streamlit/') and path != 'app_streamlit/app.py':
                output[path.replace('app_streamlit/', 'hosted_streamlit/', 1)] = content
        output['hosted_streamlit/requirements.txt'] = 'streamlit[snowflake]>=1.53.1,<2\nsnowflake-connector-python>=3.3,<5\npandas>=2,<3\nplotly>=5,<7\n'
        runtime = json.loads(output['hosted_streamlit/runtime.json'])
        runtime['connection'] = ''
        runtime['role'] = ''
        output['hosted_streamlit/runtime.json'] = json.dumps(runtime, indent=2)
        files = sorted(path.removeprefix('hosted_streamlit/') for path in output if path.startswith('hosted_streamlit/'))
        manifest = {'definition_version': 2, 'entities': {'report': {
            'type': 'streamlit', 'identifier': {'name': config.quote(naming.streamlit),
                'database': config.quote(naming.database), 'schema': config.quote(naming.analytics_schema)},
            'query_warehouse': warehouse, 'runtime_name': 'SYSTEM$ST_CONTAINER_RUNTIME_PY3_11',
            'compute_pool': config.quote(config.identifier(settings.compute_pool)),
            'external_access_integrations': [config.quote(config.identifier(item)) for item in settings.external_access_integrations],
            'main_file': 'app.py', 'artifacts': files}}}
        output['hosted_streamlit/snowflake.yml'] = yaml.safe_dump(manifest, sort_keys=False)
        streamlit = config.fqn(naming.database, naming.analytics_schema, naming.streamlit)
        operations.append({'kind': 'STREAMLIT', 'name': streamlit,
            'directory': 'hosted_streamlit', 'command': ['snow', 'streamlit', 'deploy', 'report', '--connection', settings.connection,
                                                       '--role', settings.role],
            'verify_sql': 'DESCRIBE STREAMLIT ' + streamlit + ';'})
        if settings.grant_role:
            grants.append(f'GRANT USAGE ON STREAMLIT {streamlit} TO ROLE {config.quote(config.identifier(settings.grant_role))};')
    if 'react' in hosted:
        for path, content in artifacts.items():
            if path.startswith('app_react/'):
                output[path.replace('app_react/', 'hosted_react/', 1)] = content
        package = json.loads(output['hosted_react/package.json'])
        package['scripts']['start'] = 'next start --hostname 0.0.0.0 --port 8080'
        output['hosted_react/package.json'] = json.dumps(package, indent=2)
        output['hosted_react/app/page.tsx'] = output['hosted_react/app/page.tsx'].replace(
            'if (!runtime.connection || !runtime.warehouse || !runtime.role)',
            "if (process.env.B2S_RUNTIME !== 'spcs')").replace(
            'Local single-user preview. Queries use your configured Snowflake role. Do not expose this server publicly.',
            'Queries run as the signed-in viewer. Missing caller identity is denied, never replaced with service-owner access.')
        runtime = json.loads(output['hosted_react/runtime.json'])
        runtime['connection'] = ''
        runtime['role'] = ''
        output['hosted_react/runtime.json'] = json.dumps(runtime, indent=2)
        output['hosted_react/app/health/route.ts'] = "export function GET() { return Response.json({status: 'ready'}); }\n"
        output['hosted_react/Dockerfile'] = '''FROM node:22-bookworm-slim
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY package.json ./
RUN npm install --ignore-scripts --no-audit --no-fund
COPY . .
RUN npm run build
ENV NODE_ENV=production
ENV B2S_RUNTIME=spcs
USER node
EXPOSE 8080
CMD ["npm", "start"]
'''
        output['hosted_react/requirements.txt'] = 'snowflake-connector-python>=3.3,<5\n'
        output['hosted_react/.dockerignore'] = 'node_modules\n.next\n.git\n.env*\n*.toml\n__pycache__\n'
        spec = {'spec': {'containers': [{'name': 'app', 'image': settings.container_image,
                'env': {'B2S_RUNTIME': 'spcs', 'NEXT_TELEMETRY_DISABLED': '1'},
                'readinessProbe': {'port': 8080, 'path': '/health'}}],
                'endpoints': [{'name': 'web', 'port': 8080, 'public': True}]},
                'capabilities': {'securityContext': {'executeAsCaller': True}}}
        service = config.fqn(naming.database, naming.analytics_schema, naming.app_service)
        output['hosted_react/service-spec.yaml'] = yaml.safe_dump(spec, sort_keys=False)
        output['hosted_react/create-service.sql'] = 'CREATE SERVICE ' + service + ' IN COMPUTE POOL ' + config.quote(config.identifier(settings.compute_pool)) + ' FROM SPECIFICATION ' + config.literal(output['hosted_react/service-spec.yaml']) + ';\n'
        operations.append({'kind': 'SERVICE', 'name': service, 'directory': 'hosted_react',
            'image': settings.container_image, 'sql_file': 'hosted_react/create-service.sql',
            'verify_sql': 'SHOW ENDPOINTS IN SERVICE ' + service + ';',
            'prerequisites': ['Build linux/amd64 Docker image', 'Upload image to the configured Snowflake repository',
                              'Verify existing compute pool, image repository and caller grants']})
        if settings.grant_role:
            grants.append(f'GRANT SERVICE ROLE {service}!ALL_ENDPOINTS_USAGE TO ROLE {config.quote(config.identifier(settings.grant_role))};')
    output['hosting-plan.json'] = json.dumps({'status': 'prepared_not_deployed', 'operations': operations,
        'owner_role': settings.role, 'connection': settings.connection,
        'requires_review': ['Existing destinations must not be replaced', 'No automatic compute or EAI creation',
                            'Review caller grants and ordinary viewer grants', 'Validate two users with different access',
                            'Record verified created objects before cleanup']}, indent=2)
    output['caller-grants-review.sql'] = '\n'.join(grants) + '\n'
    return output
