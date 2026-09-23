"""Excel workbook generator for semantic extraction inventories.

Produces a 5-tab .xlsx:
  1. Summary        – stats, readiness score, instructions, quick links
  2. Review & Curate – conflicts (Resolution dropdown) + items needing
                       attention (Include in SV? dropdown)
  3. DAX Translation – developer punch list for complex measures
  4. Dashboard Inventory & Overlap – page inventory with inferred purpose,
                       per-page field overlap, pairwise similarity matrix
  5. Reference       – Full Inventory, Dashboard Pages, Tables,
                       Relationships, Parallel Definitions, Errors

Requires: openpyxl (auto-installed by the CLI dependency mechanism).
"""

import os
from typing import Any

from ..common.logger import get_logger

log = get_logger("excel_report")

# Snowflake brand colours (hex for openpyxl)
_ACCENT = "29B5E8"
_HEADER_BG = "1B2A4A"
_HEADER_FG = "FFFFFF"
_ROW_ALT = "F7FBFD"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_str(val: Any, fallback: str = "") -> str:
    """Return *val* as a usable display string.

    - ``None`` / ``"nan"`` / ``"None"`` / empty → *fallback*
    - Otherwise ``str(val)`` with leading/trailing whitespace stripped.
    """
    if val is None:
        return fallback
    s = str(val).strip()
    if s.lower() in ("nan", "none", ""):
        return fallback
    return s


def _best_description(item: dict) -> str:
    """Pick the best available description for an inventory item."""
    desc = _safe_str(item.get("description"))
    if desc:
        return desc
    return _safe_str(item.get("dax_description"))


