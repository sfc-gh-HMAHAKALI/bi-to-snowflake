-- =====================================================================
-- Open backlog: orders booked that have not shipped
-- =====================================================================
-- Deriving bookings purely by shifting sales back nine days made bookings a
-- strict function of shipments. Any window beginning at a period boundary then
-- loses nine days of bookings, and backlog goes negative -- an artefact of the
-- generator rather than a modelling error, but one that reads as a broken metric.
--
-- Real backlog is orders with no shipment yet, so those rows have to exist
-- independently. These are booked within the last 45 days and have no matching
-- sales line at all, which is what makes bookings genuinely lead sales in every
-- window rather than only in aggregate.
-- =====================================================================

USE DATABASE {{DB}};

DELETE FROM SALES_ANALYTICS.FACT_BOOKED_SUMMARY WHERE LOAD_STATUS = 'OPEN_BACKLOG';

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
WITH n AS (
    SELECT SEQ8() AS s FROM TABLE(GENERATOR(ROWCOUNT => 12000))
), sites AS (
    SELECT CUST_ACCT_SITE_ID, ROW_NUMBER() OVER (ORDER BY CUST_ACCT_SITE_ID) - 1 AS rn,
           COUNT(*) OVER () AS cnt
    FROM COMMON_ANALYTICS.CUSTOMERS_US WHERE SITE_USE_CODE = 'SHIP_TO'
), items AS (
    SELECT INVENTORY_ITEM_ID, GPC1,
           ROW_NUMBER() OVER (ORDER BY INVENTORY_ITEM_ID) - 1 AS rn,
           COUNT(*) OVER () AS cnt
    FROM COMMON_ANALYTICS.EDW_ORA_COA_PROD_GFP_W_NORA
), base AS (
    SELECT s,
           -- Spread over the last 75 days so the open backlog spans the fiscal
           -- year boundary as well as the current quarter.
           DATEADD('day', -MOD(s * 7919, 75), CURRENT_DATE()) AS raw_dt,
           MOD(s * 31, (SELECT cnt FROM sites LIMIT 1)) AS site_rn,
           MOD(s * 17, (SELECT cnt FROM items LIMIT 1)) AS item_rn
    FROM n
), dated AS (
    SELECT b.*,
           IFF(DAYOFWEEK(raw_dt) = 0, DATEADD('day', 1, raw_dt),
               IFF(DAYOFWEEK(raw_dt) = 6, DATEADD('day', 2, raw_dt), raw_dt)) AS dt
    FROM base b
), joined AS (
    SELECT d.s, d.dt, si.CUST_ACCT_SITE_ID, it.INVENTORY_ITEM_ID, it.GPC1,
           CASE it.GPC1 WHEN 'Flow Generators' THEN 780 WHEN 'Humidifiers' THEN 240
                        WHEN 'Masks' THEN 95 ELSE 28 END AS unit_price
    FROM dated d
    JOIN sites si ON si.rn = d.site_rn
    JOIN items it ON it.rn = d.item_rn
)
SELECT
    dt, DATE_TRUNC('month', dt), CUST_ACCT_SITE_ID, INVENTORY_ITEM_ID,
    9000000 + s, 9500000 + s, 9800000 + s, NULL,
    qty, qty, amt, amt, amt, amt,
    'ORD', 'Order', 'N', 'USD',
    'NEW EQUIPMENT',
    CASE MOD(s, 3) WHEN 0 THEN 'STANDARD' WHEN 1 THEN 'DROP SHIP' ELSE 'STOCK' END,
    'BOOKING',
    'NONE', 'NONE',
    -- Flagged so open backlog is identifiable rather than merely inferred from
    -- the absence of a shipment.
    'OPEN_BACKLOG',
    NULL, NULL, NULL, NULL, NULL, NULL, NULL,
    CURRENT_DATE(), CURRENT_DATE()
FROM (
    SELECT j.*, (MOD(s, 9) + 1) AS qty,
           ROUND(unit_price * (MOD(s, 9) + 1) * (1 - MOD(s, 11) * 0.01), 2) AS amt
    FROM joined j
);
