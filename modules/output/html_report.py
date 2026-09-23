"""Self-contained HTML report generator for semantic extraction inventories.

Produces a single .html file with embedded CSS/JS (Snowflake Light Executive branding),
interactive tables (sort/filter), and collapsible sections.

Narrative-driven: tells a story rather than dumping numbers.
"""

import html
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from ..common.logger import get_logger

log = get_logger("html_report")

# ---------------------------------------------------------------------------
# Source-type display helpers
# ---------------------------------------------------------------------------

_SOURCE_LABELS = {
    "looker": "Looker",
    "lookml": "Looker (LookML)",
    "tableau": "Tableau",
    "powerbi": "Power BI",
    "power_bi": "Power BI",
    "denodo": "Denodo",
    "sap_bo": "SAP Business Objects",
    "merged": "Multi-Project",
    "dbt": "dbt",
}

# Source-specific term mappings — use these instead of generic dim/fact/metric
_SOURCE_TERMS: dict[str, dict[str, str]] = {
    "looker": {"dimension": "Dimension", "fact": "Measure", "metric": "Derived Metric",
               "dimensions": "Dimensions", "facts": "Measures", "metrics": "Derived Metrics"},
    "lookml": {"dimension": "Dimension", "fact": "Measure", "metric": "Derived Metric",
               "dimensions": "Dimensions", "facts": "Measures", "metrics": "Derived Metrics"},
    "tableau": {"dimension": "Dimension", "fact": "Measure", "metric": "Calculated Field",
                "dimensions": "Dimensions", "facts": "Measures", "metrics": "Calculated Fields"},
    "powerbi": {"dimension": "Column", "fact": "Measure", "metric": "DAX Measure",
                "dimensions": "Columns", "facts": "Measures", "metrics": "DAX Measures"},
    "power_bi": {"dimension": "Column", "fact": "Measure", "metric": "DAX Measure",
                 "dimensions": "Columns", "facts": "Measures", "metrics": "DAX Measures"},
}

_DEFAULT_TERMS = {"dimension": "Dimension", "fact": "Fact", "metric": "Metric",
                  "dimensions": "Dimensions", "facts": "Facts", "metrics": "Metrics"}


def _terms(source_type: str) -> dict[str, str]:
    base = source_type.lower().replace(" ", "_")
    return _SOURCE_TERMS.get(base, _DEFAULT_TERMS)


def _source_label(source_type: str) -> str:
    return _SOURCE_LABELS.get(source_type.lower().replace(" ", "_"), source_type.title())


def _errors_are_dashboard_only(errors: list) -> bool:
    """Return True if every parse error comes from a .dashboard.lookml file.
    These are expected failures — the parser doesn't extract semantic fields
    from dashboard definitions — and shouldn't be surfaced as a concern."""
    if not errors:
        return False
    for e in errors:
        fname = ""
        if isinstance(e, dict):
            fname = str(e.get("file", ""))
        else:
            fname = str(e)
        if ".dashboard.lookml" not in fname.lower():
            return False
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_html_report(
    inventory: dict,
    analysis: dict | None = None,
    output_path: str | None = None,
    source_path: str = "",
    customer_name: str | None = None,
    source_label: str | None = None,
    ai_recommendations: dict[int, str] | None = None,
    verified_queries: list[dict] | None = None,
) -> str:
    """Generate a self-contained HTML report from an inventory."""
    source_type = inventory.get("source_type", "unknown")
    tables = inventory.get("tables", [])
    dims = inventory.get("dimensions", [])
    facts = inventory.get("facts", [])
    metrics = inventory.get("metrics", [])
    flagged = inventory.get("flagged", [])
    errors = inventory.get("errors", [])
    rels = inventory.get("relationships", [])
    complexity = inventory.get("complexity_summary", {})

    total_items = len(dims) + len(facts) + len(metrics)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    terms = _terms(source_type)

    # Data type distribution
    dt_counter = Counter()
    for item in dims + facts + metrics:
        dt_counter[item.get("data_type", "UNKNOWN")] += 1

    # Items per table
    table_counts: dict[str, dict[str, int]] = {}
    for t in tables:
        table_counts[t.get("name", "?")] = {"dimensions": 0, "facts": 0, "metrics": 0}
    for kind, items in [("dimensions", dims), ("facts", facts), ("metrics", metrics)]:
        for item in items:
            tname = item.get("table", "")
            if tname not in table_counts:
                table_counts[tname] = {"dimensions": 0, "facts": 0, "metrics": 0}
            table_counts[tname][kind] += 1

    top_tables = sorted(
        table_counts.items(),
        key=lambda x: sum(x[1].values()),
        reverse=True,
    )[:20]

    # Analysis stats
    a_stats = (analysis or {}).get("stats", {})
    a_groups = (analysis or {}).get("parallel_definitions", [])
    a_reuse = (analysis or {}).get("reuse_stats", {})

    # Report intelligence narrative
    from .analysis import analyze_report_narrative
    narrative = analyze_report_narrative(inventory, analysis)
    has_report_intel = (
        narrative
        and narrative.get("active_vs_model", {}).get("total", 0) > 0
        and len(narrative.get("page_profiles", [])) > 0
    )

    parts = [
        _head(source_type, now),
        _section_header(source_type, source_path, now, tables,
                        customer_name=customer_name, source_label=source_label),
    ]

    if has_report_intel:
        # ── Three-section layout: Exec Summary → Inventory Overview → Items to Resolve
        parts += [
            _section_executive_summary(source_type, source_path, now, tables, dims, facts,
                                       metrics, flagged, errors, rels, complexity,
                                       total_items, a_stats, a_reuse, terms,
                                       narrative=narrative),
            _section_inventory_overview(inventory, narrative, tables, dims, facts,
                                         metrics, total_items, terms, dt_counter,
                                         a_reuse, top_tables),
            _section_items_to_resolve(inventory, narrative, a_groups, a_stats, terms,
                                       flagged, complexity, total_items),
        ]
    else:
        # ── Fallback: no report-level intelligence available
        parts += [
            _section_executive_summary(source_type, source_path, now, tables, dims, facts,
                                       metrics, flagged, errors, rels, complexity,
                                       total_items, a_stats, a_reuse, terms,
                                       narrative=narrative),
            _section_inventory(tables, dims, facts, metrics, rels, total_items, terms,
                               dt_counter, a_reuse),
            _section_what_needs_attention(a_groups, a_stats, terms, ai_recommendations or {}),
            _section_common_columns(a_groups, a_stats, terms),
            _section_reuse(a_reuse, terms),
            _section_complexity(complexity, total_items),
            _section_details(top_tables, dt_counter, flagged, errors, terms),
        ]

    if verified_queries:
        parts.append(_section_business_questions(verified_queries, terms))

    parts.append(_section_scope_notes(source_type))
    parts.append(_foot())

    html_str = "\n".join(parts)

    if output_path:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html_str)
        log.info("HTML report written to %s", output_path)

    return html_str


# ---------------------------------------------------------------------------
# CSS + JS
# ---------------------------------------------------------------------------

_CSS = """\
@import url('https://fonts.googleapis.com/css2?family=Lato:wght@300;400;700;900&family=JetBrains+Mono:wght@400;500&display=swap');
:root {
  --bg: #ffffff; --surface: #ffffff; --card: #f8fafc; --border: #e2e8f0; --border-light: #cbd5e1;
  --text: #262626; --text-secondary: #475569; --muted: #94a3b8; --dimmed: #cbd5e1;
  --accent: #29B5E8; --accent-dark: #0e84ae; --accent-subtle: #e0f4fc;
  --green: #16a34a; --green-bg: #dcfce7;
  --amber: #d97706; --amber-bg: #fef3c7;
  --red: #dc2626; --red-bg: #fee2e2;
  --purple: #7c3aed; --purple-bg: #ede9fe;
  --cyan: #0891b2;
  --page-bg: #f1f5f9;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { background:var(--page-bg); color:var(--text); font-family:'Lato','Helvetica Neue',Arial,sans-serif;
       font-size:14px; line-height:1.6; }
.report { max-width:960px; margin:32px auto; padding:0 40px 48px; background:var(--bg);
          border-radius:12px; box-shadow:0 4px 24px rgba(0,0,0,0.08); }

/* ── Header ── */
.report-header { background:linear-gradient(135deg, #29B5E8 0%, #0e84ae 100%);
  margin:0 -40px; padding:32px 40px 28px; border-radius:12px 12px 0 0; color:#fff; }
.brand-label { font-size:11px; font-weight:700; letter-spacing:2px; text-transform:uppercase;
  opacity:0.85; margin-bottom:12px; }
.report-title { font-size:28px; font-weight:900; color:#fff; margin-bottom:6px; }
.report-subtitle { font-size:14px; color:rgba(255,255,255,0.85); }
.report-subtitle strong { color:#fff; }
.report-source { font-size:12px; color:rgba(255,255,255,0.6); margin-top:4px; }

/* ── Sections ── */
.section { margin-bottom:28px; }
.section-head { font-size:12px; font-weight:700; color:var(--accent-dark); text-transform:uppercase; letter-spacing:1.2px;
                padding-bottom:8px; border-bottom:2px solid var(--accent); margin-bottom:14px;
                display:flex; align-items:center; gap:8px; }
.section-head .pipe { color:var(--accent); font-weight:700; }

/* ── Cards ── */
.card { background:var(--bg); border:1px solid var(--border); border-radius:8px;
        padding:16px; margin-bottom:12px; }
.narrative { font-size:14px; color:var(--text-secondary); line-height:1.7; margin-bottom:16px; }
.narrative strong { color:var(--text); font-weight:700; }
.narrative em { color:var(--accent-dark); font-style:normal; font-weight:700; }

/* ── KPI strip ── */
.kpi-strip { display:flex; gap:10px; margin-bottom:16px; flex-wrap:wrap; }
.kpi { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:12px 16px;
       flex:1; min-width:120px; border-top:3px solid var(--accent); }
.kpi-value { font-size:26px; font-weight:900; color:var(--accent-dark); font-family:'JetBrains Mono',monospace; }
.kpi-label { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.5px; margin-top:2px; }
.kpi.green { border-top-color:var(--green); }
.kpi.green .kpi-value { color:var(--green); }
.kpi.amber { border-top-color:var(--amber); }
.kpi.amber .kpi-value { color:var(--amber); }
.kpi.red { border-top-color:var(--red); }
.kpi.red .kpi-value { color:var(--red); }
.kpi.purple { border-top-color:var(--purple); }
.kpi.purple .kpi-value { color:var(--purple); }
.funnel-val { font-weight:700; font-family:'JetBrains Mono',monospace; color:var(--accent-dark); }

/* ── Gauge / battery ── */
.gauge { display:flex; height:8px; border-radius:4px; overflow:hidden; background:var(--border); margin:8px 0; }
.gauge-seg { transition:width 0.3s; }
.gauge-legend { display:flex; gap:16px; flex-wrap:wrap; margin-top:6px; }
.gauge-legend-item { display:flex; align-items:center; gap:6px; font-size:12px; color:var(--text-secondary); }
.gauge-legend-dot { width:8px; height:8px; border-radius:2px; }

/* ── Tables ── */
.tbl-wrap { overflow-x:auto; }
.tbl-scroll { max-height:520px; overflow-y:auto; overflow-x:hidden; border:1px solid var(--border);
  border-radius:6px; }
 .tbl-scroll table { border-collapse:separate; border-spacing:0; width:100%; }
.tbl-scroll thead th { position:sticky; top:0; z-index:2; background:var(--card); }
table { width:100%; border-collapse:collapse; font-size:13px; }
th { text-align:left; padding:8px 10px; background:var(--card); color:var(--accent-dark); font-weight:700;
     font-size:11px; text-transform:uppercase; letter-spacing:0.3px;
     border-bottom:2px solid var(--border); cursor:pointer; user-select:none; white-space:nowrap; }
th:hover { color:var(--text); }
th .sort-arrow { font-size:9px; margin-left:4px; opacity:0.4; }
th.sorted .sort-arrow { opacity:1; color:var(--accent); }
td { padding:6px 10px; border-bottom:1px solid var(--border); vertical-align:top; }
tr:hover td { background:var(--accent-subtle); }
.mono { font-family:'JetBrains Mono','Fira Code',monospace; font-size:12px; }
.truncate { max-width:400px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.wrap { white-space:normal; word-break:break-word; overflow-wrap:break-word; }
.wrap .mono { word-break:break-all; }
table .tag { padding:1px 6px; font-size:9px; white-space:nowrap; }
.muted-text { color:var(--muted); }
.ai-rec { font-size:12px; color:#c9d1d9; line-height:1.4; }
/* ── Wide section override ── */
.section-wide { margin-left:-40px; margin-right:-40px; padding:0 40px; }
@media (max-width:960px) { .section-wide { margin-left:0; margin-right:0; padding:0; } }

/* ── Tags ── */
.tag { display:inline-block; padding:2px 8px; border-radius:4px; font-size:10px;
       font-weight:700; text-transform:uppercase; letter-spacing:0.5px; }
.tag-dup { background:var(--amber-bg); color:var(--amber); }
.tag-par { background:var(--red-bg); color:var(--red); }
.tag-ovl { background:var(--purple-bg); color:var(--purple); }
.tag-calc { background:var(--accent-subtle); color:var(--accent-dark); }
.tag-triv { background:#f1f5f9; color:var(--muted); }
.tag-simple { background:var(--green-bg); color:var(--green); }
.tag-translate { background:var(--amber-bg); color:var(--amber); }
.tag-manual { background:var(--red-bg); color:var(--red); }

/* ── Bars ── */
.bar-row { display:flex; align-items:center; gap:8px; margin:3px 0; }
.bar-label { font-size:12px; color:var(--text-secondary); min-width:100px; }
.bar { height:18px; border-radius:3px; min-width:2px; transition:width 0.3s; }
.bar-value { font-size:12px; color:var(--text); min-width:50px; text-align:right; }

/* ── Collapsible ── */
.collapsible { border:1px solid var(--border); border-radius:8px; margin-bottom:12px; overflow:hidden; }
.collapsible-toggle { width:100%; background:var(--card); border:none; color:var(--text);
  font-family:'Lato',sans-serif; font-size:14px; font-weight:700; padding:12px 16px;
  text-align:left; cursor:pointer; display:flex; align-items:center; justify-content:space-between; }
.collapsible-toggle:hover { background:#eef2f7; }
.collapsible-toggle .chevron { font-size:12px; color:var(--muted); transition:transform 0.2s; }
.collapsible-toggle[aria-expanded="true"] .chevron { transform:rotate(180deg); }
.collapsible-body { display:none; padding:16px; border-top:1px solid var(--border); }
.collapsible-body.open { display:block; }

/* ── Filter chips ── */
.filter-bar { display:flex; gap:6px; margin-bottom:12px; flex-wrap:wrap; align-items:center; }
.filter-chip { font-size:11px; padding:4px 12px; border-radius:16px; border:1px solid var(--border);
  background:transparent; color:var(--text-secondary); cursor:pointer; transition:all 0.15s; }
.filter-chip:hover, .filter-chip.active { background:var(--accent); color:#fff; border-color:var(--accent); }
.filter-count { font-family:'JetBrains Mono',monospace; font-size:10px; margin-left:3px; opacity:0.7; }
.search-box { background:var(--bg); border:1px solid var(--border); border-radius:6px;
  padding:5px 10px; font-size:12px; color:var(--text); font-family:'Lato',sans-serif; width:200px; }
.search-box::placeholder { color:var(--muted); }
.search-box:focus { outline:none; border-color:var(--accent); box-shadow:0 0 0 3px rgba(41,181,232,0.15); }

/* ── Category headers ── */
.cat-label { font-size:12px; font-weight:700; letter-spacing:0.5px; margin-bottom:2px; color:var(--text); }
.cat-desc { font-size:12px; color:var(--text-secondary); line-height:1.5; margin-bottom:12px; }

/* ── Empty state ── */
.empty { color:var(--muted); font-style:italic; padding:16px; text-align:center; }

/* ── Footer ── */
.report-footer { margin-top:40px; padding:16px 0; border-top:1px solid var(--border);
  color:var(--muted); font-size:11px; text-align:center; }
.report-footer strong { color:var(--text-secondary); }

/* ── Print ── */
@media print {
  body { background:#fff !important; -webkit-print-color-adjust:exact; print-color-adjust:exact; }
  .report { max-width:100%; margin:0; box-shadow:none; border-radius:0; padding:0 20px 20px; }
  .report-header { margin:0 -20px; border-radius:0; }
  .card, .collapsible, .kpi { break-inside:avoid; }
  .collapsible-body { display:block !important; }
  .filter-bar, .search-box { display:none; }
  .section-wide { margin-left:0; margin-right:0; padding:0; }
  .page-break { break-before:page; }
}
"""

