"""Retain reusable guards while replacing fixed-model phase assertions."""
import builtins
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_pipeline_regressions as legacy


def test_reusable_legacy_static_guards():
    legacy.__builtins__ = builtins
    legacy.failures.clear()
    legacy.test_no_undefined_module_names()
    legacy.test_sql_templates_are_fstrings()
    legacy.test_no_module_level_fstring_constants()
    legacy.test_kb_global_is_rebound_where_declared()
    legacy.test_charts_library_hardening()
    legacy.test_composition_rules_cover_the_silent_defects()
    assert not legacy.failures
