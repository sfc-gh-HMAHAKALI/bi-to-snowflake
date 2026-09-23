-- =====================================================================
-- Knowledge Base consumer views
-- =====================================================================
-- Every downstream consumer reads a view here, never a KB table directly.
--
-- That indirection is the point. The Horizon catalog load, the glossary, the
-- OSI export, the ontology builder, the BI front end and a steward's
-- spreadsheet are all equal-status consumers with no privileged path between
-- them. Adding a seventh consumer, or swapping Cognos for Power BI as the
-- source, does not touch the loader or any existing consumer.
--
-- All views are source-agnostic and carry SOURCE_SYSTEM / SOURCE_MODEL through,
-- so a consumer can filter to one model or span the whole estate.
-- =====================================================================

USE SCHEMA {{KB_DB}}.KNOWLEDGE_BASE;

-- ---------------------------------------------------------------------
-- Glossary
-- ---------------------------------------------------------------------

CREATE OR REPLACE VIEW KB_V_GLOSSARY
COMMENT = 'Business terms and metrics as one governed list. The glossary feed.'
AS
SELECT
    t.SOURCE_SYSTEM,
    t.SOURCE_MODEL,
    'TERM'                          AS TERM_KIND,
    t.TERM_NAME                     AS BUSINESS_TERM,
    t.ENTITY_NAME                   AS SUBJECT_AREA,
    t.TERM_ROLE                     AS CLASSIFICATION,
    t.DEFINITION,
    t.SYNONYMS,
    t.DATA_TYPE,
    e.PHYSICAL_DATABASE,
    e.PHYSICAL_SCHEMA,
    e.PHYSICAL_OBJECT,
    t.PHYSICAL_COLUMN,
    -- A term with no physical column exists only in the BI layer. Worth
    -- surfacing: it is the population that has no warehouse equivalent yet.
    IFF(t.PHYSICAL_COLUMN IS NULL, TRUE, FALSE) AS IS_MODEL_ONLY,
    t.EXPRESSION,
    t.CONVERSION_TIER,
    t.STEWARD,
    t.IS_CERTIFIED,
    t.LOADED_AT
FROM KB_TERM t
LEFT JOIN KB_ENTITY e
       ON e.SOURCE_SYSTEM = t.SOURCE_SYSTEM
      AND e.SOURCE_MODEL  = t.SOURCE_MODEL
      AND e.ENTITY_NAME   = t.ENTITY_NAME
UNION ALL
SELECT
    m.SOURCE_SYSTEM,
    m.SOURCE_MODEL,
    'METRIC'                        AS TERM_KIND,
    m.METRIC_CANONICAL              AS BUSINESS_TERM,
    'Metrics'                       AS SUBJECT_AREA,
    m.METRIC_KIND                   AS CLASSIFICATION,
    m.DEFINITION,
    m.SYNONYMS,
    'NUMBER'                        AS DATA_TYPE,
    NULL, NULL, NULL, NULL,
    TRUE                            AS IS_MODEL_ONLY,
    m.EXPRESSION,
    m.CONVERSION_TIER,
    m.STEWARD,
    m.IS_CERTIFIED,
    m.LOADED_AT
FROM KB_METRIC m;

-- ---------------------------------------------------------------------
-- Metric library
-- ---------------------------------------------------------------------

CREATE OR REPLACE VIEW KB_V_METRIC_LIBRARY
COMMENT = 'Metric patterns with their Snowflake expression and readiness to deploy.'
AS
SELECT
    SOURCE_SYSTEM,
    SOURCE_MODEL,
    METRIC_CANONICAL          AS METRIC,
    METRIC_KIND,
    TRANSLATED_EXPR           AS SNOWFLAKE_EXPR,
    EXPRESSION                AS SOURCE_EXPR,
    TIME_WINDOWS,
    REQUIRES_FISCAL_CALENDAR,
    FORMAT_KIND,
    FORMAT_DECIMALS,
    CURRENCY_CODE,
    CONVERSION_TIER,
    -- Readiness, not just convertibility. A metric that needs the fiscal
    -- calendar is convertible but not deployable until the date dimension's
    -- fiscal columns are bound, and shipping it with a date offset instead
    -- would be wrong only at period boundaries.
    CASE
        WHEN TRANSLATED_EXPR IS NULL OR TRANSLATED_EXPR = '' THEN 'not_convertible'
        WHEN REQUIRES_FISCAL_CALENDAR                       THEN 'needs_fiscal_binding'
        WHEN METRIC_KIND = 'unsupported'                     THEN 'not_convertible'
        ELSE 'ready'
    END                       AS DEPLOY_READINESS,
    IS_CERTIFIED,
    STEWARD
