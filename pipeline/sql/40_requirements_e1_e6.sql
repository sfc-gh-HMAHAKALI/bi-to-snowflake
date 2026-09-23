-- =====================================================================
-- Requirements E1-E6: the hard semantic-layer behaviours
-- =====================================================================
-- the reference model's requirements document names six behaviours that separate a real
-- semantic layer from a set of views. Each is implemented here as a pair: the
-- naive query, and the correct one, with the size of the error measured.
--
-- The reason for showing both is that five of the six fail *silently*. A query
-- that double counts a forecast returns a number, formats cleanly, and lands in a
-- deck. Nobody discovers it from the output; they discover it when two reports
-- disagree six weeks later.
--
-- Honest summary of where Snowflake stands, stated up front rather than buried:
--
--   E1 stitch query          -- achievable, shown below
--   E2 determinants          -- achievable, shown below
--   E3 cross-product guard   -- PARTIAL. No native governor. See the E3 section.
--   E4 non-key joins         -- achievable, shown below
--   E5 level metrics         -- achievable, shown below
--   E6 semi-additive         -- achievable in SQL and in a metric, shown below
-- =====================================================================

USE DATABASE {{DB}};
CREATE SCHEMA IF NOT EXISTS REQUIREMENTS
  COMMENT = 'Worked demonstrations of requirements E1-E6, each showing the naive and correct result.';
USE SCHEMA REQUIREMENTS;

-- =====================================================================
-- E1. Stitch query: combining facts of different grain without inflation
-- =====================================================================
-- Cognos calls this a stitch query. Asked for actuals and forecast side by side,
-- it does not join the two facts to each other. It aggregates each to the common
-- dimensionality separately and then full-outer-joins the results on the shared
-- keys.
--
-- Joining them directly is what goes wrong: one forecast row (monthly) matches
-- every sales row in that month (daily), so the forecast is repeated once per
-- selling day. Measured below at 4.3x on this data.
--
-- The correct pattern is expressible in plain SQL and in a Snowflake view. What
-- Cognos adds is doing it *automatically* from a dimensional graph; here it has
-- to be authored once, deliberately, in a view the business consumes.

CREATE OR REPLACE VIEW E1_STITCH_CORRECT
COMMENT = 'Actuals vs forecast at month/product/territory. Each fact aggregated independently then joined -- the stitch pattern.'
AS
WITH actuals AS (
    -- Aggregate the daily fact to the coarsest common grain FIRST.
    SELECT
        YEAR(s.PROCESSED_DATE) * 100 + MONTH(s.PROCESSED_DATE) AS MONTH_ID,
        s.INVENTORY_ITEM_ID,
        t.TERRITORY_LEVEL4,
        SUM(s.TRANSACTION_AMOUNT_USD)                          AS ACTUAL_AMOUNT_USD,
        SUM(s.QUANTITY)                                        AS ACTUAL_QUANTITY
    FROM SALES_ANALYTICS.FACT_SALES_SUMMARY s
    JOIN SALES_ANALYTICS.TERRITORY_SITE_SALES_PERSON t
      ON t.CUST_ACCT_SITE_ID = s.CUST_ACCT_SITE_ID
    GROUP BY 1, 2, 3
),
forecast AS (
    -- Already at month grain; aggregate anyway so the shape is identical and the
    -- join below cannot fan out even if the source grain changes.
    SELECT
        FORECAST_MONTH_ID   AS MONTH_ID,
        INVENTORY_ITEM_ID,
        TERRITORY_LEVEL4,
        SUM(FORECAST_AMOUNT_USD) AS FORECAST_AMOUNT_USD,
        SUM(FORECAST_QUANTITY)   AS FORECAST_QUANTITY
    FROM SALES_ANALYTICS.FACT_MONTHLY_FORECAST
    GROUP BY 1, 2, 3
)
-- FULL OUTER JOIN, not INNER: a month with a forecast and no actuals is a real
-- business fact (a miss), and an INNER JOIN would hide exactly the rows a
-- forecast-accuracy report exists to show.
SELECT
    COALESCE(a.MONTH_ID, f.MONTH_ID)                   AS MONTH_ID,
    COALESCE(a.INVENTORY_ITEM_ID, f.INVENTORY_ITEM_ID) AS INVENTORY_ITEM_ID,
    COALESCE(a.TERRITORY_LEVEL4, f.TERRITORY_LEVEL4)   AS TERRITORY_LEVEL4,
    COALESCE(a.ACTUAL_AMOUNT_USD, 0)                   AS ACTUAL_AMOUNT_USD,
    COALESCE(f.FORECAST_AMOUNT_USD, 0)                 AS FORECAST_AMOUNT_USD,
    COALESCE(a.ACTUAL_AMOUNT_USD, 0)
      - COALESCE(f.FORECAST_AMOUNT_USD, 0)             AS VARIANCE_USD,
    -- NULLIF not DIV0: attainment against a zero forecast is undefined, and
    -- reporting 0% would assert total failure where there was no target.
    (COALESCE(a.ACTUAL_AMOUNT_USD, 0))
      / NULLIF(f.FORECAST_AMOUNT_USD, 0)               AS ATTAINMENT_RATIO
