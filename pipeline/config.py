"""Single source for every object name this pipeline creates.

Before this module the same two database names were defined independently in four
places (build.py, kb_loader.py, path1_catalog_glossary.py,
path3_ossie_semantic_view.py) and hardcoded a further 625 times across the SQL. A
build therefore could not target anything but the account it was first written
against, and missing one definition produced a half-migrated run that failed at a
random phase.

Naming is now declared once here and reaches SQL through placeholder substitution
in ``render_sql``. The placeholders are deliberately ``{{NAME}}`` rather than
Python ``str.format`` braces, because the DDL is full of JSON bodies and
``OBJECT_CONSTRUCT`` calls whose own braces would otherwise have to be escaped.

Schema names (ANALYTICS, SALES_ANALYTICS, COMMON_ANALYTICS,
KNOWLEDGE_BASE) are configurable here but are NOT templated into the SQL. They are
generic, this pipeline creates them itself, and threading another ~850
substitutions through the DDL would add risk without making the build any more
portable.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass, asdict, field


@dataclass(frozen=True)
class Naming:
    """Every name the build writes into the account.

    ``prefix`` is the one knob most users will touch: it namespaces the objects a
    build creates so two models can live in one account without colliding.
    """

    # Databases and schemas
    database: str = "BI2SF"
    kb_database: str = "BI2SF_KB"
    kb_schema: str = "KNOWLEDGE_BASE"
    analytics_schema: str = "ANALYTICS"
    source_schemas: tuple[str, ...] = ("SALES_ANALYTICS", "COMMON_ANALYTICS")

    # Object prefix and the objects themselves
    prefix: str = "BI"
    model_name: str = "SALES_BOOKINGS"
    model_label: str = "BI Model"

    @property
    def semantic_view(self) -> str:
        return "%s_%s" % (self.prefix, self.model_name)

    @property
    def agent(self) -> str:
        return "%s_ANALYST" % self.prefix

    @property
    def ask_procedure(self) -> str:
        return "ASK_%s_ANALYST" % self.prefix

    @property
    def search_service(self) -> str:
        return "%s_GLOSSARY_SEARCH" % self.prefix

    @property
    def row_access_policy(self) -> str:
        return "%s_TERRITORY_POLICY" % self.prefix

    @property
    def security_role(self) -> str:
        return "%s_SECURITY_ADMIN" % self.prefix

    @property
    def app_stage(self) -> str:
        return "%s_APP_STAGE" % self.prefix

    @property
    def streamlit(self) -> str:
        return "%s_REPORT" % self.prefix

    @property
    def stage_dir(self) -> str:
        """Lowercase on purpose: Snowflake stage paths are case-sensitive."""
        return "%s_report" % self.prefix.lower()

    @property
    def app_service(self) -> str:
        return "%s_%s_REPORT" % (self.database, self.prefix)

    @property
    def compute_pool(self) -> str:
        return "%s_%s_SIS_POOL" % (self.database, self.prefix)

    # Fully-qualified conveniences, so callers stop concatenating by hand.
    @property
    def kb(self) -> str:
        return "%s.%s" % (self.kb_database, self.kb_schema)

    @property
    def analytics(self) -> str:
        return "%s.%s" % (self.database, self.analytics_schema)

    def substitutions(self) -> dict[str, str]:
        """The placeholder table applied to the .sql files."""
        return {
            "KB_DB": self.kb_database,
            "KB_SCHEMA": self.kb_schema,
            "DB": self.database,
            "ANALYTICS_SCHEMA": self.analytics_schema,
            "PREFIX": self.prefix,
            "SEMANTIC_VIEW": self.semantic_view,
            "AGENT": self.agent,
            "ASK_PROC": self.ask_procedure,
            "SEARCH": self.search_service,
            "POLICY": self.row_access_policy,
            "SECURITY_ROLE": self.security_role,
            "STAGE": self.app_stage,
            "STAGE_DIR": self.stage_dir,
            "STREAMLIT": self.streamlit,
            "APP_SERVICE": self.app_service,
            "POOL": self.compute_pool,
            "MODEL_LABEL": self.model_label,
        }


DEFAULT = Naming()

_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


def render_sql(text: str, naming: Naming = DEFAULT) -> str:
    """Substitute ``{{NAME}}`` placeholders, failing loudly on an unknown one.

    Failing loudly matters: a silently unreplaced placeholder becomes an invalid
    Snowflake identifier and the phase fails halfway through a build, with the
    earlier statements already committed.
    """
    table = naming.substitutions()
    unknown = sorted({m.group(1) for m in _PLACEHOLDER.finditer(text)}
                     - set(table))
    if unknown:
        raise KeyError("unknown SQL placeholder(s): %s" % ", ".join(unknown))
    return _PLACEHOLDER.sub(lambda m: table[m.group(1)], text)


def add_arguments(ap: argparse.ArgumentParser) -> None:
    """Attach the naming flags to any script that needs them."""
    g = ap.add_argument_group("naming")
    g.add_argument("--database", default=DEFAULT.database,
                   help="Target database for the analytics layer "
                        "(default: %(default)s)")
    g.add_argument("--kb-database", default=DEFAULT.kb_database,
                   help="Database holding the knowledge base "
                        "(default: %(default)s)")
    g.add_argument("--kb-schema", default=DEFAULT.kb_schema,
                   help="Schema holding the knowledge base "
                        "(default: %(default)s)")
    g.add_argument("--analytics-schema", default=DEFAULT.analytics_schema,
                   help="Schema for the governed views and agent "
                        "(default: %(default)s)")
    g.add_argument("--prefix", default=DEFAULT.prefix,
                   help="Prefix namespacing every object this build creates, so "
                        "two models can share an account (default: %(default)s)")
    g.add_argument("--model-name", default=DEFAULT.model_name,
                   help="Semantic view name, after the prefix "
                        "(default: %(default)s)")
    g.add_argument("--model-label", default=DEFAULT.model_label,
                   help="Human label used in comments and tag values "
                        "(default: %(default)s)")
    g.add_argument("--naming-file", metavar="JSON",
                   help="Read any of the above from a JSON file; explicit flags "
                        "still win")


def from_args(args: argparse.Namespace) -> Naming:
    """Build a Naming from parsed flags, with --naming-file as the base layer."""
    values = asdict(DEFAULT)
    path = getattr(args, "naming_file", None)
    if path:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            loaded = json.load(fh)
        unknown = sorted(set(loaded) - set(values))
        if unknown:
            raise KeyError("unknown key(s) in %s: %s" % (path, ", ".join(unknown)))
        values.update(loaded)

    # An explicit flag beats the file. argparse cannot tell "given" from
    # "defaulted", so compare against the dataclass default instead.
    for key in list(values):
        given = getattr(args, key, None)
        if given is not None and given != getattr(DEFAULT, key):
            values[key] = given

    values["source_schemas"] = tuple(values["source_schemas"])
    return Naming(**values)


def forward_flags(naming: Naming) -> list[str]:
    """The flags needed to reproduce this naming in a child process."""
    return [
        "--database", naming.database,
        "--kb-database", naming.kb_database,
        "--kb-schema", naming.kb_schema,
        "--analytics-schema", naming.analytics_schema,
        "--prefix", naming.prefix,
        "--model-name", naming.model_name,
        "--model-label", naming.model_label,
    ]
