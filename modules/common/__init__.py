"""Common infrastructure: logging, errors, file crawling, retry."""
from .logger import get_logger, setup_logging
from .errors import (
    ExtractionError,
    ConnectionError,
    ParseError,
    FileDiscoveryError,
    ValidationError,
    ClassificationError,
    fail_step,
    validate_base_url,
)
from .file_crawler import discover_files, DiscoveredFile
from .retry import retry_request
