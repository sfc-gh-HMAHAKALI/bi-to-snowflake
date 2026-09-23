"""Shared retry-with-backoff helper for API clients.

Provides a single retry loop that all API clients delegate to, avoiding
~80 lines of near-identical retry logic per client.
"""

import time
from typing import Callable, TypeVar

import requests

from .logger import get_logger
from .errors import ConnectionError as KGConnectionError

log = get_logger("common.retry")

T = TypeVar("T")

# Defaults — callers can override per-call.
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE = 1.0


def retry_request(
    fn: Callable[[], T],
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_base: float = DEFAULT_BACKOFF_BASE,
    label: str = "request",
    on_401: Callable[[], None] | None = None,
) -> T:
    """Execute *fn* with retry on transient failures.

    Retries on:
      - ``requests.exceptions.ConnectionError``
      - ``requests.exceptions.Timeout``
      - ``requests.exceptions.HTTPError`` with status >= 500
      - HTTP 401 (calls *on_401* once then retries)

    Args:
        fn: Zero-argument callable that performs the HTTP request and returns
            the parsed result.  Should call ``response.raise_for_status()``
            internally so HTTP errors propagate as exceptions.
        max_retries: Maximum number of attempts (default 3).
        backoff_base: Base delay in seconds; doubles each attempt.
        label: Human-readable label for log messages (e.g. ``"GET /api/foo"``).
        on_401: Optional callback invoked once on a 401 response to refresh
            auth tokens before the next retry.

    Returns:
        Whatever *fn* returns on success.

    Raises:
        KGConnectionError: If all retry attempts are exhausted.
    """
    last_exc: Exception | None = None
    auth_refreshed = False

    for attempt in range(1, max_retries + 1):
        try:
            return fn()

        except requests.exceptions.HTTPError as exc:
            last_exc = exc
            status = exc.response.status_code if exc.response is not None else 0

            # 401 — refresh auth once, then retry immediately
            if status == 401 and on_401 and not auth_refreshed:
                log.warning("%s: 401 — refreshing auth (attempt %d/%d)", label, attempt, max_retries)
                auth_refreshed = True
                on_401()
                continue

            # 5xx — transient server error, retry with backoff
            if status >= 500 and attempt < max_retries:
                wait = backoff_base * (2 ** (attempt - 1))
                log.warning(
                    "%s: %d server error (attempt %d/%d) — retrying in %.1fs",
                    label, status, attempt, max_retries, wait,
                )
                time.sleep(wait)
                continue

            # 4xx (non-401) or exhausted retries — raise
            raise KGConnectionError(
                f"{label} failed: HTTP {status}",
                context={"status": status, "attempts": attempt},
            ) from exc

        except requests.exceptions.ConnectionError as exc:
            last_exc = exc
            if attempt < max_retries:
                wait = backoff_base * (2 ** (attempt - 1))
                log.warning(
                    "%s: connection error (attempt %d/%d) — retrying in %.1fs",
                    label, attempt, max_retries, wait,
                )
                time.sleep(wait)
                continue
            raise KGConnectionError(
                f"{label} failed after {max_retries} attempts: {exc}",
                context={"attempts": max_retries},
            ) from exc

        except requests.exceptions.Timeout as exc:
            last_exc = exc
            if attempt < max_retries:
                wait = backoff_base * (2 ** (attempt - 1))
                log.warning(
                    "%s: timeout (attempt %d/%d) — retrying in %.1fs",
                    label, attempt, max_retries, wait,
                )
                time.sleep(wait)
                continue
            raise KGConnectionError(
                f"{label} timed out after {max_retries} attempts",
                context={"attempts": max_retries},
            ) from exc

    # Should not reach here, but guard against it
    raise KGConnectionError(
        f"{label} failed after {max_retries} attempts",
        context={"attempts": max_retries, "last_error": str(last_exc)},
    )
