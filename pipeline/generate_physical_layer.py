"""Physical source tables are inputs, never implicit generated demo data."""
from __future__ import annotations


def main(argv=None):
    print('Physical/demo generation was removed. Bind existing sources with source_mappings. '
          'No tables were created, replaced, truncated or populated.')
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