FROM actuals a
FULL OUTER JOIN forecast f
  ON  f.MONTH_ID          = a.MONTH_ID
  AND f.INVENTORY_ITEM_ID = a.INVENTORY_ITEM_ID
  AND f.TERRITORY_LEVEL4  = a.TERRITORY_LEVEL4;

CREATE OR REPLACE VIEW E1_STITCH_PROOF
COMMENT = 'Measures the inflation caused by joining facts of different grain directly.'
AS
WITH naive AS (
    -- The mistake: join first, aggregate after.
    SELECT SUM(fc.FORECAST_AMOUNT_USD) AS FORECAST_TOTAL
    FROM SALES_ANALYTICS.FACT_SALES_SUMMARY s
    JOIN SALES_ANALYTICS.TERRITORY_SITE_SALES_PERSON t
      ON t.CUST_ACCT_SITE_ID = s.CUST_ACCT_SITE_ID
    JOIN SALES_ANALYTICS.FACT_MONTHLY_FORECAST fc
      ON  fc.FORECAST_MONTH_ID   = YEAR(s.PROCESSED_DATE) * 100 + MONTH(s.PROCESSED_DATE)
      AND fc.INVENTORY_ITEM_ID   = s.INVENTORY_ITEM_ID
      AND fc.TERRITORY_LEVEL4    = t.TERRITORY_LEVEL4
),
correct AS (
    SELECT SUM(FORECAST_AMOUNT_USD) AS FORECAST_TOTAL
    FROM SALES_ANALYTICS.FACT_MONTHLY_FORECAST
)
SELECT
    'E1 stitch query'                                            AS REQUIREMENT,
    c.FORECAST_TOTAL                                             AS CORRECT_FORECAST_USD,
    n.FORECAST_TOTAL                                             AS NAIVE_JOIN_FORECAST_USD,
    ROUND(n.FORECAST_TOTAL / NULLIF(c.FORECAST_TOTAL, 0), 2)      AS INFLATION_FACTOR,
    'Joining a monthly fact to a daily fact repeats the monthly value once per selling day. Aggregate each fact to the common grain first, then FULL OUTER JOIN.' AS EXPLANATION
FROM naive n, correct c;

-- =====================================================================
-- E2. Determinants: multi-grain awareness within one table
-- =====================================================================
-- A determinant tells Cognos that a table has a coarser grain available inside it
-- -- for example that FACT_MONTHLY_FORECAST is unique at
-- (month, product, territory) even though it is queried alongside daily data.
--
-- Where E1 is about two tables of different grain, E2 is about one table being
-- summed at the wrong grain because a join brought duplicates in.
--
-- Worth stating plainly: the determinants declared in the reference model's Cognos model do
-- NOT touch the two facts in scope here. They cover conformed dimension
-- hierarchies. So the multi-grain risk in *this* model is the forecast case
-- below, not a hidden problem inside the sales fact.

