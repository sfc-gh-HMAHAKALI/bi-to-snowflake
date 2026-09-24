"""Structured logging with file and console output.

Every module function should log entry/exit/errors through this logger.
Log files are written to ~/.snowflake/cortex/logs/semantic-extraction/
with automatic rotation.
"""

import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_DIR = Path.home() / ".snowflake" / "cortex" / "logs" / "semantic-extraction"
_INITIALIZED = False


def _ensure_log_dir() -> Path:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    return _LOG_DIR


def setup_logging(
    level: str = "INFO",
    log_file: str | None = None,
    console: bool = True,
) -> logging.Logger:
    """Configure the root semantic-extraction logger.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR).
        log_file: Explicit log file path. If None, auto-generated with timestamp.
        console: Whether to also log to stderr.

    Returns:
        The configured root logger for this skill's module namespace.
    """
    global _INITIALIZED

    # "bi_to_snowflake", not "semantic_extraction": the old name is the name of a
    # *separately installed skill* on some machines, so log lines read as though
    # they came from a different skill entirely. On a test whose whole point was
    # proving no cross-skill leakage that cost a verification detour, and
    # `import semantic_extraction` raises ModuleNotFoundError -- the name was
    # vestigial branding, never an importable package.
    logger = logging.getLogger("bi_to_snowflake")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Avoid duplicate handlers on repeated calls
    if _INITIALIZED:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler with rotation (5 MB, keep 3 backups)
    log_dir = _ensure_log_dir()
    if log_file is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = str(log_dir / f"extraction_{ts}.log")

    fh = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)  # File always gets full detail
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    # Console handler (stderr so it doesn't pollute JSON stdout)
    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(getattr(logging, level.upper(), logging.INFO))
        ch.setFormatter(formatter)
        logger.addHandler(ch)

    _INITIALIZED = True
    logger.info("Logging initialized — file: %s, level: %s", log_file, level)
    return logger


def get_logger(name: str) -> logging.Logger:
    """Get a child logger under this skill's logging namespace.

    Usage:
        log = get_logger(__name__)
        log.info("Parsing file: %s", path)
    """
    return logging.getLogger(f"bi_to_snowflake.{name}")
