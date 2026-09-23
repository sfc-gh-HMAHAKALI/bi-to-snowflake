-- =====================================================================
-- Deploy the report as a Streamlit in Snowflake app (container runtime)
-- =====================================================================
-- Two things changed from the first attempt at this, both because the first attempt
-- shipped a broken app.
--
-- 1. Dependencies are declared. Deployed without a dependency file, a warehouse-runtime
--    app falls back to Streamlit 1.22.0, and a single `hide_index=True` raised a
--    TypeError that aborted the script: the tab bar rendered, every tab was empty, and
--    the sidebar would not open. pyproject.toml pins the version, and bim_ui/compat.py
--    degrades gracefully if it is ever wrong again.
--
-- 2. Container runtime rather than warehouse runtime, which is what makes the pinning
--    possible -- warehouse runtime installs only from the Snowflake Anaconda Channel,
--    container runtime resolves from the PyPI artifact repository with uv.
--
-- Worth knowing about the tradeoff: a container-runtime app cold-starts after its
-- compute pool auto-suspends. On a screenshare that pause reads as breakage, so open
-- the app once before presenting.
-- =====================================================================

USE SCHEMA {{DB}}.ANALYTICS;

-- XS is sufficient: this app fetches 3,108 rows once and filters in memory. The
-- warehouse does the aggregation; the pool only runs the Python process.
CREATE COMPUTE POOL IF NOT EXISTS {{POOL}}
  MIN_NODES = 1
  MAX_NODES = 1
  INSTANCE_FAMILY = CPU_X64_XS
  AUTO_RESUME = TRUE
  AUTO_SUSPEND_SECS = 300
  COMMENT = 'Container runtime for the analytics Streamlit app.';

-- Required for uv to resolve plotly and the pinned Streamlit. Without it the runtime
-- can only use what ships in the base image, and a version specifier on a
-- pre-installed package errors when that image is next rebuilt.
GRANT DATABASE ROLE SNOWFLAKE.PYPI_REPOSITORY_USER TO ROLE ACCOUNTADMIN;

CREATE STAGE IF NOT EXISTS {{STAGE}}
  DIRECTORY = (ENABLE = TRUE)
  COMMENT = 'Source for the analytics Streamlit app.';
