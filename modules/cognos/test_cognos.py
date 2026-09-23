"""Tests for the Cognos Framework Manager adapter.

Focused on the logic most likely to break silently: datatype narrowing, the
fiscal-vs-Gregorian split, member alias resolution, scalar-tail extraction, and
security column normalisation. These are all regex- or rule-driven, so a wrong
answer looks plausible rather than raising.

Run with:  python3 -m pytest modules/cognos/test_cognos.py -q
"""

from __future__ import annotations

import textwrap

import pytest

from modules.cognos import analysis, expressions as X, rls
from modules.cognos.classifier import (
    MANUAL_REQUIRED,
    NEEDS_TRANSLATION,
    SIMPLE,
    classify_cognos_expression,
)
from modules.cognos.parser import (
    canonical_calculation_name,
    map_cognos_datatype,
    parse_refobj,
    parse_framework_manager_model,
)


# ---------------------------------------------------------------------------
# Datatype narrowing
# ---------------------------------------------------------------------------


class TestDatatype:
    def test_datetime_with_zero_precision_is_date(self):
        # Every dateTime in the reference model is precision=0 size=12, i.e. a
        # physical DATE that the Cognos JDBC driver widened.
        assert map_cognos_datatype("dateTime", scale="6", precision="0", size="12") == "DATE"
        assert map_cognos_datatype("dateTime", scale="0", precision="0", size="12") == "DATE"

    def test_datetime_with_real_precision_stays_timestamp(self):
        assert (
            map_cognos_datatype("dateTime", scale="9", precision="9", size="16")
            == "TIMESTAMP_NTZ"
        )

    def test_character_length_token_is_not_a_width(self):
        # characterLength16 is an encoding marker, not a 16-char limit.
        assert map_cognos_datatype("characterLength16", precision="4000", size="8002") == "VARCHAR"

    def test_unknown_token_falls_back_to_varchar(self):
        assert map_cognos_datatype("someFutureType") == "VARCHAR"
        assert map_cognos_datatype("") == "VARCHAR"


# ---------------------------------------------------------------------------
# Object references
# ---------------------------------------------------------------------------


class TestRefobj:
    def test_three_part_reference(self):
        r = parse_refobj("[Import View].[FACT_SALES_SUMMARY].[PROCESSED_DATE]")
        assert r["namespace"] == "Import View"
        assert r["object"] == "FACT_SALES_SUMMARY"
        assert r["item"] == "PROCESSED_DATE"

    def test_four_part_dmr_reference_keeps_last_as_item(self):
        # DMR hierarchies duplicate the hierarchy name in the middle.
        r = parse_refobj("[DMR View].[WTD Grouped].[WTD Grouped].[RELATIVE_TIME_DESC]")
        assert r["item"] == "RELATIVE_TIME_DESC"
        assert r["namespace"] == "DMR View"

    def test_empty_reference_does_not_raise(self):
        assert parse_refobj("")["item"] == ""


# ---------------------------------------------------------------------------
# Fiscal-year clone collapsing
# ---------------------------------------------------------------------------


class TestCloneNames:
    @pytest.mark.parametrize(
        "raw,base,fy",
        [
            ("YTD Growth_FY23", "YTD Growth", "FY23"),
            ("YTD Growth FY22", "YTD Growth", "FY22"),
            ("YTD Growth", "YTD Growth", None),
        ],
    )
    def test_calculation_names(self, raw, base, fy):
        assert canonical_calculation_name(raw) == (base, fy)

    def test_object_names(self):
        assert analysis.canonical_object_name("FACT_SALES_SUMMARY_FY22") == (
            "FACT_SALES_SUMMARY",
            "FY22",
        )
        assert analysis.canonical_object_name("FACT_SALES_SUMMARY") == (
            "FACT_SALES_SUMMARY",
            None,
        )

    def test_fy_inside_name_is_not_stripped(self):
        # Only a trailing suffix is a clone marker.
        assert analysis.canonical_object_name("DIM_FY_CALENDAR")[1] is None


# ---------------------------------------------------------------------------
# Relative-time translation
# ---------------------------------------------------------------------------


