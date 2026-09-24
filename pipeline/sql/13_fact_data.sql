-- =====================================================================
-- Synthetic fact data
-- =====================================================================
-- Volumes are chosen so aggregates are realistic and queries stay interactive:
-- roughly 180k sales lines and 180k booked lines over three fiscal years.
--
-- Two deliberate properties:
--
--  * Sales are written only against SHIP_TO customer sites, which is what makes
--    the missing SITE_USE_CODE filter demonstrable: the correct join returns one
--    customer row per sale, the naive one returns two.
--
--  * Bookings lead sales. Booked amounts run ahead of shipped amounts for recent
--    periods, so the sales-versus-booked gap is a real backlog rather than noise
--    around zero.
-- =====================================================================

USE DATABASE {{DB}};

-- ---------------------------------------------------------------------
-- Sales: order-line grain, daily
-- ---------------------------------------------------------------------

TRUNCATE TABLE IF EXISTS SALES_ANALYTICS.FACT_SALES_SUMMARY;

INSERT INTO SALES_ANALYTICS.FACT_SALES_SUMMARY
    (PROCESSED_DATE, PERIOD_DATE, CUST_ACCT_SITE_ID, INVENTORY_ITEM_ID,
     ORDER_LINE_ID, CUSTOMER_TRX_LINE_ID, COMM_LINES_API_ID, KIT_INVENTORY_ITEM_ID,
     QUANTITY, QUANTITY_EXCL_RESUPPLY,
     TRANSACTION_AMOUNT, TRANSACTION_AMOUNT_USD,
     TRANSACTION_AMOUNT_EXCL_RESPLY, TRAN_AMT_EXCL_RESPLY_USD,
     TRANSACTION_TYPE, TRANSACTION_TYPE_DESC, TRANSACTION_TYPE_INVOICE,
     TRANSACTION_CURRENCY_CODE, ORDER_CATEGORY, ORDER_TYPE, ORIGINAL_TRX_TYPE,
     ADJUST_STATUS, DISCOUNT_TYPE, LOAD_STATUS,
     CONSIGNMENT_ACCOUNT_NAME, CONSIGNMENT_ACCOUNT_NUMBER,
     SPLIT_SOURCE_ACCOUNT_NAME, SPLIT_SOURCE_ACCOUNT_NUMBER, SPLIT_SOURCE_SITE_NUMBER,
     TRACING_SOURCE_ACCOUNT_NAME, TRACING_SOURCE_ACCOUNT_NUMBER,
     DATA_LOAD_TIME, DATA_UPDATE_TIME)
WITH n AS (
    SELECT SEQ8() AS s FROM TABLE(GENERATOR(ROWCOUNT => 180000))
), sites AS (
    SELECT CUST_ACCT_SITE_ID, ROW_NUMBER() OVER (ORDER BY CUST_ACCT_SITE_ID) - 1 AS rn,
           COUNT(*) OVER () AS cnt
    FROM COMMON_ANALYTICS.CUSTOMERS_US
    WHERE SITE_USE_CODE = 'SHIP_TO'      -- sales ship to a SHIP_TO site, by definition
), items AS (
    SELECT INVENTORY_ITEM_ID, GPC1,
           ROW_NUMBER() OVER (ORDER BY INVENTORY_ITEM_ID) - 1 AS rn,
           COUNT(*) OVER () AS cnt
    FROM COMMON_ANALYTICS.EDW_ORA_COA_PROD_GFP_W_NORA
), base AS (
    SELECT
        s,
        -- Spread across roughly three fiscal years ending today, weekdays only,
        -- so weekly and month-to-date windows behave like a real sales calendar.
        DATEADD('day', -MOD(s * 7919, 1095), CURRENT_DATE()) AS raw_dt,
        MOD(s * 31,   (SELECT cnt FROM sites LIMIT 1))       AS site_rn,
        MOD(s * 17,   (SELECT cnt FROM items LIMIT 1))       AS item_rn
    FROM n
), dated AS (
    SELECT
        b.*,
        -- Push weekend dates onto the following Monday.
        IFF(DAYOFWEEK(raw_dt) = 0, DATEADD('day', 1, raw_dt),
            IFF(DAYOFWEEK(raw_dt) = 6, DATEADD('day', 2, raw_dt), raw_dt)) AS dt
    FROM base b
), joined AS (
    SELECT
        d.s, d.dt,
        si.CUST_ACCT_SITE_ID,
        it.INVENTORY_ITEM_ID,
        it.GPC1,
        -- Price band per product line, so product mix drives revenue shape
        -- rather than every line being interchangeable.
        CASE it.GPC1
            WHEN 'Flow Generators' THEN 780
            WHEN 'Humidifiers'     THEN 240
            WHEN 'Masks'           THEN 95
            ELSE 28
        END AS unit_price
    FROM dated d
    JOIN sites si ON si.rn = d.site_rn
    JOIN items it ON it.rn = d.item_rn
)
SELECT
    dt                                              AS PROCESSED_DATE,
    DATE_TRUNC('month', dt)                         AS PERIOD_DATE,
    CUST_ACCT_SITE_ID,
    INVENTORY_ITEM_ID,
    1000000 + s                                     AS ORDER_LINE_ID,
    2000000 + s                                     AS CUSTOMER_TRX_LINE_ID,
    3000000 + s                                     AS COMM_LINES_API_ID,
    NULL                                            AS KIT_INVENTORY_ITEM_ID,
    qty                                             AS QUANTITY,
    IFF(is_resupply, 0, qty)                        AS QUANTITY_EXCL_RESUPPLY,
    amt                                             AS TRANSACTION_AMOUNT,
    amt                                             AS TRANSACTION_AMOUNT_USD,
    IFF(is_resupply, 0, amt)                        AS TRANSACTION_AMOUNT_EXCL_RESPLY,
    IFF(is_resupply, 0, amt)                        AS TRAN_AMT_EXCL_RESPLY_USD,
    'INV'                                           AS TRANSACTION_TYPE,
    'Invoice'                                       AS TRANSACTION_TYPE_DESC,
    'Y'                                             AS TRANSACTION_TYPE_INVOICE,
    'USD'                                           AS TRANSACTION_CURRENCY_CODE,
    IFF(is_resupply, 'RESUPPLY', 'NEW EQUIPMENT')   AS ORDER_CATEGORY,
    CASE MOD(s, 3) WHEN 0 THEN 'STANDARD' WHEN 1 THEN 'DROP SHIP' ELSE 'STOCK' END AS ORDER_TYPE,
    'ORDER'                                         AS ORIGINAL_TRX_TYPE,
    'NONE'                                          AS ADJUST_STATUS,
    CASE MOD(s, 7) WHEN 0 THEN 'CONTRACT' WHEN 1 THEN 'VOLUME' ELSE 'NONE' END AS DISCOUNT_TYPE,
    'LOADED'                                        AS LOAD_STATUS,
    NULL, NULL, NULL, NULL, NULL, NULL, NULL,
    CURRENT_DATE(), CURRENT_DATE()
