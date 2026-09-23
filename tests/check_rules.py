#!/usr/bin/env python3
"""Assert the composition rules never name a component that does not exist.

A rules file citing a phantom component is worse than no rules file: it reads as
authoritative and sends a generated page at an import that fails.
"""
from __future__ import annotations

import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent


def main() -> int:
    rules = (HERE / "references" / "composition-rules.md").read_text()
    react = set(re.findall(r"^export function (\w+)",
                           (HERE / "assets" / "react_ui" / "charts.tsx").read_text(), re.M))
    py = set(re.findall(r"^def (\w+)",
                        (HERE / "assets" / "streamlit_ui" / "charts.py").read_text(), re.M))
    py |= set(re.findall(r"^def (\w+)",
                         (HERE / "assets" / "streamlit_ui" / "ui.py").read_text(), re.M))

    bad = [(a, b) for a, b in re.findall(r"`([A-Z]\w+)`\s*/\s*`(\w+)`", rules)
           if a not in react or b not in py]
    if bad:
        for a, b in bad:
            miss = []
            if a not in react:
                miss.append(f"react:{a}")
            if b not in py:
                miss.append(f"streamlit:{b}")
            print(f"  rules cite missing {', '.join(miss)}")
        return 1

    pairs = re.findall(r"`([A-Z]\w+)`\s*/\s*`(\w+)`", rules)
    print(f"  {len(pairs)} component pairs cited, all exist")
    return 0


if __name__ == "__main__":
    sys.exit(main())
