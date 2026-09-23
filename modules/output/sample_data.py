"""Sample-data (seed) SQL generator.

Reads a unified inventory and produces Snowflake CTAS statements that
create tables populated with synthetic data using native Snowflake
generation functions (GENERATOR, UNIFORM, RANDSTR, SEQ8, etc.).

No data leaves Snowflake. No LLM calls. All SQL-native.
"""

import re
from typing import Any

from ..common.logger import get_logger

log = get_logger("sample_data")

# ---------------------------------------------------------------------------
# Keyword → value domain mappings for realistic VARCHAR generation
# ---------------------------------------------------------------------------

_VALUE_DOMAINS: dict[str, list[str]] = {
    # --- General ---
    "status": ["Active", "Inactive", "Pending", "Completed", "Cancelled"],
    "priority": ["Low", "Medium", "High", "Critical"],
    "type": ["Type A", "Type B", "Type C", "Type D"],
    "category": ["Category 1", "Category 2", "Category 3", "Category 4"],
    "region": ["North", "South", "East", "West", "Central"],
    "state": ["CA", "TX", "NY", "FL", "IL", "PA", "OH", "GA", "NC", "MI"],
    "country": ["US", "CA", "UK", "AU", "DE", "FR", "JP", "IN", "BR", "MX"],
    "department": ["Operations", "Finance", "Engineering", "Sales", "HR", "Marketing"],
    "gender": ["Male", "Female", "Non-Binary", "Prefer Not to Say"],
    "color": ["Red", "Blue", "Green", "Yellow", "Orange", "Purple"],
    "channel": ["Online", "In-Store", "Phone", "Email", "Mobile App"],
    "payment": ["Credit Card", "Debit Card", "Cash", "Wire Transfer", "ACH"],
    "yes_no": ["Yes", "No"],
    # --- Names ---
    "first_name": ["James", "Mary", "Robert", "Patricia", "John", "Jennifer",
                    "Michael", "Linda", "David", "Elizabeth", "Sarah", "Daniel",
                    "Laura", "Carlos", "Priya", "Wei"],
    "last_name": ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia",
                   "Miller", "Davis", "Rodriguez", "Martinez", "Anderson",
                   "Taylor", "Thomas", "Wilson", "Moore", "Lee"],
    "city": ["New York", "Los Angeles", "Chicago", "Houston", "Phoenix",
             "Philadelphia", "San Antonio", "San Diego", "Dallas", "Austin"],
    # --- Healthcare (detected from fulfillment-style inventories) ---
    "discipline": ["Nursing", "Physical Therapy", "Respiratory Therapy",
                    "Occupational Therapy", "Speech Therapy", "Lab",
                    "Radiology", "Pharmacy", "Social Work"],
    "specialty": ["ICU", "ER", "Med-Surg", "Pediatrics", "Oncology",
                   "Cardiology", "Orthopedics", "Neurology", "OB/GYN"],
    "shift": ["Day", "Night", "Swing", "Weekend Day", "Weekend Night"],
    "setting": ["Inpatient", "Outpatient", "ED", "OR", "ICU", "Ambulatory"],
    "hotel": ["Hilton Garden Inn", "Marriott Courtyard", "Hampton Inn",
              "Holiday Inn Express", "Residence Inn", "Comfort Suites"],
    "supplier": ["AMN Healthcare", "Cross Country", "Aya Healthcare",
                  "Medical Solutions", "Trustaff", "FlexCare"],
    "hub": ["Central Hub", "North Hub", "South Hub", "East Hub", "West Hub"],
    "approval": ["Approved", "Pending", "Rejected", "Under Review"],
    "union": ["Union A", "Union B", "Union C", "Non-Union"],
    # --- Finance ---
    "currency": ["USD", "EUR", "GBP", "JPY", "CAD", "AUD"],
    "quarter": ["Q1", "Q2", "Q3", "Q4"],
    # --- Retail ---
    "size": ["XS", "S", "M", "L", "XL", "XXL"],
    "brand": ["Brand A", "Brand B", "Brand C", "Brand D", "Brand E"],
}

