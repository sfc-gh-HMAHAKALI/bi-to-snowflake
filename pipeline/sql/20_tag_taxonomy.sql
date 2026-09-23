
USE SCHEMA {{KB_DB}}.KNOWLEDGE_BASE;

-- Tags live in the KB schema, not on the data, so the taxonomy is versioned
-- alongside the metadata that populates it.
CREATE TAG IF NOT EXISTS SUBJECT_AREA
    COMMENT = 'Business subject area this object belongs to, from the source BI model';
CREATE TAG IF NOT EXISTS SOURCE_BI_MODEL
    COMMENT = 'BI model the definition was extracted from';
CREATE TAG IF NOT EXISTS CONVERSION_TIER
    ALLOWED_VALUES 'simple', 'needs_translation', 'manual_required'
    COMMENT = 'How cleanly this object converts from its BI source';
CREATE TAG IF NOT EXISTS DATA_STEWARD
    COMMENT = 'Accountable steward. Unset until assigned by governance';
CREATE TAG IF NOT EXISTS TERM_ROLE
    ALLOWED_VALUES 'measure', 'identifier', 'attribute'
    COMMENT = 'Role the source BI model declares for this column';
CREATE TAG IF NOT EXISTS SEMANTIC_RISK
    ALLOWED_VALUES 'none', 'misdeclared_measure', 'semi_additive', 'grain_sensitive', 'filter_dependent'
    COMMENT = 'Known semantic hazard that makes naive aggregation wrong';