def _dax_action_category(rec: str) -> str:
    """Derive an action chip label from a recommendation string."""
    rl = (rec or "").lower()
    if "direct sql" in rl or "auto-convert" in rl:
        return "Auto-Convert"
    if "translate" in rl:
        return "Translate to SQL"
    if "rewrite" in rl:
        return "Rewrite"
    if "manual" in rl:
        return "Manual Review"
    if "keep in dax" in rl:
        return "Keep in DAX"
    return "Review"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_excel_workbook(
    inventory: dict,
    analysis: dict | None = None,
    output_path: str = "report.xlsx",
    verified_queries: list[dict] | None = None,
) -> str:
    """Generate an Excel workbook from an inventory.

    Args:
        inventory: Unified inventory dict.
        analysis: Output from analysis.analyze_inventory() (optional).
        output_path: File path for the .xlsx.

    Returns:
        The output_path written to.
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = openpyxl.Workbook()

    # Shared styles
    header_font = Font(name="Inter", bold=True, color=_HEADER_FG, size=11)
    header_fill = PatternFill(start_color=_HEADER_BG, end_color=_HEADER_BG, fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    accent_font_lg = Font(name="Inter", bold=True, color=_ACCENT, size=24)
    accent_font = Font(name="Inter", bold=True, color=_ACCENT, size=12)
    label_font = Font(name="Inter", color="666666", size=9)
    thin_border = Border(bottom=Side(style="thin", color="DDDDDD"))
    alt_fill = PatternFill(start_color=_ROW_ALT, end_color=_ROW_ALT, fill_type="solid")

    # Section header style — dark blue banner row used in Reference tab
    section_fill = PatternFill(start_color="1E3A5F", end_color="1E3A5F", fill_type="solid")
    section_font = Font(name="Inter", bold=True, color="FFFFFF", size=12)

    # Color fills for DAX action categories
    fill_auto = PatternFill(start_color="D5F5E3", end_color="D5F5E3", fill_type="solid")
    fill_translate = PatternFill(start_color="D6EAF8", end_color="D6EAF8", fill_type="solid")
    fill_rewrite = PatternFill(start_color="FDEBD0", end_color="FDEBD0", fill_type="solid")
    fill_manual = PatternFill(start_color="FADBD8", end_color="FADBD8", fill_type="solid")
    fill_keep = PatternFill(start_color="D5F5E3", end_color="D5F5E3", fill_type="solid")

    _action_fill = {
        "Auto-Convert": fill_auto,
        "Translate to SQL": fill_translate,
        "Rewrite": fill_rewrite,
        "Manual Review": fill_manual,
        "Keep in DAX": fill_keep,
    }

    def style_header(ws, col_count, row=1):
        for col in range(1, col_count + 1):
            cell = ws.cell(row=row, column=col)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
        ws.freeze_panes = f"A{row + 1}"
        ws.auto_filter.ref = (
            f"{get_column_letter(1)}{row}:{get_column_letter(col_count)}{row}"
        )

    def auto_width(ws, col_count, max_width=50):
        for col in range(1, col_count + 1):
            max_len = 0
            for row in ws.iter_rows(min_col=col, max_col=col, values_only=False):
                for cell in row:
                    if cell.value:
                        max_len = max(max_len, len(str(cell.value)))
            ws.column_dimensions[get_column_letter(col)].width = min(
                max(max_len + 2, 10), max_width
            )

    def apply_banding(ws, start_row, end_row, col_count):
        """Apply alternating row fill starting from start_row."""
        for r in range(start_row, end_row + 1):
            if (r - start_row) % 2 == 1:
                for c in range(1, col_count + 1):
                    ws.cell(row=r, column=c).fill = alt_fill

    def write_section_banner(ws, row, text, col_count):
        """Write a dark-blue section header spanning col_count columns."""
        cell = ws.cell(row=row, column=1, value=text)
        cell.font = section_font
        cell.fill = section_fill
        cell.alignment = Alignment(vertical="center")
        for c in range(2, col_count + 1):
            ws.cell(row=row, column=c).fill = section_fill
        if col_count > 1:
            ws.merge_cells(
                start_row=row, start_column=1,
                end_row=row, end_column=col_count,
            )

    # ── Extract inventory data ────────────────────────────────────────────
    tables = inventory.get("tables", [])
    dims = inventory.get("dimensions", [])
    facts = inventory.get("facts", [])
    metrics = inventory.get("metrics", [])
    flagged = inventory.get("flagged", [])
    errors = inventory.get("errors", [])
    rels = inventory.get("relationships", [])
    complexity = inventory.get("complexity_summary", {})
    a_stats = (analysis or {}).get("stats", {})
    a_reuse = (analysis or {}).get("reuse_stats", {})

    # Backfill source_view for inventories generated before the field existed.
    _table_to_view = {t["name"]: t["view_name"] for t in tables if t.get("view_name")}
    if _table_to_view:
        for item_list in (dims, facts, metrics):
            for item in item_list:
                if not item.get("source_view"):
                    item["source_view"] = _table_to_view.get(item.get("table", ""), "")

    # ── Build conflict group lookup from analysis ─────────────────────────
    _KIND_TAB = {"dimension": "dimensions", "fact": "facts", "metric": "metrics"}
    conflict_lookup: dict[tuple, str] = {}  # (kind, table, name) → group_id
    a_groups_all = (analysis or {}).get("parallel_definitions", [])
    for g in a_groups_all:
        for item in g.get("items", []):
            if _table_to_view and not item.get("source_view"):
                item["source_view"] = _table_to_view.get(item.get("table", ""), "")
        if not g.get("has_calculation") or g.get("cross_kind"):
            continue
        gid = g["group_id"]
        for item in g.get("items", []):
            key = (item.get("kind", ""), item.get("table", ""), item.get("name", ""))
            conflict_lookup[key] = gid

    actionable_groups = [
        g for g in a_groups_all
        if g.get("has_calculation") and not g.get("cross_kind")
    ]
    actionable_groups.sort(
        key=lambda g: ", ".join(
            dict.fromkeys(it.get("name", "") for it in g.get("items", []))
        ).lower()
    )

    def _default_include(
        complexity_val: str,
        has_conflict: bool = False,
        is_active: bool = False,
    ) -> str:
        """Determine default Include in SV? value.

        Active = appears on at least one dashboard page.
        Model-only fields default to Review so the customer decides.
        """
        if has_conflict:
            return "Review"
        if complexity_val == "simple":
            return "Yes" if is_active else "Review"
        elif complexity_val == "needs_translation":
            return "Review"
        return "No"

    # ══════════════════════════════════════════════════════════════════════
    # TAB 1: SUMMARY
    # ══════════════════════════════════════════════════════════════════════
    ws_sum = wb.active
    ws_sum.title = "Summary"
    ws_sum.sheet_properties.tabColor = _ACCENT

    # ── Readiness score (from Report Intelligence narrative) ──────────
    from .analysis import analyze_report_narrative
    narr = analyze_report_narrative(inventory, analysis)
    reuse_info = narr.get("reuse_distribution", {})
    roadmap = narr.get("semantic_model_recommendation", {})
    conflicts_narr = narr.get("conflict_coverage", {})
    pg_label = narr.get("page_label", "pages")
    avm = narr.get("active_vs_model", {})

    core_ct = reuse_info.get("core_field_count", 0)
    active_conflicts = conflicts_narr.get("active_conflicts", 0)
    total_conflicts = conflicts_narr.get("total", 0)
    conflict_ratio = conflicts_narr.get("active_conflict_ratio", 0)
    p1 = roadmap.get("priority_1", {})
    p1b = roadmap.get("priority_1_blocked", {})
    p2 = roadmap.get("priority_2", {})

    if core_ct >= 5 and conflict_ratio < 1:
        readiness_label = "High"
    elif core_ct >= 1 and conflict_ratio < 5:
        readiness_label = "Good"
    elif conflict_ratio <= 15 or (core_ct < 5 and active_conflicts > 0):
        readiness_label = "Moderate"
    else:
        readiness_label = "Low"

    # Gap statement
    p1_ct = p1.get("count", 0)
    p1b_ct = p1b.get("count", 0)
    p2_ct = p2.get("count", 0)
    if active_conflicts == 0 and p1_ct > 0:
        gap_stmt = f"{p1_ct} conflict-free core fields ready to include now."
    elif active_conflicts > 0 and p1_ct > 0:
        gap_stmt = (
            f"{p1_ct} fields ready now. Resolve {active_conflicts} "
            f"conflicts to unlock {p1b_ct + p2_ct} more."
        )
    elif active_conflicts > 0:
        gap_stmt = f"Resolve {active_conflicts} conflicts before building the semantic model."
    else:
        gap_stmt = "No core fields identified yet."

    # Left panel: summary stats
    summary_data = [
        ("Semantic Model Readiness", readiness_label),
        ("", gap_stmt),
        ("", ""),
        ("Source Type", inventory.get("source_type", "unknown")),
        ("Tables", len(tables)),
        ("Dimensions", len(dims)),
        ("Facts", len(facts)),
        ("Metrics", len(metrics)),
        ("Relationships", len(rels)),
        ("Flagged Items", len(flagged)),
        ("Parse Errors", len(errors)),
    ]

    if avm.get("total", 0) > 0:
        summary_data += [
            ("", ""),
            ("Field Utilization", ""),
            (f"  Active (on {pg_label})", f"{avm['active_count']} of {avm['total']} ({avm['active_pct']}%)"),
            ("  Model-Only", avm["model_only_count"]),
        ]

    summary_data += [
        ("", ""),
        ("Complexity Breakdown", ""),
        ("  Simple", complexity.get("simple", 0)),
        ("  Needs Translation", complexity.get("needs_translation", 0)),
        ("  Manual Required", complexity.get("manual_required", 0)),
    ]

    if a_stats:
        summary_data += [
            ("", ""),
            ("Conflicts Requiring Review", ""),
            ("  Action Items", a_stats.get("actionable_groups", 0)),
            ("    Duplicated Definitions", a_stats.get("calculated_duplicates", 0)),
            ("    Parallel Definitions", a_stats.get("calculated_parallel_defs", 0)),
            ("    Semantic Overlaps", a_stats.get("calculated_overlaps", 0)),
        ]

    if a_reuse.get("reuse_instances", 0) > 0:
        summary_data += [
            ("", ""),
            ("Definition Reuse", ""),
            ("  Total References", a_reuse["total_raw"]),
            ("  Unique Definitions", a_reuse["total_unique"]),
            ("  Reuse Instances", a_reuse["reuse_instances"]),
            ("  Reuse %", f"{a_reuse['reuse_pct']}%"),
        ]

    ws_sum.cell(row=1, column=1, value="Semantic Extraction Summary").font = Font(
        name="Inter", bold=True, color=_ACCENT, size=16
    )
    ws_sum.merge_cells("A1:B1")

    for i, (label, value) in enumerate(summary_data, start=3):
        c_label = ws_sum.cell(row=i, column=1, value=label)
        c_value = ws_sum.cell(row=i, column=2, value=value)
        c_label.font = Font(name="Inter", size=11, bold=label and not label.startswith(" "))
        if label == "Semantic Model Readiness":
            c_label.font = Font(name="Inter", bold=True, color=_ACCENT, size=14)
            c_value.font = Font(name="Inter", bold=True, size=14)
        elif isinstance(value, int) and value > 0:
            c_value.font = Font(name="Inter", size=11, bold=True)
        c_value.alignment = Alignment(horizontal="right")

    ws_sum.column_dimensions["A"].width = 30
    ws_sum.column_dimensions["B"].width = 22

    # Tab Quick Links
    link_row = len(summary_data) + 5
    ws_sum.cell(row=link_row, column=1, value="Tab Quick Links").font = Font(
        name="Inter", bold=True, color=_ACCENT, size=12
    )
    tab_names = ["Review & Curate", "DAX Translation", "Reference"]
    for i, tab in enumerate(tab_names, start=link_row + 1):
        cell = ws_sum.cell(row=i, column=1, value=tab)
        cell.font = Font(name="Inter", color=_ACCENT, underline="single", size=11)
        cell.hyperlink = f"#'{tab}'!A1"

    # ── Instructions panel (columns D-F) ─────────────────────────────
    instr_title_font = Font(name="Inter", bold=True, color=_ACCENT, size=14)
    instr_head_font = Font(name="Inter", bold=True, color="1B2A4A", size=11)
    instr_body_font = Font(name="Inter", color="333333", size=10)
    instr_wrap = Alignment(wrap_text=True, vertical="top")

    ws_sum.merge_cells("D1:F1")
    ws_sum.cell(row=1, column=4, value="How to Use This Workbook").font = instr_title_font

    instructions = [
        ("heading", "Step 1: Understand the Scope"),
        ("body", "Review the summary statistics to the left. The readiness "
                 "score and gap statement tell you how close this model is "
                 "to being ready for a Semantic View."),
        ("spacer", ""),
        ("heading", "Step 2: Resolve Conflicts & Review Flagged Items"),
        ("body", "Go to the Review & Curate tab. It shows two sections:"),
        ("body", "  \u2022 Section A: Conflicts \u2014 Set the Resolution dropdown for "
                 "each conflict (Merge, Keep All, Exclude, Defer, Review)."),
        ("body", "  \u2022 Section B: Items Needing Attention \u2014 Fields with "
                 "complex logic or conflicts that need your review."),
        ("body", "Then go to the Reference tab \u2192 Full Inventory to set "
                 "Include in SV? for each field. Dashboard-active simple fields "
                 "default to Yes; everything else defaults to Review."),
        ("spacer", ""),
        ("heading", "Step 3: Review DAX Translation Plan"),
        ("body", "The DAX Translation tab is the developer punch list. Each "
                 "row is a complex measure with an action category "
                 "(Translate to SQL, Rewrite, Manual Review, Keep in DAX), "
                 "the recommended SQL approach, and effort estimate."),
        ("body", "No dropdowns here \u2014 this tab drives implementation work."),
        ("spacer", ""),
        ("heading", "Step 4: Return This Workbook"),
        ("body", "Save and return this workbook to your Snowflake contact. "
                 "Only rows marked Include = Yes will be included in the "
                 "generated Semantic View YAML."),
        ("spacer", ""),
        ("heading", "Scope Notes"),
        ("body", "This extraction captures all semantic model definitions "
                 "(tables, columns, measures, relationships, expressions) and "
                 "which dashboard pages/visuals reference each field."),
        ("body", "It does NOT capture runtime interactive behaviors:"),
        ("body", "  \u2022 Grain-switching / drill-down \u2014 Slicers and "
                 "drill hierarchies that toggle day/week/month views. The "
                 "underlying fields are in the inventory; DAX measures using "
                 "SELECTEDVALUE/SWITCH are flagged for translation."),
        ("body", "  \u2022 Visual formatting \u2014 Conditional formatting, "
                 "color rules, data bars."),
        ("body", "  \u2022 Row-level security \u2014 RLS roles must be "
                 "re-implemented as Snowflake row access policies."),
        ("body", "Fields involved in these behaviors are present and flagged. "
                 "Re-implement the behavioral semantics in your application "
                 "layer or Cortex Analyst configuration."),
        ("spacer", ""),
        ("heading", "Reference Tab"),
        ("body", "The Reference tab contains the complete inventory of all "
                 "{n_total} fields with Include in SV? dropdowns, plus "
                 "Dashboard Pages, Tables, Relationships, Parallel "
                 "Definitions, and Errors.".format(
                     n_total=len(dims) + len(facts) + len(metrics)
                 )),
        ("body", "Use it to override auto-classifications or deep-dive "
                 "into specific tables and relationships."),
    ]

    row_i = 3
    for kind, text in instructions:
        if kind == "spacer":
            row_i += 1
            continue
        cell = ws_sum.cell(row=row_i, column=4, value=text)
        ws_sum.merge_cells(start_row=row_i, start_column=4,
                           end_row=row_i, end_column=6)
        cell.alignment = instr_wrap
        if kind == "heading":
            cell.font = instr_head_font
        else:
            cell.font = instr_body_font
        row_i += 1

    ws_sum.column_dimensions["D"].width = 20
    ws_sum.column_dimensions["E"].width = 25
    ws_sum.column_dimensions["F"].width = 25

    # ══════════════════════════════════════════════════════════════════════
    # TAB 2: REVIEW & CURATE
    # ══════════════════════════════════════════════════════════════════════
    ws_rc = wb.create_sheet("Review & Curate")
    ws_rc.sheet_properties.tabColor = "E74C3C"

    # We'll build this tab with two sections separated by a styled banner.
    # Section A: Conflicts  |  Section B: Items Needing Attention
    rc_max_cols = 12  # widest section determines merge width

    # ── Section A: Conflicts ──────────────────────────────────────────
    write_section_banner(ws_rc, 1, f"Conflicts ({len(actionable_groups)})", rc_max_cols)

    ai_headers = [
        "Name", "Category", "Items", "Sources",
        "Expr Match", "Detail", "Recommendation", "Resolution",
        "Notes", "Group ID",
    ]
    for c, h in enumerate(ai_headers, 1):
        cell = ws_rc.cell(row=2, column=c, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align

    def _group_display_name(g):
        names = list(dict.fromkeys(it.get("name", "") for it in g.get("items", [])))
        return ", ".join(names)

    def _group_sources(g):
        labels = []
        for it in g.get("items", []):
            parts = []
            sv = it.get("source_view", "")
            if sv:
                parts.append(sv)
            db = it.get("dashboard_name", "")
            pg = it.get("page_name", "")
            if db or pg:
                loc = " > ".join(filter(None, [db, pg]))
                parts.append(f"[{loc}]")
            if parts:
                labels.append(" ".join(parts))
        return ", ".join(dict.fromkeys(labels))

    def _recommend(g):
        cat = g.get("category", "")
        expr_match = g.get("expression_match", False)
        if cat == "duplicate":
            return "Merge \u2014 identical definitions, keep one"
        elif cat == "parallel_definition":
            return "Review \u2014 same name, different expressions"
        elif cat == "semantic_overlap" and expr_match:
            return "Merge \u2014 same expression, different names"
        elif cat == "semantic_overlap":
            return "Review \u2014 related definitions with different logic"
        return "Review"

    dv_resolution = DataValidation(
        type="list", formula1='"Merge,Defer,Exclude,Keep All,Review"', allow_blank=True
    )
    dv_resolution.error = "Pick Merge, Defer, Exclude, Keep All, or Review"
    dv_resolution.errorTitle = "Invalid choice"
    dv_resolution.showErrorMessage = True

    conflict_start_row = 3
    for r, g in enumerate(actionable_groups, start=conflict_start_row):
        items = g.get("items", [])
        ws_rc.cell(row=r, column=1, value=_group_display_name(g))
        ws_rc.cell(row=r, column=2, value=g.get("category", ""))
        ws_rc.cell(row=r, column=3, value=len(items))
        ws_rc.cell(row=r, column=4, value=_group_sources(g))
        ws_rc.cell(row=r, column=5, value="Yes" if g.get("expression_match") else "No")
        ws_rc.cell(row=r, column=6, value=g.get("detail", ""))
        ws_rc.cell(row=r, column=7, value=_recommend(g))
        ws_rc.cell(row=r, column=8, value="Review")
        ws_rc.cell(row=r, column=9, value="")
        ws_rc.cell(row=r, column=10, value=g["group_id"])

    conflict_end_row = conflict_start_row + len(actionable_groups) - 1
    if actionable_groups:
        ws_rc.add_data_validation(dv_resolution)
        dv_resolution.add(f"H{conflict_start_row}:H{conflict_end_row}")
        apply_banding(ws_rc, conflict_start_row, conflict_end_row, len(ai_headers))

    # ── Section B: Items Needing Attention ─────────────────────────────
    # Only items where complexity != 'simple' OR item is in a conflict group.
    # This is an advisory view — the authoritative Include in SV? dropdown
    # lives on the Reference tab → Full Inventory.
    review_items = []
    for kind_label, item_list in [
        ("dimension", dims), ("fact", facts), ("metric", metrics),
    ]:
        for item in item_list:
            tbl = item.get("table", "")
            name = item.get("name", "")
            cplx = item.get("complexity", "simple")
            gid = conflict_lookup.get((kind_label, tbl, name))
            has_conflict = gid is not None
            if cplx != "simple" or has_conflict:
                review_items.append({
                    **item,
                    "kind": kind_label,
                    "conflict_group": gid or "",
                })

    # Sort: conflict items first, then by table/name
    review_items.sort(key=lambda x: (
        0 if x["conflict_group"] else 1,
        x.get("conflict_group", ""),
        x.get("table", ""),
        x.get("name", ""),
    ))

    section_b_banner_row = conflict_end_row + 2 if actionable_groups else 3
    write_section_banner(
        ws_rc, section_b_banner_row,
        f"Items Needing Attention ({len(review_items)})",
        rc_max_cols,
    )

    ri_headers = [
        "Name", "Description", "Kind", "Complexity",
        "Dashboard", "Page/Sheet",
        "Table", "Source", "Expression",
        "Conflict Group",
    ]
    ri_header_row = section_b_banner_row + 1
    for c, h in enumerate(ri_headers, 1):
        cell = ws_rc.cell(row=ri_header_row, column=c, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align

    ri_start_row = ri_header_row + 1
    for r, item in enumerate(review_items, start=ri_start_row):
        ws_rc.cell(row=r, column=1, value=item.get("name", ""))
        ws_rc.cell(row=r, column=2, value=_best_description(item))
        ws_rc.cell(row=r, column=3, value=item.get("kind", ""))
        ws_rc.cell(row=r, column=4, value=item.get("complexity", ""))
        ws_rc.cell(row=r, column=5, value=item.get("dashboard_name", ""))
        ws_rc.cell(row=r, column=6, value=item.get("page_name", ""))
        ws_rc.cell(row=r, column=7, value=item.get("table", ""))
        ws_rc.cell(row=r, column=8, value=item.get("source_view", ""))
        ws_rc.cell(row=r, column=9, value=item.get("expr", ""))
        ws_rc.cell(row=r, column=10, value=item.get("conflict_group", ""))

    ri_end_row = ri_start_row + len(review_items) - 1
    if review_items:
        apply_banding(ws_rc, ri_start_row, ri_end_row, len(ri_headers))

    # Note at bottom pointing to Reference → Full Inventory
    note_row = ri_end_row + 2 if review_items else ri_header_row + 2
    note_cell = ws_rc.cell(
        row=note_row, column=1,
        value=(
            f"Set Include in SV? on the Reference tab \u2192 Full Inventory. "
            f"Only dashboard-active simple fields default to Yes; all other "
            f"{len(dims) + len(facts) + len(metrics)} items default to Review."
        ),
    )
    note_cell.font = Font(name="Inter", color="888888", size=10, italic=True)
    ws_rc.merge_cells(
        start_row=note_row, start_column=1,
        end_row=note_row, end_column=6,
    )

    auto_width(ws_rc, rc_max_cols)
    # Freeze below conflict section header row
    ws_rc.freeze_panes = "A3"

    # ══════════════════════════════════════════════════════════════════════
    # TAB 3: DAX TRANSLATION
    # ══════════════════════════════════════════════════════════════════════
    ws_dax = wb.create_sheet("DAX Translation")
    ws_dax.sheet_properties.tabColor = "E67E22"

    dax_headers = [
        "Action", "Measure", "Table", "Effort",
        "DAX Patterns", "SQL Recommendation", "Description",
        "Expression",
    ]
    for c, h in enumerate(dax_headers, 1):
        ws_dax.cell(row=1, column=c, value=h)
    style_header(ws_dax, len(dax_headers))

    # Sort flagged items by action category priority
    _action_sort = {
        "Auto-Convert": 0, "Translate to SQL": 1, "Rewrite": 2,
        "Manual Review": 3, "Keep in DAX": 4, "Review": 5,
    }

    sorted_flagged = []
    for f in flagged:
        rec = _safe_str(f.get("recommendation", f.get("reason", "")))
        action = _dax_action_category(rec)
        sorted_flagged.append({**f, "_action": action, "_rec": rec})
    sorted_flagged.sort(key=lambda x: (
        _action_sort.get(x["_action"], 99),
        x.get("table", ""),
        x.get("measure", x.get("column", "")),
    ))

    for r, f in enumerate(sorted_flagged, start=2):
        action = f["_action"]
        ws_dax.cell(row=r, column=1, value=action)
        ws_dax.cell(
            row=r, column=2,
            value=_safe_str(f.get("measure", f.get("column", ""))),
        )
        ws_dax.cell(row=r, column=3, value=_safe_str(f.get("table")))
        ws_dax.cell(row=r, column=4, value=_safe_str(f.get("effort")))
        ws_dax.cell(row=r, column=5, value=_safe_str(f.get("dax_patterns")))
        ws_dax.cell(row=r, column=6, value=f["_rec"])
        ws_dax.cell(row=r, column=7, value=_safe_str(
            f.get("description"), _safe_str(f.get("dax_description"))
        ))
        ws_dax.cell(
            row=r, column=8,
            value=_safe_str(f.get("expression_excerpt")),
        )

        # Color-code entire row by action category
        row_fill = _action_fill.get(action)
        if row_fill:
            for c in range(1, len(dax_headers) + 1):
                ws_dax.cell(row=r, column=c).fill = row_fill

    auto_width(ws_dax, len(dax_headers))
    ws_dax.freeze_panes = "A2"

    # ══════════════════════════════════════════════════════════════════════
    # TAB 4: DASHBOARD INVENTORY & OVERLAP
    # ══════════════════════════════════════════════════════════════════════
    page_profiles = narr.get("page_profiles", [])
    dash_cov = narr.get("dashboard_coverage", {})
    dash_list = dash_cov.get("dashboards", [])
    sim_matrix = dash_cov.get("similarity_matrix", [])
    consol_opps = narr.get("consolidation_opportunities", [])

    if page_profiles and len(page_profiles) > 1:
        ws_dash = wb.create_sheet("Dashboard Inventory & Overlap")
        ws_dash.sheet_properties.tabColor = "8E44AD"

        dash_col_count = 10
        d_row = 1

        # ── Sub-section: Page Inventory ──────────────────────────────
        write_section_banner(
            ws_dash, d_row,
            f"Page Inventory ({len(page_profiles)} pages)",
            dash_col_count,
        )
        d_row += 1

        pg_inv_headers = [
            "Page / Sheet", "Inferred Purpose", "Total Fields",
            "Facts", "Metrics", "Tables", "Widgets",
        ]
        for c, h in enumerate(pg_inv_headers, 1):
            cell = ws_dash.cell(row=d_row, column=c, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
        pg_inv_header_row = d_row
        d_row += 1
        pg_inv_start = d_row

        for pp in page_profiles:
            ws_dash.cell(row=d_row, column=1, value=pp["name"])
            ws_dash.cell(row=d_row, column=2, value=pp["purpose"])
            ws_dash.cell(row=d_row, column=3, value=pp["total_fields"])
            ws_dash.cell(row=d_row, column=4, value=pp["fact_count"])
            ws_dash.cell(row=d_row, column=5, value=pp["metric_count"])
            ws_dash.cell(row=d_row, column=6, value=pp["table_count"])
            ws_dash.cell(
                row=d_row, column=7,
                value=", ".join(pp.get("widgets", []))[:200],
            )
            d_row += 1

        apply_banding(ws_dash, pg_inv_start, d_row - 1, len(pg_inv_headers))

        # ── Sub-section: Per-Page Field Overlap ─────────────────────
        if dash_list and len(dash_list) > 1:
            d_row += 2
            write_section_banner(
                ws_dash, d_row,
                f"Per-Page Field Overlap ({len(dash_list)} pages)",
                dash_col_count,
            )
            d_row += 1

            ol_headers = [
                "Page / Sheet", "Total Fields",
                "Unique to This Page", "Shared with Other Pages",
                "Shared %", "Dimensions", "Facts", "Metrics",
            ]
            for c, h in enumerate(ol_headers, 1):
                cell = ws_dash.cell(row=d_row, column=c, value=h)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = header_align
            d_row += 1
            ol_start = d_row

            for d in dash_list:
                total = d["total_fields"]
                shared = d["shared_fields"]
                shared_pct = round(shared / total * 100, 1) if total > 0 else 0
                ws_dash.cell(row=d_row, column=1, value=d["name"])
                ws_dash.cell(row=d_row, column=2, value=total)
                ws_dash.cell(row=d_row, column=3, value=d["unique_fields"])
                ws_dash.cell(row=d_row, column=4, value=shared)
                ws_dash.cell(row=d_row, column=5, value=f"{shared_pct}%")
                ws_dash.cell(row=d_row, column=6, value=d["dimensions"])
                ws_dash.cell(row=d_row, column=7, value=d["facts"])
                ws_dash.cell(row=d_row, column=8, value=d["metrics"])
                d_row += 1

            apply_banding(ws_dash, ol_start, d_row - 1, len(ol_headers))

        # ── Sub-section: Pairwise Similarity ────────────────────────
        # Filter to pairs with meaningful overlap (Jaccard > 0)
        meaningful_pairs = [
            p for p in sim_matrix
            if p["jaccard"] > 0 and "(model-only)" not in (p["dashboard_a"], p["dashboard_b"])
        ]
        meaningful_pairs.sort(key=lambda p: -p["jaccard"])

        if meaningful_pairs:
            d_row += 2
            write_section_banner(
                ws_dash, d_row,
                f"Pairwise Similarity ({len(meaningful_pairs)} pairs)",
                dash_col_count,
            )
            d_row += 1

            sim_headers = [
                "Page A", "Page B", "Similarity %",
                "Shared Fields", "Consolidation Candidate?",
                "Top Shared Fields",
            ]
            for c, h in enumerate(sim_headers, 1):
                cell = ws_dash.cell(row=d_row, column=c, value=h)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = header_align
            d_row += 1
            sim_start = d_row

            # Build a set of consolidation pairs for quick lookup
            consol_set = set()
            for co in consol_opps:
                consol_set.add((co["page_a"], co["page_b"]))
                consol_set.add((co["page_b"], co["page_a"]))

            high_fill = PatternFill(
                start_color="FADBD8", end_color="FADBD8", fill_type="solid"
            )
            med_fill = PatternFill(
                start_color="FDEBD0", end_color="FDEBD0", fill_type="solid"
            )

            for pair in meaningful_pairs:
                pct = round(pair["jaccard"] * 100, 1)
                is_consol = (pair["dashboard_a"], pair["dashboard_b"]) in consol_set
                ws_dash.cell(row=d_row, column=1, value=pair["dashboard_a"])
                ws_dash.cell(row=d_row, column=2, value=pair["dashboard_b"])
                ws_dash.cell(row=d_row, column=3, value=f"{pct}%")
                ws_dash.cell(row=d_row, column=4, value=pair["shared_count"])
                ws_dash.cell(
                    row=d_row, column=5,
                    value="Yes" if is_consol else "",
                )
                # Show up to 10 shared field names (strip table prefix)
                shared_short = [
                    f.split(".")[-1] if "." in f else f
                    for f in pair.get("shared_fields", [])[:10]
                ]
                ws_dash.cell(
                    row=d_row, column=6,
                    value=", ".join(shared_short),
                )

                # Highlight high-overlap rows
                if pct >= 80:
                    for c in range(1, len(sim_headers) + 1):
                        ws_dash.cell(row=d_row, column=c).fill = high_fill
                elif pct >= 50:
                    for c in range(1, len(sim_headers) + 1):
                        ws_dash.cell(row=d_row, column=c).fill = med_fill

                d_row += 1

            if d_row > sim_start:
                apply_banding(ws_dash, sim_start, d_row - 1, len(sim_headers))

        # Freeze and auto-width
        ws_dash.freeze_panes = f"A{pg_inv_header_row + 1}"
        auto_width(ws_dash, dash_col_count)

    # ══════════════════════════════════════════════════════════════════════
    # TAB 5: REFERENCE
    # ══════════════════════════════════════════════════════════════════════
    ws_ref = wb.create_sheet("Reference")
    ws_ref.sheet_properties.tabColor = "1ABC9C"

    ref_col_count = 13  # widest sub-section (Full Inventory)
    row_i = 1

    # ── Sub-section: Full Inventory ──────────────────────────────────
    all_items_flat = []
    for kind_label, item_list in [
        ("dimension", dims), ("fact", facts), ("metric", metrics),
    ]:
        for item in item_list:
            tbl = item.get("table", "")
            name = item.get("name", "")
            gid = conflict_lookup.get((kind_label, tbl, name))
            has_conflict = gid is not None
            is_active = bool(item.get("page_name"))
            all_items_flat.append({
                **item,
                "kind": kind_label,
                "conflict_group": gid or "",
                "default_include": _default_include(
                    item.get("complexity", "simple"), has_conflict, is_active
                ),
            })

    # Sort: conflict items first, then by table/name
    all_items_flat.sort(key=lambda x: (
        0 if x["conflict_group"] else 1,
        x.get("kind", ""),
        x.get("table", ""),
        x.get("name", ""),
    ))

    write_section_banner(
        ws_ref, row_i,
        f"Full Inventory ({len(all_items_flat)} items)",
        ref_col_count,
    )
    row_i += 1

    # Column order follows data lineage:
    # What is it → Where is it used → Where does it come from → Status
    inv_headers = [
        "Name", "Description", "Kind",
        "Dashboard", "Page/Sheet", "Widget",
        "Table", "Source", "Expression", "Data Type",
        "Conflict Group", "Complexity", "Include in SV?",
    ]
    for c, h in enumerate(inv_headers, 1):
        cell = ws_ref.cell(row=row_i, column=c, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align

    inv_header_row = row_i
    row_i += 1
    inv_start = row_i

    dv_include_ref = DataValidation(
        type="list", formula1='"Yes,No,Review"', allow_blank=True
    )

    for item in all_items_flat:
        ws_ref.cell(row=row_i, column=1, value=item.get("name", ""))
        ws_ref.cell(row=row_i, column=2, value=_best_description(item))
        ws_ref.cell(row=row_i, column=3, value=item.get("kind", ""))
        ws_ref.cell(row=row_i, column=4, value=item.get("dashboard_name", ""))
        ws_ref.cell(row=row_i, column=5, value=item.get("page_name", ""))
        ws_ref.cell(row=row_i, column=6, value=item.get("widget_name", ""))
        ws_ref.cell(row=row_i, column=7, value=item.get("table", ""))
        ws_ref.cell(row=row_i, column=8, value=item.get("source_view", ""))
        ws_ref.cell(row=row_i, column=9, value=item.get("expr", ""))
        ws_ref.cell(row=row_i, column=10, value=item.get("data_type", ""))
        ws_ref.cell(row=row_i, column=11, value=item.get("conflict_group", ""))
        ws_ref.cell(row=row_i, column=12, value=item.get("complexity", "simple"))
        ws_ref.cell(row=row_i, column=13, value=item.get("default_include", "Review"))
        row_i += 1

    inv_end = row_i - 1
    if all_items_flat:
        ws_ref.add_data_validation(dv_include_ref)
        dv_include_ref.add(f"M{inv_start}:M{inv_end}")
        apply_banding(ws_ref, inv_start, inv_end, len(inv_headers))

    ws_ref.freeze_panes = f"A{inv_header_row + 1}"
    ws_ref.auto_filter.ref = (
        f"A{inv_header_row}:{get_column_letter(len(inv_headers))}{inv_header_row}"
    )

    # ── Sub-section: Dashboard Pages ─────────────────────────────────
    page_profiles = narr.get("page_profiles", [])
    row_i += 2  # gap
    write_section_banner(
        ws_ref, row_i,
        f"Dashboard Pages ({len(page_profiles)})",
        ref_col_count,
    )
    row_i += 1

    pg_headers = ["Page", "Purpose", "Fields", "Facts", "Metrics", "Tables"]
    for c, h in enumerate(pg_headers, 1):
        cell = ws_ref.cell(row=row_i, column=c, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
    pg_header_row = row_i
    row_i += 1
    pg_start = row_i

    for p in page_profiles:
        ws_ref.cell(row=row_i, column=1, value=p["name"])
        ws_ref.cell(row=row_i, column=2, value=p["purpose"])
        ws_ref.cell(row=row_i, column=3, value=p["total_fields"])
        ws_ref.cell(row=row_i, column=4, value=p["fact_count"])
        ws_ref.cell(row=row_i, column=5, value=p["metric_count"])
        ws_ref.cell(row=row_i, column=6, value=p["table_count"])
        row_i += 1

    if page_profiles:
        apply_banding(ws_ref, pg_start, row_i - 1, len(pg_headers))

    # ── Sub-section: Tables ──────────────────────────────────────────
    row_i += 2
    write_section_banner(ws_ref, row_i, f"Tables ({len(tables)})", ref_col_count)
    row_i += 1

    tbl_headers = ["Name", "Physical Table", "Dimensions", "Facts", "Metrics", "Total"]
    for c, h in enumerate(tbl_headers, 1):
        cell = ws_ref.cell(row=row_i, column=c, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
    row_i += 1
    tbl_start = row_i

    # Count items per table
    tbl_counts: dict[str, dict[str, int]] = {}
    for t in tables:
        tbl_counts[t.get("name", "")] = {"dims": 0, "facts": 0, "metrics": 0}
    for item in dims:
        tn = item.get("table", "")
        if tn not in tbl_counts:
            tbl_counts[tn] = {"dims": 0, "facts": 0, "metrics": 0}
        tbl_counts[tn]["dims"] += 1
    for item in facts:
        tn = item.get("table", "")
        if tn not in tbl_counts:
            tbl_counts[tn] = {"dims": 0, "facts": 0, "metrics": 0}
        tbl_counts[tn]["facts"] += 1
    for item in metrics:
        tn = item.get("table", "")
        if tn not in tbl_counts:
            tbl_counts[tn] = {"dims": 0, "facts": 0, "metrics": 0}
        tbl_counts[tn]["metrics"] += 1

    for t in tables:
        tn = t.get("name", "")
        ct = tbl_counts.get(tn, {"dims": 0, "facts": 0, "metrics": 0})
        total = ct["dims"] + ct["facts"] + ct["metrics"]
        ws_ref.cell(row=row_i, column=1, value=tn)
        ws_ref.cell(row=row_i, column=2, value=t.get("physical_table", ""))
        ws_ref.cell(row=row_i, column=3, value=ct["dims"])
        ws_ref.cell(row=row_i, column=4, value=ct["facts"])
        ws_ref.cell(row=row_i, column=5, value=ct["metrics"])
        ws_ref.cell(row=row_i, column=6, value=total)
        row_i += 1

    if tables:
        apply_banding(ws_ref, tbl_start, row_i - 1, len(tbl_headers))

    # ── Sub-section: Relationships ───────────────────────────────────
    row_i += 2
    write_section_banner(ws_ref, row_i, f"Relationships ({len(rels)})", ref_col_count)
    row_i += 1

    rel_headers = ["Left Table", "Left Column", "Right Table", "Right Column", "Type", "Active"]
    for c, h in enumerate(rel_headers, 1):
        cell = ws_ref.cell(row=row_i, column=c, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
    row_i += 1
    rel_start = row_i

    for rel in rels:
        ws_ref.cell(row=row_i, column=1, value=rel.get("left_table", ""))
        ws_ref.cell(row=row_i, column=2, value=rel.get("left_column", ""))
        ws_ref.cell(row=row_i, column=3, value=rel.get("right_table", ""))
        ws_ref.cell(row=row_i, column=4, value=rel.get("right_column", ""))
        ws_ref.cell(row=row_i, column=5, value=rel.get("relationship_type", ""))
        ws_ref.cell(row=row_i, column=6, value="Yes" if rel.get("is_active", True) else "No")
        row_i += 1

    if rels:
        apply_banding(ws_ref, rel_start, row_i - 1, len(rel_headers))

    # ── Sub-section: Parallel Definitions ────────────────────────────
    a_groups = [
        g for g in a_groups_all
        if not g.get("cross_kind")
    ]
    if a_groups:
        row_i += 2
        write_section_banner(
            ws_ref, row_i,
            f"Parallel Definitions ({len(a_groups)} groups)",
            ref_col_count,
        )
        row_i += 1

        pd_headers = [
            "Group", "Category", "Actionable", "Kind", "Table", "Name",
            "Expression", "Name Similarity", "Expr Match", "Detail",
            "Resolution", "Keep?",
        ]
        for c, h in enumerate(pd_headers, 1):
            cell = ws_ref.cell(row=row_i, column=c, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
        row_i += 1

        group_fill_pd = PatternFill(
            start_color="1E3A5F", end_color="1E3A5F", fill_type="solid"
        )
        group_font_pd = Font(name="Inter", bold=True, color="FFFFFF", size=11)
        group_font_accent_pd = Font(name="Inter", bold=True, color=_ACCENT, size=11)

        dv_resolve = DataValidation(
            type="list", formula1='"Merge,Defer,Exclude"', allow_blank=True
        )
        dv_keep = DataValidation(
            type="list", formula1='"Yes,No"', allow_blank=True
        )
        pd_start = row_i

        for g in a_groups:
            actionable_label = "Yes" if g.get("has_calculation") else "No"
            items = g.get("items", [])

            # Group header row
            ws_ref.cell(row=row_i, column=1, value=g["group_id"]).font = group_font_accent_pd
            ws_ref.cell(row=row_i, column=2, value=g["category"]).font = group_font_pd
            ws_ref.cell(row=row_i, column=3, value=actionable_label).font = group_font_pd
            ws_ref.cell(row=row_i, column=6, value=f"{len(items)} items").font = group_font_pd
            ws_ref.cell(row=row_i, column=8, value=g["name_similarity"]).font = group_font_pd
            ws_ref.cell(
                row=row_i, column=9,
                value="Yes" if g["expression_match"] else "No",
            ).font = group_font_pd
            ws_ref.cell(row=row_i, column=10, value=g["detail"]).font = group_font_pd
            for col in range(1, len(pd_headers) + 1):
                ws_ref.cell(row=row_i, column=col).fill = group_fill_pd
            row_i += 1

            for item in items:
                ws_ref.cell(row=row_i, column=4, value=item.get("kind", ""))
                ws_ref.cell(row=row_i, column=5, value=item.get("table", ""))
                ws_ref.cell(row=row_i, column=6, value=item.get("name", ""))
                ws_ref.cell(row=row_i, column=7, value=item.get("expr", ""))
                row_i += 1

        pd_end = row_i - 1
        if pd_end >= pd_start:
            ws_ref.add_data_validation(dv_resolve)
            dv_resolve.add(f"K{pd_start}:K{pd_end}")
            ws_ref.add_data_validation(dv_keep)
            dv_keep.add(f"L{pd_start}:L{pd_end}")

    # ── Sub-section: Errors ──────────────────────────────────────────
    if errors:
        row_i += 2
        write_section_banner(
            ws_ref, row_i, f"Errors ({len(errors)})", ref_col_count
        )
        row_i += 1

        err_headers = ["File", "Error Type", "Message"]
        for c, h in enumerate(err_headers, 1):
            cell = ws_ref.cell(row=row_i, column=c, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
        row_i += 1
        err_start = row_i

        for e in errors:
            if isinstance(e, dict):
                ws_ref.cell(row=row_i, column=1, value=_safe_str(e.get("file")))
                ws_ref.cell(row=row_i, column=2, value=_safe_str(e.get("error_type")))
                ws_ref.cell(
                    row=row_i, column=3,
                    value=_safe_str(e.get("error_message", e.get("error", "")))[:500],
                )
            else:
                ws_ref.cell(row=row_i, column=3, value=str(e)[:500])
            row_i += 1

        apply_banding(ws_ref, err_start, row_i - 1, len(err_headers))

    auto_width(ws_ref, ref_col_count)

    # ══════════════════════════════════════════════════════════════════════
    # Tab 6: Verified Queries (optional)
    # ══════════════════════════════════════════════════════════════════════
    if verified_queries:
        ws_vq = wb.create_sheet("Verified Queries")
        vq_headers = ["Dashboard", "Page", "Name", "Question", "SQL", "Tables",
                       "Verified At", "Verified By", "Onboarding"]
        for ci, h in enumerate(vq_headers, 1):
            ws_vq.cell(row=1, column=ci, value=h)
        style_header(ws_vq, len(vq_headers))

        for ri, vq in enumerate(verified_queries, 2):
            ws_vq.cell(row=ri, column=1, value=_safe_str(vq.get("dashboard_name", "")))
            ws_vq.cell(row=ri, column=2, value=_safe_str(vq.get("page_name", "")))
            ws_vq.cell(row=ri, column=3, value=_safe_str(vq.get("name", "")))
            ws_vq.cell(row=ri, column=4, value=_safe_str(vq.get("question", "")))
            ws_vq.cell(row=ri, column=5, value=_safe_str(vq.get("sql", "")))
            tables_str = ", ".join(vq.get("tables", []))
            ws_vq.cell(row=ri, column=6, value=tables_str)
            ws_vq.cell(row=ri, column=7, value=_safe_str(vq.get("verified_at", "")))
            ws_vq.cell(row=ri, column=8, value=_safe_str(vq.get("verified_by", "")))
            ws_vq.cell(row=ri, column=9,
                       value="Yes" if vq.get("use_as_onboarding_question") else "No")

        apply_banding(ws_vq, 2, len(verified_queries) + 1, len(vq_headers))
        auto_width(ws_vq, len(vq_headers))

    # ── Save ─────────────────────────────────────────────────────────────
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    wb.save(output_path)
    log.info("Excel workbook written to %s", output_path)

    return output_path
