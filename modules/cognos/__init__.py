"""IBM Cognos Framework Manager adapter.

Parses a Framework Manager project (`model.xml` from a `.cpf` project directory)
into the unified inventory shape shared with the Tableau, Looker, Power BI,
Denodo and SAP BusinessObjects adapters.

Cognos carries four kinds of modelling metadata that the other five source
systems either lack or express differently, and that a plain
dimension/fact/metric extraction throws away:

* **Determinants** -- explicit grain declarations per query subject. These are
  how Framework Manager avoids double-counting when one table serves several
  levels of aggregation. They map to ``grain_declarations``.
* **regularAggregate / semiAggregate** -- a *pair* of aggregation rules per
  query item. ``semiAggregate`` is the "rollup" behaviour used for closing
  balances (``last``), and has no equivalent in a plain SQL aggregate. They map
  to ``aggregation_rules``.
* **dimension / hierarchy / level** -- named drill paths with ordered levels,
  member captions and business keys. They map to ``hierarchies``.
* **securityFilterDefinition** -- per-account row filters, which is how Cognos
  implements territory-based row-level security. They map to ``security_rules``.

All four are emitted as source-agnostic structures, not Cognos vocabulary, so a
Power BI or Tableau model can populate the same fields later.
"""

from .parser import parse_framework_manager_model, parse_model

__all__ = ["parse_framework_manager_model", "parse_model"]