FROM KB_METRIC;

-- ---------------------------------------------------------------------
-- Hierarchy tree (drives the business-user item picker, requirement A2)
-- ---------------------------------------------------------------------

CREATE OR REPLACE VIEW KB_V_HIERARCHY_TREE
COMMENT = 'Flattened drill paths with a display path per level. Feeds a metadata-driven picker.'
AS
SELECT
    l.SOURCE_SYSTEM,
    l.SOURCE_MODEL,
    h.DIMENSION_NAME,
    h.HIERARCHY_NAME,
    h.IS_DEFAULT              AS IS_DEFAULT_HIERARCHY,
    h.LEVEL_COUNT,
    l.LEVEL_ORDINAL,
    l.LEVEL_NAME,
    l.IS_ALL_LEVEL,
    l.CAPTION_TERM,
    l.BUSINESS_KEY_TERM,
    l.SOURCE_ENTITY,
    l.COLUMNS,
    -- Dotted path so a UI can render a tree without recursing.
    h.DIMENSION_NAME || ' > ' || h.HIERARCHY_NAME || ' > ' || l.LEVEL_NAME AS DISPLAY_PATH
FROM KB_HIERARCHY_LEVEL l
JOIN KB_HIERARCHY h
  ON h.SOURCE_SYSTEM   = l.SOURCE_SYSTEM
 AND h.SOURCE_MODEL    = l.SOURCE_MODEL
 AND h.DIMENSION_NAME  = l.DIMENSION_NAME
 AND h.HIERARCHY_NAME  = l.HIERARCHY_NAME;

-- ---------------------------------------------------------------------
-- Lineage
-- ---------------------------------------------------------------------

CREATE OR REPLACE VIEW KB_V_LINEAGE
COMMENT = 'Term/metric to physical column edges, with the physical object resolved.'
AS
SELECT
    lg.SOURCE_SYSTEM,
    lg.SOURCE_MODEL,
    lg.FROM_KIND,
    lg.FROM_NAME,
    lg.FROM_ENTITY,
    lg.EDGE_TYPE,
    lg.TO_KIND,
    lg.TO_NAME,
    lg.TO_ENTITY,
    e.PHYSICAL_DATABASE,
    e.PHYSICAL_SCHEMA,
    e.PHYSICAL_OBJECT,
    CASE
        WHEN lg.TO_KIND = 'physical_column'
         AND e.PHYSICAL_OBJECT IS NOT NULL
        THEN e.PHYSICAL_DATABASE || '.' || e.PHYSICAL_SCHEMA || '.'
             || e.PHYSICAL_OBJECT || '.' || lg.TO_NAME
    END AS FULLY_QUALIFIED_TARGET
FROM KB_LINEAGE lg
LEFT JOIN KB_ENTITY e
       ON e.SOURCE_SYSTEM = lg.SOURCE_SYSTEM
      AND e.SOURCE_MODEL  = lg.SOURCE_MODEL
      AND e.ENTITY_NAME   = COALESCE(NULLIF(lg.TO_ENTITY, ''), lg.FROM_ENTITY);

-- ---------------------------------------------------------------------
-- Security
-- ---------------------------------------------------------------------

