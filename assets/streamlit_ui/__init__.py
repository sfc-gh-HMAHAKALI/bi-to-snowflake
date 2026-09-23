"""Locked Streamlit component library.

Generated pages compose against this package; they never restyle it. The visual
vocabulary -- palette, KPI card, section rule, and the eight chart shapes -- is
fixed here so that two runs over the same model produce the same look.
"""
from . import charts, compat, filters, metrics, ui  # noqa: F401
from .ui import (  # noqa: F401
    SF_ACCENT, SF_BLUE, SF_MIDNIGHT, SF_PURPLE, SF_STAR, SF_TEAL,
    kpi_card, section,
)
