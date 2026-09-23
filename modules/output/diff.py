"""Compare two inventory JSON files and produce a structured diff.

Usage:
    from modules.output.diff import diff_inventories

    result = diff_inventories(inventory_a, inventory_b)
    # result["summary"] = {identical, expression_differs, only_in_a, only_in_b, total_a, total_b}
    # result["items"]   = list of {key, status, item_a, item_b, ...}
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import datetime

from ..common.logger import get_logger

log = get_logger("diff")

# ---------------------------------------------------------------------------
# Expression normalization (mirrors analysis.py)
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r'\s+')
_TABLE_PREFIX_RE = re.compile(r'(\b\w+)\.(\w+)')


def _normalize_expression(expr: str) -> str:
    """Normalize an expression for comparison."""
    if not expr:
        return ""
    s = expr.strip().lower()
    s = _WHITESPACE_RE.sub(" ", s)
    # Strip surrounding quotes
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1]
    return s


# ---------------------------------------------------------------------------
# Core diff
# ---------------------------------------------------------------------------

def diff_inventories(
    inv_a: dict,
    inv_b: dict,
    label_a: str = "A",
    label_b: str = "B",
) -> dict:
    """Compare two inventories item-by-item.

    Items are matched by (kind, table, name) tuple.  For each match,
    expressions are compared using normalized form.

    Args:
        inv_a: First inventory dict (from load_inventory).
        inv_b: Second inventory dict (from load_inventory).
        label_a: Human label for inventory A (e.g. "Our Parser").
        label_b: Human label for inventory B (e.g. "Josh's Parser").

    Returns:
        Dict with "summary", "items", "label_a", "label_b".
    """
    items_a = _extract_items(inv_a)
    items_b = _extract_items(inv_b)

    index_a = _index_items(items_a)
    index_b = _index_items(items_b)

    all_keys = set(index_a.keys()) | set(index_b.keys())

    results = []
    counts = {"identical": 0, "expression_differs": 0, "only_in_a": 0, "only_in_b": 0}

    for key in sorted(all_keys):
        ia = index_a.get(key)
        ib = index_b.get(key)

        if ia and not ib:
            counts["only_in_a"] += 1
            results.append({
                "key": _key_str(key),
                "kind": key[0],
                "table": key[1],
                "name": key[2],
                "status": "only_in_a",
                "expr_a": ia["expr"],
                "expr_b": None,
            })
        elif ib and not ia:
            counts["only_in_b"] += 1
            results.append({
                "key": _key_str(key),
                "kind": key[0],
                "table": key[1],
                "name": key[2],
                "status": "only_in_b",
                "expr_a": None,
                "expr_b": ib["expr"],
            })
        else:
            norm_a = _normalize_expression(ia["expr"])
            norm_b = _normalize_expression(ib["expr"])
            if norm_a == norm_b:
                counts["identical"] += 1
                results.append({
                    "key": _key_str(key),
                    "kind": key[0],
                    "table": key[1],
                    "name": key[2],
                    "status": "identical",
                    "expr_a": ia["expr"],
                    "expr_b": ib["expr"],
                })
            else:
                counts["expression_differs"] += 1
                results.append({
                    "key": _key_str(key),
                    "kind": key[0],
                    "table": key[1],
                    "name": key[2],
                    "status": "expression_differs",
                    "expr_a": ia["expr"],
                    "expr_b": ib["expr"],
                })

    summary = {
        **counts,
        "total_a": len(items_a),
        "total_b": len(items_b),
        "matched_keys": counts["identical"] + counts["expression_differs"],
    }

    return {
        "summary": summary,
        "items": results,
        "label_a": label_a,
        "label_b": label_b,
    }


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def diff_to_json(diff_result: dict, output_path: str) -> str:
    """Write diff result to a JSON file."""
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(diff_result, fh, indent=2, default=str)
    log.info("Diff JSON written to %s", output_path)
    return output_path


def diff_to_html(diff_result: dict, output_path: str) -> str:
    """Write diff result to a standalone HTML report."""
    summary = diff_result["summary"]
    items = diff_result["items"]
    label_a = diff_result["label_a"]
    label_b = diff_result["label_b"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Only show non-identical items in the detail table
    diff_items = [i for i in items if i["status"] != "identical"]

    rows = ""
    for item in diff_items[:500]:
        status_cls = {
            "only_in_a": "tag-dup",
            "only_in_b": "tag-par",
            "expression_differs": "tag-ovr",
        }.get(item["status"], "")
        status_label = {
            "only_in_a": f"Only in {label_a}",
            "only_in_b": f"Only in {label_b}",
            "expression_differs": "Expr Differs",
        }.get(item["status"], item["status"])

        expr_a = _esc(item["expr_a"] or "—")[:120]
        expr_b = _esc(item["expr_b"] or "—")[:120]

        rows += f"""<tr>
  <td><span class="tag {status_cls}">{status_label}</span></td>
  <td class="mono">{_esc(item['table'])}.{_esc(item['name'])}</td>
  <td>{_esc(item['kind'])}</td>
  <td class="mono wrap">{expr_a}</td>
  <td class="mono wrap">{expr_b}</td>
