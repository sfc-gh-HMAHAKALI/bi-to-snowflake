"""Versioned run configuration and context-specific Snowflake identifiers.

Explicit arguments override files even when their value equals a default. Source
bindings never inherit generated-object destinations.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")


def identifier(value: str) -> str:
    """Return the logical name: fold unquoted input, preserve quoted input."""
    if not isinstance(value, str) or not value:
        raise ValueError("Identifier must be a nonempty string")
    if value.startswith('"'):
        if not re.fullmatch(r'"(?:[^"\x00-\x1f]|"")+"', value):
            raise ValueError("Malformed quoted identifier")
        name = value[1:-1].replace('""', '"')
    elif _IDENTIFIER.fullmatch(value):
        name = value.upper()
    else:
        raise ValueError("Use an unquoted identifier or double-quote the complete name")
    if len(name) > 255:
        raise ValueError("Identifier exceeds Snowflake's 255-character limit")
    return name


def quote(name: str) -> str:
    """Serialize a logical identifier (not a dotted SQL expression)."""
    if not isinstance(name, str) or not name or len(name) > 255 or any(ord(c) < 32 for c in name):
        raise ValueError("Invalid logical identifier")
    return '"' + name.replace('"', '""') + '"'


def qualified(value: str, count: int = 3) -> tuple[str, ...]:
    """Parse qualified identifiers without splitting dots within quoted names."""
    if not isinstance(value, str):
        raise ValueError("Qualified name must be a string")
    tokens = re.findall(r'"(?:[^"\x00-\x1f]|"")*"|[A-Za-z_][A-Za-z0-9_$]*|\.', value)
    if ''.join(tokens) != value or len(tokens) != count * 2 - 1 or any(t != '.' for t in tokens[1::2]):
        raise ValueError(f"Expected {count} explicitly qualified identifier components: {value!r}")
    return tuple(identifier(t) for t in tokens[::2])


def fqn(*parts: str) -> str:
    return '.'.join(quote(p) for p in parts)


def literal(value: str) -> str:
    return "'" + str(value).replace('\\', '\\\\').replace("'", "''") + "'"


@dataclass(frozen=True)
class Naming:
    # Internal legacy defaults remain for importing utility modules, not build input.
    database: str = "BI2SF"
    kb_database: str = "BI2SF_KB"
    kb_schema: str = "KNOWLEDGE_BASE"
    analytics_schema: str = "ANALYTICS"
    prefix: str = "BI"
    model_name: str = "MODEL"
    model_label: str = "BI Model"
    source_schemas: tuple[str, ...] = ()

    def __post_init__(self):
        for key in ('database', 'kb_database', 'kb_schema', 'analytics_schema', 'prefix', 'model_name'):
            object.__setattr__(self, key, identifier(getattr(self, key)))
        for name in (self.semantic_view, self.agent, self.ask_procedure, self.search_service,
                     self.app_service, self.compute_pool):
            quote(name)

    @property
    def semantic_view(self): return f'{self.prefix}_{self.model_name}'
    @property
    def agent(self): return f'{self.prefix}_ANALYST'
    @property
    def ask_procedure(self): return f'ASK_{self.prefix}_ANALYST'
    @property
    def search_service(self): return f'{self.prefix}_GLOSSARY_SEARCH'
    @property
    def row_access_policy(self): return f'{self.prefix}_ACCESS_POLICY'
    @property
    def security_role(self): return f'{self.prefix}_SECURITY_ADMIN'
    @property
    def app_stage(self): return f'{self.prefix}_APP_STAGE'
    @property
    def streamlit(self): return f'{self.prefix}_REPORT'
    @property
    def stage_dir(self): return 'app'
    @property
    def app_service(self): return f'{self.prefix}_{self.model_name}_REPORT'
    @property
    def compute_pool(self): return f'{self.prefix}_{self.model_name}_POOL'
    @property
    def kb(self): return fqn(self.kb_database, self.kb_schema)
    @property
    def analytics(self): return fqn(self.database, self.analytics_schema)

    def substitutions(self):
        names = {'DB': self.database, 'KB_DB': self.kb_database, 'KB_SCHEMA': self.kb_schema,
                 'ANALYTICS_SCHEMA': self.analytics_schema, 'SEMANTIC_VIEW': self.semantic_view,
                 'AGENT': self.agent, 'SEARCH': self.search_service, 'PREFIX': self.prefix}
        return {key: quote(value) for key, value in names.items()}


DEFAULT = Naming()


def render_sql(text, naming=DEFAULT):
    values = naming.substitutions()
    def replace(match):
        if match[1] not in values:
            raise ValueError('Unknown identifier placeholder: ' + match[1])
        return values[match[1]]
    return re.sub(r'\{\{([A-Z_]+)\}\}', replace, text)


def add_arguments(parser):
    for key in ('database', 'kb_database', 'kb_schema', 'analytics_schema', 'prefix', 'model_name', 'model_label'):
        parser.add_argument('--' + key.replace('_', '-'), default=argparse.SUPPRESS)
    parser.add_argument('--naming-file')


def naming_from_values(values):
    # Requote logical names before the dataclass normalizes user input.
    return Naming(**{k: quote(v) if k in {'database', 'kb_database', 'kb_schema', 'analytics_schema', 'prefix', 'model_name'} else v
                     for k, v in values.items()})


def from_args(args):
    values = asdict(DEFAULT)
    if getattr(args, 'naming_file', None):
        loaded = read_json(args.naming_file)
        if set(loaded) - set(values):
            raise ValueError('Unknown naming configuration keys')
        values.update(loaded)
    for key in values:
        if hasattr(args, key):
            values[key] = getattr(args, key)
    return Naming(**values)


def forward_flags(naming):
    return [arg for key, value in asdict(naming).items() if key != 'source_schemas'
            for arg in ('--' + key.replace('_', '-'), quote(value) if key != 'model_label' else value)]


def read_json(path):
    with Path(path).expanduser().open(encoding='utf-8') as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f'{path}: expected a JSON object')
    return value


@dataclass
class RunConfig:
    version: int = 1
    database: str = ''
    analytics_schema: str = ''
    kb_database: str = ''
    kb_schema: str = 'KNOWLEDGE_BASE'
    prefix: str = 'BI'
    model_name: str = 'MODEL'
    model_label: str = 'BI Model'
    connection: str = ''
    warehouse: str = ''
    role: str = ''
    grant_role: str = ''
    source_type: str = ''
    extract: str = ''
    inventory: str = ''
    output_dir: str = ''
    outputs: list[str] = field(default_factory=lambda: ['semantic'])
    deploy: str = 'none'
    source_mappings: dict[str, str] = field(default_factory=dict)
    # Reviewed replacements are keyed by the stable item reference printed in issues.json.
    translations: dict[str, str] = field(default_factory=dict)
    relationship_mappings: dict[str, dict] = field(default_factory=dict)
    security: dict = field(default_factory=dict)
    glossary_search: bool = False
    chat: bool = False
    compute_pool: str = ''
    external_access_integrations: list[str] = field(default_factory=list)
    container_image: str = ''

    def validate(self):
        if self.version != 1:
            raise ValueError('Unsupported run configuration version')
        for key in ('database', 'analytics_schema', 'output_dir'):
            if not getattr(self, key):
                raise ValueError(f'Missing --{key.replace("_", "-")}; choose it explicitly or use --guided')
        if not self.inventory and not self.extract:
            raise ValueError('Provide --inventory or --extract')
        if self.inventory and self.extract:
            raise ValueError('Choose --inventory OR --extract, not both')
        if self.extract and self.source_type not in ('cognos', 'tableau', 'powerbi', 'looker', 'denodo', 'businessobjects'):
            raise ValueError('--extract requires --source-type: cognos/tableau/powerbi/looker/denodo/businessobjects')
        if not isinstance(self.outputs, list) or not self.outputs or set(self.outputs) - {'semantic', 'agent', 'catalog', 'streamlit', 'react'}:
            raise ValueError('outputs must be a nonempty list of semantic, agent, catalog, streamlit, react')
        if self.deploy not in ('none', 'local', 'streamlit', 'react', 'all', 'both'):
            raise ValueError('Invalid deploy mode')
        for key in ('database', 'analytics_schema', 'kb_database', 'kb_schema', 'prefix', 'model_name',
                    'model_label', 'connection', 'warehouse', 'role', 'grant_role', 'source_type',
                    'extract', 'inventory', 'output_dir', 'deploy', 'compute_pool', 'container_image'):
            if not isinstance(getattr(self, key), str):
                raise ValueError(f'{key} must be a string')
        for key in ('source_mappings', 'translations', 'relationship_mappings', 'security'):
            if not isinstance(getattr(self, key), dict):
                raise ValueError(f'{key} must be an object')
        if any(not isinstance(value, str) for value in self.translations.values()):
            raise ValueError('translations values must be SQL expression strings')
        if any(not isinstance(value, dict) for value in self.relationship_mappings.values()):
            raise ValueError('relationship_mappings values must be objects')
        for value in self.source_mappings.values():
            qualified(value)
        for key in ('warehouse', 'role', 'grant_role'):
            if getattr(self, key): identifier(getattr(self, key))
        for key in ('glossary_search', 'chat'):
            if not isinstance(getattr(self, key), bool): raise ValueError(key + ' must be Boolean')
        if self.compute_pool: identifier(self.compute_pool)
        if not isinstance(self.external_access_integrations, list): raise ValueError('external_access_integrations must be a list')
        for name in self.external_access_integrations: identifier(name)
        if self.glossary_search and not self.warehouse: raise ValueError('glossary_search requires warehouse')
        if self.chat and not set(self.outputs) & {'streamlit', 'react'}: raise ValueError('chat requires an app output')
        hosted = self.hosted_outputs
        if hosted and (not self.compute_pool or not self.warehouse or not self.role):
            raise ValueError('Hosted outputs require explicit compute_pool, warehouse and owner role')
        if 'streamlit' in hosted and not self.external_access_integrations:
            raise ValueError('Hosted Streamlit dependencies require explicit external_access_integrations')
        if 'react' in hosted and not re.fullmatch(r'/[A-Za-z0-9_$.-]+/[A-Za-z0-9_$.-]+/[A-Za-z0-9_$.-]+/[a-z0-9_.-]+(?::[A-Za-z0-9_.-]+|@sha256:[a-f0-9]{64})', self.container_image):
            raise ValueError('Hosted React requires an explicit Snowflake container_image /database/schema/repository/image:tag')
        if 'react' in hosted and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,61}[A-Za-z0-9]', self.naming.app_service):
            raise ValueError('Hosted service name must be a DNS-compatible identifier of at most 63 characters')
        self.naming  # validate destination identifiers and derived names
        self.output_dir = str(Path(self.output_dir).expanduser().resolve())
        for key in ('inventory', 'extract'):
            if getattr(self, key):
                setattr(self, key, str(Path(getattr(self, key)).expanduser().resolve()))
        return self

    @property
    def naming(self):
        return Naming(database=self.database, analytics_schema=self.analytics_schema,
                      kb_database=self.kb_database or self.database, kb_schema=self.kb_schema,
                      prefix=self.prefix, model_name=self.model_name, model_label=self.model_label)

    @property
    def effective_outputs(self):
        result = set(self.outputs)
        if self.chat: result.add('agent')
        if self.glossary_search: result.add('catalog')
        if result & {'agent', 'streamlit', 'react'}: result.add('semantic')
        return sorted(result)

    @property
    def hosted_outputs(self):
        if self.deploy in ('none', 'local'): return set()
        requested = {'streamlit', 'react'} if self.deploy in ('all', 'both') else {self.deploy}
        if not requested <= set(self.outputs): raise ValueError('Hosting choice requires matching app outputs')
        return requested

    def public_dict(self):
        return asdict(self)  # only connection name; never credentials


def resolve(args):
    values = asdict(RunConfig())
    for path in (getattr(args, 'resume', None), getattr(args, 'config', None), getattr(args, 'naming_file', None)):
        if path:
            source = Path(path).expanduser()
            if source.is_dir(): source = source / 'resolved-config.json'
            loaded = read_json(source)
            if set(loaded) - set(values):
                raise ValueError('Unknown configuration keys: ' + ', '.join(sorted(set(loaded) - set(values))))
            values.update(loaded)
    for key in values:
        if hasattr(args, key): values[key] = getattr(args, key)
    if isinstance(values['outputs'], str):
        values['outputs'] = [v.strip() for v in values['outputs'].split(',') if v.strip()]
    return RunConfig(**values)


def guided(settings, ask=input):
    """Prompt only for unresolved required values; returned config is reusable."""
    for key, suggestion in (('database', 'BI_WORK'), ('analytics_schema', 'ANALYTICS'),
                            ('output_dir', str(Path.cwd() / 'b2s-output'))):
        if not getattr(settings, key):
            setattr(settings, key, ask(f'{key} [{suggestion}]: ').strip() or suggestion)
    if not settings.inventory and not settings.extract:
        settings.inventory = ask('Unified inventory JSON path: ').strip()
    return settings