_JS = """\
// Sortable tables
document.addEventListener('click', e => {
  const th = e.target.closest('th[data-col]');
  if (!th) return;
  const table = th.closest('table');
  const tbody = table.querySelector('tbody');
  if (!tbody) return;
  const col = parseInt(th.dataset.col);
  const rows = Array.from(tbody.querySelectorAll('tr'));
  const isNum = rows.every(r => {
    const c = r.children[col]; return c && (/^[\\d,\\.\\-]+$/.test(c.textContent.trim()) || c.textContent.trim() === '');
  });
  const asc = th.classList.contains('sorted-asc');
  table.querySelectorAll('th').forEach(h => h.classList.remove('sorted','sorted-asc','sorted-desc'));
  th.classList.add('sorted', asc ? 'sorted-desc' : 'sorted-asc');
  rows.sort((a, b) => {
    let va = a.children[col]?.textContent.trim() || '';
    let vb = b.children[col]?.textContent.trim() || '';
    if (isNum) { va = parseFloat(va.replace(/,/g,'')) || 0; vb = parseFloat(vb.replace(/,/g,'')) || 0; }
    else { va = va.toLowerCase(); vb = vb.toLowerCase(); }
    return (asc ? -1 : 1) * (va < vb ? -1 : va > vb ? 1 : 0);
  });
  rows.forEach(r => tbody.appendChild(r));
});

// Collapsible sections
document.querySelectorAll('.collapsible-toggle').forEach(btn => {
  btn.addEventListener('click', () => {
    const expanded = btn.getAttribute('aria-expanded') === 'true';
    btn.setAttribute('aria-expanded', !expanded);
    btn.nextElementSibling.classList.toggle('open', !expanded);
  });
});

// Filter chips
document.querySelectorAll('[data-filter-group]').forEach(group => {
  const chips = group.querySelectorAll('.filter-chip');
  const tableId = group.dataset.filterGroup;
  const table = document.getElementById(tableId);
  if (!table) return;
  const tbody = table.querySelector('tbody');
  chips.forEach(chip => {
    chip.addEventListener('click', () => {
      const isActive = chip.classList.contains('active');
      chips.forEach(c => c.classList.remove('active'));
      if (!isActive) chip.classList.add('active');
      const filter = isActive ? '' : chip.dataset.filter;
      Array.from(tbody.querySelectorAll('tr')).forEach(row => {
        if (!filter) { row.style.display = ''; return; }
        const cat = row.dataset.category || '';
        row.style.display = cat === filter ? '' : 'none';
      });
    });
  });
});

// Search boxes
document.querySelectorAll('.search-box').forEach(input => {
  const tableId = input.dataset.searchTarget;
  const table = document.getElementById(tableId);
  if (!table) return;
  const tbody = table.querySelector('tbody');
  input.addEventListener('input', () => {
    const q = input.value.toLowerCase();
    Array.from(tbody.querySelectorAll('tr')).forEach(row => {
      row.style.display = row.textContent.toLowerCase().includes(q) ? '' : 'none';
    });
  });
});
"""


# ---------------------------------------------------------------------------
# HTML building blocks
# ---------------------------------------------------------------------------

def _head(source_type: str, now: str) -> str:
    label = _source_label(source_type)
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Lato:wght@300;400;700;900&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<title>Semantic Extraction Report — {_esc(label)}</title>
<style>{_CSS}</style>
</head><body><div class="report">
"""


def _section_business_questions(questions: list[dict], terms: dict) -> str:
    """Render the Verified Queries / Business Questions section."""
    if not questions:
        return ""

    # Group questions by dashboard/page
    from collections import defaultdict
    grouped: dict[str, list[dict]] = defaultdict(list)
    for q in questions:
        page = q.get("page_name", "Unknown")
        dash = q.get("dashboard_name", "")
        key = f"{dash} — {page}" if dash else page
        grouped[key].append(q)

    rows = []
    for group_key in sorted(grouped):
        gqs = grouped[group_key]
        rows.append(f'<tr class="group-header"><td colspan="3" style="font-weight:700;'
                    f'background:var(--accent-subtle);padding:8px 12px;font-size:13px">'
                    f'{_esc(group_key)}</td></tr>')
        for q in gqs:
            onboarding = ' <span class="badge badge-green" style="font-size:10px">onboarding</span>' if q.get("use_as_onboarding_question") else ""
            sql_escaped = _esc(q.get("sql", ""))
            rows.append(
                f'<tr>'
                f'<td style="padding:6px 12px;font-size:12px;vertical-align:top;width:200px">'
                f'<code>{_esc(q.get("name", ""))}</code></td>'
                f'<td style="padding:6px 12px;font-size:13px;vertical-align:top">'
                f'{_esc(q.get("question", ""))}{onboarding}</td>'
                f'<td style="padding:6px 12px;vertical-align:top">'
                f'<details><summary style="font-size:11px;cursor:pointer;color:var(--accent-dark)">Show SQL</summary>'
                f'<pre style="font-size:11px;margin:6px 0 0 0;padding:8px;background:var(--card);'
                f'border-radius:4px;overflow-x:auto;white-space:pre-wrap">{sql_escaped}</pre>'
                f'</details></td>'
                f'</tr>'
            )

    return f"""
<div class="section section-wide">
  <div class="section-head"><span class="pipe">|</span> Verified Queries</div>
  <p style="font-size:13px;color:var(--text-secondary);margin:0 0 12px 0">
    Business questions generated from {terms.get('source', 'source')} metadata — ready for
    <code>verified_queries</code> in Snowflake Semantic View YAML. {len(questions)} questions
    across {len(grouped)} pages.
  </p>
  <table class="data-table" style="width:100%">
    <thead>
      <tr style="background:var(--card)">
        <th style="padding:8px 12px;text-align:left;font-size:12px">Name</th>
        <th style="padding:8px 12px;text-align:left;font-size:12px">Question</th>
        <th style="padding:8px 12px;text-align:left;font-size:12px;width:120px">SQL</th>
      </tr>
    </thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>
</div>"""


# ---------------------------------------------------------------------------
# Section: Scope notes
# ---------------------------------------------------------------------------

def _section_scope_notes(source_type: str) -> str:
    """Callout box documenting what the extraction captures vs. what it cannot."""
    is_pbi = source_type in ("powerbi", "power_bi")

    # Build source-specific behavioral items
    behavioral_items = ""
    if is_pbi:
        behavioral_items = """
      <li><strong>Grain-switching &amp; drill-down</strong> — Slicers, drill-down
        hierarchies, and parameter tables that let users toggle between day/week/month
        views are <em>not</em> captured as interactive behaviors. The underlying fields
        (e.g.&nbsp;<code>Date[Month]</code>, <code>Date[Year]</code>) appear in the
        inventory, and DAX measures using <code>SELECTEDVALUE</code>/<code>SWITCH</code>
        patterns are flagged for translation — but the fact that a slicer <em>controls</em>
        another visual's grain is a runtime behavior that lives outside the semantic model.</li>
      <li><strong>Visual-level formatting &amp; conditional rules</strong> — Color rules,
        data bars, and conditional formatting are presentation-layer behaviors not
        represented in the semantic model definition.</li>
      <li><strong>Row-level security (RLS)</strong> — RLS roles defined in the .pbix
        model are not extracted. These must be re-implemented as Snowflake row access
        policies or Semantic View filters.</li>"""
    else:
        behavioral_items = """
      <li><strong>Grain-switching &amp; drill-down</strong> — If dashboards allow users
        to toggle between time grains (day/week/month) or drill into hierarchies, the
        underlying fields are captured in the inventory but the interactive behavior
        itself is not. Re-implement grain controls in your application layer or
        Cortex Analyst parameters.</li>
      <li><strong>Conditional formatting &amp; presentation logic</strong> — Visual
        formatting rules are not part of the semantic model and are not extracted.</li>"""

    return f"""
