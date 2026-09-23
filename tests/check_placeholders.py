"""Every {{NAME}} placeholder in the DDL must be known to config.

A placeholder config cannot resolve would reach Snowflake as an invalid identifier
and fail its phase after earlier statements in the same build had already committed.
Cheaper to catch here.
"""
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))
import config  # noqa: E402

SQL_DIR = os.path.join(os.path.dirname(__file__), "..", "pipeline", "sql")


def main() -> int:
    total = 0
    for name in sorted(os.listdir(SQL_DIR)):
        if not name.endswith(".sql"):
            continue
        text = io.open(os.path.join(SQL_DIR, name), encoding="utf-8").read()
        try:
            rendered = config.render_sql(text)
        except KeyError as exc:
            print("  FAIL %s: %s" % (name, exc))
            return 1
        if "{{" in rendered:
            print("  FAIL %s: a placeholder survived substitution" % name)
            return 1
        total += 1
    print("  %d DDL files render, no unknown placeholders" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
