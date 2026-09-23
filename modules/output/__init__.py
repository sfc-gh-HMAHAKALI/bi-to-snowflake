"""Output generators: inventory builder and YAML generator."""
from .inventory import build_unified_inventory, merge_inventories
from .yaml_generator import generate_semantic_view_yaml, generate_all_yamls