CREATE OR REPLACE VIEW KB_V_SECURITY_RULES
COMMENT = 'Row-security rules with the level number abstracted, to show how few policies are needed.'
AS
SELECT
    SOURCE_SYSTEM,
    SOURCE_MODEL,
    ROLE_GROUP,
    COLUMN_NAME,
    -- TERRITORY_LEVEL4 and TERRITORY_LEVEL2 are the same rule at different
    -- depths. Generalising the level number is what turns a per-level policy
    -- count into a per-pattern one.
    REGEXP_REPLACE(COLUMN_NAME, '[0-9]+$', '#') AS COLUMN_PATTERN,
    COUNT(DISTINCT PRINCIPAL)                    AS PRINCIPAL_COUNT,
    COUNT(DISTINCT COLUMN_VALUE)                 AS DISTINCT_VALUES,
    COUNT(*)                                     AS GRANT_COUNT
FROM KB_SECURITY_MAPPING
GROUP BY ALL;

CREATE OR REPLACE VIEW KB_V_SECURITY_COLLAPSE
COMMENT = 'How many source filter definitions collapse into how many Snowflake policies.'
AS
SELECT
    r.SOURCE_SYSTEM,
    r.SOURCE_MODEL,
    COUNT(*)                                        AS SOURCE_FILTER_DEFINITIONS,
    COUNT(DISTINCT r.PRINCIPAL)                     AS DISTINCT_PRINCIPALS,
    (SELECT COUNT(*) FROM KB_SECURITY_MAPPING m
      WHERE m.SOURCE_SYSTEM = r.SOURCE_SYSTEM
        AND m.SOURCE_MODEL  = r.SOURCE_MODEL)       AS MAPPING_ROWS,
    (SELECT COUNT(DISTINCT REGEXP_REPLACE(m.COLUMN_NAME, '[0-9]+$', '#'))
       FROM KB_SECURITY_MAPPING m
      WHERE m.SOURCE_SYSTEM = r.SOURCE_SYSTEM
        AND m.SOURCE_MODEL  = r.SOURCE_MODEL)       AS POLICY_PATTERNS_REQUIRED
FROM KB_SECURITY_RULE r
GROUP BY r.SOURCE_SYSTEM, r.SOURCE_MODEL;

-- ---------------------------------------------------------------------
-- Duplication analysis
-- ---------------------------------------------------------------------

CREATE OR REPLACE VIEW KB_V_CLONE_ANALYSIS
COMMENT = 'Object and metric duplication in the source model, by clone family.'
AS
SELECT
    SOURCE_SYSTEM,
    SOURCE_MODEL,
    'ENTITY'                       AS OBJECT_KIND,
    ENTITY_CANONICAL               AS CANONICAL_NAME,
    COUNT(*)                       AS VARIANT_COUNT,
    ARRAY_AGG(COALESCE(CLONE_VARIANT, 'base')) WITHIN GROUP (ORDER BY CLONE_VARIANT) AS VARIANTS
FROM KB_ENTITY
GROUP BY SOURCE_SYSTEM, SOURCE_MODEL, ENTITY_CANONICAL
HAVING COUNT(*) > 1
UNION ALL
SELECT
    SOURCE_SYSTEM,
    SOURCE_MODEL,
    'METRIC'                       AS OBJECT_KIND,
    METRIC_CANONICAL               AS CANONICAL_NAME,
    COUNT(*)                       AS VARIANT_COUNT,
    ARRAY_AGG(COALESCE(CLONE_VARIANT, 'base')) WITHIN GROUP (ORDER BY CLONE_VARIANT) AS VARIANTS
FROM KB_METRIC
GROUP BY SOURCE_SYSTEM, SOURCE_MODEL, METRIC_CANONICAL
HAVING COUNT(*) > 1;

-- ---------------------------------------------------------------------
-- Grain and aggregation risk
-- ---------------------------------------------------------------------

