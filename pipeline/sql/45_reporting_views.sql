-- =====================================================================
-- Reporting views over the semantic view
-- =====================================================================
-- These exist for one reason: SEMANTIC_VIEW() is its own query syntax, so a tool
-- that emits ordinary SQL -- "SELECT dim, SUM(measure) FROM x GROUP BY dim" --
-- cannot read a semantic view directly. That covers the React generator, and it
-- also covers MicroStrategy, Tableau and every JDBC/ODBC client the reference model already
-- owns.
--
-- The alternative would be letting each client write its own SUM() against the
-- fact tables, which is exactly the fragmentation the semantic layer exists to
-- stop: the moment two tools compute SALES_FISCAL_YTD independently, they will
-- eventually disagree, and the disagreement surfaces in a board pack rather than
-- in a test. Every view below resolves its numbers THROUGH the semantic view, so
-- there is still one definition of every metric.
--
-- Naming: RPT_ prefix, one view per reporting grain. Grain is in the name because
-- a caller who joins two of these without noticing they are at different grains
-- will double count, and the name is the last warning before that happens.
-- =====================================================================

USE SCHEMA {{DB}}.ANALYTICS;

-- Headline metrics, no dimensions. One row.
CREATE OR REPLACE VIEW RPT_KPI_SUMMARY
  COMMENT = 'Headline metrics at total grain. Resolved through {{SEMANTIC_VIEW}}.'
AS
SELECT * FROM SEMANTIC_VIEW(
  {{SEMANTIC_VIEW}}
  METRICS
    SALES_AMOUNT_TOTAL,
    BOOKED_AMOUNT_TOTAL,
    SALES_FISCAL_YTD,
    SALES_FISCAL_PYTD,
    SALES_FISCAL_YTD_GROWTH
);

-- Fiscal period trend. The grain is fiscal month, which is the grain the Cognos
-- model declares -- not calendar month, and the two do not align for the reference model.
CREATE OR REPLACE VIEW RPT_SALES_BY_PERIOD
  COMMENT = 'Sales and bookings by fiscal year/quarter/month. Resolved through {{SEMANTIC_VIEW}}.'
AS
SELECT * FROM SEMANTIC_VIEW(
  {{SEMANTIC_VIEW}}
  DIMENSIONS
    CALENDAR.FISCAL_YEAR_NAME,
    CALENDAR.FISCAL_QUARTER_YEAR,
    CALENDAR.FISCAL_MONTH_NAME,
    CALENDAR.FISCAL_MONTH_ID
  METRICS
    SALES_AMOUNT_TOTAL,
    BOOKED_AMOUNT_TOTAL
);

CREATE OR REPLACE VIEW RPT_SALES_BY_PRODUCT
  COMMENT = 'Sales and bookings by product hierarchy GPC1-GPC4. Resolved through {{SEMANTIC_VIEW}}.'
AS
SELECT * FROM SEMANTIC_VIEW(
  {{SEMANTIC_VIEW}}
  DIMENSIONS
    PRODUCT.GPC1,
    PRODUCT.GPC2,
    PRODUCT.GFP_GROUP
  METRICS
    SALES_AMOUNT_TOTAL,
    BOOKED_AMOUNT_TOTAL,
    SALES_QUANTITY_TOTAL
);

-- Territory is the dimension the row access policy filters on, so this view is
-- the one that demonstrates security: two users querying it see different rows
-- without either of them filtering.
CREATE OR REPLACE VIEW RPT_SALES_BY_TERRITORY
  COMMENT = 'Sales and bookings by territory level 1-4. Row access policy applies. Resolved through {{SEMANTIC_VIEW}}.'
AS
SELECT * FROM SEMANTIC_VIEW(
  {{SEMANTIC_VIEW}}
  DIMENSIONS
    TERRITORY.TERRITORY_LEVEL1,
    TERRITORY.TERRITORY_LEVEL2,
    TERRITORY.TERRITORY_LEVEL3,
    TERRITORY.TERRITORY_LEVEL4
  METRICS
    SALES_AMOUNT_TOTAL,
    BOOKED_AMOUNT_TOTAL
);

