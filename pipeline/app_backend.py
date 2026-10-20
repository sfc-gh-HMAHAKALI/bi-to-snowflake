"""Generated-app backend: fixed queries, bounded chat, caller identity per request."""
import json
import os
from pathlib import Path
import sys


def chat_payload(question):
    if not isinstance(question, str) or not question.strip() or len(question) > 8000:
        raise ValueError('Question must contain 1 to 8000 characters')
    return json.dumps({'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': question}]}],
                       'stream': False})


def chat_response(value):
    response = json.loads(value) if isinstance(value, str) else value
    if not isinstance(response, dict): raise ValueError('Unexpected agent response')
    text = '\n'.join(item['text'] for item in response.get('content', [])
                     if item.get('type') == 'text' and isinstance(item.get('text'), str))
    warnings = response.get('warnings') or []
    if response.get('error') or not text: raise ValueError('Agent returned no answer')
    return {'text': text, 'warnings': warnings}


def connection_params(runtime, caller_token=None):
    if os.environ.get('B2S_RUNTIME') == 'spcs':
        if not isinstance(caller_token, str) or not caller_token:
            raise ValueError('Missing ingress caller identity; service-owner fallback is forbidden')
        return {'host': os.environ['SNOWFLAKE_HOST'], 'account': os.environ['SNOWFLAKE_ACCOUNT'],
                'authenticator': 'oauth', 'token': Path('/snowflake/session/token').read_text().strip() + '.' + caller_token,
                'warehouse': runtime['warehouse']}
    if not all(runtime.get(key) for key in ('connection', 'warehouse', 'role')):
        raise ValueError('Explicit local connection, warehouse and role required')
    return {key: runtime[key] for key in ('warehouse', 'role')} | {'connection_name': runtime['connection']}


def execute(runtime, request, connect=None):
    if request.get('action') == 'chat':
        if not runtime.get('agent'): raise ValueError('Chat is not enabled')
        sql = 'SELECT SNOWFLAKE.CORTEX.DATA_AGENT_RUN(%s, %s) AS RESPONSE'
        bindings = (runtime['agent'], chat_payload(request.get('question')))
    elif request.get('action') == 'query':
        dataset = next((item for item in runtime['datasets'] if item['name'] == request.get('dataset')), None)
        query = next((item for item in dataset['queries'] if item['name'] == request.get('query')), None) if dataset else None
        if query is None: raise ValueError('Unknown generated query')
        sql, bindings = query['sql'], None
    else:
        raise ValueError('Unknown action')
    parameters = connection_params(runtime, request.get('caller_token'))
    if connect is None:
        import snowflake.connector
        connect = snowflake.connector.connect
    with connect(**parameters) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, bindings)
            if request['action'] == 'chat': return chat_response(cursor.fetchone()[0])
            columns = [item[0] for item in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchmany(500)]


def main():
    try:
        request = json.loads(sys.stdin.read(20000))
        runtime = json.loads(Path(__file__).with_name('runtime.json').read_text())
        print(json.dumps(execute(runtime, request), default=str))
        return 0
    except Exception:
        # Connector messages can contain identity, SQL or credentials. Do not
        # include them in HTTP errors or subprocess stderr.
        print(json.dumps({'error': 'Request failed; verify deployment and caller grants.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
