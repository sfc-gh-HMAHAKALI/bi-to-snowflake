-- =====================================================================
-- Path 3c: Cortex Search over the glossary + the Cortex agent
-- =====================================================================
-- Two tools, for two genuinely different question types:
--
--   query_sales   -- "what were bookings in the southeast last quarter"
--                        Structured. Goes to the semantic view.
--   lookup_definition -- "what does backlog mean here", "which metric should I
--                        use for growth"
--                        Definitional. Goes to the governed glossary.
--
-- The second tool is the point. Without it an agent asked "what is backlog"
-- answers from the language model's general knowledge, which is plausible,
-- fluent, and not the reference model's definition. With it, the answer is the definition a
-- steward signed off, with its lineage back to the Cognos calculation it came
-- from. That is the difference between an agent that sounds authoritative and one
-- that is.
-- =====================================================================

USE SCHEMA {{KB_DB}}.KNOWLEDGE_BASE;

-- ---------------------------------------------------------------------
-- Searchable projection of the glossary
-- ---------------------------------------------------------------------
-- Cortex Search needs one text column to index. Concatenating term, definition,
-- synonyms and lineage into a single searchable body means a user can find a term
-- by its Cognos name, its business name, or a synonym -- which matters here
-- because report authors know FINC_YR_ID while the warehouse stores
-- FISCAL_YEAR_ID.

CREATE OR REPLACE VIEW GLOSSARY_SEARCH_SOURCE AS
SELECT
    TERM_ID,
    BUSINESS_TERM,
    TERM_KIND,
    SUBJECT_AREA,
    CLASSIFICATION,
    COALESCE(SEMANTIC_RISK, 'none')            AS SEMANTIC_RISK,
    DEFINITION_SOURCE,
    CERTIFICATION_STATUS,
    -- The indexed body.
    BUSINESS_TERM
      || CASE WHEN ARRAY_SIZE(SYNONYMS) > 0
              THEN ' (also called: ' || ARRAY_TO_STRING(SYNONYMS, ', ') || ')'
              ELSE '' END
      || '. ' || COALESCE(DEFINITION, '')
      || CASE WHEN CALCULATION_LOGIC IS NOT NULL
              THEN ' Calculated as: ' || CALCULATION_LOGIC ELSE '' END
      || CASE WHEN PHYSICAL_BINDING IS NOT NULL
              THEN ' Bound to column ' || PHYSICAL_BINDING ELSE '' END
      || ' Subject area: ' || COALESCE(SUBJECT_AREA, 'unknown') || '.'
      || ' Classified as: ' || COALESCE(CLASSIFICATION, 'unknown') || '.'
      || CASE WHEN SEMANTIC_RISK <> 'none'
              THEN ' Caution: this term carries a known semantic hazard ('
                   || SEMANTIC_RISK || '), so naive aggregation of it is wrong.'
              ELSE '' END
      || CASE WHEN DEFINITION_SOURCE = 'auto_generated'
              THEN ' Note: this definition was generated from the column name and'
                   || ' has not been reviewed by a data steward.'
              ELSE '' END
      || ' Originally defined in BI model: ' || COALESCE(SOURCE_BI_MODEL, 'unknown')
      || ' (' || COALESCE(SOURCE_BI_SYSTEM, 'unknown') || ').'
                                               AS SEARCH_BODY
FROM BUSINESS_GLOSSARY;

CREATE OR REPLACE CORTEX SEARCH SERVICE {{SEARCH}}
  ON SEARCH_BODY
  ATTRIBUTES TERM_KIND, SUBJECT_AREA, CLASSIFICATION, SEMANTIC_RISK, DEFINITION_SOURCE, CERTIFICATION_STATUS
  WAREHOUSE = AI_ML_WH_SALES
  TARGET_LAG = '1 hour'
  COMMENT = 'Searchable business glossary derived from the semantic knowledge base.'
  AS SELECT TERM_ID, BUSINESS_TERM, TERM_KIND, SUBJECT_AREA, CLASSIFICATION,
            SEMANTIC_RISK, DEFINITION_SOURCE, CERTIFICATION_STATUS, SEARCH_BODY
     FROM GLOSSARY_SEARCH_SOURCE;