CREATE OR REPLACE VIEW KB_V_AGGREGATION_RISK
COMMENT = 'Entities where a naive SUM can be wrong: declared grain, or semi-additive measures.'
AS
SELECT
    e.SOURCE_SYSTEM,
    e.SOURCE_MODEL,
    e.ENTITY_NAME,
    e.PHYSICAL_OBJECT,
    COUNT(DISTINCT g.GRAIN_NAME)                                     AS GRAIN_DECLARATIONS,
    COUNT(DISTINCT IFF(g.DOUBLE_COUNT_RISK, g.GRAIN_NAME, NULL))     AS RISKY_GRAINS,
    COUNT(DISTINCT IFF(a.IS_SEMI_ADDITIVE, a.TERM_NAME, NULL))       AS SEMI_ADDITIVE_MEASURES,
    MAX(g.RISK_REASON)                                               AS GRAIN_RISK_REASON,
    CASE
        WHEN COUNT(DISTINCT IFF(g.DOUBLE_COUNT_RISK, g.GRAIN_NAME, NULL)) > 0
          OR COUNT(DISTINCT IFF(a.IS_SEMI_ADDITIVE, a.TERM_NAME, NULL)) > 0
        THEN TRUE ELSE FALSE
    END                                                              AS NEEDS_REVIEW
FROM KB_ENTITY e
LEFT JOIN KB_GRAIN g
       ON g.SOURCE_SYSTEM = e.SOURCE_SYSTEM
      AND g.SOURCE_MODEL  = e.SOURCE_MODEL
      AND g.ENTITY_NAME   = e.ENTITY_NAME
LEFT JOIN KB_AGGREGATION_RULE a
       ON a.SOURCE_SYSTEM = e.SOURCE_SYSTEM
      AND a.SOURCE_MODEL  = e.SOURCE_MODEL
      AND a.ENTITY_NAME   = e.ENTITY_NAME
GROUP BY ALL;

-- ---------------------------------------------------------------------
-- Coverage
-- ---------------------------------------------------------------------

CREATE OR REPLACE VIEW KB_V_COVERAGE
COMMENT = 'What the source artifact contained versus what was extracted and is deployable.'
AS
SELECT
    sm.SOURCE_SYSTEM,
    sm.SOURCE_MODEL,
    sm.SOURCE_FILE,
    sm.LOADED_AT,
    (SELECT COUNT(*) FROM KB_ENTITY e
      WHERE e.SOURCE_SYSTEM = sm.SOURCE_SYSTEM AND e.SOURCE_MODEL = sm.SOURCE_MODEL) AS ENTITIES,
    (SELECT COUNT(*) FROM KB_TERM t
      WHERE t.SOURCE_SYSTEM = sm.SOURCE_SYSTEM AND t.SOURCE_MODEL = sm.SOURCE_MODEL) AS TERMS,
    (SELECT COUNT(*) FROM KB_METRIC m
      WHERE m.SOURCE_SYSTEM = sm.SOURCE_SYSTEM AND m.SOURCE_MODEL = sm.SOURCE_MODEL) AS METRIC_PATTERNS,
    (SELECT COUNT(*) FROM KB_METRIC m
      WHERE m.SOURCE_SYSTEM = sm.SOURCE_SYSTEM AND m.SOURCE_MODEL = sm.SOURCE_MODEL
        AND m.TRANSLATED_EXPR IS NOT NULL AND m.METRIC_KIND <> 'unsupported')        AS METRICS_TRANSLATED,
    (SELECT COUNT(*) FROM KB_METRIC m
      WHERE m.SOURCE_SYSTEM = sm.SOURCE_SYSTEM AND m.SOURCE_MODEL = sm.SOURCE_MODEL
        AND m.REQUIRES_FISCAL_CALENDAR)                                              AS METRICS_NEEDING_FISCAL,
    (SELECT COUNT(*) FROM KB_HIERARCHY h
      WHERE h.SOURCE_SYSTEM = sm.SOURCE_SYSTEM AND h.SOURCE_MODEL = sm.SOURCE_MODEL) AS HIERARCHIES,
    (SELECT COUNT(*) FROM KB_RELATIONSHIP r
      WHERE r.SOURCE_SYSTEM = sm.SOURCE_SYSTEM AND r.SOURCE_MODEL = sm.SOURCE_MODEL) AS RELATIONSHIPS,
    (SELECT COUNT(*) FROM KB_RELATIONSHIP r
      WHERE r.SOURCE_SYSTEM = sm.SOURCE_SYSTEM AND r.SOURCE_MODEL = sm.SOURCE_MODEL
        AND r.SOURCE_STATUS <> 'valid')                                              AS RELATIONSHIPS_FLAGGED_BY_SOURCE,
    (SELECT COUNT(*) FROM KB_RELATIONSHIP r
      WHERE r.SOURCE_SYSTEM = sm.SOURCE_SYSTEM AND r.SOURCE_MODEL = sm.SOURCE_MODEL
        AND r.SOURCE_STATUS <> 'valid' AND r.IS_SIMPLE_EQUALITY)                     AS FLAGGED_BUT_SIMPLE_EQUALITY,
    (SELECT COUNT(*) FROM KB_SECURITY_RULE s
      WHERE s.SOURCE_SYSTEM = sm.SOURCE_SYSTEM AND s.SOURCE_MODEL = sm.SOURCE_MODEL) AS SECURITY_RULES,
    (SELECT COUNT(*) FROM KB_GRAIN g
      WHERE g.SOURCE_SYSTEM = sm.SOURCE_SYSTEM AND g.SOURCE_MODEL = sm.SOURCE_MODEL) AS GRAIN_DECLARATIONS,
    (SELECT COUNT(*) FROM KB_ISSUE i
      WHERE i.SOURCE_SYSTEM = sm.SOURCE_SYSTEM AND i.SOURCE_MODEL = sm.SOURCE_MODEL
        AND i.SEVERITY = 'blocker')                                                  AS OPEN_BLOCKERS,
    (SELECT COUNT(*) FROM KB_ISSUE i
      WHERE i.SOURCE_SYSTEM = sm.SOURCE_SYSTEM AND i.SOURCE_MODEL = sm.SOURCE_MODEL
        AND i.SEVERITY = 'risk')                                                     AS OPEN_RISKS,
    sm.ELEMENT_COUNTS,
    sm.EXTRACTION_SUMMARY