</tr>\n"""

    more = ""
    if len(diff_items) > 500:
        more = f'<p style="color:#888;font-size:12px;margin-top:8px">Showing 500 of {len(diff_items):,} differences. See the JSON output for the full list.</p>'

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<title>Parser Comparison Report</title>
<style>
  :root {{ --bg:#0d1117; --fg:#e6edf3; --accent:#29B5E8; --muted:#555; --red:#ff6b6b; --green:#69db7c; --amber:#ffa94d; }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:'Inter',system-ui,sans-serif; background:var(--bg); color:var(--fg); padding:32px; font-size:13px; }}
  .header {{ text-align:center; margin-bottom:32px; }}
  .header h1 {{ font-size:20px; color:var(--accent); margin-bottom:4px; }}
  .header .sub {{ color:var(--muted); font-size:12px; }}
  .kpi-strip {{ display:flex; gap:16px; justify-content:center; margin-bottom:24px; flex-wrap:wrap; }}
  .kpi {{ background:#161b22; border:1px solid #30363d; border-radius:8px; padding:12px 20px; text-align:center; min-width:120px; }}
  .kpi .val {{ font-size:22px; font-weight:700; font-family:'JetBrains Mono',monospace; }}
  .kpi .label {{ font-size:11px; color:var(--muted); margin-top:2px; }}
  .kpi .val.green {{ color:var(--green); }}
  .kpi .val.amber {{ color:var(--amber); }}
  .kpi .val.red {{ color:var(--red); }}
  table {{ width:100%; border-collapse:collapse; margin-top:16px; }}
  th {{ text-align:left; padding:8px 10px; border-bottom:2px solid #30363d; font-size:11px; color:var(--muted); text-transform:uppercase; }}
  td {{ padding:6px 10px; border-bottom:1px solid #21262d; vertical-align:top; }}
  .mono {{ font-family:'JetBrains Mono',monospace; font-size:12px; }}
  .wrap {{ max-width:300px; word-break:break-all; }}
  .tag {{ display:inline-block; padding:2px 8px; border-radius:4px; font-size:11px; font-weight:600; }}
  .tag-dup {{ background:rgba(255,107,107,0.15); color:var(--red); }}
  .tag-par {{ background:rgba(41,181,232,0.15); color:var(--accent); }}
  .tag-ovr {{ background:rgba(255,169,77,0.15); color:var(--amber); }}
</style>
</head><body>
<div class="header">
  <h1>Parser Comparison Report</h1>
  <div class="sub">{_esc(label_a)} vs {_esc(label_b)} &middot; {now}</div>
</div>

<div class="kpi-strip">
  <div class="kpi"><div class="val green">{summary['identical']:,}</div><div class="label">Identical</div></div>
  <div class="kpi"><div class="val amber">{summary['expression_differs']:,}</div><div class="label">Expression Differs</div></div>
  <div class="kpi"><div class="val red">{summary['only_in_a']:,}</div><div class="label">Only in {_esc(label_a)}</div></div>
  <div class="kpi"><div class="val red">{summary['only_in_b']:,}</div><div class="label">Only in {_esc(label_b)}</div></div>
  <div class="kpi"><div class="val">{summary['total_a']:,}</div><div class="label">{_esc(label_a)} Items</div></div>
  <div class="kpi"><div class="val">{summary['total_b']:,}</div><div class="label">{_esc(label_b)} Items</div></div>
</div>

<table>
<thead><tr>
  <th>Status</th><th>Definition</th><th>Kind</th>
  <th>{_esc(label_a)} Expression</th><th>{_esc(label_b)} Expression</th>
</tr></thead>
<tbody>
{rows}
</tbody>
</table>
{more}
</body></html>"""

    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    log.info("Diff HTML written to %s", output_path)
    return output_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_items(inv: dict) -> list[dict]:
    """Flatten inventory into a list of (kind, table, name, expr) items."""
    items = []
    for dim in inv.get("dimensions", []):
        items.append({**dim, "kind": "dimension"})
    for fact in inv.get("facts", []):
        items.append({**fact, "kind": "fact"})
    for metric in inv.get("metrics", []):
        items.append({**metric, "kind": "metric"})
    return items


def _index_items(items: list[dict]) -> dict[tuple, dict]:
    """Index items by (kind, table, name). Last write wins for duplicates."""
    index = {}
    for item in items:
        key = (item.get("kind", ""), item.get("table", ""), item.get("name", ""))
        index[key] = item
    return index


def _key_str(key: tuple) -> str:
    return f"{key[0]}::{key[1]}::{key[2]}"


def _esc(text: str) -> str:
    """Escape HTML special characters."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