<div class="section section-wide" style="margin-top:0">
  <div class="section-head"><span class="pipe">|</span> Extraction Scope Notes</div>
  <div class="card" style="padding:16px;border-left:3px solid var(--accent);background:var(--card)">
    <p style="font-size:13px;margin:0 0 10px 0;color:var(--text)">
      <strong>What this extraction captures:</strong> All semantic model definitions —
      tables, columns, measures, relationships, calculated fields, and their expressions —
      plus which dashboard pages and visuals reference each field. This is the
      <em>structural</em> inventory of your semantic layer.
    </p>
    <p style="font-size:13px;margin:0 0 10px 0;color:var(--text)">
      <strong>What it does not capture:</strong> Runtime and interactive behaviors that
      exist outside the model definition:
    </p>
    <ul style="font-size:12px;color:var(--text-secondary);margin:0 0 10px 16px;line-height:1.7">
{behavioral_items}
    </ul>
    <p style="font-size:12px;margin:0;color:var(--muted);font-style:italic">
      Fields involved in these behaviors are present in the inventory and flagged
      appropriately on the DAX Translation tab. The behavioral semantics should be
      re-implemented in the consuming application or Cortex Analyst configuration.
    </p>
  </div>
</div>"""


def _foot() -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""
<div class="report-footer">
  Generated by <strong>Snowflake Semantic Extraction Utility</strong> &middot; {now}
</div>
</div>
<script>{_JS}</script>
</body></html>"""


# ---------------------------------------------------------------------------
# Section: Header
# ---------------------------------------------------------------------------

def _section_header(source_type: str, source_path: str, now: str,
                    tables: list | None = None,
                    customer_name: str | None = None,
                    source_label: str | None = None) -> str:
    label = _source_label(source_type)

    # Subtitle line 1: "Prepared for Customer - Date" or fallback
    if customer_name:
        # Format date nicely from now (e.g. "2026-04-03 19:32 UTC" → "April 3, 2026")
        try:
            dt = datetime.strptime(now.split(" ")[0], "%Y-%m-%d")
            date_str = dt.strftime("%B %-d, %Y")
        except (ValueError, IndexError):
            date_str = now
        subtitle = f"Prepared for <strong>{_esc(customer_name)}</strong> &mdash; {date_str}"
    else:
        src_line = ""
        if source_path:
            src_line = f" &mdash; <strong>{_esc(source_path)}</strong>"
        subtitle = f"{_esc(label)} source{src_line} &middot; {now}"

    # Subtitle line 2: source description
    source_line = ""
    if source_label:
        source_line = f'<div class="report-source">{_esc(source_label)}</div>'
    elif tables is not None and len(tables) > 0:
        source_line = (
            f'<div class="report-source">{_esc(label)} &middot; '
            f'{len(tables):,} table{"s" if len(tables) != 1 else ""}</div>'
        )

    return f"""
<div class="report-header">
  <div class="brand-label">Snowflake Professional Services</div>
  <div class="report-title">Semantic Extraction Report</div>
  <div class="report-subtitle">{subtitle}</div>
  {source_line}
</div>"""


# ---------------------------------------------------------------------------
# Section: Executive Summary (plain English narrative)
# ---------------------------------------------------------------------------

