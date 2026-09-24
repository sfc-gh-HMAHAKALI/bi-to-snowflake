-- =====================================================================
-- Path 3b: row access policy + Cortex Agent
-- =====================================================================
-- The 19,051 Cognos security filter definitions collapse to 888 mapping rows and
-- two policy patterns. This builds the policy from the mapping the knowledge base
-- already derived, rather than hand-writing rules.
--
-- Design decisions that matter:
--
--  * Prefix matching on the dotted territory path, not equality. Territory codes
--    are a hierarchy encoded as a string (EAST.GREATLAKES.KAM.CLEVELAND), so a
--    principal mapped to EAST.GREATLAKES should see every descendant. Equality
--    would give a manager access to their own node and nothing beneath it, which
--    is the L1-through-L4 inheritance case the previous handoff flagged as
--    unaddressed.
--
--  * No fail-open branch. A principal with no mapping row sees no rows. Adding
--    an "OR NOT EXISTS (...)" escape so unmapped users see everything is how a
--    row access policy silently stops protecting anything, and it fails in the
--    direction of disclosure.
--
--  * The policy is applied to the base tables, not to the semantic view. That way
--    it governs every consumer equally -- the semantic view, a Cortex Agent, a
--    Streamlit app, MicroStrategy over JDBC, and an ad-hoc SELECT all get the
--    same rows. Enforcing in the semantic layer would leave the base tables open.
-- =====================================================================

USE DATABASE {{DB}};

-- ---------------------------------------------------------------------
-- Bypass role for administrators and pipeline service accounts
-- ---------------------------------------------------------------------
-- A named bypass is better than a fail-open policy: it is explicit, grantable,
-- auditable, and revocable, where a fail-open branch is invisible.

CREATE ROLE IF NOT EXISTS {{SECURITY_ROLE}}
    COMMENT = 'Bypasses territory row access. Grant deliberately and audit.';

GRANT ROLE {{SECURITY_ROLE}} TO ROLE SYSADMIN;

-- ---------------------------------------------------------------------
-- Demo principal mapping
-- ---------------------------------------------------------------------
-- The 888 mapping rows are keyed to Cognos account names, which do not exist as
-- Snowflake users here. Two demo principals are added so the policy can be shown
-- working end to end: one scoped to a single L4 territory, one scoped to an L2
-- parent so hierarchical inheritance is visible.
--
-- Deleted before inserting, keyed on LOAD_ID: a plain INSERT duplicates these two
-- rows on every re-run. The policy is EXISTS-based so duplicates do not change
-- who sees what, which is exactly why this would have gone unnoticed.

DELETE FROM {{KB_DB}}.KNOWLEDGE_BASE.KB_SECURITY_MAPPING
WHERE LOAD_ID = 'demo-principals';

INSERT INTO {{KB_DB}}.KNOWLEDGE_BASE.KB_SECURITY_MAPPING
    (SOURCE_SYSTEM, SOURCE_MODEL, PRINCIPAL, ROLE_GROUP, COLUMN_NAME, COLUMN_VALUE, ENTITY_NAME, LOAD_ID)
SELECT 'cognos', '{{MODEL_LABEL}}', 'DEMO_L4_REP', 'KAM-CENTRAL',
       'TERRITORY_LEVEL4', MIN(TERRITORY_LEVEL4), 'TERRITORY_SITE_SALES_PERSON', 'demo-principals'
FROM SALES_ANALYTICS.TERRITORY_SITE_SALES_PERSON
UNION ALL
SELECT 'cognos', '{{MODEL_LABEL}}', 'DEMO_L2_MANAGER', 'RSM',
       'TERRITORY_LEVEL4', MIN(TERRITORY_LEVEL2), 'TERRITORY_SITE_SALES_PERSON', 'demo-principals'
FROM SALES_ANALYTICS.TERRITORY_SITE_SALES_PERSON;

-- ---------------------------------------------------------------------
-- The policy
-- ---------------------------------------------------------------------
-- Detached before it is replaced. Snowflake permits CREATE OR REPLACE on a row
-- access policy only while it is unattached: once any table references it, the
-- replace is refused with "cannot be dropped/replaced as it is associated with
-- one or more entities", so a second run of this phase fails where the first
-- succeeded. DROP ALL ROW ACCESS POLICIES is the idempotent form -- it is not an
-- error when nothing is attached, unlike DROP ROW ACCESS POLICY <name>, so this
-- works on a first build and a rebuild alike.

ALTER TABLE SALES_ANALYTICS.TERRITORY_SITE_SALES_PERSON
  DROP ALL ROW ACCESS POLICIES;

CREATE OR REPLACE ROW ACCESS POLICY SALES_ANALYTICS.{{POLICY}}
  AS (TERRITORY_LEVEL4 VARCHAR) RETURNS BOOLEAN ->
  -- Prefix match: a principal mapped to a parent node in the dotted path sees
  -- all descendants, which is what reproduces L1-L4 role inheritance.
  EXISTS (
      SELECT 1
      FROM {{KB_DB}}.KNOWLEDGE_BASE.KB_SECURITY_MAPPING m
      WHERE m.PRINCIPAL   = CURRENT_USER()
        AND m.COLUMN_NAME = 'TERRITORY_LEVEL4'
        AND TERRITORY_LEVEL4 LIKE m.COLUMN_VALUE || '%'
  )
  -- Explicit, auditable bypass. Deliberately not a fail-open branch.
  OR IS_ROLE_IN_SESSION('{{SECURITY_ROLE}}')
;

COMMENT ON ROW ACCESS POLICY SALES_ANALYTICS.{{POLICY}} IS
'Replaces 19,051 Cognos security filter definitions across ~250 role-based package exports. Reads KB_SECURITY_MAPPING; prefix-matches the dotted territory hierarchy.';

ALTER TABLE SALES_ANALYTICS.TERRITORY_SITE_SALES_PERSON
  ADD ROW ACCESS POLICY SALES_ANALYTICS.{{POLICY}} ON (TERRITORY_LEVEL4);