FROM KB_SOURCE_MODEL sm;

-- ---------------------------------------------------------------------
-- Physical binding, for the paths that generate DDL or semantic views
-- ---------------------------------------------------------------------

CREATE OR REPLACE VIEW KB_V_PHYSICAL_BINDING
COMMENT = 'Entity to physical object mapping, with the row filters the source model applies.'
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
    COUNT(t.TERM_NAME)                                      AS TERM_COUNT,
    COUNT(IFF(t.TERM_ROLE = 'fact', 1, NULL))               AS MEASURE_COUNT,
    COUNT(IFF(t.TERM_ROLE = 'identifier', 1, NULL))         AS KEY_COUNT
FROM KB_ENTITY e
LEFT JOIN KB_TERM t
       ON t.SOURCE_SYSTEM = e.SOURCE_SYSTEM
      AND t.SOURCE_MODEL  = e.SOURCE_MODEL
      AND t.ENTITY_NAME   = e.ENTITY_NAME
WHERE e.PHYSICAL_OBJECT IS NOT NULL
GROUP BY ALL;

-- ---------------------------------------------------------------------
-- Open issues, ordered for review
-- ---------------------------------------------------------------------

CREATE OR REPLACE VIEW KB_V_ISSUES
COMMENT = 'The honesty ledger, ordered blocker-first. Read this before trusting any conversion.'
AS
SELECT
    SOURCE_SYSTEM,
    SOURCE_MODEL,
    SEVERITY,
    CATEGORY,
    OBJECT_KIND,
    OBJECT_NAME,
    ISSUE,
    IMPLICATION,
    DETAIL,
    LOADED_AT
FROM KB_ISSUE
ORDER BY CASE SEVERITY WHEN 'blocker' THEN 1 WHEN 'risk' THEN 2 ELSE 3 END,
         CATEGORY, OBJECT_NAME;
