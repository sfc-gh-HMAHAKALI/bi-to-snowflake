#!/usr/bin/env bash
# Every check that needs neither a Snowflake connection nor a generated app.
# Run before shipping any change to this skill.
set -u
cd "$(dirname "$0")/.."
fail=0

echo "== python syntax =="
python3 -m compileall -q modules pipeline assets/streamlit_ui tests || fail=1

echo "== cognos adapter tests =="
python3 -m pytest modules/cognos/test_cognos.py -q || fail=1

echo "== component libraries expose every shape =="
python3 tests/check_libraries.py || fail=1

echo "== composition rules cite only real components =="
python3 tests/check_rules.py || fail=1

echo "== fingerprint self-comparison is 100% =="
python3 tests/composition_fingerprint.py compare \
  tests/reference/react.json tests/reference/react.json >/dev/null || fail=1
python3 tests/composition_fingerprint.py compare \
  tests/reference/streamlit.json tests/reference/streamlit.json >/dev/null || fail=1
echo "  both reference fingerprints self-compare clean"

echo "== every DDL placeholder is known to config =="
python3 tests/check_placeholders.py || fail=1

echo "== no customer identifiers =="
python3 tests/check_identifiers.py || fail=1

echo "== teardown refuses to act without --execute =="
if grep -q 'help="Actually run it"' pipeline/teardown.py; then
  echo "  --execute required"
else
  echo "  MISSING the --execute guard"; fail=1
fi

echo
if [ "$fail" -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "FAILURES ABOVE"; fi
exit "$fail"
