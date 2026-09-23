-- =====================================================================
-- Synthetic dimension data
-- =====================================================================
-- Shaped to make the reference model's harder requirements demonstrable rather than arguable.
-- Specific decisions and why:
--
--  * Customer sites carry BOTH a BILL_TO and a SHIP_TO row per account. The
--    source Cognos model filters DIM_US_PROD_CUSTOMERS to SITE_USE_CODE =
--    'SHIP_TO'. With that filter the customer join is one-to-one; without it the
--    same join doubles every sales row. Generating only SHIP_TO rows would hide
--    the fan-out and make the missing-filter finding untestable.
--
--  * Territory values come from KB_SECURITY_MAPPING, so the row access policy is
--    exercised against the 367 real principals the Cognos model declares rather
--    than invented ones.
--
--  * The monthly forecast is genuinely monthly and the stock snapshot genuinely
--    daily, so E2 (multi-grain) and E6 (semi-additive) have real data to be
--    wrong about if handled naively.
-- =====================================================================

USE DATABASE {{DB}};

-- ---------------------------------------------------------------------
-- Product: 4-level hierarchy (GPC1..GPC4) keyed on INVENTORY_ITEM_ID
-- ---------------------------------------------------------------------

TRUNCATE TABLE IF EXISTS COMMON_ANALYTICS.EDW_ORA_COA_PROD_GFP_W_NORA;

INSERT INTO COMMON_ANALYTICS.EDW_ORA_COA_PROD_GFP_W_NORA
    (INVENTORY_ITEM_ID, ITEM_NO, INVENTORY_ITEM_DESC,
     GPC1, GPC2, GPC3, GPC4, GFP_GROUP,
     SOURCE, SOFT_DELETE_IND, ROW_CREATE_DATE, ROW_UPDATE_DATE,
     ENDED_DATE, TGT_MD5, DATA_LOAD_TIME, DATA_UPDATE_TIME)
WITH items AS (
    SELECT SEQ4() + 1 AS i FROM TABLE(GENERATOR(ROWCOUNT => 240))
), shaped AS (
    SELECT
        100000 + i AS item_id,
        i,
        -- Four product lines, each with sub-families, so a drill path has depth.
        CASE MOD(i, 4)
            WHEN 0 THEN 'Flow Generators'
            WHEN 1 THEN 'Masks'
            WHEN 2 THEN 'Accessories'
            ELSE 'Humidifiers'
        END AS gpc1,
        CASE MOD(i, 4)
            WHEN 0 THEN CASE MOD(i, 12) WHEN 0 THEN 'AirSense' WHEN 4 THEN 'AirCurve' ELSE 'AirMini' END
            WHEN 1 THEN CASE MOD(i, 12) WHEN 1 THEN 'Full Face' WHEN 5 THEN 'Nasal' ELSE 'Nasal Pillow' END
            WHEN 2 THEN CASE MOD(i, 12) WHEN 2 THEN 'Tubing' WHEN 6 THEN 'Filters' ELSE 'Cushions' END
            ELSE CASE MOD(i, 12) WHEN 3 THEN 'HumidAir' WHEN 7 THEN 'ClimateLine' ELSE 'Integrated' END
        END AS gpc2
    FROM items
)
SELECT
    item_id,
    'ITM-' || LPAD(i::VARCHAR, 5, '0'),
    gpc1 || ' / ' || gpc2 || ' model ' || (MOD(i, 9) + 1),
    gpc1,
    gpc2,
    gpc2 || ' Series ' || (MOD(i, 3) + 1),
    'SKU-' || LPAD(i::VARCHAR, 5, '0'),
    CASE MOD(i, 2) WHEN 0 THEN 'SLEEP' ELSE 'RESPIRATORY CARE' END,
    'EDW', 'N',
    TO_CHAR(DATE '2018-01-01', 'YYYY-MM-DD'),
    TO_CHAR(CURRENT_DATE(), 'YYYY-MM-DD'),
    NULL, MD5(item_id::VARCHAR), CURRENT_DATE(), CURRENT_DATE()
FROM shaped;