class TestRelativeTime:
    def test_gregorian_member_resolves(self):
        assert X.window_for_member("Week To Date") is not None
        assert X.window_for_member("week to date") is not None  # case-insensitive

    def test_alias_resolves_to_shared_definition(self):
        assert X.window_for_member("Prior Last 3 Months") == X.window_for_member(
            "Previous 3 Months"
        )
        assert X.window_for_member("Previous (to Last) 6 Months") == X.window_for_member(
            "Previous 6 Months"
        )

    def test_n_mon_shorthand(self):
        # "1 Mon" is the single completed month one month back.
        w = X.window_for_member("1 Mon")
        assert w is not None and "month" in w[0]

    def test_fiscal_member_returns_no_gregorian_window(self):
        # The important negative case: a fiscal window must not be given
        # CURRENT_DATE() arithmetic, because the reference model's fiscal calendar does not
        # align to calendar boundaries.
        assert X.is_fiscal_member("Fiscal Year To Date")
        assert X.window_for_member("Fiscal Year To Date") is None
        assert X.fiscal_requirement("Fiscal Year To Date")["grain"] == "year"

    def test_fiscal_calculation_flags_rather_than_guesses(self):
        expr = "[X]->[all].[Fiscal Year To Date] - [X]->[all].[Prior Fiscal Year To Date]"
        out = X.translate_relative_time_calculation(expr, "amt", "d")
        assert out["kind"] == "needs_fiscal_calendar"
        assert out["expr"] == ""
        assert len(out["fiscal_members"]) == 2

    def test_fiscal_calculation_translates_with_columns(self):
        expr = "[X]->[all].[Fiscal Year To Date] - [X]->[all].[Prior Fiscal Year To Date]"
        out = X.translate_relative_time_calculation(
            expr,
            "amt",
            "d",
            {"year": "dd.fiscal_year_id", "year_current": "2026"},
        )
        assert out["kind"] == "change"
        assert "fiscal_year_id" in out["expr"]
        assert "CURRENT_DATE" not in out["expr"]

    def test_growth_uses_nullif_not_div0(self):
        expr = (
            "([X]->[all].[Week To Date] - [X]->[all].[Prior Week To Date]) "
            "/ [X]->[all].[Prior Week To Date]"
        )
        out = X.translate_relative_time_calculation(expr, "amt", "d")
        assert out["kind"] == "growth"
        # A zero base makes growth undefined, not zero.
        assert "NULLIF" in out["expr"]
        assert "DIV0" not in out["expr"]

    def test_scalar_tail_is_read_from_expression(self):
        # 6 Months Rate is (Last 3 Months / 3) * 6 in the source model -- a
        # 6-month projection off a 3-month base. Reading the operators from the
        # expression rather than the metric name is the whole point.
        assert X.extract_scalar_tail("[DMR].[Last 3 Months]->[all] /3") == "/3"
        assert X.extract_scalar_tail("([DMR].[Last 3 Months]->[all] /3)*12") == "/3*12"
        assert X.extract_scalar_tail("([DMR].[Last 3 Months]->[all] /3)*6") == "/3*6"
        assert X.extract_scalar_tail("[X]->[all].[Week To Date]") == ""

    def test_rate_kind_applies_tail(self):
        out = X.translate_relative_time_calculation(
            "[DMR View].[Last 3 Months].[Last 3 Months].[Last 3 Months]->[all] /3",
            "amt",
            "d",
        )
        assert out["kind"] == "rate"
        assert out["expr"].endswith("/3")

    def test_unknown_member_is_unsupported_not_silently_wrong(self):
        out = X.translate_relative_time_calculation(
            "[X]->[all].[Some Bucket We Have Never Seen]", "amt", "d"
        )
        assert out["kind"] == "unsupported"
        assert out["expr"] == ""
        assert out["unresolved"] == ["Some Bucket We Have Never Seen"]


# ---------------------------------------------------------------------------
# Scalar function translation
# ---------------------------------------------------------------------------


class TestScalarTranslation:
    def test_add_months_argument_order_is_swapped(self):
        # Cognos _add_months(date, n) -> Snowflake DATEADD('month', n, date).
        out = X.translate_scalar_expression("_add_months(SALE_DATE, -3)")
        assert out == "DATEADD('month', -3, SALE_DATE)"

    def test_days_between_argument_order_is_swapped(self):
        out = X.translate_scalar_expression("_days_between(END_DT, START_DT)")
        assert out == "DATEDIFF('day', START_DT, END_DT)"

    def test_nested_call_preserved(self):
        out = X.translate_scalar_expression("_add_days(_first_of_month(D), 5)")
        assert "DATEADD('day', 5," in out
        assert "DATE_TRUNC('month', D)" in out

    def test_sysdate_becomes_current_date(self):
        assert "CURRENT_DATE()" in X.translate_scalar_expression("sysdate")


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


class TestClassifier:
    def test_empty_expression_is_simple_passthrough(self):
        # No expression means a plain physical column, the easiest case.
        assert classify_cognos_expression("") == SIMPLE

    def test_member_reference_needs_human_decision(self):
        assert classify_cognos_expression("[X]->[all].[Week To Date]") == MANUAL_REQUIRED

    def test_macro_cannot_be_resolved_statically(self):
        assert classify_cognos_expression("#prompt('p_region')#") == MANUAL_REQUIRED

    def test_scalar_function_is_translatable(self):
        assert classify_cognos_expression("TO_CHAR(D, 'YYYY')") == NEEDS_TRANSLATION

    def test_olap_function_is_manual(self):
        assert classify_cognos_expression("aggregate(currentMeasure)") == MANUAL_REQUIRED


# ---------------------------------------------------------------------------
# Security normalisation
# ---------------------------------------------------------------------------


