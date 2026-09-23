-- =====================================================================
-- Knowledge Base view corrections and measure-quality detection
-- =====================================================================

USE SCHEMA {{KB_DB}}.KNOWLEDGE_BASE;

-- The loader writes TERM_ROLE = 'measure' for items the source declares as
-- facts; the original view filtered on the literal 'fact' and so counted zero.
CREATE OR REPLACE VIEW KB_V_PHYSICAL_BINDING
COMMENT = 'Entity to physical object mapping with term/measure/key counts.'
AS
SELECT
    e.SOURCE_SYSTEM,
    e.SOURCE_MODEL,
    e.ENTITY_NAME,
    e.ENTITY_CANONICAL,
    e.CLONE_VARIANT,
    e.PHYSICAL_DATABASE,
    e.PHYSICAL_SCHEMA,
    e.PHYSICAL_OBJECT,
    e.SOURCE_CONNECTION,
    e.SOURCE_PLATFORM,
    e.IS_PASSTHROUGH,
    e.CONVERSION_TIER,
    e.SOURCE_STATUS,
    COUNT(t.TERM_NAME)                                        AS TERM_COUNT,
    COUNT(IFF(t.TERM_ROLE = 'measure', 1, NULL))              AS MEASURE_COUNT,
    COUNT(IFF(t.TERM_ROLE = 'identifier', 1, NULL))           AS KEY_COUNT,
    COUNT(IFF(t.TERM_ROLE = 'attribute', 1, NULL))            AS ATTRIBUTE_COUNT
FROM KB_ENTITY e
LEFT JOIN KB_TERM t
       ON t.SOURCE_SYSTEM = e.SOURCE_SYSTEM
      AND t.SOURCE_MODEL  = e.SOURCE_MODEL
      AND t.ENTITY_NAME   = e.ENTITY_NAME
WHERE e.PHYSICAL_OBJECT IS NOT NULL
GROUP BY ALL;

-- ---------------------------------------------------------------------
-- Measure quality
-- ---------------------------------------------------------------------
-- The source model declares join keys as measures. In the reference model
-- FACT_SALES_SUMMARY declares CUST_ACCT_SITE_ID, INVENTORY_ITEM_ID,
-- ORDER_LINE_ID, CUSTOMER_TRX_LINE_ID, KIT_INVENTORY_ITEM_ID and two account
-- numbers all with usage=fact -- 7 of its 14 declared measures are identifiers.
--
-- This matters because the declaration is what an automated conversion trusts.
-- Taken literally it produces SUM(INVENTORY_ITEM_ID) as a business metric, which
-- is meaningless but will compute happily and appear in a metric picker. Trusting
-- the source declaration is still the right default -- it beats name heuristics
-- for genuine measures -- but it has to be checked where the declaration and the
-- name disagree, which is exactly what this view surfaces.
--
-- The same column is declared usage=identifier on the dimension side, so the
-- model contradicts itself rather than being consistently wrong.
CREATE OR REPLACE VIEW KB_V_MEASURE_QUALITY
COMMENT = 'Declared measures that are probably identifiers. Prevents SUM(some_id) reaching a metric list.'
AS
WITH declared AS (
    SELECT
        t.SOURCE_SYSTEM,
        t.SOURCE_MODEL,
        t.ENTITY_NAME,
        t.TERM_NAME,
        t.PHYSICAL_COLUMN,
        t.DATA_TYPE,
        a.REGULAR_AGGREGATE,
        a.ROLLUP_AGGREGATE,
        -- Name-shape evidence, used only to contradict a declaration, never to
        -- create one.
        (t.TERM_NAME ILIKE '%\\_ID' ESCAPE '\\'
         OR t.TERM_NAME ILIKE '%\\_NUMBER' ESCAPE '\\'
         OR t.TERM_NAME ILIKE '%\\_CODE' ESCAPE '\\'
         OR t.TERM_NAME ILIKE '%\\_KEY' ESCAPE '\\')            AS NAME_LOOKS_LIKE_KEY,
        -- A genuine additive measure is declared sum. count/min/max on a numeric
        -- column is what Cognos falls back to when it cannot treat it as additive.
        (a.REGULAR_AGGREGATE IN ('count', 'countdistinct', 'min', 'max')) AS AGG_LOOKS_LIKE_KEY
    FROM KB_TERM t
    LEFT JOIN KB_AGGREGATION_RULE a
           ON a.SOURCE_SYSTEM = t.SOURCE_SYSTEM
          AND a.SOURCE_MODEL  = t.SOURCE_MODEL
          AND a.ENTITY_NAME   = t.ENTITY_NAME
          AND a.TERM_NAME     = t.TERM_NAME
    WHERE t.TERM_ROLE = 'measure'
)
SELECT
    SOURCE_SYSTEM,
    SOURCE_MODEL,
    ENTITY_NAME,
    TERM_NAME,
    PHYSICAL_COLUMN,
    DATA_TYPE,
    REGULAR_AGGREGATE,
    ROLLUP_AGGREGATE,
    NAME_LOOKS_LIKE_KEY,
    AGG_LOOKS_LIKE_KEY,
    CASE
        WHEN NAME_LOOKS_LIKE_KEY AND AGG_LOOKS_LIKE_KEY THEN 'almost_certainly_identifier'
        WHEN NAME_LOOKS_LIKE_KEY                        THEN 'probably_identifier'
        WHEN AGG_LOOKS_LIKE_KEY                         THEN 'possibly_identifier'
        ELSE 'genuine_measure'
    END AS VERDICT,
    -- The actionable field: only terms passing this should become metrics.
    IFF(NAME_LOOKS_LIKE_KEY OR AGG_LOOKS_LIKE_KEY, FALSE, TRUE) AS SAFE_TO_EXPOSE_AS_METRIC
FROM declared;

-- Convenience view: the measures that are actually safe to build metrics from.
CREATE OR REPLACE VIEW KB_V_SAFE_MEASURES
COMMENT = 'Declared measures that survive the identifier check. The metric-generation input.'
AS
SELECT
    q.SOURCE_SYSTEM,
    q.SOURCE_MODEL,
    q.ENTITY_NAME,
    q.TERM_NAME,
    q.PHYSICAL_COLUMN,
    q.DATA_TYPE,
    q.REGULAR_AGGREGATE,
    q.ROLLUP_AGGREGATE,
    b.PHYSICAL_SCHEMA,
    b.PHYSICAL_OBJECT
FROM KB_V_MEASURE_QUALITY q
JOIN KB_V_PHYSICAL_BINDING b
  ON b.SOURCE_SYSTEM = q.SOURCE_SYSTEM
 AND b.SOURCE_MODEL  = q.SOURCE_MODEL
 AND b.ENTITY_NAME   = q.ENTITY_NAME
WHERE q.SAFE_TO_EXPOSE_AS_METRIC;