def _section_executive_summary(source_type, source_path, now, tables, dims, facts,
                                metrics, flagged, errors, rels, complexity,
                                total_items, a_stats, a_reuse, terms,
                                narrative=None) -> str:
    label = _source_label(source_type)
    total_raw = a_reuse.get("total_raw", 0) if a_reuse else 0
    distinct_names = a_reuse.get("distinct_field_names", 0) if a_reuse else 0
    calculated_defs = a_reuse.get("calculated_definitions", 0) if a_reuse else 0
    inconsistent = a_reuse.get("inconsistent_names", 0) if a_reuse else 0
    unique = a_reuse.get("total_unique", total_items) if a_reuse else total_items

    actionable = a_stats.get("actionable_groups", 0)
    total_groups = a_stats.get("total_groups", 0)
    cross_kind = a_stats.get("cross_kind_overlaps", 0)
    calc_dups = a_stats.get("calculated_duplicates", 0)
    calc_pars = a_stats.get("calculated_parallel_defs", 0)

    c_simple = complexity.get("simple", 0)
    c_translate = complexity.get("needs_translation", 0)
    c_manual = complexity.get("manual_required", 0)
    c_total = c_simple + c_translate + c_manual

    suppress_errors = _errors_are_dashboard_only(errors)

    # ── Determine if we have report-level intelligence ──
    has_report_intel = (
        narrative
        and narrative.get("active_vs_model", {}).get("total", 0) > 0
        and len(narrative.get("page_profiles", [])) > 0
    )

    paras = []
    kpis = []

    if has_report_intel:
        # ════════════════════════════════════════════════════════════════
        # SEMANTIC MODEL narrative — lead with the story
        # ════════════════════════════════════════════════════════════════
        avm = narrative["active_vs_model"]
        pages = narrative["page_profiles"]
        roadmap = narrative["semantic_model_recommendation"]
        conflicts = narrative["conflict_coverage"]
        reuse = narrative["reuse_distribution"]
        pg = narrative.get("page_label", "pages")  # source-aware: "dashboard pages", "explores", etc.
        pg_s = pg.rstrip("s") if pg.endswith("s") else pg  # singular form
        p1 = roadmap.get("priority_1", {})
        p2 = roadmap.get("priority_2", {})
        p3 = roadmap.get("priority_3", {})

        # ── Semantic model readiness scoring ──
        # Two dimensions: core grain density + active conflict exposure (ratio-based)
        active_pct = avm["active_pct"]
        core_ct = reuse.get("core_field_count", 0)
        active_conflicts = conflicts.get("active_conflicts", 0)
        total_conflicts = conflicts.get("total", 0)
        mo_conflicts = conflicts.get("model_only_conflicts", 0)
        conflict_ratio = conflicts.get("active_conflict_ratio", 0)
        p1_blocked = roadmap.get("priority_1_blocked", {})
        p1_blocked_ct = p1_blocked.get("count", 0)

        # 4-tier scoring
        if core_ct >= 5 and conflict_ratio < 1:
            readiness_color = "var(--green)"
            readiness_label = "High"
        elif core_ct >= 1 and conflict_ratio < 5:
            readiness_color = "var(--green)"
            readiness_label = "Good"
        elif conflict_ratio <= 15 or (core_ct < 5 and active_conflicts > 0):
            readiness_color = "var(--amber)"
            readiness_label = "Moderate"
        else:
            readiness_color = "var(--red)"
            readiness_label = "Low"

        # ── Lead paragraph: concrete gap statement ──
        p1_ct = p1.get("count", 0)
        p2_ct = p2.get("count", 0)
        p3_ct = p3.get("count", 0)

        if active_conflicts == 0 and p1_ct > 0:
            gap_stmt = (
                f"<strong>{p1_ct} conflict-free core fields</strong> are ready to include now. "
                f"{p2_ct} more page-specific fields can be added based on business need."
            )
        elif active_conflicts > 0 and p1_ct > 0:
            gap_stmt = (
                f"<strong>{p1_ct} fields are ready now.</strong> "
                f"Resolve <strong>{active_conflicts} conflicts</strong> "
                f"to unlock {p1_blocked_ct + p2_ct} more."
            )
        elif active_conflicts > 0:
            gap_stmt = (
                f"<strong>{active_conflicts} conflicts</strong> in active report fields "
                f"must be resolved before building. "
                f"No conflict-free core fields exist yet."
            )
        else:
            gap_stmt = (
                f"Fields are spread thinly across pages — no strong core grain. "
                f"Start with the most-reused fields and build incrementally."
            )

        paras.append(
            f"<strong style='color:{readiness_color}'>Semantic Model Readiness: "
            f"{readiness_label}.</strong> {gap_stmt}"
        )

        # ── Roadmap summary: the action ──
        roadmap_parts = []
        if p1_ct > 0:
            roadmap_parts.append(
                f"<strong style='color:var(--green)'>P1:</strong> {p1_ct:,} core fields "
                f"— include immediately"
            )
        if p1_blocked_ct > 0:
            roadmap_parts.append(
                f"<strong style='color:var(--amber)'>Blocked:</strong> {p1_blocked_ct:,} core fields "
                f"held back by conflicts"
            )
        if p2_ct > 0:
            roadmap_parts.append(
                f"<strong style='color:var(--accent)'>P2:</strong> {p2_ct:,} page-specific "
                f"fields — include based on need"
            )
        if p3_ct > 0:
            roadmap_parts.append(
                f"<strong style='color:var(--muted)'>P3:</strong> {p3_ct:,} model-only "
                f"— triage by table"
            )
        if roadmap_parts:
            paras.append(
                "<strong>Recommended path:</strong> " + " &rarr; ".join(roadmap_parts) + "."
            )

        # ── DAX / translation callout (one line) ──
        dax_parts = []
        # Count flagged items by action category
        dax_keep = sum(1 for f in flagged if "keep in dax" in f.get("reason", "").lower())
        dax_translate = sum(1 for f in flagged if f.get("reason", "").lower().startswith("translate"))
        dax_rewrite = sum(1 for f in flagged if f.get("reason", "").lower().startswith("rewrite"))
        dax_manual = sum(1 for f in flagged if "manual review" in f.get("reason", "").lower())
        dax_total = len(flagged)
        if dax_total > 0:
            if dax_keep > 0:
                dax_parts.append(f"{dax_keep} keep-in-DAX")
            if dax_translate > 0:
                dax_parts.append(f"{dax_translate} translatable")
            if dax_rewrite + dax_manual > 0:
                dax_parts.append(f"{dax_rewrite + dax_manual} need manual work")
            paras.append(
                f"<strong>DAX remapping:</strong> {dax_total} measures flagged — "
                + ", ".join(dax_parts) + ". See <em>Items to Resolve</em> below."
            )

        # ── Dashboard overlap callout (1-2 lines) ──
        consol = narrative.get("consolidation_opportunities", [])
        dash_cov = narrative.get("dashboard_coverage", {})
        dash_stats = dash_cov.get("stats", {})
        if consol:
            # Highlight the highest-overlap pair
            top = max(consol, key=lambda c: c["jaccard"])
            pct = round(top["jaccard"] * 100)
            paras.append(
                f"<strong>Page overlap:</strong> "
                f"<em>{_esc(top['page_a'])}</em> and <em>{_esc(top['page_b'])}</em> "
                f"share {pct}% of their fields — likely candidates for a shared "
                f"semantic model. "
                f"{'%d other pairs also overlap significantly. ' % (len(consol) - 1) if len(consol) > 1 else ''}"
                f"See the <em>Dashboard Inventory &amp; Overlap</em> tab in the "
                f"Excel workbook for details."
            )
        elif dash_stats.get("total_dashboards", 0) > 1:
            avg_j = dash_stats.get("avg_jaccard", 0)
            if avg_j > 0:
                paras.append(
                    f"<strong>Page overlap:</strong> Average field similarity across "
                    f"{dash_stats['total_dashboards']} pages is {round(avg_j * 100)}% "
                    f"— pages are largely distinct."
                )

        # ── KPI strip: semantic model story ──
        kpis.append(f'<div class="kpi"><div class="kpi-value">{len(pages)}</div><div class="kpi-label">{_esc(pg.title())}</div></div>')
        kpis.append(
            f'<div class="kpi" style="border-left:3px solid var(--green)">'
            f'<div class="kpi-value" style="color:var(--green)">{avm["active_count"]:,}</div>'
            f'<div class="kpi-label">Active Fields ({avm["active_pct"]}%)</div></div>'
        )
        if p1_ct > 0:
            kpis.append(
                f'<div class="kpi" style="border-left:3px solid var(--accent)">'
                f'<div class="kpi-value" style="color:var(--accent)">{p1_ct}</div>'
                f'<div class="kpi-label">Ready Now (P1)</div></div>'
            )
        if active_conflicts > 0:
            kpis.append(
                f'<div class="kpi" style="border-left:3px solid var(--amber)">'
                f'<div class="kpi-value" style="color:var(--amber)">{active_conflicts}</div>'
                f'<div class="kpi-label">Conflicts (active)</div></div>'
            )
        if dax_total > 0:
            kpis.append(
                f'<div class="kpi" style="border-left:3px solid var(--purple)">'
                f'<div class="kpi-value" style="color:var(--purple)">{dax_total}</div>'
                f'<div class="kpi-label">DAX Remapping</div></div>'
            )

        funnel_html = ""

        # ── Scoped readiness breakdown (per-dashboard or per-page) ──
        scoped_readiness = narrative.get("scoped_readiness", [])
        scoped_readiness_html = ""
        if scoped_readiness and len(scoped_readiness) > 1:
            scope_type = scoped_readiness[0].get("scope_type", "page")
            scope_label = "Dashboard" if scope_type == "dashboard" else pg_s.title()
            sr_rows = ""
            for sr in scoped_readiness:
                sr_rows += (
                    f"<tr>"
                    f"<td>{_esc(sr['scope_name'])}</td>"
                    f"<td style='text-align:center'>{sr['field_count']:,}</td>"
                    f"<td style='text-align:center'>{sr['core_field_count']}</td>"
                    f"<td style='text-align:center'>{sr['conflict_count']}</td>"
                    f"<td style='text-align:center'>"
                    f"<strong style='color:{sr['readiness_color']}'>{sr['readiness_label']}</strong></td>"
                    f"</tr>\n"
                )
            scope_intro = (
                f"Readiness assessed per {scope_label.lower()} — "
                f"{'each project is evaluated independently' if scope_type == 'dashboard' else 'each page is evaluated independently'}."
            )
            scoped_readiness_html = (
                f"<div style='margin-top:16px'>"
                f"<p class='narrative' style='font-size:12px;margin-bottom:8px'>"
                f"<strong>Readiness by {scope_label}:</strong> {scope_intro}</p>"
                f"<div class='card'><div class='tbl-scroll' style='max-height:320px'>"
                f"<table style='font-size:12px'>"
                f"<thead><tr>"
                f"<th>{scope_label}</th>"
                f"<th style='text-align:center'>Fields</th>"
                f"<th style='text-align:center'>Core Fields</th>"
                f"<th style='text-align:center'>Conflicts</th>"
                f"<th style='text-align:center'>Readiness</th>"
                f"</tr></thead>"
                f"<tbody>{sr_rows}</tbody>"
                f"</table>"
                f"</div></div></div>"
            )
        funnel_html = scoped_readiness_html + funnel_html

    else:
        # ════════════════════════════════════════════════════════════════
        # FALLBACK: Migration readiness (no report pages available)
        # ════════════════════════════════════════════════════════════════
        simple_pct = (c_simple / c_total * 100) if c_total else 100
        actionable_pct = (actionable / distinct_names * 100) if distinct_names else 0
        if simple_pct >= 80 and actionable_pct < 3 and c_manual == 0:
            readiness_color = "var(--green)"
            readiness_label = "High"
        elif c_manual > 0 or actionable_pct >= 10 or simple_pct < 50:
            readiness_color = "var(--amber)"
            readiness_label = "Moderate"
        else:
            readiness_color = "var(--green)"
            readiness_label = "Good"

        paras.append(
            f"<strong style='color:{readiness_color}'>Migration Readiness: {readiness_label}.</strong> "
            f"This {_esc(label)} environment has {total_items:,} field definitions across "
            f"{len(tables):,} tables."
        )

        if calc_pars > 0:
            paras.append(
                f"<strong>Key risk:</strong> {calc_pars:,} parallel definition{'s' if calc_pars != 1 else ''} "
                f"— the same concept defined with different logic. Resolve before migration."
            )
        if calc_dups > 0:
            paras.append(
                f"<strong>Quick win:</strong> {calc_dups:,} duplicated calculation{'s' if calc_dups != 1 else ''} "
                f"can be collapsed — identical logic, just needs dedup."
            )
        if c_translate > 0 or c_manual > 0:
            parts_list = []
            if c_translate > 0:
                parts_list.append(f"{c_translate:,} need SQL translation")
            if c_manual > 0:
                parts_list.append(f"{c_manual:,} require manual review")
            paras.append(
                f"<strong>Translation effort:</strong> {' and '.join(parts_list)}. "
                f"The remaining {c_simple:,} definitions are simple column references."
            )
        elif c_total > 0:
            paras.append("All definitions are simple column references — no complex translation needed.")

        if cross_kind > 0:
            paras.append(
                f"<em style='color:var(--muted);font-style:normal'>"
                f"{cross_kind:,} semantic overlap{'s' if cross_kind != 1 else ''} "
                f"(dimension-vs-measure reuse) — standard {_esc(label)} pattern, no action needed.</em>"
            )

        # Fallback KPIs
        kpis.append(f'<div class="kpi"><div class="kpi-value">{len(tables):,}</div><div class="kpi-label">Tables</div></div>')
        if distinct_names:
            kpis.append(f'<div class="kpi"><div class="kpi-value">{distinct_names:,}</div><div class="kpi-label">Field Names</div></div>')
        if calculated_defs:
            kpis.append(f'<div class="kpi purple"><div class="kpi-value">{calculated_defs:,}</div><div class="kpi-label">Calculated Definitions</div></div>')
        if inconsistent > 0:
            kpis.append(f'<div class="kpi amber"><div class="kpi-value">{inconsistent:,}</div><div class="kpi-label">Inconsistent Names</div></div>')
        if actionable > 0:
            kpis.append(f'<div class="kpi red"><div class="kpi-value">{actionable:,}</div><div class="kpi-label">Need Attention</div></div>')
        if rels:
            kpis.append(f'<div class="kpi"><div class="kpi-value">{len(rels):,}</div><div class="kpi-label">Relationships</div></div>')
        if errors and not suppress_errors:
            kpis.append(f'<div class="kpi red"><div class="kpi-value">{len(errors)}</div><div class="kpi-label">Parse Errors</div></div>')

        # Fallback funnel
        funnel_html = ""
        if total_raw and distinct_names:
            reuse_pct = a_reuse.get("reuse_pct", 0) if a_reuse else 0
            simple_defs = unique - calculated_defs if unique > calculated_defs else 0
            inconsistent_note = ""
            if inconsistent > 0 and calc_pars > 0:
                inconsistent_note = (
                    f' Of those, <strong>{calc_pars:,}</strong> involve calculated business logic '
                    f'and appear in <em>What Needs Attention</em> below.'
                )
            funnel_steps = []
            funnel_steps.append(
                f'<span class="funnel-val">{total_raw:,}</span> total field references'
                f' <span class="muted-text">&rarr;</span> '
                f'<span class="funnel-val">{unique:,}</span> unique definitions'
                f' <span class="muted-text">({reuse_pct:.0f}% healthy reuse removed)</span>'
            )
            funnel_steps.append(
                f'<span class="funnel-val">{unique:,}</span> unique definitions cover '
                f'<span class="funnel-val">{distinct_names:,}</span> distinct field names'
            )
            funnel_steps.append(
                f'<span class="funnel-val">{calculated_defs:,}</span> contain calculated business logic'
                f' <span class="muted-text">&middot;</span> '
                f'<span class="funnel-val">{simple_defs:,}</span> are simple column references'
            )
            if inconsistent > 0:
                funnel_steps.append(
                    f'<span class="funnel-val" style="color:var(--amber)">{inconsistent:,}</span>'
                    f' field names have inconsistent definitions.' + inconsistent_note
                )
            if actionable > 0:
                calc_overlaps = actionable - calc_dups - calc_pars
                bp = []
                if calc_dups:
                    bp.append(f'{calc_dups:,} duplicates')
                if calc_pars:
                    bp.append(f'{calc_pars:,} parallel defs')
                if calc_overlaps > 0:
                    bp.append(f'{calc_overlaps:,} overlaps')
                funnel_steps.append(
                    f'<span class="funnel-val" style="color:var(--red)">{actionable:,}</span>'
                    f' groups need attention'
                    f' <span class="muted-text">({" + ".join(bp)})</span>'
                )
            funnel_html = (
                '<div class="funnel" style="margin-top:16px;padding:12px 16px;'
                'border-left:3px solid var(--accent);background:var(--card-bg);border-radius:4px;'
                'font-size:13px;line-height:2">'
                + '<br>'.join(f'<span style="color:var(--muted);margin-right:4px">{i+1}.</span> {s}'
                              for i, s in enumerate(funnel_steps))
                + '</div>'
            )

    narr_html = "</p><p class='narrative'>".join(paras)

    return f"""
<div class="section">
  <div class="section-head"><span class="pipe">|</span> Executive Summary</div>
  <p class="narrative">{narr_html}</p>
  <div class="kpi-strip">{"".join(kpis)}</div>
  {funnel_html}
</div>"""


# ---------------------------------------------------------------------------
# Section: Inventory at a Glance
# ---------------------------------------------------------------------------

def _section_inventory(tables, dims, facts, metrics, rels, total_items, terms,
                       dt_counter, a_reuse) -> str:
    # Battery / gauge chart showing composition
    seg_data = []
    if dims:
        seg_data.append(("var(--accent)", len(dims), terms["dimensions"]))
    if facts:
        seg_data.append(("var(--green)", len(facts), terms["facts"]))
    if metrics:
        seg_data.append(("var(--amber)", len(metrics), terms["metrics"]))

    gauge_segs = ""
    gauge_legend = ""
    if total_items > 0:
        for color, count, label in seg_data:
            pct = count / total_items * 100
            gauge_segs += f'<div class="gauge-seg" style="width:{pct:.1f}%;background:{color}"></div>'
            gauge_legend += (
                f'<div class="gauge-legend-item">'
                f'<div class="gauge-legend-dot" style="background:{color}"></div>'
                f'{label}: {count:,} ({pct:.0f}%)</div>'
            )

    # Top data types (compact)
    top_dt = dt_counter.most_common(6)
    dt_tags = ""
    if top_dt:
        dt_tags = '<div style="margin-top:12px;display:flex;gap:6px;flex-wrap:wrap">'
        for dt, cnt in top_dt:
            dt_tags += f'<span class="tag" style="background:var(--border);color:var(--text-secondary)">{_esc(dt)} <span class="mono" style="color:var(--muted)">{cnt:,}</span></span>'
        if len(dt_counter) > 6:
            dt_tags += f'<span class="tag" style="background:var(--border);color:var(--muted)">+{len(dt_counter)-6} more</span>'
        dt_tags += '</div>'

    return f"""
<div class="section">
  <div class="section-head"><span class="pipe">|</span> Inventory at a Glance</div>
  <div class="card">
    <div class="gauge">{gauge_segs}</div>
    <div class="gauge-legend">{gauge_legend}</div>
    {dt_tags}
  </div>
</div>"""


# ---------------------------------------------------------------------------
# Section: What Needs Attention (actionable groups only)
# ---------------------------------------------------------------------------

_CATEGORY_DEFS = {
    "duplicate": {
        "label": "Duplicated Definitions",
        "desc": "Same name <strong>and</strong> same calculation appearing in multiple places. "
                "These should be consolidated into a single source of truth.",
        "tag_cls": "tag-dup",
        "tag_label": "Duplicate",
    },
    "parallel_definition": {
        "label": "Parallel Definitions",
        "desc": "Similar name but <strong>different</strong> calculation logic. "
                "These represent inconsistent definitions of what appears to be the same concept — "
                "a common source of reporting discrepancies.",
        "tag_cls": "tag-par",
        "tag_label": "Parallel Def",
    },
    "semantic_overlap": {
        "label": "Semantic Overlaps",
        "desc": "Different names but <strong>identical</strong> calculation. "
                "These are the same logic under different aliases — candidates for deduplication.",
        "tag_cls": "tag-ovl",
        "tag_label": "Overlap",
    },
}


