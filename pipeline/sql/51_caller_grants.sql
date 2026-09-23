-- =====================================================================
-- Caller grants for the App Runtime report
-- =====================================================================
-- Why this file exists: the React app queried its views and got
--
--   Object '{{DB}}.ANALYTICS.RPT_SALES_BY_TERRITORY' does not exist or not
--   authorized. This executable runs with restricted caller's rights. The owner role
--   ACCOUNTADMIN must have at least one CALLER privilege granted on TABLE ...
--
-- which is easy to misread as a missing object. It is not. App Runtime services run with
-- *restricted* caller's rights, and that is a deliberate two-key design:
--
--   * the viewer must hold the privilege, and
--   * an administrator must separately allow the app to *use* that privilege on their
--     behalf, via GRANT CALLER.
--
-- A caller grant confers nothing by itself. It cannot widen what a viewer can see; it only
-- decides which of their existing privileges the app may exercise. So an app cannot
-- quietly become a privilege-escalation route just because someone deployed it, which is
-- the whole point.
--
-- Granted to ACCOUNTADMIN because that is the role that owns the application service.
-- Caller grants attach to the owner of the executable, not to the viewer.
-- =====================================================================

-- Container-level first: without USAGE on the database and schema, object-level grants
-- are unreachable and the error is identical to having no grant at all.
GRANT CALLER USAGE ON DATABASE {{DB}} TO ROLE ACCOUNTADMIN;
GRANT CALLER USAGE ON SCHEMA {{DB}}.ANALYTICS TO ROLE ACCOUNTADMIN;
GRANT CALLER USAGE ON SCHEMA {{DB}}.SALES_ANALYTICS TO ROLE ACCOUNTADMIN;
GRANT CALLER USAGE ON SCHEMA {{DB}}.COMMON_ANALYTICS TO ROLE ACCOUNTADMIN;

-- The reporting views the app reads.
--
-- IMPORTANT: this is point-in-time, not forward-looking. An earlier comment here claimed
-- INHERITED covered views added later; it does not. INHERITED concerns the role hierarchy,
-- not future objects, and `ON FUTURE VIEWS` is rejected outright for CALLER grants --
-- "syntax error ... unexpected 'FUTURE'". Proven the hard way: adding
-- RPT_FISCAL_CALENDAR_EXPOSURE left the React app failing with "Could not load Fiscal year
-- to date against calendar year to date" while the owner-rights Streamlit app worked fine,
-- until this statement was re-run.
--
-- So: re-run this file after adding any RPT_ view. There is no way to make it automatic.
GRANT INHERITED CALLER SELECT ON ALL VIEWS IN SCHEMA {{DB}}.ANALYTICS
  TO ROLE ACCOUNTADMIN;

-- The base tables beneath those views. Needed because the row access policy is enforced
-- here: this is precisely where the viewer's own entitlement is evaluated, and it is the
-- reason the app shows different rows to different people rather than one shared answer.
GRANT INHERITED CALLER SELECT ON ALL TABLES IN SCHEMA {{DB}}.SALES_ANALYTICS
  TO ROLE ACCOUNTADMIN;
GRANT INHERITED CALLER SELECT ON ALL TABLES IN SCHEMA {{DB}}.COMMON_ANALYTICS
  TO ROLE ACCOUNTADMIN;

-- The semantic view, which the RPT_ views resolve through.
GRANT CALLER SELECT ON SEMANTIC VIEW {{DB}}.ANALYTICS.{{SEMANTIC_VIEW}}
  TO ROLE ACCOUNTADMIN;

-- The agent bridge. Without this the charts load and only the chat panel fails, which is
-- a confusing half-working state to debug.
GRANT CALLER USAGE ON PROCEDURE {{DB}}.ANALYTICS.{{ASK_PROC}}(STRING, STRING)
  TO ROLE ACCOUNTADMIN;

-- Compute for the queries.
GRANT CALLER USAGE ON WAREHOUSE AI_ML_WH_SALES TO ROLE ACCOUNTADMIN;
