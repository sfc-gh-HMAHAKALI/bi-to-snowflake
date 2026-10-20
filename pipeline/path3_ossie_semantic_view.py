"""Source-derived OSI generation. No embedded entities or business rules."""
from __future__ import annotations
import json
from pathlib import Path
import yaml
from model_contract import osi, resolve_inventory


def to_yaml(model, indent=0):
    return yaml.safe_dump(model, sort_keys=False, allow_unicode=True)


def build_ossie_model(inventory, settings):
    return osi(resolve_inventory(inventory, settings))


def main(argv=None):
    from build import main as build_main
    return build_main(argv)


if __name__ == '__main__':
    raise SystemExit(main())