def _section_what_needs_attention(groups: list, stats: dict, terms: dict,
                                  ai_recommendations: dict[int, str] | None = None) -> str:
    actionable = [g for g in groups
                  if g.get("has_calculation") and not g.get("cross_kind")]
    if not actionable:
        return ""

    ai_recs = ai_recommendations or {}
    has_ai = bool(ai_recs)

    calc_dups = stats.get("calculated_duplicates", 0)
    calc_pars = stats.get("calculated_parallel_defs", 0)
    total_actionable = stats.get("actionable_groups", 0)

    # Category definitions
    cat_defs_html = ""
    for cat_key, cat_info in _CATEGORY_DEFS.items():
        count = calc_dups if cat_key == "duplicate" else calc_pars if cat_key == "parallel_definition" else 0
        # Count semantic overlaps with calculation
        if cat_key == "semantic_overlap":
            count = sum(1 for g in actionable if g["category"] == "semantic_overlap")
        if count == 0:
            continue
        cat_defs_html += (
            f'<div style="margin-bottom:10px">'
            f'<div class="cat-label"><span class="tag {cat_info["tag_cls"]}">{cat_info["tag_label"]}</span> '
            f'&mdash; {count:,} group{"s" if count != 1 else ""}</div>'
            f'<div class="cat-desc">{cat_info["desc"]}</div></div>'
        )

    # Filter chips
    filter_chips = '<div class="filter-bar" data-filter-group="tbl-actionable">'
    filter_chips += '<span style="font-size:11px;color:var(--muted);margin-right:4px">Filter:</span>'
    if calc_dups:
        filter_chips += f'<button class="filter-chip" data-filter="duplicate">Duplicates<span class="filter-count">{calc_dups}</span></button>'
    if calc_pars:
        filter_chips += f'<button class="filter-chip" data-filter="parallel_definition">Parallel Defs<span class="filter-count">{calc_pars}</span></button>'
    so_count = sum(1 for g in actionable if g["category"] == "semantic_overlap")
    if so_count:
        filter_chips += f'<button class="filter-chip" data-filter="semantic_overlap">Overlaps<span class="filter-count">{so_count}</span></button>'
    filter_chips += f'<input class="search-box" placeholder="Search definitions…" data-search-target="tbl-actionable">'
    filter_chips += '</div>'

    # Table
    rows = ""
    for g in actionable[:100]:
        cat_info = _CATEGORY_DEFS.get(g["category"], {"tag_cls": "", "tag_label": "?"})
        items_parts = []
        for i in g["items"][:6]:
            line = (
                f"<span class='mono'>{_esc(i['table'])}.{_esc(i['name'])}</span> "
                f"<span class='muted-text'>({i['kind']})</span>"
            )
            # Append provenance if available
            prov_parts = []
            db = i.get("dashboard_name", "")
            pg = i.get("page_name", "")
            wg = i.get("widget_name", "")
            if db:
                prov_parts.append(db)
            if pg:
                prov_parts.append(pg)
            if wg:
                prov_parts.append(wg)
            if prov_parts:
                prov_str = " &rsaquo; ".join(_esc(p) for p in prov_parts)
                line += f" <span class='muted-text' style='font-size:11px'>[{prov_str}]</span>"
            items_parts.append(line)
        items_html = "<br>".join(items_parts)
        if len(g["items"]) > 6:
            items_html += f"<br><span class='muted-text'>… +{len(g['items']) - 6} more</span>"

        # Show distinct expressions for parallel defs
        detail = _esc(g.get("detail", ""))
        if g["category"] == "parallel_definition":
            exprs = set()
            for item in g["items"][:6]:
                e = item.get("expr", "").strip()
                if e:
                    exprs.add(e[:80])
            if exprs:
                detail = "Expressions: " + " vs ".join(
                    f"<span class='mono'>{_esc(e)}</span>" for e in list(exprs)[:3]
                )

        rows += f"""<tr data-category="{g['category']}">
  <td><span class="tag {cat_info['tag_cls']}">{cat_info['tag_label']}</span></td>
  <td class="wrap">{items_html}</td>
  <td>{len(g['items'])}</td>
  <td class="wrap">{detail}</td>"""
        if has_ai:
            rec = _esc(ai_recs.get(g["group_id"], ""))
            empty_cell = '<span class="muted-text">—</span>'
            cell_content = rec if rec else empty_cell
            rows += f'\n  <td class="wrap ai-rec">{cell_content}</td>'
        rows += "\n</tr>\n"

    more = ""
    if len(actionable) > 100:
        more = f'<p class="muted-text" style="margin-top:8px;font-size:12px">Showing 100 of {len(actionable)} groups. See the Excel workbook for the full list.</p>'

    ai_header = ""
    if has_ai:
        ai_header = '<th data-col="4" style="width:25%">AI Recommendation<span class="sort-arrow">&#9650;</span></th>'

    return f"""
<div class="section section-wide">
  <div class="section-head"><span class="pipe">|</span> What Needs Attention</div>
  <p class="narrative">
    These <em>{total_actionable:,}</em> groups contain calculated business logic that is either
    duplicated or defined inconsistently. Each group represents definitions that should be
    reviewed and consolidated into a single governed definition in your Semantic View.
  </p>
  {cat_defs_html}
  {filter_chips}
  <div class="card"><div class="tbl-scroll">
    <table id="tbl-actionable">
      <thead><tr>
        <th data-col="0" style="width:90px">Category<span class="sort-arrow">&#9650;</span></th>
        <th data-col="1" style="width:{('40%' if has_ai else '50%')}">Definitions<span class="sort-arrow">&#9650;</span></th>
        <th data-col="2" style="width:55px">Count<span class="sort-arrow">&#9650;</span></th>
        <th data-col="3" style="width:{('15%' if has_ai else '25%')}">Detail<span class="sort-arrow">&#9650;</span></th>
        {ai_header}
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>{more}</div>
</div>"""


# ---------------------------------------------------------------------------
# Section: Common Columns (trivial — collapsed by default)
# ---------------------------------------------------------------------------

def _section_common_columns(groups: list, stats: dict, terms: dict) -> str:
    trivial = [g for g in groups if not g.get("has_calculation")]
    if not trivial:
        return ""

    triv_dups = stats.get("trivial_duplicates", 0)
    triv_pars = stats.get("trivial_parallel_defs", 0)
    total_trivial = triv_dups + triv_pars

    # Compact table — just show first 30
    rows = ""
    for g in trivial[:30]:
        cat_info = _CATEGORY_DEFS.get(g["category"], {"tag_cls": "tag-triv", "tag_label": "?"})
        names = ", ".join(set(i["name"] for i in g["items"][:4]))
        if len(set(i["name"] for i in g["items"])) > 4:
            names += " …"
        tables = ", ".join(set(_short_table(i["table"]) for i in g["items"][:3]))
        if len(set(i["table"] for i in g["items"])) > 3:
            tables += " …"
        rows += f"<tr><td class='mono'>{_esc(names)}</td><td class='mono muted-text'>{_esc(tables)}</td><td>{len(g['items'])}</td></tr>\n"

    more = ""
    if len(trivial) > 30:
        more = f'<p class="muted-text" style="margin-top:8px;font-size:12px">{len(trivial) - 30:,} more groups in the Excel workbook.</p>'

    return f"""
<div class="section">
  <div class="section-head"><span class="pipe">|</span> Common Columns</div>
  <div class="collapsible">
    <button class="collapsible-toggle" aria-expanded="false">
      <span>{total_trivial:,} groups of shared column references (IDs, dates, keys) &mdash; informational only</span>
      <span class="chevron">&#9660;</span>
    </button>
    <div class="collapsible-body">
      <p class="narrative" style="margin-bottom:12px">
        These are simple column references (like patient IDs, encounter dates, status codes) that
        appear across multiple tables. This is normal for a data model — these columns don't contain
        business logic and don't need consolidation.
      </p>
      <div class="tbl-wrap"><table>
        <thead><tr>
          <th data-col="0">Column Name<span class="sort-arrow">&#9650;</span></th>
          <th data-col="1">Tables<span class="sort-arrow">&#9650;</span></th>
          <th data-col="2">Occurrences<span class="sort-arrow">&#9650;</span></th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table></div>
      {more}
    </div>
  </div>
</div>"""


# ---------------------------------------------------------------------------
# Section: Definition Reuse
# ---------------------------------------------------------------------------

def _section_reuse(reuse: dict, terms: dict) -> str:
    if not reuse or reuse.get("reuse_instances", 0) == 0:
        return ""

    total_raw = reuse["total_raw"]
    total_unique = reuse["total_unique"]
    reuse_instances = reuse["reuse_instances"]
    reuse_pct = reuse["reuse_pct"]
    top = reuse.get("top_reused", [])

    # Gauge
    unique_pct = total_unique / total_raw * 100 if total_raw else 0
    gauge = (
        f'<div class="gauge">'
        f'<div class="gauge-seg" style="width:{unique_pct:.1f}%;background:var(--accent)"></div>'
        f'<div class="gauge-seg" style="width:{reuse_pct:.1f}%;background:var(--dimmed)"></div>'
        f'</div>'
        f'<div class="gauge-legend">'
        f'<div class="gauge-legend-item"><div class="gauge-legend-dot" style="background:var(--accent)"></div>Unique: {total_unique:,} ({unique_pct:.0f}%)</div>'
        f'<div class="gauge-legend-item"><div class="gauge-legend-dot" style="background:var(--dimmed)"></div>Reuse: {reuse_instances:,} ({reuse_pct:.0f}%)</div>'
        f'</div>'
    )

    # Top reused table
    top_rows = ""
    if top:
        for item in top[:10]:
            top_rows += (
                f"<tr><td class='mono'>{_esc(item['name'])}</td>"
                f"<td class='mono muted-text'>{_esc(item['table'])}</td>"
                f"<td>{item['kind']}</td>"
                f"<td><strong>{item['count']}</strong></td></tr>\n"
            )

    top_table = ""
    if top_rows:
        top_table = f"""
<div style="margin-top:16px">
  <div class="cat-label" style="color:var(--text);margin-bottom:8px">Most Reused Definitions</div>
  <div class="tbl-wrap"><table>
    <thead><tr><th data-col="0">Name<span class="sort-arrow">&#9650;</span></th><th data-col="1">Table<span class="sort-arrow">&#9650;</span></th><th data-col="2">Kind<span class="sort-arrow">&#9650;</span></th><th data-col="3">References<span class="sort-arrow">&#9650;</span></th></tr></thead>
    <tbody>{top_rows}</tbody>
  </table></div>
</div>"""

    return f"""
<div class="section">
  <div class="section-head"><span class="pipe">|</span> Definition Reuse</div>
  <div class="collapsible">
    <button class="collapsible-toggle" aria-expanded="false">
      <span>{reuse_pct:.0f}% of field references are healthy reuse across reports/explores</span>
      <span class="chevron">&#9660;</span>
    </button>
    <div class="collapsible-body">
      <p class="narrative" style="margin-bottom:12px">
        Of <strong>{total_raw:,}</strong> total field references,
        <strong>{reuse_instances:,}</strong> ({reuse_pct:.1f}%) are the same definition
        appearing in multiple explores or reports. This is healthy reuse — a single canonical
        definition referenced in many places — and has been factored out of the duplicate analysis.
      </p>
      {gauge}
      {top_table}
    </div>
  </div>
</div>"""


# ---------------------------------------------------------------------------
# Section: Report Intelligence (replaces Dashboard Landscape)
# ---------------------------------------------------------------------------