FROM (
    SELECT
        j.*,
        MOD(s, 5) = 0                                          AS is_resupply,
        (MOD(s, 9) + 1)                                        AS qty,
        ROUND(unit_price * (MOD(s, 9) + 1)
              * (1 - MOD(s, 11) * 0.01)                        -- small discount spread
              -- Gentle upward trend so year-over-year growth is positive and
              -- period comparisons are not pure noise.
              * (1 + DATEDIFF('day', DATE '2023-01-01', dt) * 0.00018), 2) AS amt
    FROM joined j
);

-- ---------------------------------------------------------------------
-- Bookings: same grain, running ahead of shipped sales
-- ---------------------------------------------------------------------

TRUNCATE TABLE IF EXISTS SALES_ANALYTICS.FACT_BOOKED_SUMMARY;

INSERT INTO SALES_ANALYTICS.FACT_BOOKED_SUMMARY
    (PROCESSED_DATE, PERIOD_DATE, CUST_ACCT_SITE_ID, INVENTORY_ITEM_ID,
     ORDER_LINE_ID, CUSTOMER_TRX_LINE_ID, COMM_LINES_API_ID, KIT_INVENTORY_ITEM_ID,
     QUANTITY, QUANTITY_EXCL_RESUPPLY,
     TRANSACTION_AMOUNT, TRANSACTION_AMOUNT_USD,
     TRANSACTION_AMOUNT_EXCL_RESPLY, TRAN_AMT_EXCL_RESPLY_USD,
     TRANSACTION_TYPE, TRANSACTION_TYPE_DESC, TRANSACTION_TYPE_INVOICE,
     TRANSACTION_CURRENCY_CODE, ORDER_CATEGORY, ORDER_TYPE, ORIGINAL_TRX_TYPE,
     ADJUST_STATUS, DISCOUNT_TYPE, LOAD_STATUS,
     CONSIGNMENT_ACCOUNT_NAME, CONSIGNMENT_ACCOUNT_NUMBER,
     SPLIT_SOURCE_ACCOUNT_NAME, SPLIT_SOURCE_ACCOUNT_NUMBER, SPLIT_SOURCE_SITE_NUMBER,
     TRACING_SOURCE_ACCOUNT_NAME, TRACING_SOURCE_ACCOUNT_NUMBER,
     DATA_LOAD_TIME, DATA_UPDATE_TIME)
SELECT
    -- Booked 9 days before shipment, so bookings lead sales and the backlog gap
    -- is genuinely positive in recent periods.
    DATEADD('day', -9, PROCESSED_DATE)              AS PROCESSED_DATE,
    DATE_TRUNC('month', DATEADD('day', -9, PROCESSED_DATE)) AS PERIOD_DATE,
    CUST_ACCT_SITE_ID, INVENTORY_ITEM_ID,
    ORDER_LINE_ID + 500000, CUSTOMER_TRX_LINE_ID + 500000,
    COMM_LINES_API_ID + 500000, KIT_INVENTORY_ITEM_ID,
    QUANTITY, QUANTITY_EXCL_RESUPPLY,
    -- Booked slightly above shipped: not everything booked ships in period.
    ROUND(TRANSACTION_AMOUNT * 1.06, 2),
    ROUND(TRANSACTION_AMOUNT_USD * 1.06, 2),
    ROUND(TRANSACTION_AMOUNT_EXCL_RESPLY * 1.06, 2),
    ROUND(TRAN_AMT_EXCL_RESPLY_USD * 1.06, 2),
    'ORD', 'Order', 'N', TRANSACTION_CURRENCY_CODE,
    ORDER_CATEGORY, ORDER_TYPE, 'BOOKING',
    ADJUST_STATUS, DISCOUNT_TYPE, LOAD_STATUS,
    NULL, NULL, NULL, NULL, NULL, NULL, NULL,
    CURRENT_DATE(), CURRENT_DATE()
FROM SALES_ANALYTICS.FACT_SALES_SUMMARY
WHERE MOD(ORDER_LINE_ID, 20) <> 0;   -- a few shipped lines have no booking record

-- ---------------------------------------------------------------------