-- ---------------------------------------------------------------------
-- Customers: two site-use rows per account
-- ---------------------------------------------------------------------
-- This is the table that proves the missing-filter finding. Each account gets a
-- SHIP_TO and a BILL_TO site sharing CUSTOMER_ID but with distinct
-- CUST_ACCT_SITE_ID. Sales are written against SHIP_TO sites only, so:
--   with    SITE_USE_CODE='SHIP_TO' -> one customer row per sale
--   without SITE_USE_CODE='SHIP_TO' -> the BILL_TO row also matches on
--                                      CUSTOMER_ID-derived joins and totals inflate
-- ---------------------------------------------------------------------

TRUNCATE TABLE IF EXISTS COMMON_ANALYTICS.CUSTOMERS_US;

INSERT INTO COMMON_ANALYTICS.CUSTOMERS_US
    (CUST_ACCT_SITE_ID, CUSTOMER_ID, CUSTOMER_NUMBER, CUSTOMER_NAME, SITE_NAME,
     SITE_USE_CODE, SITE_USE_ID, PARTY_SITE_ID, PARTY_SITE_NUMBER,
     CUSTOMER_CLASS_CODE, CUSTOMER_TYPE, CUST_SEGMENT, BUSINESS_TYPE_SITE,
     CITY, STATE, CUSTOMER_COUNTRY, CUSTOMER_COUNTY,
     CUSTOMER_TIER1, CUSTOMER_TIER2, CUSTOMER_TIER3,
     CUST_ACCT_SITES_STATUS, ACTIVE_USLEEP_CUSTOMER_FLAG,
     PRIMARY_SALES_REP_NAME, SITE_CREATION_DATE,
     DATA_LOAD_TIME, DATA_UPDATE_TIME)
WITH accts AS (
    SELECT SEQ4() + 1 AS a FROM TABLE(GENERATOR(ROWCOUNT => 400))
), uses AS (
    SELECT 'SHIP_TO' AS use_code, 0 AS ofs
    UNION ALL SELECT 'BILL_TO', 500000
), shaped AS (
    SELECT
        a, use_code, ofs,
        CASE MOD(a, 6)
            WHEN 0 THEN '10 HOMECARE'
            WHEN 1 THEN '20 HOSPITAL'
            WHEN 2 THEN '30 DISTRIBUTOR'
            -- Two classes the source model explicitly excludes. Present on
            -- purpose so the exclusion filter has something to remove.
            WHEN 3 THEN '31 DIAGNOSTIK'
            WHEN 4 THEN '31 HAENDLER EXPORT'
            ELSE '40 RETAIL'
        END AS class_code,
        CASE MOD(a, 5)
            WHEN 0 THEN 'CA' WHEN 1 THEN 'TX' WHEN 2 THEN 'NY'
            WHEN 3 THEN 'OH' ELSE 'FL'
        END AS st
    FROM accts CROSS JOIN uses
)
SELECT
    200000 + a + ofs                                  AS CUST_ACCT_SITE_ID,
    300000 + a                                        AS CUSTOMER_ID,
    'CUST-' || LPAD(a::VARCHAR, 5, '0'),
    'Customer Account ' || a,
    'Site ' || a || ' (' || use_code || ')',
    use_code,
    400000 + a + ofs,
    500000 + a + ofs,
    'PS-' || LPAD(a::VARCHAR, 5, '0'),
    class_code,
    CASE MOD(a, 3) WHEN 0 THEN 'ORGANIZATION' ELSE 'PROVIDER' END,
    CASE MOD(a, 4) WHEN 0 THEN 'KEY ACCOUNT' WHEN 1 THEN 'NATIONAL' ELSE 'REGIONAL' END,
    CASE MOD(a, 2) WHEN 0 THEN 'DME' ELSE 'HOSPITAL' END,
    'City ' || MOD(a, 60),
    st,
    'US',
    'County ' || MOD(a, 30),
    CASE MOD(a, 4) WHEN 0 THEN 'TIER 1' WHEN 1 THEN 'TIER 2' ELSE 'TIER 3' END,
    'GROUP ' || MOD(a, 8),
    'SUBGROUP ' || MOD(a, 16),
    'ACTIVE',
    IFF(MOD(a, 20) = 0, 'N', 'Y'),
    'Rep ' || MOD(a, 40),
    DATEADD('day', -MOD(a * 7, 2000), CURRENT_DATE()),
    CURRENT_DATE(), CURRENT_DATE()