# Keywords that match each domain. Checked against column name + description.
_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "status": ["status", "state", "condition"],
    "priority": ["priority", "urgency", "severity"],
    "type": ["type", "kind"],
    "category": ["category", "class", "group", "segment"],
    "region": ["region"],
    "state": ["state", "province"],
    "country": ["country", "nation"],
    "department": ["department", "dept", "division"],
    "gender": ["gender", "sex"],
    "color": ["color", "colour"],
    "channel": ["channel", "source_channel"],
    "payment": ["payment", "pay_method", "payment_method", "paymethod"],
    "yes_no": ["flag", "is_", "has_"],
    "first_name": ["first_name", "firstname", "fname", "given_name",
                    "clinicianfirstname", "emp_first_name"],
    "last_name": ["last_name", "lastname", "lname", "surname", "family_name",
                   "clinicianlastname", "emp_last_name"],
    "city": ["city", "town", "municipality"],
    "discipline": ["discipline"],
    "specialty": ["specialty", "speciality"],
    "shift": ["shift", "shifttype", "shift_type"],
    "setting": ["setting", "care_setting"],
    "hotel": ["hotel", "hotelname", "hotel_name"],
    "supplier": ["supplier", "vendor", "suppliername", "supplier_name"],
    "hub": ["hub", "hubname", "hub_name"],
    "approval": ["approval", "approval_status", "approvalstatus"],
    "union": ["union", "unionname", "union_name"],
    "currency": ["currency", "currencycode", "currency_code"],
    "quarter": ["quarter", "fiscal_quarter"],
    "size": ["size", "product_size"],
    "brand": ["brand", "brand_name", "brandname"],
}

# Time-related keywords for DATE/TIMESTAMP inference
_TIME_KEYWORDS = {
    "DATE", "MONTH", "QUARTER", "YEAR", "WEEK", "DAY", "PERIOD", "TIME",
    "DATETIME", "TIMESTAMP", "CREATED", "UPDATED", "MODIFIED", "START",
    "END", "DUE", "EXPIRE", "BIRTH", "HIRED", "TERMINATED", "CHECKIN",
    "CHECKOUT",
}

# ID-related patterns
_ID_RE = re.compile(r'(?:^|_)(ID|KEY|CODE|NUM|NUMBER)$', re.IGNORECASE)
_PK_RE = re.compile(r'(?:^|_)ID$', re.IGNORECASE)

# TABLE.COLUMN reference regex (reused from analysis.py pattern)
_COL_REF_RE = re.compile(r'\b([A-Za-z_][A-Za-z0-9_ ]*)\.([A-Za-z_][A-Za-z0-9_]*)\b')