class TestSecurityNormalisation:
    def test_spelling_variants_collapse(self):
        # The same logical column across Import/Model/DMR namespaces.
        assert (
            rls.normalize_column("Territory Level 4")
            == rls.normalize_column("TERRITORY_LEVEL4")
            == rls.normalize_column("territory_level_4")
            == "TERRITORY_LEVEL4"
        )

    def test_role_case_variants_collapse(self):
        assert rls.normalize_column("Role") == rls.normalize_column("ROLE") == "ROLE"

    def test_level_number_generalizes_for_policy_count(self):
        # Four territory levels are one policy taking the level as a parameter.
        assert rls.generalize_column("TERRITORY_LEVEL4") == "TERRITORY_LEVEL#"
        assert rls.generalize_column("Territory Level 2") == "TERRITORY_LEVEL#"
        assert rls.generalize_column("ROLE") == "ROLE"

    def test_policy_has_no_fail_open_branch(self):
        sql = rls.generate_row_access_policy(
            {"hierarchical_columns": ["TERRITORY_LEVEL4"]},
            "P",
            "DB.S.MAP",
            "TERRITORY_LEVEL4",
        )
        # An unmapped principal must see nothing, so there is no NOT EXISTS escape.
        assert "NOT EXISTS" not in sql
        assert "LIKE m.column_value || '%'" in sql  # prefix match on dotted path

    def test_non_hierarchical_column_uses_equality(self):
        sql = rls.generate_row_access_policy(
            {"hierarchical_columns": []}, "P", "DB.S.MAP", "REGION"
        )
        assert "REGION = m.column_value" in sql


# ---------------------------------------------------------------------------
# End-to-end parse of a minimal model
# ---------------------------------------------------------------------------

_MINIMAL_MODEL = textwrap.dedent(
    """\
    <?xml version="1.0" encoding="UTF-8" ?>
    <project xmlns="http://www.developer.cognos.com/schemas/bmt/60/12">
      <name>Tiny Model</name>
      <namespace>
        <name locale="en">Import View</name>
        <querySubject status="valid">
          <name locale="en">FACT_X</name>
          <definition><dbQuery>
            <sources><dataSourceRef>[].[dataSources].[SF_CONN]</dataSourceRef></sources>
            <sql type="cognos">Select * from [SF_CONN].FACT_X</sql>
            <tableType>table</tableType>
          </dbQuery></definition>
          <determinants>
            <determinant>
              <name>MONTH_ID</name>
              <key><refobj>[Import View].[FACT_X].[MONTH_ID]</refobj></key>
              <attributes><refobj>[Import View].[FACT_X].[AMT]</refobj></attributes>
              <canGroup>true</canGroup>
              <identifiesRow>false</identifiesRow>
            </determinant>
          </determinants>
          <queryItem>
            <name locale="en">AMT</name>
            <externalName>AMOUNT</externalName>
            <usage>fact</usage>
            <datatype>decimal</datatype>
            <precision>38</precision><scale>2</scale><size>20</size>
            <nullable>true</nullable>
            <regularAggregate>sum</regularAggregate>
            <semiAggregate>last</semiAggregate>
          </queryItem>
        </querySubject>
      </namespace>
    </project>
    """
)


@pytest.fixture
def minimal_model(tmp_path):
    p = tmp_path / "model.xml"
    p.write_text(_MINIMAL_MODEL, encoding="utf-8")
    return str(p)


class TestEndToEnd:
    def test_parses_structure_and_namespace(self, minimal_model):
        r = parse_framework_manager_model(minimal_model)
        assert r["errors"] == []
        assert len(r["query_subjects"]) == 1
        qs = r["query_subjects"][0]
        assert qs["name"] == "FACT_X"
        # The namespace must resolve even though <name> closes before siblings.
        assert qs["namespace"] == "Import View"
        assert qs["data_source"] == "SF_CONN"
        assert qs["is_passthrough"] is True
        assert qs["physical_object"] == "FACT_X"

    def test_semi_additive_detected(self, minimal_model):
        r = parse_framework_manager_model(minimal_model)
        rules = analysis.extract_aggregation_rules(r)
        amt = next(x for x in rules if x["term"] == "AMT")
        # regular=sum, rollup=last -> semi-additive closing balance.
        assert amt["is_semi_additive"] is True
        assert amt["regular_sql"] == "SUM"
        assert amt["rollup_sql"] == "LAST_VALUE"

    def test_groupable_determinant_raises_grain_risk(self, minimal_model):
        r = parse_framework_manager_model(minimal_model)
        audit = analysis.audit_grain_coverage(r, ["FACT_X"])
        assert audit["in_scope_count"] == 1
        assert audit["risks"], "a groupable non-row-grain determinant is a fan-out risk"

    def test_accepts_directory_path(self, minimal_model, tmp_path):
        r = parse_framework_manager_model(str(tmp_path))
        assert r["query_subjects"][0]["name"] == "FACT_X"

    def test_missing_file_raises_parse_error(self):
        from modules.common.errors import ParseError

        with pytest.raises(ParseError):
            parse_framework_manager_model("/nonexistent/model.xml")