FROM shaped;

-- ---------------------------------------------------------------------
-- Territory: L1-L4 hierarchy, values taken from the real security mapping
-- ---------------------------------------------------------------------
-- Territory codes are the dotted paths the Cognos filters constrain
-- (EAST.GREATLAKES.KAM.CLEVELAND), assigned to SHIP_TO sites only. Using the
-- real values means the row access policy is tested against the principals the
-- source model actually declares.
-- ---------------------------------------------------------------------

TRUNCATE TABLE IF EXISTS SALES_ANALYTICS.TERRITORY_SITE_SALES_PERSON;

INSERT INTO SALES_ANALYTICS.TERRITORY_SITE_SALES_PERSON
    (CUST_ACCT_SITE_ID, SITE_USE_ID, SITE_NUMBER, TERRITORY_ID,
     TERRITORY_LEVEL1, TERRITORY_LEVEL2, TERRITORY_LEVEL3, TERRITORY_LEVEL4,
     TERRITORY_TYPE, TERRITORY_TIME_PERIOD, TERRITORY_START_DATE, TERRITORY_END_DATE,
     ROLE,
     L1_SALES_PERSON_ID, L1_SALES_PERSON_NAME, L1_SALES_PERSON_NUMBER,
     L1_SALES_PERSON_ROLE_NAME, L1_SALES_PERSON_EMAIL_ID,
     L2_SALES_PERSON_ID, L2_SALES_PERSON_NAME, L2_SALES_PERSON_NUMBER,
     L2_SALES_PERSON_ROLE_NAME, L2_SALES_PERSON_EMAIL_ID,
     L3_SALES_PERSON_ID, L3_SALES_PERSON_NAME, L3_SALES_PERSON_NUMBER,
     L3_SALES_PERSON_ROLE_NAME, L3_SALES_PERSON_EMAIL_ID,
     L4_SALES_PERSON_ID, L4_SALES_PERSON_NAME, L4_SALES_PERSON_NUMBER,
     L4_SALES_PERSON_ROLE_NAME, L4_SALES_PERSON_EMAIL_ID,
     L4_SALES_PERSON_ACTIVE_FLAG, L4_SALES_PERSON_START_DATE, L4_SALES_PERSON_END_DATE,
     L4_SALE_PERSON_TERR_ACTIVE_FLG, L4_SALE_PERSON_TERR_START_DATE,
     L4_SALE_PERSON_TERR_END_DATE,
     TERR_SITE_RELATION_START_DATE, TERR_SITE_RELATION_END_DATE,
     TERR_SITE_RELATION_TIME_PERIOD,
     DATA_LOAD_TIME, DATA_UPDATE_TIME)