CREATE OR REPLACE VIEW RPT_SALES_BY_CUSTOMER
  COMMENT = 'Sales and bookings by customer and geography. Resolved through {{SEMANTIC_VIEW}}.'
AS
SELECT * FROM SEMANTIC_VIEW(
  {{SEMANTIC_VIEW}}
  DIMENSIONS
    CUSTOMERS.CUSTOMER_NAME,
    CUSTOMERS.CUSTOMER_COUNTRY,
    CUSTOMERS.CUSTOMER_TIER1,
    CUSTOMERS.STATE
  METRICS
    SALES_AMOUNT_TOTAL,
    BOOKED_AMOUNT_TOTAL
);

-- Combined grain, for cross-filtering.
--
-- The single-dimension views above are the right shape for a tool that queries one
-- axis at a time, and the wrong shape for click-to-filter: clicking a product group
-- cannot narrow a period trend if the period view carries no product column. Filters
-- applied to a frame that lacks the filtered dimension are silently ignored, which
-- looks exactly like a broken interaction.
--
-- This view carries every dimension the UI filters on, at 3,162 rows -- small enough
-- to fetch once and filter in memory, which is also why the interaction feels
-- immediate rather than issuing a query per click.
CREATE OR REPLACE VIEW RPT_SALES_DETAIL
  COMMENT = 'Sales and bookings at fiscal month x product x territory x country. Backs cross-filtering.'
AS
SELECT * FROM SEMANTIC_VIEW(
  {{SEMANTIC_VIEW}}
  DIMENSIONS
    CALENDAR.FISCAL_YEAR_NAME,
    CALENDAR.FISCAL_QUARTER_YEAR,
    CALENDAR.FISCAL_MONTH_NAME,
    CALENDAR.FISCAL_MONTH_ID,
    PRODUCT.GPC1,
    PRODUCT.GFP_GROUP,
    TERRITORY.TERRITORY_LEVEL1,
    CUSTOMERS.CUSTOMER_COUNTRY
  METRICS
    SALES_AMOUNT_TOTAL,
    BOOKED_AMOUNT_TOTAL,
    SALES_QUANTITY_TOTAL
);

-- =====================================================================
-- The calendar boundary gap, made queryable
-- =====================================================================
-- The source model filters the fact with PROCESSED_DATE <= CURRENT_DATE() and the
-- calendar with DAY_DATE < CURRENT_DATE(). Both are reproduced faithfully, which
-- means rows processed today join to no calendar row: they are counted in
-- SALES_AMOUNT_TOTAL but absent from every fiscal breakdown.
--
-- This view is not a fix. Changing either filter would alter the reference model's numbers on
-- our initiative, which is not ours to decide. It makes the discrepancy a number
-- somebody can put on a dashboard and act on, rather than a silent gap that only
-- shows up as "the total doesn't tie".
CREATE OR REPLACE VIEW RPT_CALENDAR_BOUNDARY_GAP
  COMMENT = 'Rows excluded from fiscal breakdowns by the model''s own DAY_DATE < CURRENT_DATE() calendar filter.'
AS
WITH stranded AS (
    SELECT COUNT(*) AS row_count,
           COALESCE(SUM(TRANSACTION_AMOUNT_USD), 0) AS usd
    FROM {{DB}}.SALES_ANALYTICS.FACT_SALES_SUMMARY
    WHERE PROCESSED_DATE <= CURRENT_DATE()
      AND PROCESSED_DATE NOT IN (
          SELECT DAY_DATE FROM {{DB}}.COMMON_ANALYTICS.DIM_DATE
          WHERE FISCAL_YEAR_ID >= 2018 AND DAY_DATE < CURRENT_DATE()
      )
)
SELECT
    row_count                                   AS STRANDED_ROWS,
    ROUND(usd, 2)                               AS STRANDED_USD,
    CURRENT_DATE()                              AS AS_OF_DATE,
    'FACT filter PROCESSED_DATE <= CURRENT_DATE() admits today; '
    || 'CALENDAR filter DAY_DATE < CURRENT_DATE() excludes it. '
    || 'Both reproduced from the source model.'  AS EXPLANATION
