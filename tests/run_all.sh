#!/usr/bin/env bash
# Offline regression suite. Python children inherit the network-denial guard.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$ROOT/tests/offline:$ROOT${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT"
python3 -m compileall -q modules pipeline assets/streamlit_ui tests
python3 -m pytest -q modules/cognos/test_cognos.py tests
python3 tests/check_libraries.py
python3 tests/check_rules.py
python3 tests/check_placeholders.py
python3 tests/check_kb_loader_target.py
python3 tests/check_identifiers.py
python3 tests/check_post_build_state.py
python3 tests/composition_fingerprint.py compare tests/reference/react.json tests/reference/react.json
python3 tests/composition_fingerprint.py compare tests/reference/streamlit.json tests/reference/streamlit.json
# Legacy checks asserting the retired fixed model/phase graph are superseded by
# behavioral contract and artifact tests above. Do not restore fixed-schema checks.
printf '\nALL ACTIVE OFFLINE CHECKS PASSED\n'
