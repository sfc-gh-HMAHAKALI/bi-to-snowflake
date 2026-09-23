
USE DATABASE {{DB}};

TRUNCATE TABLE IF EXISTS COMMON_ANALYTICS.DIM_DATE;

INSERT INTO COMMON_ANALYTICS.DIM_DATE (
    DAY_DATE, DAY_NUMBER, DAY_WORK_NUMBER, BUSINESS_DAY_FLAG,
    CALENDAR_MONTH_ID, CALENDAR_MONTH_NAME,
    CALENDAR_QUARTER_ID, CALENDAR_QUARTER_NAME,
    CALENDAR_YEAR_ID, CALENDAR_YEAR_NAME,
    FISCAL_YEAR_ID, FISCAL_YEAR_NAME,
    FISCAL_QUARTER_ID, FISCAL_QUARTER_NAME, FISCAL_QUARTER_YEAR,
    FISCAL_MONTH_ID, FISCAL_MONTH_NAME,
    MONTH_YEAR, WEEK_NUMBER_OF_YEAR, START_OF_WEEK,
    START_WEEK_NAME, END_WEEK_NAME,
    DATA_LOAD_TIME, DATA_UPDATE_TIME
)
WITH d AS (
    SELECT DATEADD('day', SEQ4(), DATE '2018-01-01') AS dt
    FROM TABLE(GENERATOR(ROWCOUNT => 3652))
), f AS (
    SELECT
        dt,
        -- Fiscal year starts 1 July: Jul-Dec rolls into the next fiscal year.
        IFF(MONTH(dt) >= 7, YEAR(dt) + 1, YEAR(dt))       AS fy,
        -- Fiscal month 1 = July. Shifting the ordinal, not the date.
        IFF(MONTH(dt) >= 7, MONTH(dt) - 6, MONTH(dt) + 6)  AS fm
    FROM d
)
SELECT
    dt                                                         AS DAY_DATE,
    DAYOFMONTH(dt)                                             AS DAY_NUMBER,
    -- Working-day ordinal within the fiscal month, for business-day reporting.
    SUM(IFF(DAYOFWEEK(dt) BETWEEN 1 AND 5, 1, 0))
        OVER (PARTITION BY fy, fm ORDER BY dt)                 AS DAY_WORK_NUMBER,
    IFF(DAYOFWEEK(dt) BETWEEN 1 AND 5, 1, 0)                   AS BUSINESS_DAY_FLAG,
    YEAR(dt) * 100 + MONTH(dt)                                 AS CALENDAR_MONTH_ID,
    MONTHNAME(dt)                                              AS CALENDAR_MONTH_NAME,
    YEAR(dt) * 10 + QUARTER(dt)                                AS CALENDAR_QUARTER_ID,
    'CY' || YEAR(dt) || ' Q' || QUARTER(dt)                     AS CALENDAR_QUARTER_NAME,
    YEAR(dt)                                                   AS CALENDAR_YEAR_ID,
    'CY' || YEAR(dt)                                           AS CALENDAR_YEAR_NAME,
    fy                                                         AS FISCAL_YEAR_ID,
    'FY' || fy                                                 AS FISCAL_YEAR_NAME,
    fy * 10 + CEIL(fm / 3.0)                                   AS FISCAL_QUARTER_ID,
    'Q' || CEIL(fm / 3.0)                                      AS FISCAL_QUARTER_NAME,
    'FY' || fy || ' Q' || CEIL(fm / 3.0)                        AS FISCAL_QUARTER_YEAR,
    fy * 100 + fm                                              AS FISCAL_MONTH_ID,
    'FY' || fy || ' P' || LPAD(fm::VARCHAR, 2, '0')              AS FISCAL_MONTH_NAME,
    MONTHNAME(dt) || '-' || YEAR(dt)                            AS MONTH_YEAR,
    WEEKOFYEAR(dt)                                             AS WEEK_NUMBER_OF_YEAR,
    DATE_TRUNC('week', dt)                                     AS START_OF_WEEK,
    TO_CHAR(DATE_TRUNC('week', dt), 'DD-MON-YY')               AS START_WEEK_NAME,
    TO_CHAR(DATEADD('day', 6, DATE_TRUNC('week', dt)), 'DD-MON-YY') AS END_WEEK_NAME,
    CURRENT_DATE()                                             AS DATA_LOAD_TIME,
    CURRENT_DATE()                                             AS DATA_UPDATE_TIME
FROM f;
