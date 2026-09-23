"""Exception hierarchy and graceful failure handling.

Every extraction step should catch specific exceptions and call fail_step()
to log context and continue with partial results.
"""

import json
import os
import sys
import traceback
from typing import Any
from urllib.parse import urlparse

from .logger import get_logger

log = get_logger("errors")

# Controls whether full tracebacks appear in error records.
# Set SEMANTIC_EXTRACTION_DEBUG=1 to include them.
_DEBUG = os.environ.get("SEMANTIC_EXTRACTION_DEBUG", "").strip() in ("1", "true", "yes")

# Maximum length for string values in error context dicts.
_MAX_CONTEXT_VALUE_LEN = 200


class ExtractionError(Exception):
    """Base exception for all extraction errors."""

    def __init__(self, message: str, context: dict[str, Any] | None = None):
        self.context = context or {}
        super().__init__(message)

    def __str__(self):
        base = super().__str__()
        if self.context:
            ctx = ", ".join(f"{k}={v}" for k, v in self.context.items())
            return f"{base} [{ctx}]"
        return base


class ConnectionError(ExtractionError):
    """Failed to establish or maintain a connection (JDBC, REST, etc.)."""
    pass


class ParseError(ExtractionError):
    """Failed to parse a source file (XML, JSON, VQL, LookML, etc.)."""
    pass


class FileDiscoveryError(ExtractionError):
    """Failed to discover or access source files."""
    pass


class ValidationError(ExtractionError):
    """Extracted data failed validation checks."""
    pass


class ClassificationError(ExtractionError):
    """Failed to classify an object's complexity."""
    pass


# Allowed URL schemes for API base URLs.
_ALLOWED_SCHEMES = {"http", "https"}


def validate_base_url(url: str, label: str = "base_url") -> None:
    """Reject base URLs that could enable SSRF attacks.

    Only ``http`` and ``https`` schemes are allowed.  ``file://``, ``ftp://``,
    and other exotic schemes are rejected.

    Args:
        url: The user-supplied URL to validate.
        label: Human-readable label for error messages.

    Raises:
        ConnectionError: If the URL scheme is not in _ALLOWED_SCHEMES.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ConnectionError(
            f"Invalid {label} scheme '{parsed.scheme}' — only http/https allowed",
            context={label: url},
        )
    if not parsed.hostname:
        raise ConnectionError(
            f"Invalid {label} — no hostname found",
            context={label: url},
        )


def _sanitize_context(context: dict[str, Any]) -> dict[str, Any]:
    """Truncate long string values in an error context dict."""
    sanitized = {}
    for k, v in context.items():
        # Strip response bodies that may contain sensitive data
        if k in ("body", "response_body", "response_text"):
            sanitized[k] = "<redacted — set SEMANTIC_EXTRACTION_DEBUG=1 to include>"
            continue
        if isinstance(v, str) and len(v) > _MAX_CONTEXT_VALUE_LEN:
            sanitized[k] = v[:_MAX_CONTEXT_VALUE_LEN] + "…<truncated>"
        else:
            sanitized[k] = v
    return sanitized


def fail_step(
    step_name: str,
    error: Exception,
    partial_results: Any = None,
    fatal: bool = False,
) -> dict:
    """Log a step failure gracefully and return a structured error record.

    Args:
        step_name: Human-readable name of the failing step.
        error: The caught exception.
        partial_results: Any results collected before the failure.
        fatal: If True, also print error JSON to stdout and exit(1).

    Returns:
        A dict describing the failure, suitable for inclusion in output JSON.
    """
    tb = traceback.format_exception(type(error), error, error.__traceback__)
    context = {}
    if isinstance(error, ExtractionError):
        context = _sanitize_context(error.context)

    record = {
        "step": step_name,
        "error_type": type(error).__name__,
        "error_message": str(error),
        "context": context,
        "partial_results_available": partial_results is not None,
    }

    # Only include full tracebacks in debug mode to avoid leaking
    # internal paths and stack details in production output.
    if _DEBUG:
        record["traceback"] = "".join(tb)

    log.error(
        "Step '%s' failed: %s: %s",
        step_name,
        type(error).__name__,
        error,
    )
    log.debug("Traceback:\n%s", "".join(tb))

    if partial_results is not None:
        log.warning(
            "Step '%s' has partial results — continuing with what we have.",
            step_name,
        )

    if fatal:
        output = {"status": "error", "failure": record}
        if partial_results is not None:
            output["partial_results"] = partial_results
        print(json.dumps(output, indent=2, default=str))
        sys.exit(1)

    return record