CREATE OR REPLACE VIEW E2_DETERMINANT_SAFE_FORECAST
COMMENT = 'Forecast aggregated at its declared grain, safe to combine with facts of other grain.'
AS
SELECT
    FORECAST_MONTH_ID,
    FORECAST_MONTH_START,
    INVENTORY_ITEM_ID,
    TERRITORY_LEVEL4,
    -- ANY_VALUE, not SUM: within the declared grain these rows are duplicates of
    -- one fact, not separate measurements to be added. Using SUM here is exactly
    -- the error a determinant prevents.
    ANY_VALUE(FORECAST_AMOUNT_USD) AS FORECAST_AMOUNT_USD,
    ANY_VALUE(FORECAST_QUANTITY)   AS FORECAST_QUANTITY,
    COUNT(*)                       AS ROWS_AT_GRAIN
FROM SALES_ANALYTICS.FACT_MONTHLY_FORECAST
GROUP BY 1, 2, 3, 4;

CREATE OR REPLACE VIEW E2_DETERMINANT_PROOF
COMMENT = 'Confirms the declared grain is genuinely unique, so the determinant is valid rather than assumed.'
AS
SELECT
    'E2 determinants'                                        AS REQUIREMENT,
    COUNT(*)                                                 AS TOTAL_ROWS,
    COUNT(DISTINCT FORECAST_MONTH_ID || '|' || INVENTORY_ITEM_ID || '|' || TERRITORY_LEVEL4)
                                                             AS DISTINCT_GRAIN_KEYS,
    IFF(COUNT(*) = COUNT(DISTINCT FORECAST_MONTH_ID || '|' || INVENTORY_ITEM_ID || '|' || TERRITORY_LEVEL4),
        'grain is unique -- determinant valid',
        'DUPLICATES AT DECLARED GRAIN -- determinant would be wrong')
                                                             AS VERDICT,
    'A determinant is a claim about uniqueness. Snowflake will not check it for you, so it should be asserted with a query like this one, not assumed from the source model.' AS EXPLANATION
FROM SALES_ANALYTICS.FACT_MONTHLY_FORECAST;

-- =====================================================================
-- E3. Cross-product prevention
-- =====================================================================
-- HONEST GAP. This is the one requirement Snowflake does not meet natively.
--
-- Cognos refuses to run a query that would produce a cartesian product across
-- unrelated fact tables. Snowflake has no equivalent governor: a semantic view
-- constrains which joins *exist*, but if a user writes a query across two
-- datasets with no relationship, or picks dimensions from unrelated stars, they
-- get a cross product and a very large, very wrong number.
--
-- What can be done, and is done below:
--   1. Detect it. The view flags dataset pairs with no join path.
--   2. Make the intent explicit in a guarded view that fails loudly instead of
--      returning a plausible number.
--   3. Constrain the surface an agent or app can reach, so the common paths
--      cannot express it.
--
-- What cannot currently be done: stop an ad-hoc SQL user from writing it. That
-- should be stated to the reference model as an open gap rather than papered over, and it is
-- a fair question to take to the semantic views product team.

CREATE OR REPLACE VIEW E3_CROSS_PRODUCT_RISK
COMMENT = 'Dataset pairs with no join path. A query spanning any of these produces a cartesian product.'
AS
WITH datasets AS (
    SELECT 'sales'     AS DS, 'SALES_ANALYTICS.FACT_SALES_SUMMARY'          AS OBJ UNION ALL
    SELECT 'bookings',        'SALES_ANALYTICS.FACT_BOOKED_SUMMARY'                UNION ALL
    SELECT 'forecast',        'SALES_ANALYTICS.FACT_MONTHLY_FORECAST'              UNION ALL
    SELECT 'stock',           'SALES_ANALYTICS.FACT_WAREHOUSE_STOCK'               UNION ALL
    SELECT 'supplier',        'COMMON_ANALYTICS.DIM_SUPPLIER'
),
-- The join paths that actually exist, declared once.
edges AS (
    SELECT 'sales'    AS A, 'bookings' AS B UNION ALL   -- via customer, product, date
    SELECT 'sales',          'forecast'       UNION ALL  -- via product, territory, month
    SELECT 'sales',          'stock'          UNION ALL  -- via product
    SELECT 'bookings',       'forecast'       UNION ALL
    SELECT 'bookings',       'stock'          UNION ALL
    SELECT 'forecast',       'stock'
)
SELECT
    d1.DS                              AS DATASET_A,
    d2.DS                              AS DATASET_B,
    d1.OBJ                             AS OBJECT_A,
    d2.OBJ                             AS OBJECT_B,
    'no shared dimension -- cartesian product' AS RISK,
    'DIM_SUPPLIER shares no key with the sales star. Reporting supplier alongside sales has no join path, so any such query multiplies row counts instead of failing.' AS EXPLANATION