FROM stranded;


-- =====================================================================
-- Decision: these stay views. They are not materialised.
-- =====================================================================
-- Recorded here because the reasoning is not obvious and the measurements that
-- drive it are easy to re-run.
--
-- The best-practice instinct for a reporting surface read far more often than it
-- is written is an incremental materialisation -- a dynamic table. That is not
-- available:
--
--     CREATE DYNAMIC TABLE ... AS SELECT * FROM SEMANTIC_VIEW(...)
--     -> Invalid Dynamic Table definition. Semantic views are not allowed in
--        the definition of a dynamic table.
--
-- The next rung is a plain table refreshed by a task. Measured, the case for it
-- is stronger than it first looks, and the reason is the result cache rather
-- than the scan:
--
--   deterministic query, second run    ->       0 bytes,    66 ms  (cache served)
--   these views, second run            -> 7,910,912 bytes, 1,533 ms (full rescan)
--
-- The views inherit PROCESSED_DATE <= CURRENT_DATE() from the Cognos model. That
-- makes every query non-deterministic, so the result cache can never serve it and
-- each repeat read pays the full scan again. Materialising would make the query
-- deterministic and put repeat reads on the 66 ms path.
--
-- We are not doing it yet, for two reasons that are worth stating rather than
-- leaving as an omission:
--
--  * Sequencing. First paint today issues five queries against the facts, and
--    each one independently resolves the whole semantic view, so each costs about
--    the same ~7.9 MB no matter how few rows it returns:
--
--        period     7,993,856      customer  7,960,064
--        territory  7,918,080      product   7,910,912
--        gap        4,424,704      -> 36,207,616 bytes total
--
--    RPT_SALES_DETAIL carries every filterable dimension, so it replaces all four
--    aggregates for 8,133,120 bytes. Two fact queries instead of five, and
--    12,557,824 bytes instead of 36,207,616 -- a 65% reduction that adds no
--    object, no schedule, and no staleness window. That is the larger and cheaper
--    win, and it should land first so any later materialisation is measured
--    against the improved baseline rather than the old one.
--
--  * It changes what "as of" means. RPT_CALENDAR_BOUNDARY_GAP exists to measure
--    rows stranded by the CURRENT_DATE() boundary in the source model. Freezing
--    that boundary into a refreshed table would make the gap a property of the
--    last refresh rather than of the model, which is precisely the finding we are
--    reporting. Materialise the aggregates if the latency matters; leave the gap
--    view live.
--
-- To re-measure: run any RPT_ query twice, then compare BYTES_SCANNED across the
-- two runs in INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION. Equal bytes means the
-- cache is not serving it.
-- =====================================================================