WITH real_terr AS (
    -- The genuine L4 territory codes and their owning role, straight from the
    -- knowledge base rather than invented.
    SELECT DISTINCT COLUMN_VALUE AS terr4, ROLE_GROUP AS role_group
    FROM {{KB_DB}}.KNOWLEDGE_BASE.KB_SECURITY_MAPPING
    WHERE COLUMN_NAME = 'TERRITORY_LEVEL4'
      AND COLUMN_VALUE LIKE '%.%.%.%'
), numbered AS (
    SELECT terr4, role_group, ROW_NUMBER() OVER (ORDER BY terr4) AS rn,
           COUNT(*) OVER () AS n
    FROM real_terr
), sites AS (
    SELECT CUST_ACCT_SITE_ID, ROW_NUMBER() OVER (ORDER BY CUST_ACCT_SITE_ID) AS srn
    FROM COMMON_ANALYTICS.CUSTOMERS_US
    WHERE SITE_USE_CODE = 'SHIP_TO'
), assigned AS (
    SELECT
        s.CUST_ACCT_SITE_ID,
        s.srn,
        t.terr4,
        t.role_group,
        SPLIT_PART(t.terr4, '.', 1) AS l1,
        SPLIT_PART(t.terr4, '.', 2) AS l2,
        SPLIT_PART(t.terr4, '.', 3) AS l3
    FROM sites s
    JOIN numbered t ON t.rn = MOD(s.srn, t.n) + 1
)
SELECT
    CUST_ACCT_SITE_ID, 400000 + srn, 600000 + srn, 700000 + MOD(srn, 300),
    l1, l1 || '.' || l2, l1 || '.' || l2 || '.' || l3, terr4,
    'SALES', 'CURRENT', DATE '2018-07-01', DATE '2099-12-31',
    role_group,
    800000 + MOD(srn, 40),  'L1 Rep ' || MOD(srn, 40),  'E' || LPAD(MOD(srn, 40)::VARCHAR, 5, '0'),
        role_group, 'l1rep' || MOD(srn, 40) || '@example.com',
    810000 + MOD(srn, 20),  'L2 Mgr ' || MOD(srn, 20),  'E' || LPAD((1000 + MOD(srn, 20))::VARCHAR, 5, '0'),
        'DISTRICT MANAGER', 'l2mgr' || MOD(srn, 20) || '@example.com',
    820000 + MOD(srn, 8),   'L3 Dir ' || MOD(srn, 8),   'E' || LPAD((2000 + MOD(srn, 8))::VARCHAR, 5, '0'),
        'REGIONAL MANAGER', 'l3dir' || MOD(srn, 8) || '@example.com',
    830000 + MOD(srn, 3),   'L4 VP ' || MOD(srn, 3),    'E' || LPAD((3000 + MOD(srn, 3))::VARCHAR, 5, '0'),
        'VP SALES', 'l4vp' || MOD(srn, 3) || '@example.com',
    'Y', DATE '2018-07-01', DATE '2099-12-31',
    'Y', DATE '2018-07-01', DATE '2099-12-31',
    DATE '2018-07-01', DATE '2099-12-31', 'CURRENT',
    CURRENT_DATE(), CURRENT_DATE()
FROM assigned;

-- ---------------------------------------------------------------------
-- Currency conversion: validity ranges, not a key (requirement E4)
-- ---------------------------------------------------------------------

TRUNCATE TABLE IF EXISTS COMMON_ANALYTICS.DIM_CURRENCY_CONVERSION;

INSERT INTO COMMON_ANALYTICS.DIM_CURRENCY_CONVERSION
    (FROM_CURRENCY_CODE, TO_CURRENCY_CODE, START_DATE, END_DATE, CONVERSION_RATE)
WITH months AS (
    SELECT DATEADD('month', SEQ4(), DATE '2018-01-01') AS m
    FROM TABLE(GENERATOR(ROWCOUNT => 120))
), cur AS (
    SELECT 'EUR' AS c, 1.10 AS base UNION ALL
    SELECT 'GBP', 1.28 UNION ALL
    SELECT 'CAD', 0.74 UNION ALL
    SELECT 'AUD', 0.66 UNION ALL
    SELECT 'USD', 1.00
)
SELECT
    c, 'USD',
    m                                                   AS START_DATE,
    LAST_DAY(m)                                         AS END_DATE,
    -- Small deterministic drift so joining on the wrong period is visible.
    ROUND(base * (1 + (MOD(DATEDIFF('month', DATE '2018-01-01', m), 12) - 6) * 0.004), 8)
FROM months CROSS JOIN cur;

-- ---------------------------------------------------------------------
-- Supplier: shares no key with the sales star (requirement E3)
-- ---------------------------------------------------------------------

TRUNCATE TABLE IF EXISTS COMMON_ANALYTICS.DIM_SUPPLIER;

INSERT INTO COMMON_ANALYTICS.DIM_SUPPLIER (SUPPLIER_ID, SUPPLIER_NAME, SUPPLIER_COUNTRY, CONTRACT_TIER)
SELECT
    900000 + SEQ4(),
    'Supplier ' || (SEQ4() + 1),
    CASE MOD(SEQ4(), 4) WHEN 0 THEN 'US' WHEN 1 THEN 'CN' WHEN 2 THEN 'MY' ELSE 'DE' END,
    CASE MOD(SEQ4(), 3) WHEN 0 THEN 'STRATEGIC' WHEN 1 THEN 'PREFERRED' ELSE 'TRANSACTIONAL' END
FROM TABLE(GENERATOR(ROWCOUNT => 45));