def _section_report_intelligence(inventory: dict, narrative: dict,
                                  groups: list = None, stats: dict = None,
                                  terms: dict = None) -> str:
    """Narrative-driven report intelligence: pages, utilization, consolidation, roadmap."""
    pages = narrative.get("page_profiles", [])
    avm = narrative.get("active_vs_model", {})
    reuse = narrative.get("reuse_distribution", {})
    consol = narrative.get("consolidation_opportunities", [])
    conflicts = narrative.get("conflict_coverage", {})
    roadmap = narrative.get("semantic_model_recommendation", {})
    pg = narrative.get("page_label", "pages")
    pg_s = pg.rstrip("s") if pg.endswith("s") else pg

    if not pages and avm.get("total", 0) == 0:
        return ""

    parts = []

    # ── 1. Dashboard Pages & Purpose ────────────────────────────────────────
    if pages:
        has_dash_col = any(p.get("dashboard_names") for p in pages)
        page_rows = ""
        for p in pages:
            widget_list = ", ".join(p["widgets"][:5])
            if len(p["widgets"]) > 5:
                widget_list += f" (+{len(p['widgets']) - 5})"
            dash_cell = ""
            if has_dash_col:
                dnames = p.get("dashboard_names", [])
                dash_cell = f"<td>{_esc(', '.join(dnames[:3]))}{'&hellip;' if len(dnames) > 3 else ''}</td>"
            page_rows += (
                f"<tr>"
                f"{dash_cell}"
                f"<td class='mono'>{_esc(p['name'])}</td>"
                f"<td>{_esc(p['purpose'])}</td>"
                f"<td>{p['total_fields']}</td>"
                f"<td>{p['fact_count']}</td>"
                f"<td>{p['metric_count']}</td>"
                f"<td>{p['table_count']}</td>"
                f"<td class='muted-text' style='font-size:11px'>{_esc(widget_list)}</td>"
                f"</tr>\n"
            )
        dash_header = "<th>Dashboard</th>" if has_dash_col else ""
        parts.append(f"""
<div style="margin-bottom:24px">
  <div class="section-head" style="font-size:14px"><span class="pipe">|</span> {_esc(pg.title())} &amp; Purpose</div>
  <p class="narrative" style="font-size:12px">
    Each {_esc(pg_s)} serves a distinct analytical purpose. Purpose is inferred from
    the metrics and fields present on each page.
  </p>
  <div class="card"><div class="tbl-scroll">
    <table>
      <thead><tr>
        {dash_header}<th>Page</th><th>Inferred Purpose</th><th>Fields</th>
        <th>Facts</th><th>Metrics</th><th>Tables</th><th>Widgets</th>
      </tr></thead>
      <tbody>{page_rows}</tbody>
    </table>
  </div></div>
</div>""")

    # ── 2. Field Utilization ─────────────────────────────────────────────
    if avm.get("total", 0) > 0:
        active_ct = avm["active_count"]
        mo_ct = avm["model_only_count"]
        active_pct = avm["active_pct"]
        mo_pct = avm["model_only_pct"]
        total = avm["total"]

        # KPI strip
        kpi_strip = (
            f'<div class="kpi-strip" style="margin-bottom:12px">'
            f'<div class="kpi"><div class="kpi-value">{total:,}</div><div class="kpi-label">Total Fields</div></div>'
            f'<div class="kpi" style="border-left:3px solid var(--green)">'
            f'<div class="kpi-value" style="color:var(--green)">{active_ct:,}</div>'
            f'<div class="kpi-label">Active on Pages ({active_pct}%)</div></div>'
            f'<div class="kpi" style="border-left:3px solid var(--muted)">'
            f'<div class="kpi-value" style="color:var(--muted)">{mo_ct:,}</div>'
            f'<div class="kpi-label">Model-Only ({mo_pct}%)</div></div>'
            f'</div>'
        )

        # Stacked bar
        bar_html = (
            f'<div style="display:flex;height:20px;border-radius:4px;overflow:hidden;margin-bottom:8px">'
            f'<div style="width:{active_pct}%;background:var(--green)" title="Active: {active_ct}"></div>'
            f'<div style="width:{mo_pct}%;background:#ccc" title="Model-only: {mo_ct}"></div>'
            f'</div>'
            f'<div style="display:flex;justify-content:space-between;font-size:11px;color:var(--muted)">'
            f'<span>&#9632; Active on {_esc(pg)}</span>'
            f'<span>&#9632; Model-only (not on any page)</span>'
            f'</div>'
        )

        # Model-only by table (top 5)
        mo_table_rows = ""
        for entry in avm.get("model_only_by_table", [])[:5]:
            mo_table_rows += (
                f"<tr><td class='mono'>{_esc(entry['table'])}</td>"
                f"<td>{entry['unused_count']}</td></tr>\n"
            )
        mo_table_html = ""
        if mo_table_rows:
            mo_table_html = (
                f'<div style="margin-top:12px">'
                f'<p class="narrative" style="font-size:12px;margin-bottom:6px">'
                f'<strong>Largest model-only concentrations</strong> — tables with the most unused fields:</p>'
                f'<table style="font-size:12px"><thead><tr><th>Table</th><th>Unused Fields</th></tr></thead>'
                f'<tbody>{mo_table_rows}</tbody></table></div>'
            )

        narrative_text = (
            f"Of <strong>{total:,}</strong> data elements in the model, only "
            f"<strong>{active_ct:,} ({active_pct}%)</strong> appear on {pg}. "
            f"The remaining <strong>{mo_ct:,}</strong> exist only in the data model — "
            f"they may be lookup columns, staging fields, or legacy definitions. "
            f"Focus your semantic model on the active fields first."
        )

        parts.append(f"""
<div style="margin-bottom:24px">
  <div class="section-head" style="font-size:14px"><span class="pipe">|</span> Field Utilization</div>
  <p class="narrative">{narrative_text}</p>
  {kpi_strip}
  <div class="card" style="padding:16px">
    {bar_html}
    {mo_table_html}
  </div>
</div>""")

    # ── 3. Conflicts & Blockers ─────────────────────────────────────────
    active_conflict_groups = conflicts.get("active_conflict_groups", [])
    act_c = conflicts.get("active_conflicts", 0)
    mo_c = conflicts.get("model_only_conflicts", 0)
    total_c = conflicts.get("total", 0)

    conflict_section_parts = []

    if active_conflict_groups:
        # Each active conflict as a row in a scrollable table
        for cg in active_conflict_groups:
            field_list = " vs ".join(
                f"<code>{_esc(f)}</code>" for f in cg.get("fields", [])[:4]
            )
            if len(cg.get("fields", [])) > 4:
                field_list += f" (+{len(cg['fields']) - 4})"
            page_tags = " ".join(
                f"<span class='tag' style='background:var(--amber-bg);color:var(--amber)'>{_esc(p)}</span>"
                for p in cg.get("pages_affected", [])[:6]
            )
            if len(cg.get("pages_affected", [])) > 6:
                page_tags += f" <span class='muted-text' style='font-size:11px'>(+{len(cg['pages_affected']) - 6})</span>"
            cat_label = cg.get("category", "duplicate").replace("_", " ").title()
            conflict_section_parts.append(
                f"<tr>"
                f"<td><span class='tag' style='background:var(--amber-bg);color:var(--amber)'>{_esc(cat_label)}</span></td>"
                f"<td style='font-size:12px'>{field_list}</td>"
                f"<td style='font-size:12px'>{page_tags}</td>"
                f"</tr>\n"
            )

    # Model-only conflicts summary (one line, not individual cards)
    mo_summary = ""
    if mo_c > 0:
        mo_summary = (
            f"<p class='narrative' style='font-size:12px;margin-top:8px;color:var(--muted)'>"
            f"{mo_c} additional conflicts are model-only and don't affect any {pg_s}.</p>"
        )

    # Near-clone analysis (kept, but secondary)
    consol_parts = []
    if consol:
        consol_parts.append(
            "<p class='narrative' style='font-size:12px;margin-top:16px;margin-bottom:8px'>"
            "<strong>Near-clone pages</strong> — pages sharing 80%+ of their fields "
            "may be candidates for consolidation:</p>"
        )
        for opp in consol:
            jac_pct = round(opp["jaccard"] * 100)
            only_a = ", ".join(opp["only_in_a"][:5]) or "(none)"
            only_b = ", ".join(opp["only_in_b"][:5]) or "(none)"
            consol_parts.append(
                f"<div class='card' style='padding:10px;margin-bottom:6px;border-left:3px solid var(--muted)'>"
                f"<strong>{_esc(opp['page_a'])}</strong> &amp; <strong>{_esc(opp['page_b'])}</strong> "
                f"share <strong>{jac_pct}%</strong> of their fields ({opp['shared_count']} shared)."
                f"<br><span style='font-size:11px;color:var(--muted)'>"
                f"Only in {_esc(opp['page_a'])}: {_esc(only_a)} | "
                f"Only in {_esc(opp['page_b'])}: {_esc(only_b)}</span>"
                f"</div>"
            )

    if conflict_section_parts or mo_summary or consol_parts:
        intro = ""
        if act_c > 0:
            intro = (
                f"<p class='narrative' style='font-size:12px;margin-bottom:10px'>"
                f"<strong style='color:var(--amber)'>{act_c} conflicts</strong> involve fields "
                f"that appear on {pg}. Resolve these to expand your semantic model beyond P1 core fields.</p>"
            )
        elif total_c > 0:
            intro = (
                f"<p class='narrative' style='font-size:12px;margin-bottom:10px'>"
                f"All <strong>{total_c}</strong> conflicts are in model-only fields — "
                f"none affect active {pg}.</p>"
            )

        conflict_table_html = ""
        if conflict_section_parts:
            conflict_table_html = (
                f"<div class='card'><div class='tbl-scroll'>"
                f"<table>"
                f"<thead><tr><th>Category</th><th>Fields</th><th>Affected {_esc(pg.title())}</th></tr></thead>"
                f"<tbody>{''.join(conflict_section_parts)}</tbody>"
                f"</table>"
                f"</div></div>"
            )

        parts.append(f"""
<div style="margin-bottom:24px">
  <div class="section-head" style="font-size:14px"><span class="pipe">|</span> Conflicts &amp; Blockers</div>
  {intro}
  {conflict_table_html}
  {mo_summary}
  {"".join(consol_parts)}
</div>""")

    # ── 4. Action Plan (Semantic Model Roadmap) ──────────────────────────
    if roadmap:
        p1 = roadmap.get("priority_1", {})
        p1b = roadmap.get("priority_1_blocked", {})
        p2 = roadmap.get("priority_2", {})
        p3 = roadmap.get("priority_3", {})
        total_fields = (p1.get("count", 0) + p1b.get("count", 0)
                        + p2.get("count", 0) + p3.get("count", 0))

        def _tier_card(priority_label, tier, color, border_style="solid"):
            ct = tier.get("count", 0)
            pct = round(100 * ct / total_fields) if total_fields else 0
            fields_preview = ""
            if tier.get("fields"):
                items = ", ".join(f"<code>{_esc(f)}</code>" for f in tier["fields"][:8])
                if len(tier.get("fields", [])) > 8:
                    items += " ..."
                fields_preview = (
                    f"<div style='margin-top:6px;font-size:11px;color:var(--text-secondary)'>"
                    f"{items}</div>"
                )
            return (
                f"<div class='card' style='padding:12px;margin-bottom:8px;border-left:3px {border_style} {color}'>"
                f"<div style='display:flex;justify-content:space-between;align-items:center'>"
                f"<div><strong style='color:{color}'>{_esc(priority_label)}</strong>"
                f"<br><span style='font-size:12px;color:var(--muted)'>{_esc(tier.get('description', ''))}</span></div>"
                f"<div style='text-align:right'>"
                f"<span style='font-size:20px;font-weight:700;color:{color}'>{ct:,}</span>"
                f"<br><span style='font-size:11px;color:var(--muted)'>{pct}% of total</span></div>"
                f"</div>{fields_preview}</div>"
            )

        roadmap_cards = _tier_card("P1: Core — Ready Now", p1, "var(--green)")
        if p1b.get("count", 0) > 0:
            roadmap_cards += _tier_card(
                "Blocked: Core fields held by conflicts", p1b, "var(--amber)", "dashed"
            )
        roadmap_cards += _tier_card("P2: Page-Specific", p2, "var(--accent)")
        roadmap_cards += _tier_card("P3: Model-Only", p3, "var(--muted)")

        parts.append(f"""
<div style="margin-bottom:24px">
  <div class="section-head" style="font-size:14px"><span class="pipe">|</span> Action Plan</div>
  <p class="narrative" style="font-size:12px;margin-bottom:10px">
    Prioritized path to a Snowflake Semantic View. P1 fields are ready to include today.
    Resolve active conflicts to promote blocked fields.
  </p>
  {roadmap_cards}
</div>""")

    if not parts:
        return ""

    return f"""
<div class="section section-wide">
  <div class="section-head"><span class="pipe">|</span> Report Intelligence</div>
  {"".join(parts)}
</div>"""


# ---------------------------------------------------------------------------
# Section: Inventory Overview (report-intel path)
# ---------------------------------------------------------------------------