-- =====================================================================
-- RPT_FISCAL_CALENDAR_EXPOSURE -- the fiscal-calendar finding, as a view
-- =====================================================================
-- The one RPT_ view that does not resolve through SEMANTIC_VIEW(...). The comparison
-- needs a date-grain cut of the facts, and no semantic metric exposes one.
--
-- It is a view rather than SQL inlined in the two apps because of a real failure, not a
-- preference. Both apps first carried this query inline. It worked in the Streamlit app,
-- which runs with owner's rights, and failed in the React app with "Could not load Fiscal
-- year to date against calendar year to date" -- that app runs with caller's rights and
-- the caller grants are scoped to {{DB}}, so it had no grant on {{KB_DB}} at all.
--
-- Three things this buys:
--   * Ownership chaining. The view body reads FACT_SALES_SUMMARY and KB_ISSUE as the view
--     owner, so a caller only needs SELECT on the view.
--   * The caller-rights grant surface stays inside {{DB}}.ANALYTICS, which is what
--     51_caller_grants.sql already assumes. Widening it to a second database to serve one
--     panel would have been the wrong trade.
--   * One definition. The apps had two copies of this SQL and would have drifted.
--
-- Note it is deliberately non-deterministic: CURRENT_DATE() in the body means the result
-- cache will never serve it. That is correct here -- the whole point is "as of today" --
-- and it costs one small query on one lazily-loaded tab.
--
-- Grant note: GRANT ... ON ALL VIEWS IN SCHEMA is point-in-time, so 51_caller_grants.sql
-- must be re-run after this view is created or the React app cannot read it.
CREATE OR REPLACE VIEW {{DB}}.ANALYTICS.RPT_FISCAL_CALENDAR_EXPOSURE
COMMENT = 'Fiscal YTD against calendar YTD, plus the metric exposure behind the finding. Exists so both apps read one definition and so the caller-rights grant surface stays inside {{DB}}.ANALYTICS -- the React app runs with caller rights and has no grant on {{KB_DB}}, so querying KB_ISSUE directly from the app failed while the owner-rights Streamlit app succeeded. Ownership chaining lets this view read across both databases.'
AS
WITH bounds AS (
  -- Derived, not hard-coded to 2026-07-01: the fiscal year runs July to June, so before
  -- July the current fiscal year began in the previous calendar year. A literal would be
  -- correct today and silently wrong next June.
  SELECT DATE_FROM_PARTS(
           YEAR(CURRENT_DATE()) - IFF(MONTH(CURRENT_DATE()) < 7, 1, 0), 7, 1
         ) AS FISCAL_START,
         DATE_TRUNC('year', CURRENT_DATE()) AS CALENDAR_START
),
amounts AS (
  -- Both sides are identical except for the year boundary: same table, same measure, same
  -- cut-off. That is the entire point -- the difference is only the fiscal calendar. The
  -- fiscal side reconciles exactly to SALES_FISCAL_YTD in the semantic view, which is what
  -- makes this a check rather than an illustration. verify_deployment.py asserts it.
  SELECT
    (SELECT SUM(TRANSACTION_AMOUNT_USD)
       FROM {{DB}}.SALES_ANALYTICS.FACT_SALES_SUMMARY
      WHERE PROCESSED_DATE >= (SELECT FISCAL_START FROM bounds)
        AND PROCESSED_DATE <  CURRENT_DATE())   AS FISCAL_YTD,
    (SELECT SUM(TRANSACTION_AMOUNT_USD)
       FROM {{DB}}.SALES_ANALYTICS.FACT_SALES_SUMMARY
      WHERE PROCESSED_DATE >= (SELECT CALENDAR_START FROM bounds)
        AND PROCESSED_DATE <  CURRENT_DATE())   AS CALENDAR_YTD
),
metrics_total AS (
  SELECT COUNT(*) AS TOTAL_METRICS
    FROM {{DB}}.INFORMATION_SCHEMA.SEMANTIC_METRICS
   WHERE SEMANTIC_VIEW_NAME = '{{SEMANTIC_VIEW}}'
),
exposure AS (
  -- RLIKE matches the whole string in Snowflake, not a substring, so the pattern is
  -- wrapped in .* deliberately: '_FY[0-9]{2}$' returns zero rows and would report the
  -- hard-coded metrics as non-existent.
  SELECT COUNT(*) AS BLOCKER_METRICS,
         COUNT(CASE WHEN OBJECT_NAME RLIKE '.*_FY[0-9]{2}.*' THEN 1 END) AS HARDCODED
    FROM {{KB_DB}}.KNOWLEDGE_BASE.KB_ISSUE
   WHERE SEVERITY = 'blocker'
     AND ISSUE ILIKE '%fiscal calendar%'
)
SELECT b.FISCAL_START, b.CALENDAR_START,
       a.FISCAL_YTD, a.CALENDAR_YTD,
       m.TOTAL_METRICS, e.BLOCKER_METRICS, e.HARDCODED
  FROM bounds b, amounts a, metrics_total m, exposure e;
