/*
 * Template for a composed React app's lib/queries.ts.
 *
 * Copy this into pipeline/app_react/lib/queries.ts and replace the dataset bodies.
 * The SHAPE is a contract, not a style preference: pipeline/verify_deployment.py
 * scans the composed file for a `sql:` property holding a template literal, and
 * pipeline/build.py's preflight requires the file to exist before a React deploy
 * phase runs. A file that holds the same SQL under bare dataset keys is
 * semantically identical and structurally wrong -- the verifier finds zero
 * datasets and the check fails.
 *
 * Two rules the verifier enforces:
 *
 *   1. Every dataset carries `sql:` with a backtick template literal.
 *   2. Every object reference resolves inside the target analytics schema. Use the
 *      ${DB}.${SCHEMA} interpolation below and never hardcode a database name. The
 *      React app runs with restricted caller's rights and its caller grants are
 *      scoped to that schema, so a dataset reading the knowledge base database
 *      works in the owner-rights Streamlit app and fails in this one. That
 *      happened: the fiscal-calendar dataset queried the KB schema inline and the
 *      fix was to put it behind a view in ANALYTICS.
 */

const DB = process.env.SNOWFLAKE_DATABASE ?? "BI2SF";
const SCHEMA = process.env.SNOWFLAKE_SCHEMA ?? "ANALYTICS";

export type Dataset = {
  /** The statement. Must be a template literal so the verifier can find it. */
  sql: string;
  /** One line on what the panel using this is for. */
  purpose: string;
};

export const QUERIES: Record<string, Dataset> = {
  kpi: {
    sql: `SELECT * FROM ${DB}.${SCHEMA}.RPT_KPI_SUMMARY`,
    purpose: "The KPI band. One row.",
  },
  period: {
    sql: `SELECT * FROM ${DB}.${SCHEMA}.RPT_SALES_BY_PERIOD
          ORDER BY FISCAL_MONTH_ID`,
    purpose: "Trend over fiscal periods, in declared order.",
  },
  product: {
    sql: `SELECT * FROM ${DB}.${SCHEMA}.RPT_SALES_BY_PRODUCT`,
    purpose: "Ranked product view.",
  },
  territory: {
    sql: `SELECT * FROM ${DB}.${SCHEMA}.RPT_SALES_BY_TERRITORY`,
    purpose: "Ranked territory view. Richest hierarchy in the reference model.",
  },
  customer: {
    sql: `SELECT * FROM ${DB}.${SCHEMA}.RPT_SALES_BY_CUSTOMER`,
    purpose: "Ranked customer view. Check null share before ranking -- see composition-rules.md.",
  },
  detail: {
    sql: `SELECT * FROM ${DB}.${SCHEMA}.RPT_SALES_DETAIL`,
    purpose: "The only frame carrying both a period and a category.",
  },
  boundaryGap: {
    sql: `SELECT * FROM ${DB}.${SCHEMA}.RPT_CALENDAR_BOUNDARY_GAP`,
    purpose: "Reconciliation check. Ties to the KPI band.",
  },
  fiscalCalendar: {
    sql: `SELECT * FROM ${DB}.${SCHEMA}.RPT_FISCAL_CALENDAR_EXPOSURE`,
    purpose: "Fiscal-vs-calendar exposure. Reads a view, never the KB database directly.",
  },
};