def _section_inventory_overview(inventory: dict, narrative: dict,
                                 tables, dims, facts, metrics,
                                 total_items, terms, dt_counter,
                                 a_reuse, top_tables) -> str:
    """Dashboard pages & purpose, underlying tables, field utilization."""
    pages = narrative.get("page_profiles", [])
    avm = narrative.get("active_vs_model", {})
    pg = narrative.get("page_label", "pages")
    pg_s = pg.rstrip("s") if pg.endswith("s") else pg

    parts = []

    # ── 1. Dashboard Pages & Purpose (compact: one row per page, no sub-rows) ──
    if pages:
        # Determine if any page has dashboard_name data (for Dashboard column)
        has_dash_col = any(p.get("dashboard_names") for p in pages)
        page_rows = ""
        for p in pages:
            widget_ct = len(p.get("widgets", []))
            dash_cell = ""
            if has_dash_col:
                dnames = p.get("dashboard_names", [])
                dash_cell = f"<td>{_esc(', '.join(dnames[:3]))}{'&hellip;' if len(dnames) > 3 else ''}</td>"
            page_rows += (
                f"<tr>"
                f"{dash_cell}"
                f"<td>{_esc(p['name'])}</td>"
                f"<td>{_esc(p['purpose'])}</td>"
                f"<td style='text-align:center'>{p['total_fields']}</td>"
                f"<td style='text-align:center'>{p['metric_count']}</td>"
                f"<td style='text-align:center'>{p['table_count']}</td>"
                f"<td style='text-align:center'>{widget_ct}</td>"
                f"</tr>\n"
            )

        dash_header = "<th>Dashboard</th>" if has_dash_col else ""
        parts.append(f"""
<div style="margin-bottom:24px">
  <div class="section-head" style="font-size:14px"><span class="pipe">|</span> {_esc(pg.title())} &amp; Purpose</div>
  <p class="narrative" style="font-size:12px">
    Each {_esc(pg_s)} serves a distinct analytical purpose. Purpose is inferred from
    the metrics and fields present on each page.
  </p>
  <div class="card"><div class="tbl-scroll">
    <table>
      <thead><tr>
        {dash_header}<th>Page</th><th>Purpose</th>
        <th style="text-align:center">Fields</th>
        <th style="text-align:center">Metrics</th>
        <th style="text-align:center">Tables</th>
        <th style="text-align:center">Widgets</th>
      </tr></thead>
      <tbody>{page_rows}</tbody>
    </table>
  </div></div>
</div>""")

    # ── 2. Underlying Tables ─────────────────────────────────────────────
    if top_tables:
        tbl_rows = ""
        for name, counts in top_tables[:15]:
            t = sum(counts.values())
            tbl_rows += (
                f"<tr><td>{_esc(name)}</td>"
                f"<td style='text-align:center'>{counts['dimensions']:,}</td>"
                f"<td style='text-align:center'>{counts['facts']:,}</td>"
                f"<td style='text-align:center'>{counts['metrics']:,}</td>"
                f"<td style='text-align:center'><strong>{t:,}</strong></td></tr>\n"
            )
        parts.append(f"""
<div style="margin-bottom:24px">
  <div class="section-head" style="font-size:14px"><span class="pipe">|</span> Underlying Tables</div>
  <p class="narrative" style="font-size:12px">
    {len(tables):,} tables referenced across the data model. Showing top {min(len(top_tables), 15)} by field count.
  </p>
  <div class="card"><div class="tbl-scroll">
    <table>
      <thead><tr>
        <th>Table</th>
        <th style="text-align:center">{terms['dimensions']}</th>
        <th style="text-align:center">{terms['facts']}</th>
        <th style="text-align:center">{terms['metrics']}</th>
        <th style="text-align:center">Total</th>
      </tr></thead>
      <tbody>{tbl_rows}</tbody>
    </table>
  </div></div>
</div>""")

    # ── 3. Field Utilization ─────────────────────────────────────────────
    if avm.get("total", 0) > 0:
        active_ct = avm["active_count"]
        mo_ct = avm["model_only_count"]
        active_pct = avm["active_pct"]
        mo_pct = avm["model_only_pct"]
        total = avm["total"]

        # Stacked bar
        bar_html = (
            f'<div style="display:flex;height:20px;border-radius:4px;overflow:hidden;margin-bottom:8px">'
            f'<div style="width:{active_pct}%;background:var(--green)" title="Active: {active_ct}"></div>'
            f'<div style="width:{mo_pct}%;background:#ccc" title="Model-only: {mo_ct}"></div>'
            f'</div>'
            f'<div style="display:flex;justify-content:space-between;font-size:11px;color:var(--muted)">'
            f'<span>&#9632; Active on {_esc(pg)} &mdash; {active_ct:,} ({active_pct}%)</span>'
            f'<span>&#9632; Model-only &mdash; {mo_ct:,} ({mo_pct}%)</span>'
            f'</div>'
        )

        narrative_text = (
            f"Of <strong>{total:,}</strong> data elements in the model, "
            f"<strong>{active_ct:,} ({active_pct}%)</strong> appear on {pg}. "
            f"The remaining <strong>{mo_ct:,}</strong> exist only in the data model."
        )

        parts.append(f"""
<div style="margin-bottom:24px">
  <div class="section-head" style="font-size:14px"><span class="pipe">|</span> Field Utilization</div>
  <p class="narrative" style="font-size:12px">{narrative_text}</p>
  <div class="card" style="padding:16px">
    {bar_html}
  </div>
</div>""")

    if not parts:
        return ""

    return f"""
<div class="section section-wide">
  <div class="section-head"><span class="pipe">|</span> Inventory Overview</div>
  {"".join(parts)}
</div>"""


# ---------------------------------------------------------------------------
# Section: Items to Resolve (report-intel path)
# ---------------------------------------------------------------------------

def _build_dax_remapping_table(flagged: list, pg: str) -> str:
    """Build a DAX remapping table from flagged items, grouped by action category."""
    if not flagged:
        return ""

    # Classify each flagged item
    categories = {
        "keep_in_dax": {"label": "Keep in DAX", "color": "var(--green)", "items": [],
                        "desc": "Presentation-layer logic — must remain as dashboard calculations"},
        "translate": {"label": "Translate to SQL", "color": "var(--accent)", "items": [],
                      "desc": "Has direct SQL equivalent — auto-convertible"},
        "rewrite": {"label": "Rewrite", "color": "var(--amber)", "items": [],
                    "desc": "Needs manual SQL rewrite"},
        "manual": {"label": "Manual Review", "color": "var(--red)", "items": [],
                   "desc": "Complex filter context — present DAX to developer team"},
        "other": {"label": "Other", "color": "var(--muted)", "items": [],
                  "desc": "Requires individual assessment"},
    }

    for f in flagged:
        reason = f.get("reason", "").lower()
        if "keep in dax" in reason:
            categories["keep_in_dax"]["items"].append(f)
        elif reason.startswith("translate"):
            categories["translate"]["items"].append(f)
        elif reason.startswith("rewrite"):
            categories["rewrite"]["items"].append(f)
        elif "manual review" in reason:
            categories["manual"]["items"].append(f)
        else:
            categories["other"]["items"].append(f)

    # Summary chips
    chips_html = '<div class="filter-bar" data-filter-group="dax-table">'
    for cat_key, cat in categories.items():
        ct = len(cat["items"])
        if ct == 0:
            continue
        chips_html += (
            f"<span class='filter-chip' data-filter='{cat_key}' "
            f"style='border-color:color-mix(in srgb, {cat['color']} 40%, white);"
            f"color:{cat['color']}'>"
            f"{cat['label']} <span class='filter-count'>{ct}</span></span>"
        )
    chips_html += '</div>'

    # Table rows — all items rendered, filterable via chips
    all_items = []
    for cat_key, cat in categories.items():
        for f in cat["items"]:
            all_items.append((cat_key, cat, f))

    rows = ""
    for cat_key, cat, f in all_items:
        tbl = _esc(f.get("table", ""))
        msr = _esc(f.get("measure", f.get("column", "")))
        effort = _esc(f.get("effort", "?"))
        patterns = _esc(f.get("dax_patterns", ""))
        desc = _esc(f.get("description", "")[:100])
        reason_short = _esc(f.get("reason", "")[:80])

        effort_color = {"S": "var(--green)", "M": "var(--amber)", "L": "var(--red)"}.get(effort, "var(--muted)")

        rows += (
            f"<tr data-category='{cat_key}'>"
            f"<td><span class='tag' style='background:color-mix(in srgb, {cat['color']} 15%, white);"
            f"color:{cat['color']};font-size:11px;white-space:nowrap'>{_esc(cat['label'])}</span></td>"
            f"<td style='font-size:12px'>{tbl}.{msr}</td>"
            f"<td style='text-align:center;color:{effort_color};font-weight:700'>{effort}</td>"
            f"<td class='mono' style='font-size:11px;color:var(--muted)'>{patterns}</td>"
            f"<td style='font-size:12px'>{reason_short}</td>"
            f"</tr>\n"
        )

    return f"""
<div style="margin-bottom:24px">
  <div class="section-head" style="font-size:14px"><span class="pipe">|</span> DAX Remapping</div>
  <p class="narrative" style="font-size:12px;margin-bottom:8px">
    These <strong>{len(all_items)}</strong> measures use DAX patterns that cannot be directly
    represented in a Snowflake Semantic View. Items marked <strong>Keep in DAX</strong> must
    remain as dashboard-level calculations. Click a category to filter.
  </p>
  {chips_html}
  <div class="card"><div class="tbl-scroll">
    <table id="dax-table">
      <thead><tr>
        <th>Action</th>
        <th>Measure</th>
        <th style="text-align:center">Effort</th>
        <th>DAX Patterns</th>
        <th>Recommendation</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div></div>
</div>"""