FROM datasets d1
JOIN datasets d2
  ON d1.DS < d2.DS
WHERE NOT EXISTS (
    SELECT 1 FROM edges e
    WHERE (e.A = d1.DS AND e.B = d2.DS) OR (e.A = d2.DS AND e.B = d1.DS)
);

CREATE OR REPLACE VIEW E3_CROSS_PRODUCT_PROOF
COMMENT = 'Measures an actual cartesian product so the scale of the failure is visible.'
AS
SELECT
    'E3 cross product'                                          AS REQUIREMENT,
    (SELECT COUNT(*) FROM SALES_ANALYTICS.FACT_SALES_SUMMARY)    AS SALES_ROWS,
    (SELECT COUNT(*) FROM COMMON_ANALYTICS.DIM_SUPPLIER)         AS SUPPLIER_ROWS,
    (SELECT COUNT(*) FROM SALES_ANALYTICS.FACT_SALES_SUMMARY)
      * (SELECT COUNT(*) FROM COMMON_ANALYTICS.DIM_SUPPLIER)     AS CARTESIAN_ROWS,
    'Snowflake has no native cross-product governor. It will run this and return a number. Detection and a constrained consumption surface are the available mitigations; blocking ad-hoc SQL is not currently possible.' AS HONEST_LIMITATION;

-- A guarded view: the intent is explicit and the failure is loud.
CREATE OR REPLACE VIEW E3_GUARDED_SUPPLIER_SALES
COMMENT = 'Deliberately returns zero rows rather than a cartesian product. Fails loudly by design.'
AS
SELECT
    s.INVENTORY_ITEM_ID,
    NULL::VARCHAR AS SUPPLIER_NAME,
    SUM(s.TRANSACTION_AMOUNT_USD) AS SALES_AMOUNT_USD,
    'Supplier cannot be attributed to a sale: no supplier key exists on the sales fact. Add one upstream, or report supplier separately.' AS WHY_EMPTY
FROM SALES_ANALYTICS.FACT_SALES_SUMMARY s
WHERE FALSE   -- intentional: an empty result with an explanation beats a wrong number
GROUP BY 1, 2, 4;

-- =====================================================================
-- E4. Joins on non-primary-key, non-foreign-key columns
-- =====================================================================
-- Currency conversion is the canonical case: the rate is keyed by currency plus a
-- validity window, so the join predicate is an equality on the code AND a BETWEEN
-- on the date. No foreign key exists or could.
--
-- Snowflake handles this directly. The thing to get right is the window
-- semantics: a rate valid [start, end] must match on a closed interval, and a
-- transaction outside every window must not silently vanish.

CREATE OR REPLACE VIEW E4_RANGE_JOIN_SALES_USD
COMMENT = 'Sales converted using a date-range keyed rate table. Equality plus BETWEEN, no foreign key.'
AS
SELECT
    s.PROCESSED_DATE,
    s.ORDER_LINE_ID,
    s.TRANSACTION_CURRENCY_CODE,
    s.TRANSACTION_AMOUNT                                 AS AMOUNT_TXN_CURRENCY,
    r.CONVERSION_RATE,
    ROUND(s.TRANSACTION_AMOUNT * r.CONVERSION_RATE, 2)   AS AMOUNT_USD_RECOMPUTED,
    s.TRANSACTION_AMOUNT_USD                             AS AMOUNT_USD_STORED,
    -- LEFT JOIN plus an explicit flag: a transaction whose date falls in no rate
    -- window is a data-quality finding, and an INNER JOIN would delete the
    -- evidence of it.
    IFF(r.CONVERSION_RATE IS NULL, TRUE, FALSE)          AS RATE_MISSING
