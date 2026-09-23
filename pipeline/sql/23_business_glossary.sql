
USE SCHEMA {{KB_DB}}.KNOWLEDGE_BASE;

-- Shape follows the existing BUSINESS_GLOSSARY already in this account
-- (LAB_EFFICIENCY_DATA_PRODUCT.METADATA_REGISTRY) so a steward sees one
-- consistent structure rather than a second competing glossary format.
CREATE OR REPLACE TABLE BUSINESS_GLOSSARY (
    TERM_ID              VARCHAR       COMMENT 'Stable id: source system + model + term',
    BUSINESS_TERM        VARCHAR       NOT NULL,
    TERM_KIND            VARCHAR       COMMENT 'TERM | METRIC',
    SUBJECT_AREA         VARCHAR,
    CLASSIFICATION       VARCHAR       COMMENT 'measure | identifier | attribute, or metric kind',
    DEFINITION           VARCHAR,
    -- Explicit rather than inferred. A steward must be able to see at a glance
    -- which definitions are machine-generated placeholders.
    DEFINITION_SOURCE    VARCHAR       COMMENT 'source_model | auto_generated | steward_authored',
    CALCULATION_LOGIC    VARCHAR       COMMENT 'Snowflake expression, for metrics',
    SYNONYMS             ARRAY,
    PHYSICAL_BINDING     VARCHAR       COMMENT 'Fully qualified column, where one exists',
    SOURCE_BI_MODEL      VARCHAR,
    SOURCE_BI_SYSTEM     VARCHAR,
    SEMANTIC_RISK        VARCHAR       COMMENT 'Known hazard that makes naive use wrong',
    CONVERSION_TIER      VARCHAR,
    STEWARD              VARCHAR,
    CERTIFICATION_STATUS VARCHAR       DEFAULT 'DRAFT'
                                       COMMENT 'DRAFT until a steward reviews. Never set by automation',
    LAST_UPDATED         TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'Governed business glossary, generated from the semantic knowledge base.';

USE SCHEMA {{KB_DB}}.KNOWLEDGE_BASE;

INSERT INTO BUSINESS_GLOSSARY
    (TERM_ID, BUSINESS_TERM, TERM_KIND, SUBJECT_AREA, CLASSIFICATION, DEFINITION,
     DEFINITION_SOURCE, CALCULATION_LOGIC, SYNONYMS, PHYSICAL_BINDING,
     SOURCE_BI_MODEL, SOURCE_BI_SYSTEM, SEMANTIC_RISK, CONVERSION_TIER, STEWARD)
WITH risk AS (
    SELECT SOURCE_SYSTEM, SOURCE_MODEL, ENTITY_NAME, TERM_NAME,
           'misdeclared_measure' AS RISK
    FROM KB_V_MEASURE_QUALITY WHERE NOT SAFE_TO_EXPOSE_AS_METRIC
    UNION ALL
    SELECT SOURCE_SYSTEM, SOURCE_MODEL, ENTITY_NAME, TERM_NAME, 'semi_additive'
    FROM KB_AGGREGATION_RULE WHERE IS_SEMI_ADDITIVE
)
SELECT
    g.SOURCE_SYSTEM || ':' || g.SOURCE_MODEL || ':' || g.SUBJECT_AREA || ':' || g.BUSINESS_TERM,
    g.BUSINESS_TERM,
    g.TERM_KIND,
    g.SUBJECT_AREA,
    g.CLASSIFICATION,
    g.DEFINITION,
    -- The auto-generated marker is written into the definition text by the
    -- loader, so detecting it here keeps one source of truth for that decision.
    IFF(g.DEFINITION ILIKE '%auto-generated from the source name%',
        'auto_generated', 'source_model')                       AS DEFINITION_SOURCE,
    -- Joined rather than a correlated scalar subquery: Snowflake cannot evaluate
    -- a subquery correlated on several outer columns in a projection.
    m.TRANSLATED_EXPR                                            AS CALCULATION_LOGIC,
    g.SYNONYMS,
    CASE WHEN g.PHYSICAL_OBJECT IS NOT NULL
         THEN g.PHYSICAL_DATABASE || '.' || g.PHYSICAL_SCHEMA || '.'
              || g.PHYSICAL_OBJECT || '.' || g.PHYSICAL_COLUMN
    END                                                          AS PHYSICAL_BINDING,
    g.SOURCE_MODEL,
    g.SOURCE_SYSTEM,
    COALESCE(r.RISK, 'none')                                     AS SEMANTIC_RISK,
    g.CONVERSION_TIER,
    g.STEWARD
FROM KB_V_GLOSSARY g
LEFT JOIN KB_METRIC m
       ON g.TERM_KIND       = 'METRIC'
      AND m.SOURCE_SYSTEM    = g.SOURCE_SYSTEM
      AND m.SOURCE_MODEL     = g.SOURCE_MODEL
      AND m.METRIC_CANONICAL = g.BUSINESS_TERM
LEFT JOIN risk r
       ON r.SOURCE_SYSTEM = g.SOURCE_SYSTEM
      AND r.SOURCE_MODEL  = g.SOURCE_MODEL
      AND r.ENTITY_NAME   = g.SUBJECT_AREA
      AND r.TERM_NAME     = g.BUSINESS_TERM;