def _section_items_to_resolve(inventory: dict, narrative: dict,
                               groups: list, stats: dict, terms: dict,
                               flagged: list, complexity: dict,
                               total_items: int) -> str:
    """Conflicts, DAX remapping, and action plan — focused on what's critical."""
    conflicts = narrative.get("conflict_coverage", {})
    roadmap = narrative.get("semantic_model_recommendation", {})
    consol = narrative.get("consolidation_opportunities", [])
    pg = narrative.get("page_label", "pages")
    pg_s = pg.rstrip("s") if pg.endswith("s") else pg

    parts = []

    # ── 1. Conflicts & Blockers ─────────────────────────────────────────
    active_conflict_groups = conflicts.get("active_conflict_groups", [])
    act_c = conflicts.get("active_conflicts", 0)
    mo_c = conflicts.get("model_only_conflicts", 0)
    total_c = conflicts.get("total", 0)

    conflict_table_rows = ""

    if active_conflict_groups:
        for cg in active_conflict_groups:
            field_list = " vs ".join(
                f"<code>{_esc(f)}</code>" for f in cg.get("fields", [])[:4]
            )
            if len(cg.get("fields", [])) > 4:
                field_list += f" (+{len(cg['fields']) - 4})"
            page_tags = " ".join(
                f"<span class='tag' style='background:var(--amber-bg);color:var(--amber)'>{_esc(p)}</span>"
                for p in cg.get("pages_affected", [])[:6]
            )
            if len(cg.get("pages_affected", [])) > 6:
                page_tags += f" <span class='muted-text' style='font-size:11px'>(+{len(cg['pages_affected']) - 6})</span>"
            cat_label = cg.get("category", "duplicate").replace("_", " ").title()
            conflict_table_rows += (
                f"<tr>"
                f"<td><span class='tag' style='background:var(--amber-bg);color:var(--amber)'>{_esc(cat_label)}</span></td>"
                f"<td style='font-size:12px'>{field_list}</td>"
                f"<td style='font-size:12px'>{page_tags}</td>"
                f"</tr>\n"
            )

    mo_summary = ""
    if mo_c > 0:
        mo_summary = (
            f"<p class='narrative' style='font-size:12px;margin-top:8px;color:var(--muted)'>"
            f"{mo_c} additional conflicts are model-only and don't affect any {pg_s}.</p>"
        )

    if conflict_table_rows or mo_summary:
        intro = ""
        if act_c > 0:
            intro = (
                f"<p class='narrative' style='font-size:12px;margin-bottom:10px'>"
                f"<strong style='color:var(--amber)'>{act_c} conflicts</strong> involve fields "
                f"that appear on {pg}. Resolve these to expand your semantic model beyond P1 core fields.</p>"
            )
        elif total_c > 0:
            intro = (
                f"<p class='narrative' style='font-size:12px;margin-bottom:10px'>"
                f"All <strong>{total_c}</strong> conflicts are in model-only fields — "
                f"none affect active {pg}.</p>"
            )

        conflict_table_html = ""
        if conflict_table_rows:
            conflict_table_html = (
                f"<div class='card'><div class='tbl-scroll'>"
                f"<table>"
                f"<thead><tr><th>Category</th><th>Fields</th><th>Affected {_esc(pg.title())}</th></tr></thead>"
                f"<tbody>{conflict_table_rows}</tbody>"
                f"</table>"
                f"</div></div>"
            )

        parts.append(f"""
<div style="margin-bottom:24px">
  <div class="section-head" style="font-size:14px"><span class="pipe">|</span> Conflicts &amp; Blockers</div>
  {intro}
  {conflict_table_html}
  {mo_summary}
</div>""")

    # ── 2. DAX Remapping ────────────────────────────────────────────────
    if flagged:
        parts.append(_build_dax_remapping_table(flagged, pg))

    # ── 3. Action Plan (Semantic Model Roadmap) ──────────────────────────
    if roadmap:
        p1 = roadmap.get("priority_1", {})
        p1b = roadmap.get("priority_1_blocked", {})
        p2 = roadmap.get("priority_2", {})
        p3 = roadmap.get("priority_3", {})
        total_fields = (p1.get("count", 0) + p1b.get("count", 0)
                        + p2.get("count", 0) + p3.get("count", 0))

        def _tier_card(priority_label, tier, color, border_style="solid"):
            ct = tier.get("count", 0)
            pct = round(100 * ct / total_fields) if total_fields else 0
            fields_preview = ""
            if tier.get("fields"):
                items = ", ".join(f"<code>{_esc(f)}</code>" for f in tier["fields"][:8])
                if len(tier.get("fields", [])) > 8:
                    items += " ..."
                fields_preview = (
                    f"<div style='margin-top:6px;font-size:11px;color:var(--text-secondary)'>"
                    f"{items}</div>"
                )
            return (
                f"<div class='card' style='padding:12px;margin-bottom:8px;border-left:3px {border_style} {color}'>"
                f"<div style='display:flex;justify-content:space-between;align-items:center'>"
                f"<div><strong style='color:{color}'>{_esc(priority_label)}</strong>"
                f"<br><span style='font-size:12px;color:var(--muted)'>{_esc(tier.get('description', ''))}</span></div>"
                f"<div style='text-align:right'>"
                f"<span style='font-size:20px;font-weight:700;color:{color}'>{ct:,}</span>"
                f"<br><span style='font-size:11px;color:var(--muted)'>{pct}% of total</span></div>"
                f"</div>{fields_preview}</div>"
            )

        roadmap_cards = _tier_card("P1: Core — Ready Now", p1, "var(--green)")
        if p1b.get("count", 0) > 0:
            roadmap_cards += _tier_card(
                "Blocked: Core fields held by conflicts", p1b, "var(--amber)", "dashed"
            )
        roadmap_cards += _tier_card("P2: Page-Specific", p2, "var(--accent)")
        roadmap_cards += _tier_card("P3: Model-Only", p3, "var(--muted)")

        parts.append(f"""
<div style="margin-bottom:24px">
  <div class="section-head" style="font-size:14px"><span class="pipe">|</span> Action Plan</div>
  <p class="narrative" style="font-size:12px;margin-bottom:10px">
    Prioritized path to a Snowflake Semantic View. P1 fields are ready to include today.
    Resolve active conflicts to promote blocked fields.
  </p>
  {roadmap_cards}
</div>""")

    if not parts:
        return ""

    return f"""
<div class="section section-wide">
  <div class="section-head"><span class="pipe">|</span> Items to Resolve</div>
  {"".join(parts)}
</div>"""


# ---------------------------------------------------------------------------
# Section: Complexity
# ---------------------------------------------------------------------------

def _section_complexity(complexity: dict, total: int) -> str:
    if total == 0:
        return ""
    simple = complexity.get("simple", 0)
    translate = complexity.get("needs_translation", 0)
    manual = complexity.get("manual_required", 0)

    # Skip if everything is simple and there's nothing interesting to show
    if translate == 0 and manual == 0:
        return ""

    bars = ""
    for label, val, color, tag_cls in [
        ("Simple / Direct", simple, "var(--green)", "tag-simple"),
        ("Needs Translation", translate, "var(--amber)", "tag-translate"),
        ("Manual Review", manual, "var(--red)", "tag-manual"),
    ]:
        if val == 0:
            continue
        pct = (val / total * 100) if total else 0
        bars += f"""<div class="bar-row">
  <span class="bar-label">{label}</span>
  <div class="bar" style="width:{max(pct, 0.5)}%;background:{color}"></div>
  <span class="bar-value">{val:,} ({pct:.0f}%)</span>
</div>\n"""

    return f"""
<div class="section">
  <div class="section-head"><span class="pipe">|</span> Migration Complexity</div>
  <div class="card">
    <p class="narrative" style="margin-bottom:12px">
      How complex is the migration? This classifies each field by the effort required
      to translate it into a Snowflake Semantic View definition.
    </p>
    {bars}
  </div>
</div>"""


# ---------------------------------------------------------------------------
# Section: Details (collapsed — tables, data types, flagged, errors)
# ---------------------------------------------------------------------------

def _section_details(top_tables, dt_counter, flagged, errors, terms) -> str:
    parts = []

    # Tables detail
    if top_tables:
        rows = ""
        for name, counts in top_tables:
            t = sum(counts.values())
            rows += (
                f"<tr><td class='mono'>{_esc(name)}</td>"
                f"<td>{counts['dimensions']:,}</td>"
                f"<td>{counts['facts']:,}</td>"
                f"<td>{counts['metrics']:,}</td>"
                f"<td><strong>{t:,}</strong></td></tr>\n"
            )
        parts.append(f"""
<div class="cat-label" style="color:var(--text);margin-bottom:8px;margin-top:4px">Top Tables by Field Count</div>
<div class="tbl-wrap"><table>
  <thead><tr>
    <th data-col="0">Table<span class="sort-arrow">&#9650;</span></th>
    <th data-col="1">{terms['dimensions']}<span class="sort-arrow">&#9650;</span></th>
    <th data-col="2">{terms['facts']}<span class="sort-arrow">&#9650;</span></th>
    <th data-col="3">{terms['metrics']}<span class="sort-arrow">&#9650;</span></th>
    <th data-col="4">Total<span class="sort-arrow">&#9650;</span></th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table></div>""")

    # Data types
    if dt_counter:
        top_dt = dt_counter.most_common(15)
        max_val = top_dt[0][1] if top_dt else 1
        colors = ["var(--accent)", "var(--green)", "var(--amber)", "var(--purple)",
                  "var(--red)", "#1abc9c", "#e67e22", "#3498db", "#5bc0de", "#95a5a6"]
        bars = ""
        for i, (dt, count) in enumerate(top_dt):
            pct = count / max_val * 100
            c = colors[i % len(colors)]
            bars += f"""<div class="bar-row">
  <span class="bar-label mono">{_esc(dt)}</span>
  <div class="bar" style="width:{max(pct, 1)}%;background:{c}"></div>
  <span class="bar-value">{count:,}</span>
</div>\n"""
        parts.append(f"""
<div class="cat-label" style="color:var(--text);margin-bottom:8px;margin-top:20px">Data Type Distribution</div>
{bars}""")

    # Flagged
    if flagged:
        flag_rows = ""
        for fl in flagged[:50]:
            desc = _esc(str(fl.get('description', '')))
            patterns = _esc(str(fl.get('dax_patterns', '')))
            rec = _esc(str(fl.get('recommendation', fl.get('reason', ''))))
            effort = _esc(str(fl.get('effort', '')))
            reason = _esc(str(fl.get('reason', '')))
            # Color-code recommendation
            rec_class = ""
            rec_lower = rec.lower()
            if "keep in dax" in rec_lower:
                rec_class = " style='color:#27ae60'"
            elif "translate" in rec_lower:
                rec_class = " style='color:#2980b9'"
            elif "rewrite" in rec_lower:
                rec_class = " style='color:#e67e22'"
            elif "manual" in rec_lower:
                rec_class = " style='color:#c0392b'"
            flag_rows += (
                f"<tr><td>{_esc(str(fl.get('type','')))}</td>"
                f"<td class='mono'>{_esc(str(fl.get('table','')))}</td>"
                f"<td class='mono'>{_esc(str(fl.get('measure', fl.get('column',''))))}</td>"
                f"<td>{desc}</td>"
                f"<td class='mono'>{patterns}</td>"
                f"<td{rec_class}>{rec}</td>"
                f"<td>{effort}</td></tr>\n"
            )
        more = f"<p class='muted-text' style='font-size:12px;margin-top:8px'>Showing 50 of {len(flagged)}</p>" if len(flagged) > 50 else ""
        parts.append(f"""
<div class="cat-label" style="color:var(--amber);margin-bottom:8px;margin-top:20px">Flagged Items ({len(flagged):,})</div>
<div class="tbl-wrap"><table>
  <thead><tr><th data-col="0">Type<span class="sort-arrow">&#9650;</span></th><th data-col="1">Table<span class="sort-arrow">&#9650;</span></th><th data-col="2">Item<span class="sort-arrow">&#9650;</span></th><th data-col="3">Description<span class="sort-arrow">&#9650;</span></th><th data-col="4">DAX Patterns<span class="sort-arrow">&#9650;</span></th><th data-col="5">Recommendation<span class="sort-arrow">&#9650;</span></th><th data-col="6">Effort<span class="sort-arrow">&#9650;</span></th></tr></thead>
  <tbody>{flag_rows}</tbody>
</table></div>{more}""")

    # Errors — suppress if all are .dashboard.lookml (expected failures)
    if errors and not _errors_are_dashboard_only(errors):
        err_rows = ""
        for e in errors[:30]:
            if isinstance(e, dict):
                err_rows += (
                    f"<tr><td class='mono'>{_esc(str(e.get('file','')))}</td>"
                    f"<td>{_esc(e.get('error_type',''))}</td>"
                    f"<td class='truncate'>{_esc(str(e.get('error_message', e.get('error','')))[:200])}</td></tr>\n"
                )
            else:
                err_rows += f"<tr><td colspan='3'>{_esc(str(e)[:200])}</td></tr>\n"
        parts.append(f"""
<div class="cat-label" style="color:var(--red);margin-bottom:8px;margin-top:20px">Parse Errors ({len(errors)})</div>
<div class="tbl-wrap"><table>
  <thead><tr><th data-col="0">File<span class="sort-arrow">&#9650;</span></th><th data-col="1">Type<span class="sort-arrow">&#9650;</span></th><th data-col="2">Message<span class="sort-arrow">&#9650;</span></th></tr></thead>
  <tbody>{err_rows}</tbody>
</table></div>""")

    if not parts:
        return ""

    inner = "\n".join(parts)
    # Build toggle text dynamically based on what's actually shown
    toggle_parts = []
    if top_tables:
        toggle_parts.append("tables")
    if dt_counter:
        toggle_parts.append("data types")
    if flagged:
        toggle_parts.append("flagged items")
    if errors and not _errors_are_dashboard_only(errors):
        toggle_parts.append("parse errors")
    toggle_text = ", ".join(toggle_parts).capitalize() if toggle_parts else "Details"

    return f"""
<div class="section">
  <div class="section-head"><span class="pipe">|</span> Details</div>
  <div class="collapsible">
    <button class="collapsible-toggle" aria-expanded="false">
      <span>{toggle_text}</span>
      <span class="chevron">&#9660;</span>
    </button>
    <div class="collapsible-body">
      {inner}
    </div>
  </div>
</div>"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _esc(s: str) -> str:
    return html.escape(str(s)) if s else ""


def _short_table(table_name: str) -> str:
    """Shorten a fully qualified table name for display."""
    parts = table_name.replace('"', '').split('.')
    if len(parts) >= 2:
        return parts[-1]
    return table_name