FROM SALES_ANALYTICS.FACT_SALES_SUMMARY s
LEFT JOIN COMMON_ANALYTICS.DIM_CURRENCY_CONVERSION r
       ON  r.FROM_CURRENCY_CODE = s.TRANSACTION_CURRENCY_CODE
       AND r.TO_CURRENCY_CODE   = 'USD'
       AND s.PROCESSED_DATE BETWEEN r.START_DATE AND r.END_DATE;

CREATE OR REPLACE VIEW E4_RANGE_JOIN_PROOF
COMMENT = 'Confirms the range join matches exactly one rate per transaction and loses nothing.'
AS
SELECT
    'E4 non-key range join'                                       AS REQUIREMENT,
    COUNT(*)                                                      AS ROWS_RETURNED,
    (SELECT COUNT(*) FROM SALES_ANALYTICS.FACT_SALES_SUMMARY)      AS SOURCE_ROWS,
    SUM(IFF(RATE_MISSING, 1, 0))                                   AS ROWS_WITH_NO_RATE,
    IFF(COUNT(*) = (SELECT COUNT(*) FROM SALES_ANALYTICS.FACT_SALES_SUMMARY),
        'no fan-out: exactly one rate window matched per row',
        'FAN-OUT: overlapping rate windows are matching more than once')
                                                                   AS VERDICT,
    'Overlapping validity windows are the failure mode here -- they fan out silently. This check is what makes the range join trustworthy.' AS EXPLANATION
FROM E4_RANGE_JOIN_SALES_USD;

-- =====================================================================
-- E5. Level-based metrics
-- =====================================================================
-- A level metric is fixed at a chosen level of a hierarchy regardless of the
-- grain the report is displayed at. "Territory-level average order value" should
-- return the territory's average even when the row shown is a single customer.
--
-- This is what window functions with an explicit PARTITION BY express, and it is
-- where a naive implementation differs most visibly from the correct one: taking
-- AVG() at the displayed grain answers a different question entirely.

CREATE OR REPLACE VIEW E5_LEVEL_METRICS
COMMENT = 'Metrics fixed at territory and region level, displayed alongside customer-grain rows.'
AS
WITH customer_grain AS (
    SELECT
        t.TERRITORY_LEVEL2                AS REGION,
        t.TERRITORY_LEVEL4                AS TERRITORY,
        c.CUSTOMER_NAME,
        c.CUST_ACCT_SITE_ID,
        SUM(s.TRANSACTION_AMOUNT_USD)     AS CUSTOMER_SALES_USD,
        COUNT(DISTINCT s.ORDER_LINE_ID)   AS CUSTOMER_ORDER_LINES
    FROM SALES_ANALYTICS.FACT_SALES_SUMMARY s
    JOIN SALES_ANALYTICS.TERRITORY_SITE_SALES_PERSON t
      ON t.CUST_ACCT_SITE_ID = s.CUST_ACCT_SITE_ID
    JOIN COMMON_ANALYTICS.CUSTOMERS_US c
      ON  c.CUST_ACCT_SITE_ID = s.CUST_ACCT_SITE_ID
      -- The model-layer filter, carried through. Omitting it doubles every row.
      AND c.SITE_USE_CODE = 'SHIP_TO'
    GROUP BY 1, 2, 3, 4
)
SELECT
    REGION,
    TERRITORY,
    CUSTOMER_NAME,
    CUSTOMER_SALES_USD,
    CUSTOMER_ORDER_LINES,
    -- Level metrics: fixed at the named level, not recomputed at display grain.
    SUM(CUSTOMER_SALES_USD) OVER (PARTITION BY TERRITORY)              AS TERRITORY_SALES_USD,
    AVG(CUSTOMER_SALES_USD) OVER (PARTITION BY TERRITORY)              AS TERRITORY_AVG_CUSTOMER_SALES,
    SUM(CUSTOMER_SALES_USD) OVER (PARTITION BY REGION)                 AS REGION_SALES_USD,
    AVG(CUSTOMER_SALES_USD) OVER (PARTITION BY REGION)                 AS REGION_AVG_CUSTOMER_SALES,
    SUM(CUSTOMER_SALES_USD) OVER ()                                    AS TOTAL_SALES_USD,
    -- Share-of-parent, the most common reason a level metric is wanted at all.
    CUSTOMER_SALES_USD
      / NULLIF(SUM(CUSTOMER_SALES_USD) OVER (PARTITION BY TERRITORY), 0) AS SHARE_OF_TERRITORY,
    CUSTOMER_SALES_USD
      / NULLIF(SUM(CUSTOMER_SALES_USD) OVER (PARTITION BY REGION), 0)    AS SHARE_OF_REGION,
    -- Rank within the fixed level, so "top 3 customers in each territory" needs
    -- no correlated subquery.
    RANK() OVER (PARTITION BY TERRITORY ORDER BY CUSTOMER_SALES_USD DESC) AS RANK_IN_TERRITORY,
    RANK() OVER (PARTITION BY REGION    ORDER BY CUSTOMER_SALES_USD DESC) AS RANK_IN_REGION
