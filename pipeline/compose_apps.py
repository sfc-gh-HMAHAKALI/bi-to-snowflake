"""Local previews using locked charts without changing query-grain metrics."""
import json
from pathlib import Path
import config

ROOT = Path(__file__).resolve().parents[1]


def compose(settings, datasets):
    output = {}
    runtime = {'connection': settings.connection,
               'warehouse': config.identifier(settings.warehouse) if settings.warehouse else '',
               'role': config.identifier(settings.role) if settings.role else '',
               'label': settings.model_label, 'datasets': datasets,
               'agent': config.fqn(settings.naming.database, settings.naming.analytics_schema, settings.naming.agent) if settings.chat else ''}
    if 'streamlit' in settings.outputs:
        output['app_streamlit/app_backend.py'] = (ROOT / 'pipeline/app_backend.py').read_text()
        output['app_streamlit/runtime.json'] = json.dumps(runtime, indent=2)
        for path in (ROOT / 'assets/streamlit_ui').glob('*.py'):
            output['app_streamlit/bim_ui/' + path.name] = path.read_text()
        output['app_streamlit/app.py'] = '''from pathlib import Path
import json
import streamlit as st
from app_backend import chat_payload, chat_response
from bim_ui import charts

runtime = json.loads(Path(__file__).with_name("runtime.json").read_text())
st.set_page_config(page_title=runtime["label"], layout="wide")
st.title(runtime["label"])
st.caption("Source-derived views. Each selection queries its own grain; averages and ratios are not reaggregated.")
if not runtime["connection"] or not runtime["warehouse"] or not runtime["role"]:
    st.error("Set connection, warehouse and role in the run configuration, then regenerate before opening this local app.")
    st.stop()
if not runtime["datasets"]:
    st.info("No declared metrics are available.")
    st.stop()
dataset = st.selectbox("Dataset", runtime["datasets"], format_func=lambda item: item["name"])
query = st.selectbox("Breakdown", dataset["queries"], format_func=lambda item: item["name"])
try:
    connection = st.connection("snowflake", connection_name=runtime["connection"],
                               warehouse=runtime["warehouse"], role=runtime["role"])
    frame = connection.query(query["sql"], ttl=0)
    if frame.empty:
        st.info("No rows returned under your current access.")
    else:
        if query["dimension"]:
            metric = st.selectbox("Metric", query["metrics"])
            charts.ranked_bar(frame, query["dimension"], metric, title=query["name"],
                              key="breakdown", pre_aggregated=True)
        st.dataframe(frame, hide_index=True)
        st.caption("At most 500 rows. No client-side rollups or inferred currency formatting.")
except Exception:
    st.error("Query failed. Verify the generated views are deployed and your configured role can read them. No alternative destination was used.")
if runtime.get("agent"):
    st.subheader("Ask the model")
    st.caption("Each question is independent. Answers use the configured agent and your connection's access.")
    if prompt := st.chat_input("Ask about declared metrics", max_chars=8000):
        with st.chat_message("user"):
            st.text(prompt)
        try:
            answer = connection.query("SELECT SNOWFLAKE.CORTEX.DATA_AGENT_RUN(%s, %s) AS RESPONSE",
                                      params=(runtime["agent"], chat_payload(prompt)), ttl=0)
            result = chat_response(answer.iloc[0, 0])
            with st.chat_message("assistant"):
                st.text(result["text"])
                if result["warnings"]: st.warning("Some agent tools were unavailable; this answer may be incomplete.")
        except Exception:
            st.error("Agent request failed. Check access and deployment; no alternate identity was used.")
'''
        output['app_streamlit/requirements.txt'] = 'streamlit>=1.45,<2\nsnowflake-connector-python>=3,<5\npandas>=2,<3\nplotly>=5,<7\n'
    if 'react' in settings.outputs:
        package = json.loads((ROOT / 'assets/react_ui/package.template.json').read_text())
        package.pop('_comment', None)
        package['scripts']['dev'] = 'next dev --hostname 127.0.0.1'
        package['scripts']['start'] = 'next start --hostname 127.0.0.1'
        output['app_react/package.json'] = json.dumps(package, indent=2)
        output['app_react/app_backend.py'] = (ROOT / 'pipeline/app_backend.py').read_text()
        output['app_react/runtime.json'] = json.dumps(runtime, indent=2)
        output['app_react/lib/backend.ts'] = '''import 'server-only';
import { execFileSync } from 'node:child_process';
import { join } from 'node:path';
import { headers } from 'next/headers';
export async function backend(request: Record<string, unknown>): Promise<unknown> {
  const caller = process.env.B2S_RUNTIME === 'spcs' ? (await headers()).get('sf-context-current-user-token') : undefined;
  if (process.env.B2S_RUNTIME === 'spcs' && !caller) throw new Error('Caller identity required');
  const raw = execFileSync('python3', [join(process.cwd(), 'app_backend.py')], {
    input: JSON.stringify({...request, caller_token: caller}), encoding: 'utf8',
    timeout: 180000, maxBuffer: 10000000, stdio: ['pipe', 'pipe', 'pipe']
  });
  return JSON.parse(raw);
}
'''
        output['app_react/app/api/chat/route.ts'] = '''import { NextRequest, NextResponse } from 'next/server';
import { backend } from '../../../lib/backend';
export async function POST(request: NextRequest) {
  const origin = request.headers.get('origin');
  if (!origin || new URL(origin).host !== request.headers.get('host')) return new NextResponse('Forbidden', {status: 403});
  try {
    const raw = await request.text();
    if (raw.length > 16000) return new NextResponse('Too large', {status: 413});
    const body = JSON.parse(raw);
    if (typeof body.question !== 'string' || body.question.length > 8000) return new NextResponse('Invalid question', {status: 400});
    return NextResponse.json(await backend({action: 'chat', question: body.question}), {headers: {'Cache-Control': 'no-store'}});
  } catch { return NextResponse.json({error: 'Agent request failed. Check access and deployment.'}, {status: 502}); }
}
'''
        output['app_react/components/Chat.tsx'] = '''"use client";
import { useState } from 'react';
export default function Chat() {
  const [question, setQuestion] = useState('');
  const [answer, setAnswer] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  return <section><h2>Ask the model</h2><p>Each question is independent.</p><form onSubmit={async event => {
    event.preventDefault(); setBusy(true); setError(''); setAnswer('');
    try {
      const response = await fetch('/api/chat', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({question})});
      const result = await response.json();
      if (!response.ok) throw new Error('Agent request failed. Check access and deployment.');
      setAnswer(result.text);
      if (result.warnings?.length) setError('Some tools were unavailable; answer may be incomplete.');
    } catch { setError('Agent request failed. Check access and deployment.'); }
    finally { setBusy(false); }
  }}><label>Question <textarea required maxLength={8000} value={question} onChange={event => setQuestion(event.target.value)}/></label>
  <button disabled={busy}>{busy ? 'Asking...' : 'Ask'}</button></form>
  {error && <p role="alert">{error}</p>}<p style={{whiteSpace: 'pre-wrap'}} aria-live="polite">{answer}</p></section>;
}
'''
        for name in ('theme.ts', 'charts.tsx', 'ui.tsx'):
            output['app_react/lib/' + name] = (ROOT / 'assets/react_ui' / name).read_text()
        for name in ('tailwind.config.ts', 'postcss.config.mjs', 'app/globals.css'):
            output['app_react/' + name] = (ROOT / 'assets/react_ui' / name).read_text()
        output['app_react/components/Chart.tsx'] = '''"use client";
import { useState } from 'react';
import dynamic from 'next/dynamic';
const RankedBar = dynamic(() => import('../lib/charts').then(module => module.RankedBar), {ssr: false});
export default function Chart({rows, dimension, metrics}: {
  rows: Record<string, unknown>[]; dimension: string; metrics: string[]
}) {
  const [selectedMetric, setMetric] = useState(metrics[0]);
  const metric = metrics.includes(selectedMetric) ? selectedMetric : metrics[0];
  return <><label>Metric <select value={metric} onChange={event => setMetric(event.target.value)}>
    {metrics.map(name => <option key={name}>{name}</option>)}
    </select></label><RankedBar rows={rows} labelKey={dimension} valueKey={metric} preAggregated
    format={value => value == null ? '--' : Number(value).toLocaleString(undefined, {maximumSignificantDigits: 4})}/></>;
}
'''
        output['app_react/tsconfig.json'] = json.dumps({'compilerOptions': {'paths': {'@/*': ['./*']}, 'target': 'ES2020', 'lib': ['dom', 'esnext'], 'jsx': 'preserve', 'module': 'esnext', 'moduleResolution': 'bundler', 'resolveJsonModule': True, 'esModuleInterop': True, 'strict': True, 'skipLibCheck': True, 'noEmit': True}, 'include': ['**/*.ts', '**/*.tsx']}, indent=2)
        output['app_react/app/layout.tsx'] = '''import type { ReactNode } from 'react';
import './globals.css';
import { NEUTRAL } from '../lib/theme';
export default function Layout({children}: {children: ReactNode}) {
  return <html lang="en"><body style={{background: NEUTRAL.wash, color: NEUTRAL.ink}}><main>{children}</main></body></html>;
}
'''
        output['app_react/app/page.tsx'] = '''import { backend } from '../lib/backend';
import Chat from '../components/Chat';
import runtime from '../runtime.json';
import Chart from '../components/Chart';
export const dynamic = 'force-dynamic';
export default async function Page({ searchParams }: {
  searchParams: Promise<{ dataset?: string; query?: string }>
}) {
  const input = await searchParams;
  const dataset = runtime.datasets.find(item => item.name === input.dataset) ?? runtime.datasets[0];
  const query = dataset?.queries.find(item => item.name === input.query) ?? dataset?.queries[0];
  let rows: Record<string, unknown>[] = [];
  let error = '';
  if (!runtime.connection || !runtime.warehouse || !runtime.role) {
    error = 'Set connection, warehouse and role in run configuration, then regenerate.';
  } else if (query) {
    try {
      const parsed = await backend({action: 'query', dataset: dataset.name, query: query.name});
      if (!Array.isArray(parsed)) throw new Error('Unexpected result');
      rows = parsed;
    } catch { error = 'Query failed. Check deployment, caller access and Python connector configuration. No destination fallback was used.'; }
  }
  const columns = rows.length ? Object.keys(rows[0]) : [];
  return <>
    <h1>{runtime.label}</h1>
    <p>Local single-user preview. Queries use your configured Snowflake role. Do not expose this server publicly.</p>
    <form><label>Dataset <select name="dataset" defaultValue={dataset?.name}>
      {runtime.datasets.map(item => <option key={item.name}>{item.name}</option>)}
    </select></label><button type="submit">Choose dataset</button></form>
    <form><input type="hidden" name="dataset" value={dataset?.name}/>
      <label>Breakdown <select name="query" defaultValue={query?.name}>
      {dataset?.queries.map(item => <option key={item.name}>{item.name}</option>)}
      </select></label><button type="submit">Load breakdown</button></form>
    {!error && rows.length > 0 && query?.dimension && <Chart rows={rows} dimension={query.dimension} metrics={query.metrics}/>}
    {error ? <p role="alert">{error}</p> : !rows.length ? <p>No rows returned under your current access.</p> :
      <div style={{overflowX: 'auto'}}><table><thead><tr>{columns.map(column => <th key={column}>{column}</th>)}</tr></thead>
      <tbody>{rows.map((row, index) => <tr key={index}>{columns.map(column => <td key={column}>{String(row[column] ?? '')}</td>)}</tr>)}</tbody></table></div>}
    {runtime.agent && <Chat/>}
    <p>At most 500 rows. Metrics are queried at the selected grain, not summed in the browser.</p>
  </>;
}
'''
    return output