# Address-related keywords
_ADDRESS_KEYWORDS = {"ADDRESS", "ADDR", "STREET", "ZIP", "ZIPCODE",
                     "POSTAL", "ZIP_CODE"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def assess_inventory(inventory: dict) -> dict:
    """Assess inventory completeness for seed-data generation.

    Returns a dict with per-page breakdown, per-table column maps,
    and completeness information.
    """
    source_type = inventory.get("source_type", "unknown")
    sf = inventory.get("snowflake_target", {})
    db = sf.get("database", "TARGET_DB")
    schema = sf.get("schema", "PUBLIC")

    # Collect all items with their page assignments
    all_items = []
    for category in ("dimensions", "facts", "metrics"):
        for item in inventory.get(category, []):
            all_items.append({**item, "_category": category})

    # Extract distinct pages
    pages: dict[str, dict] = {}
    for item in all_items:
        page_str = item.get("page_name", "")
        if page_str:
            for page in page_str.split(", "):
                page = page.strip()
                if page:
                    pages.setdefault(page, {"items": 0, "tables": set(), "columns": {}})
                    pages[page]["items"] += 1
                    tbl = item.get("table", "").upper()
                    if tbl:
                        pages[page]["tables"].add(tbl)
                        pages[page]["columns"].setdefault(tbl, set()).add(item["name"])

    # Build per-table column map (from ALL items, not just page-assigned)
    table_columns: dict[str, dict[str, dict]] = {}  # table -> {col_name -> {data_type, is_pk, ...}}
    for item in all_items:
        tbl = item.get("table", "").upper()
        if not tbl:
            continue
        table_columns.setdefault(tbl, {})
        col_name = item["name"]
        data_type = item.get("data_type", "VARCHAR")
        table_columns[tbl][col_name] = {
            "name": col_name,
            "data_type": data_type,
            "description": item.get("description", ""),
            "synonyms": item.get("synonyms", []),
            "category": item["_category"],
        }

    # Add FK/PK columns from relationships
    relationships = inventory.get("relationships", [])
    fk_map: dict[str, list[str]] = {}  # "CHILD.FK_COL" -> ["PARENT.PK_COL", ...]
    for rel in relationships:
        lt = rel.get("left_table", "").upper()
        lc = rel.get("left_column", "")
        rt = rel.get("right_table", "").upper()
        rc = rel.get("right_column", "")
        if lt and lc and rt and rc:
            fk_key = f"{lt}.{lc}"
            pk_val = f"{rt}.{rc}"
            fk_map.setdefault(fk_key, [])
            if pk_val not in fk_map[fk_key]:
                fk_map[fk_key].append(pk_val)
            # Ensure FK/PK columns exist in table_columns
            if lt in table_columns and lc not in table_columns[lt]:
                table_columns[lt][lc] = {
                    "name": lc, "data_type": "NUMBER",
                    "description": "", "synonyms": [], "category": "fk",
                }
            if rt in table_columns and rc not in table_columns[rt]:
                table_columns[rt][rc] = {
                    "name": rc, "data_type": "NUMBER",
                    "description": "", "synonyms": [], "category": "pk",
                }

    # Identify PK candidates per table
    pk_map: dict[str, str | None] = {}
    for tbl in table_columns:
        pk_map[tbl] = _find_pk(tbl, table_columns[tbl])

    # Count items with vs without page assignments
    items_with_pages = sum(1 for i in all_items if i.get("page_name", ""))
    items_without_pages = len(all_items) - items_with_pages

    # Build page summary for display
    page_summary = []
    for pname in sorted(pages, key=lambda p: -pages[p]["items"]):
        pinfo = pages[pname]
        total_cols = sum(len(v) for v in pinfo["columns"].values())
        page_summary.append({
            "page": pname,
            "items": pinfo["items"],
            "tables": len(pinfo["tables"]),
            "columns": total_cols,
        })

    return {
        "source_type": source_type,
        "database": db,
        "schema": schema,
        "total_tables": len(table_columns),
        "total_columns": sum(len(v) for v in table_columns.values()),
        "items_with_pages": items_with_pages,
        "items_without_pages": items_without_pages,
        "pages": page_summary,
        "table_columns": table_columns,
        "pk_map": pk_map,
        "fk_map": fk_map,
        "relationships": relationships,
    }


def generate_seed_sql(
    inventory: dict,
    *,
    rows_per_table: int = 100,
    pages: list[str] | None = None,
    tables: list[str] | None = None,
    include_all: bool = False,
    include_facts: bool = False,
    fact_row_multiplier: int = 5,
) -> dict:
    """Generate CTAS statements for seed data.

    Args:
        inventory: Unified inventory dict.
        rows_per_table: Number of rows per table (dimension baseline).
        pages: Page names to scope to (only columns used on these pages).
        tables: Table names to scope to (direct filter).
        include_all: Include every column, not just page-referenced ones.
        include_facts: Include fact tables (with all columns) plus their
            FK-referenced dimension tables.  Recommended for generating
            dashboard-representative data.
        fact_row_multiplier: Fact tables get ``rows_per_table * multiplier``
            rows to simulate realistic cardinality (default 5).

    Returns:
        Dict with ``statements``, ``assessment``, ``tables_seeded``.
    """
    assessment = assess_inventory(inventory)
    db = assessment["database"]
    schema = assessment["schema"]
    table_columns = assessment["table_columns"]
    pk_map = assessment["pk_map"]
    fk_map = assessment["fk_map"]

    # --- Identify fact-like tables (name starts with FACT, or many:1 FK to dims) ---
    fact_table_names: set[str] = set()
    dim_table_names: set[str] = set()
    for tbl in table_columns:
        if _is_fact_table(tbl, fk_map):
            fact_table_names.add(tbl)
        elif not tbl.startswith(("MEASURES", "RLS", "SUM", "SYS")):
            dim_table_names.add(tbl)

    # --- Scope filtering ---
    scoped_tables: dict[str, dict[str, dict]] = {}

    if tables:
        # Direct table filter
        table_set = {t.upper() for t in tables}
        for tbl, cols in table_columns.items():
            if tbl in table_set:
                scoped_tables[tbl] = cols
    elif include_facts:
        # Include all fact tables with ALL columns, plus their FK-referenced
        # dimension tables with all columns.  This is the recommended mode
        # for generating dashboard-representative seed data.
        for tbl in fact_table_names:
            if tbl in table_columns:
                scoped_tables[tbl] = dict(table_columns[tbl])
        # Pull in dimension tables referenced by FKs from scoped facts
        _pull_fk_parents(scoped_tables, table_columns, fk_map)
        # Also pull dims that FK into already-scoped tables (reverse direction)
        _pull_fk_children(scoped_tables, table_columns, fk_map)
    elif pages:
        # Page-based scoping: only columns that appear on specified pages
        page_set = {p.strip() for p in pages}
        for item_list_name in ("dimensions", "facts", "metrics"):
            for item in inventory.get(item_list_name, []):
                page_str = item.get("page_name", "")
                if not page_str:
                    continue
                item_pages = {p.strip() for p in page_str.split(", ")}
                if item_pages & page_set:
                    tbl = item.get("table", "").upper()
                    if tbl:
                        scoped_tables.setdefault(tbl, {})
                        col_name = item["name"]
                        if col_name in table_columns.get(tbl, {}):
                            scoped_tables[tbl][col_name] = table_columns[tbl][col_name]
        # Also add FK columns needed for scoped tables
        _pull_fk_parents(scoped_tables, table_columns, fk_map)
        _pull_fk_children(scoped_tables, table_columns, fk_map)
    elif include_all:
        scoped_tables = {tbl: dict(cols) for tbl, cols in table_columns.items()}
    else:
        # Default: page-referenced items, but auto-include fact tables
        # when nothing has page assignments (common for Power BI models).
        for item_list_name in ("dimensions", "facts", "metrics"):
            for item in inventory.get(item_list_name, []):
                if not item.get("page_name", ""):
                    continue
                tbl = item.get("table", "").upper()
                if tbl:
                    scoped_tables.setdefault(tbl, {})
                    col_name = item["name"]
                    if col_name in table_columns.get(tbl, {}):
                        scoped_tables[tbl][col_name] = table_columns[tbl][col_name]

        # If default scope found nothing (no page_name on any item), fall
        # back to include_facts behaviour automatically.
        if not scoped_tables and fact_table_names:
            log.info("No page-referenced items found; auto-including fact tables.")
            for tbl in fact_table_names:
                if tbl in table_columns:
                    scoped_tables[tbl] = dict(table_columns[tbl])

        # Add FK-referenced parents and reverse-FK children
        _pull_fk_parents(scoped_tables, table_columns, fk_map)
        _pull_fk_children(scoped_tables, table_columns, fk_map)

    if not scoped_tables:
        log.warning("No tables in scope. Use --all, --include-facts, --pages, or --tables.")
        return {
            "statements": [],
            "assessment": assessment,
            "tables_seeded": [],
        }

    # Ensure every scoped table has at least a PK column
    for tbl in list(scoped_tables):
        pk = pk_map.get(tbl)
        if pk and pk not in scoped_tables[tbl] and pk in table_columns.get(tbl, {}):
            scoped_tables[tbl][pk] = table_columns[tbl][pk]

    # --- Topological sort (parent tables first for FK consistency) ---
    ordered_tables = _topological_sort(scoped_tables, fk_map)

    # --- Generate CTAS statements ---
    statements: list[str] = []
    tables_seeded: list[dict] = []
    seed_val = 42  # deterministic seed base

    # Header comment
    scope_desc = "all columns" if include_all else (
        "fact tables + FK-referenced dimensions" if include_facts else (
        f"pages: {', '.join(pages)}" if pages else (
        f"tables: {', '.join(tables)}" if tables else
        "auto-detected (fact tables + dimensions)"
    )))
    header = (
        f"-- Seed data generated from {assessment['source_type']} inventory\n"
        f"-- Scope: {scope_desc}\n"
        f"-- Tables: {len(scoped_tables)}, "
        f"Columns: {sum(len(c) for c in scoped_tables.values())}, "
        f"Rows per dim table: {rows_per_table}, "
        f"Rows per fact table: {rows_per_table * fact_row_multiplier}\n"
    )
    statements.append(header)

    for tbl in ordered_tables:
        cols = scoped_tables[tbl]
        if not cols:
            continue

        is_fact = tbl in fact_table_names
        tbl_rows = rows_per_table * fact_row_multiplier if is_fact else rows_per_table

        pk = pk_map.get(tbl)
        col_exprs: list[str] = []
        col_names_ordered: list[str] = []

        # PK first, then FKs, then rest alphabetically
        sorted_cols = _sort_columns(cols, pk, fk_map, tbl)

        for col_name, col_info in sorted_cols:
            is_pk = (col_name == pk)
            fk_refs = fk_map.get(f"{tbl}.{col_name}")
            fk_ref = fk_refs[0] if fk_refs else None
            expr = _column_to_generator_expr(
                col_name,
                col_info.get("data_type", "VARCHAR"),
                col_info.get("description", ""),
                col_info.get("synonyms", []),
                is_pk=is_pk,
                fk_ref=fk_ref,
                rows=tbl_rows,
                dim_rows=rows_per_table,
                seed=seed_val,
                is_fact_table=is_fact,
            )
            safe_name = _sanitize_col_name(col_name)
            col_exprs.append(f"  {expr} AS {safe_name}")
            col_names_ordered.append(safe_name)
            seed_val += 1

        safe_tbl = _sanitize_table_name(tbl)
        select_block = ",\n".join(col_exprs)
        ctas = (
            f"CREATE OR REPLACE TABLE {db}.{schema}.{safe_tbl} AS\n"
            f"SELECT\n{select_block}\n"
            f"FROM TABLE(GENERATOR(ROWCOUNT => {tbl_rows}));\n"
        )
        statements.append(ctas)
        tables_seeded.append({
            "name": safe_tbl,
            "original_name": tbl,
            "columns": len(cols),
            "rows": tbl_rows,
            "pk": pk,
            "is_fact": is_fact,
        })

    return {
        "statements": statements,
        "assessment": assessment,
        "tables_seeded": tables_seeded,
    }


# ---------------------------------------------------------------------------
# Column expression generator
# ---------------------------------------------------------------------------

def _column_to_generator_expr(
    col_name: str,
    data_type: str,
    description: str,
    synonyms: list[str],
    *,
    is_pk: bool = False,
    fk_ref: str | None = None,
    rows: int = 100,
    dim_rows: int = 100,
    seed: int = 42,
    is_fact_table: bool = False,
) -> str:
    """Map a column to a Snowflake GENERATOR expression.

    Uses data_type + column name keyword heuristics to choose
    appropriate value generators.

    Args:
        dim_rows: Number of rows in dimension tables (for FK MOD range).
        is_fact_table: Whether this column belongs to a fact table — enables
            measure-aware generation for numeric columns.
    """
    dt = data_type.upper()
    cn = col_name.upper()

    # --- Primary key ---
    if is_pk:
        if dt in ("NUMBER", "INT", "INTEGER", "BIGINT"):
            return "SEQ8() + 1"
        return "'PK_' || LPAD(SEQ8()::VARCHAR, 8, '0')"

    # --- Foreign key ---
    if fk_ref:
        # Reference values from parent table's PK range (dim_rows, not fact rows)
        return f"MOD(SEQ8(), {dim_rows}) + 1"

    # --- BOOLEAN ---
    if dt == "BOOLEAN":
        return f"(UNIFORM(0, 1, RANDOM({seed})) = 1)"

    # --- DATE ---
    if dt == "DATE":
        return f"DATEADD('day', -UNIFORM(0, 365, RANDOM({seed})), CURRENT_DATE())"

    # --- TIMESTAMP ---
    if dt in ("TIMESTAMP", "TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "TIMESTAMP_TZ"):
        # Smarter date generation based on column semantics
        if any(kw in cn for kw in ("CREATE", "INSERTED", "ADDED", "OPENED")):
            # Creation dates: spread over last year
            return f"DATEADD('second', -UNIFORM(0, 31536000, RANDOM({seed})), CURRENT_TIMESTAMP())"
        if any(kw in cn for kw in ("UPDATE", "MODIFIED", "CHANGED")):
            # Update dates: more recent, last 90 days
            return f"DATEADD('second', -UNIFORM(0, 7776000, RANDOM({seed})), CURRENT_TIMESTAMP())"
        if any(kw in cn for kw in ("START", "BEGIN", "FROM", "ARRIVAL", "CHECKIN",
                                    "ORIENTATION")):
            # Start dates: within last 6 months
            return f"DATEADD('day', -UNIFORM(0, 180, RANDOM({seed})), CURRENT_DATE())::TIMESTAMP_NTZ"
        if any(kw in cn for kw in ("END", "EXPIR", "COMPLET", "CHECKOUT")):
            # End dates: within next 3 months from now
            return f"DATEADD('day', UNIFORM(0, 90, RANDOM({seed})), CURRENT_DATE())::TIMESTAMP_NTZ"
        # Default timestamp: spread over last 2 years
        return f"DATEADD('second', -UNIFORM(0, 63072000, RANDOM({seed})), CURRENT_TIMESTAMP())"

    # --- NUMBER ---
    if dt in ("NUMBER", "INT", "INTEGER", "BIGINT", "FLOAT", "DOUBLE",
              "DECIMAL", "NUMERIC"):
        # ID-like columns (non-FK IDs, keys, codes)
        if _ID_RE.search(cn):
            return f"UNIFORM(1000, 99999, RANDOM({seed}))"

        # ---- Measure-aware generation for fact tables ----
        if is_fact_table:
            role = _classify_measure(cn)
            if role == "count":
                return f"UNIFORM(0, 50, RANDOM({seed}))"
            if role == "quantity":
                return f"UNIFORM(1, 200, RANDOM({seed}))"
            if role == "rate":
                return f"ROUND(UNIFORM(0::FLOAT, 1::FLOAT, RANDOM({seed})), 4)"
            if role == "percentage":
                return f"ROUND(UNIFORM(0::FLOAT, 100::FLOAT, RANDOM({seed})), 2)"
            if role == "amount":
                return f"ROUND(UNIFORM(100::FLOAT, 50000::FLOAT, RANDOM({seed})), 2)"
            if role == "score":
                return f"UNIFORM(1, 10, RANDOM({seed}))"
            if role == "rank":
                return f"UNIFORM(1, 100, RANDOM({seed}))"
            if role == "duration":
                return f"ROUND(UNIFORM(0::FLOAT, 480::FLOAT, RANDOM({seed})), 1)"
            if role == "boolean_int":
                return f"UNIFORM(0, 1, RANDOM({seed}))"

        # Count-like columns
        if any(kw in cn for kw in ("COUNT", "QTY", "QUANTITY", "TOTAL")):
            return f"UNIFORM(0, 500, RANDOM({seed}))"
        # Percentage-like
        if any(kw in cn for kw in ("PERCENT", "PCT", "RATE", "RATIO")):
            return f"ROUND(UNIFORM(0, 10000, RANDOM({seed}))::NUMBER(12,2) / 100, 2)"
        # Amount/price-like
        if any(kw in cn for kw in ("AMOUNT", "AMT", "PRICE", "COST",
                                    "REVENUE", "SALARY", "FEE", "CHARGE")):
            return f"ROUND(UNIFORM(100, 100000, RANDOM({seed}))::NUMBER(12,2) / 100, 2)"
        # Default number
        return f"UNIFORM(1, 1000, RANDOM({seed}))"

    # --- VARCHAR (default) ---
    # Check for domain match
    domain_values = _infer_domain(cn, description, synonyms)
    if domain_values:
        escaped = ", ".join(f"'{v}'" for v in domain_values)
        max_idx = len(domain_values) - 1
        return f"ARRAY_CONSTRUCT({escaped})[UNIFORM(0, {max_idx}, RANDOM({seed}))]::VARCHAR"

    # Address-like
    if any(kw in cn for kw in _ADDRESS_KEYWORDS):
        return f"UNIFORM(100, 9999, RANDOM({seed}))::VARCHAR || ' Main St'"

    # Name column (generic)
    if "NAME" in cn and "FIRST" not in cn and "LAST" not in cn:
        # Could be a descriptive name field — use table-aware label
        return f"'{col_name.replace('_', ' ').title()} ' || SEQ8()::VARCHAR"

    # Email-like
    if "EMAIL" in cn:
        return f"'user' || SEQ8()::VARCHAR || '@example.com'"

    # Phone-like
    if "PHONE" in cn or "TEL" in cn:
        return f"'555-' || LPAD(UNIFORM(1000, 9999, RANDOM({seed}))::VARCHAR, 4, '0')"

    # Description/notes
    if any(kw in cn for kw in ("DESCRIPTION", "DESC", "NOTE", "NOTES",
                                "COMMENT", "COMMENTS", "REMARKS")):
        return f"'Sample {col_name.replace('_', ' ').lower()} ' || SEQ8()::VARCHAR"

    # Default VARCHAR
    return f"'{col_name.replace('_', ' ').title()} ' || SEQ8()::VARCHAR"


def _infer_domain(col_name: str, description: str, synonyms: list[str]) -> list[str] | None:
    """Match column name/description against _VALUE_DOMAINS via keyword heuristics."""
    cn_lower = col_name.lower()
    desc_lower = (description or "").lower()
    syn_text = " ".join(s.lower() for s in (synonyms or []))
    search_text = f"{cn_lower} {desc_lower} {syn_text}"

    for domain_key, keywords in _DOMAIN_KEYWORDS.items():
        for kw in keywords:
            # Check exact column name match or substring in search text
            if kw == cn_lower or kw in search_text:
                return _VALUE_DOMAINS[domain_key]
            # Also check if keyword is a suffix/prefix pattern
            if kw.endswith("_"):
                if cn_lower.startswith(kw):
                    return _VALUE_DOMAINS[domain_key]
            elif kw.startswith("_"):
                if cn_lower.endswith(kw):
                    return _VALUE_DOMAINS[domain_key]
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Measure-classification keywords for fact-table numeric columns.
_MEASURE_PATTERNS: dict[str, list[str]] = {
    "count": ["COUNT", "CNT", "APPLIED", "SUBMITTED", "CONFIRMED",
              "CANCELED", "CANCELLED", "SCHEDULED", "COMPLETED",
              "REJECTED", "APPROVED", "ONASSIGNMENT", "GOODTOGO",
              "ORDEREDDEMAND"],
    "quantity": ["QTY", "QUANTITY", "POSITIONS", "OPENPOSITIONS",
                 "ORDERED", "NEEDED", "DEMAND", "SUPPLY", "FILLED",
                 "VACANT"],
    "rate": ["FILLRATE", "RATE", "RATIO"],
    "percentage": ["PERCENT", "PCT", "PERCENTAGE", "MAXPERCENTAGE"],
    "amount": ["AMOUNT", "AMT", "PRICE", "COST", "REVENUE", "FEE",
               "CHARGE", "PAY", "BILL", "SALARY", "WAGE", "BUDGET"],
    "score": ["SCORE", "RATING", "LEVEL"],
    "rank": ["RANK", "PRIORITY", "SEQUENCE", "ORDER_NUM"],
    "duration": ["DURATION", "HOURS", "MINUTES", "DAYS", "LENGTH",
                 "ELAPSED"],
    "boolean_int": ["IS", "HAS", "FLAG", "ISVISIBLE", "ISCONTINUING",
                    "ISRETURNING", "ISACTIVE"],
}


def _classify_measure(col_name: str) -> str | None:
    """Classify a numeric column by its measure role based on name patterns.

    Returns one of: count, quantity, rate, percentage, amount, score, rank,
    duration, boolean_int — or None if no pattern matches.
    """
    cn = col_name.upper()
    for role, keywords in _MEASURE_PATTERNS.items():
        for kw in keywords:
            if role == "boolean_int":
                # Boolean-int: exact match or prefix (IS_, HAS_)
                if cn == kw or cn.startswith(kw + "_") or cn.startswith(kw):
                    # Avoid matching ISLAND, HISTORY, etc.
                    if kw in ("IS", "HAS") and len(cn) > len(kw) and cn[len(kw)] != "_":
                        if kw == "IS" and cn.startswith("IS"):
                            return role  # ISVISIBLE, ISCONTINUING, etc.
                        continue
                    return role
            else:
                if kw in cn:
                    return role
    return None


def _is_fact_table(table_name: str, fk_map: dict[str, list[str]]) -> bool:
    """Determine if a table is a fact table based on naming or FK fan-out.

    A table is considered a fact table if:
    - Its name starts with "FACT" (convention)
    - OR it has 3+ outgoing FKs to other tables (many:1 relationships)
    """
    tn = table_name.upper()
    if tn.startswith("FACT"):
        return True
    # Count outgoing FKs (this table as the left/child side)
    fk_count = sum(1 for k in fk_map if k.startswith(f"{tn}."))
    return fk_count >= 3


def _pull_fk_parents(
    scoped_tables: dict[str, dict[str, dict]],
    table_columns: dict[str, dict[str, dict]],
    fk_map: dict[str, list[str]],
) -> None:
    """Add FK-referenced parent tables (with all their columns) into scope.

    Also ensures FK columns are present on the child tables.
    Mutates *scoped_tables* in place.
    """
    for fk_key, pk_refs in fk_map.items():
        fk_tbl, fk_col = fk_key.rsplit(".", 1)
        if fk_tbl in scoped_tables:
            # Ensure the FK column itself is included on the child
            if fk_col in table_columns.get(fk_tbl, {}):
                scoped_tables[fk_tbl][fk_col] = table_columns[fk_tbl][fk_col]
            # Pull every parent table referenced by this FK
            for pk_key in pk_refs:
                pk_tbl, pk_col = pk_key.rsplit(".", 1)
                if pk_tbl in table_columns:
                    if pk_tbl not in scoped_tables:
                        scoped_tables[pk_tbl] = dict(table_columns[pk_tbl])
                    elif pk_col in table_columns[pk_tbl]:
                        scoped_tables[pk_tbl][pk_col] = table_columns[pk_tbl][pk_col]


def _pull_fk_children(
    scoped_tables: dict[str, dict[str, dict]],
    table_columns: dict[str, dict[str, dict]],
    fk_map: dict[str, list[str]],
) -> None:
    """Add dimension tables that FK-reference already-scoped tables.

    This catches dimensions like DIM_SHIFT_SUMMARY that point INTO a fact
    table (e.g. DIM SHIFT SUMMARY.CLINICIANEVENTID → FACT PLACEMENT.CLINICIANEVENTID).
    Only pulls non-fact, non-system tables.  Mutates *scoped_tables* in place.
    """
    for fk_key, pk_refs in fk_map.items():
        fk_tbl, fk_col = fk_key.rsplit(".", 1)
        for pk_key in pk_refs:
            pk_tbl = pk_key.rsplit(".", 1)[0]
            # If the parent is scoped but the child dimension is not yet
            if (pk_tbl in scoped_tables
                    and fk_tbl not in scoped_tables
                    and fk_tbl in table_columns
                    and not fk_tbl.startswith(("MEASURES", "RLS", "SUM", "SYS", "NAN"))):
                scoped_tables[fk_tbl] = dict(table_columns[fk_tbl])

def _find_pk(table_name: str, columns: dict[str, dict]) -> str | None:
    """Find PK candidate for a table from its columns."""
    tbl_upper = table_name.upper().replace(" ", "_")
    # Explicit PK flag (from Looker primary_key: yes)
    for cname, cinfo in columns.items():
        if cinfo.get("primary_key"):
            return cname

    # Heuristic: column named <TABLE>ID or <TABLE>_ID
    tbl_stem = re.sub(r'^(DIM|FACT|VW)[\s_]*', '', tbl_upper, flags=re.IGNORECASE)
    for cname in columns:
        cn = cname.upper()
        if cn == f"{tbl_stem}ID" or cn == f"{tbl_stem}_ID":
            return cname

    # Heuristic: any column ending in ID that's a NUMBER
    for cname, cinfo in columns.items():
        cn = cname.upper()
        if cn.endswith("ID") and cinfo.get("data_type", "").upper() in ("NUMBER", "INT", "INTEGER", "BIGINT"):
            return cname

    return None


def _topological_sort(
    scoped_tables: dict[str, dict[str, dict]],
    fk_map: dict[str, list[str]],
) -> list[str]:
    """Sort tables so parents (referenced by FKs) come before children."""
    # Build dependency graph
    deps: dict[str, set[str]] = {tbl: set() for tbl in scoped_tables}
    for fk_key, pk_refs in fk_map.items():
        fk_tbl = fk_key.rsplit(".", 1)[0]
        for pk_key in pk_refs:
            pk_tbl = pk_key.rsplit(".", 1)[0]
            if fk_tbl in deps and pk_tbl in deps and fk_tbl != pk_tbl:
                deps[fk_tbl].add(pk_tbl)

    # Kahn's algorithm
    ordered: list[str] = []
    no_deps = [t for t in deps if not deps[t]]
    visited: set[str] = set()

    while no_deps:
        tbl = no_deps.pop(0)
        if tbl in visited:
            continue
        visited.add(tbl)
        ordered.append(tbl)
        for child, child_deps in deps.items():
            child_deps.discard(tbl)
            if not child_deps and child not in visited:
                no_deps.append(child)

    # Add any remaining (circular deps) at the end
    for tbl in deps:
        if tbl not in visited:
            ordered.append(tbl)

    return ordered


def _sort_columns(
    cols: dict[str, dict],
    pk: str | None,
    fk_map: dict[str, list[str]],
    table_name: str,
) -> list[tuple[str, dict]]:
    """Sort columns: PK first, then FKs, then rest alphabetically."""
    pk_cols = []
    fk_cols = []
    rest = []
    for cname, cinfo in cols.items():
        if cname == pk:
            pk_cols.append((cname, cinfo))
        elif f"{table_name}.{cname}" in fk_map:
            fk_cols.append((cname, cinfo))
        else:
            rest.append((cname, cinfo))
    return pk_cols + sorted(fk_cols) + sorted(rest)


def _sanitize_col_name(name: str) -> str:
    """Sanitize a column name for use in SQL."""
    if not name:
        return "UNKNOWN"
    name = name.strip("[]`\"' ")
    name = re.sub(r'[^A-Za-z0-9_]', '_', name)
    name = re.sub(r'_+', '_', name)
    name = name.strip('_')
    result = name.upper() or "UNKNOWN"
    # If it starts with a digit, prefix with underscore
    if result[0].isdigit():
        result = f"_{result}"
    return result


def _sanitize_table_name(name: str) -> str:
    """Sanitize a table name for use in SQL."""
    return _sanitize_col_name(name)


def format_assessment_table(assessment: dict) -> str:
    """Format assessment as a human-readable text table."""
    lines: list[str] = []
    lines.append(f"{assessment['source_type']} inventory — "
                 f"{assessment['total_tables']} tables, "
                 f"{assessment['total_columns']} total columns")
    lines.append("")

    pages = assessment.get("pages", [])
    if pages:
        lines.append(f"{'Page':<45} {'Items':>6}  {'Tables':>6}  {'Columns':>7}")
        lines.append("─" * 70)
        for p in pages:
            lines.append(f"{p['page']:<45} {p['items']:>6}  "
                         f"{p['tables']:>6}  {p['columns']:>7}")
        lines.append("")

    lines.append(f"Columns with page assignments: {assessment['items_with_pages']}")
    lines.append(f"Columns without page assignments: {assessment['items_without_pages']} "
                 f"(skipped unless --all)")
    lines.append("")
    lines.append('Use --pages "PageName" to seed specific pages.')
    lines.append("Use --all to seed every column in the model.")

    return "\n".join(lines)