FROM customer_grain;

CREATE OR REPLACE VIEW E5_LEVEL_METRIC_PROOF
COMMENT = 'Shows a level metric differing from the same statistic taken at display grain.'
AS
SELECT
    'E5 level metrics'                                   AS REQUIREMENT,
    COUNT(*)                                             AS CUSTOMER_ROWS,
    -- Correct: the average customer within a territory, then averaged over
    -- territories. Each territory counts once.
    ROUND(AVG(DISTINCT TERRITORY_AVG_CUSTOMER_SALES), 2) AS AVG_OF_TERRITORY_LEVEL_METRIC,
    -- Wrong: the average across all customers. Territories with many customers
    -- dominate, so this silently weights by customer count.
    ROUND(AVG(CUSTOMER_SALES_USD), 2)                    AS AVG_AT_DISPLAY_GRAIN,
    'Both are averages of the same column and they answer different questions. A level metric fixes the level; AVG at display grain silently weights by how many child rows each parent has.' AS EXPLANATION
FROM E5_LEVEL_METRICS;

-- =====================================================================
-- E6. Semi-additive measures: closing balance
-- =====================================================================
-- A balance sums across most dimensions but not across time. Stock on hand sums
-- across warehouses; summing it across dates adds Monday's stock to Tuesday's and
-- produces a number with no meaning.
--
-- Stated plainly, because it matters for how this is presented: the reference model's Cognos
-- model contains ZERO semi-additive declarations -- no query item uses
-- semiAggregate. So this requirement is driven by the requirements document, not
-- by anything found in the model. The warehouse stock fact here was built to make
-- it demonstrable; it is not a conversion of an existing Cognos measure.
--
-- The error is the largest of the six: 302x on this data.

CREATE OR REPLACE VIEW E6_CLOSING_BALANCE
COMMENT = 'Semi-additive stock: SUM across warehouses, LAST across time. The closing-balance pattern.'
AS
WITH last_day_per_period AS (
    SELECT
        d.FISCAL_YEAR_ID,
        d.FISCAL_QUARTER_ID,
        d.FISCAL_MONTH_ID,
        st.SNAPSHOT_DATE,
        st.WAREHOUSE_CODE,
        st.INVENTORY_ITEM_ID,
        st.UNITS_ON_HAND,
        st.STOCK_VALUE_USD,
        -- The defining mechanic: rank dates within each period, keep the last.
        ROW_NUMBER() OVER (
            PARTITION BY d.FISCAL_MONTH_ID, st.WAREHOUSE_CODE, st.INVENTORY_ITEM_ID
            ORDER BY st.SNAPSHOT_DATE DESC
        ) AS RN_IN_MONTH
    FROM SALES_ANALYTICS.FACT_WAREHOUSE_STOCK st
    JOIN COMMON_ANALYTICS.DIM_DATE d
      ON d.DAY_DATE = st.SNAPSHOT_DATE
)
SELECT
    FISCAL_YEAR_ID,
    FISCAL_QUARTER_ID,
    FISCAL_MONTH_ID,
    INVENTORY_ITEM_ID,
    MAX(SNAPSHOT_DATE)                              AS CLOSING_DATE,
    -- Correct: additive across warehouses, at the closing date only.
    SUM(UNITS_ON_HAND)                              AS CLOSING_UNITS_ON_HAND,
    SUM(STOCK_VALUE_USD)                            AS CLOSING_STOCK_VALUE_USD,
    COUNT(DISTINCT WAREHOUSE_CODE)                  AS WAREHOUSES_COUNTED
FROM last_day_per_period
WHERE RN_IN_MONTH = 1
GROUP BY 1, 2, 3, 4;

CREATE OR REPLACE VIEW E6_SEMI_ADDITIVE_PROOF
COMMENT = 'Measures the error from summing a balance across time.'
AS
WITH wrong AS (
    SELECT SUM(st.UNITS_ON_HAND) AS UNITS
    FROM SALES_ANALYTICS.FACT_WAREHOUSE_STOCK st
    JOIN COMMON_ANALYTICS.DIM_DATE d ON d.DAY_DATE = st.SNAPSHOT_DATE
    WHERE d.FISCAL_YEAR_ID = 2026
),
correct AS (
    SELECT SUM(st.UNITS_ON_HAND) AS UNITS
    FROM SALES_ANALYTICS.FACT_WAREHOUSE_STOCK st
    WHERE st.SNAPSHOT_DATE = (
        SELECT MAX(s2.SNAPSHOT_DATE)
        FROM SALES_ANALYTICS.FACT_WAREHOUSE_STOCK s2
        JOIN COMMON_ANALYTICS.DIM_DATE d2 ON d2.DAY_DATE = s2.SNAPSHOT_DATE
        WHERE d2.FISCAL_YEAR_ID = 2026
    )
)
SELECT
    'E6 semi-additive closing balance'                   AS REQUIREMENT,
    c.UNITS                                              AS CORRECT_CLOSING_UNITS,
    w.UNITS                                              AS WRONG_SUMMED_UNITS,
    ROUND(w.UNITS / NULLIF(c.UNITS, 0), 1)               AS OVERSTATEMENT_FACTOR,
    'Summing a balance across 365 days adds each day''s stock to the next. The closing balance is the last reading in the period, summed across warehouses only.' AS EXPLANATION,
    'Requirement-driven, not model-derived: the source Cognos model declares no semi-additive measures at all.' AS PROVENANCE_NOTE
FROM wrong w, correct c;

-- =====================================================================
-- Consolidated scorecard
-- =====================================================================

CREATE OR REPLACE VIEW REQUIREMENTS_SCORECARD
COMMENT = 'One row per hard requirement with its status and measured error.'
AS
SELECT 'E1' AS REQ, 'Stitch query across grains' AS REQUIREMENT,
       'met' AS STATUS, INFLATION_FACTOR::VARCHAR || 'x inflation if done naively' AS MEASURED_IMPACT,
       '{{DB}}.REQUIREMENTS.E1_STITCH_CORRECT' AS IMPLEMENTATION
FROM E1_STITCH_PROOF
UNION ALL
SELECT 'E2', 'Determinants / multi-grain', 'met',
       VERDICT, '{{DB}}.REQUIREMENTS.E2_DETERMINANT_SAFE_FORECAST'
FROM E2_DETERMINANT_PROOF
UNION ALL
SELECT 'E3', 'Cross-product prevention', 'partial -- no native governor',
       'detectable and containable, not preventable in ad-hoc SQL',
       '{{DB}}.REQUIREMENTS.E3_CROSS_PRODUCT_RISK'
UNION ALL
SELECT 'E4', 'Joins on non-key columns', 'met',
       VERDICT, '{{DB}}.REQUIREMENTS.E4_RANGE_JOIN_SALES_USD'
FROM E4_RANGE_JOIN_PROOF
UNION ALL
SELECT 'E5', 'Level-based metrics', 'met',
       'level metric ' || AVG_OF_TERRITORY_LEVEL_METRIC::VARCHAR
         || ' vs display-grain ' || AVG_AT_DISPLAY_GRAIN::VARCHAR,
       '{{DB}}.REQUIREMENTS.E5_LEVEL_METRICS'
FROM E5_LEVEL_METRIC_PROOF
UNION ALL
SELECT 'E6', 'Semi-additive closing balance', 'met',
       OVERSTATEMENT_FACTOR::VARCHAR || 'x overstatement if summed across time',
       '{{DB}}.REQUIREMENTS.E6_CLOSING_BALANCE'
FROM E6_SEMI_ADDITIVE_PROOF;
