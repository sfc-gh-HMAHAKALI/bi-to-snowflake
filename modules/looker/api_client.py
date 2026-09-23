"""Looker API client for the Looker REST API 4.0.

Handles authentication, token refresh, retry with exponential backoff,
and per-request logging. Raises ConnectionError on unrecoverable failures.
"""

from typing import Any
from urllib.parse import quote

from ..common.errors import ConnectionError, fail_step, validate_base_url
from ..common.logger import get_logger
from ..common.retry import retry_request

log = get_logger("looker.api_client")

_MAX_RETRIES = 3
_BASE_BACKOFF_S = 1.0
_TIMEOUT_S = 30


class LookerApiClient:
    """REST API client for Looker 4.0.

    Usage:
        client = LookerApiClient(base_url, client_id, client_secret)
        client.authenticate()
        models = client.get_models()
        client.close()

    Auth token is refreshed automatically on 401 responses.
    All 5xx responses and connection errors are retried up to _MAX_RETRIES times
    with exponential backoff before raising ConnectionError.
    """

    def __init__(self, base_url: str, client_id: str, client_secret: str) -> None:
        validate_base_url(base_url, label="looker_base_url")
        self._base_url = base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._session: Any = None  # requests.Session, populated on first call

        log.info(
            "LookerApiClient: initialized — base_url=%s", self._base_url
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """Obtain a Looker auth token via POST /api/4.0/login.

        Retries up to _MAX_RETRIES times with exponential backoff.

        Raises:
            ConnectionError: If authentication fails after all retries.
        """
        log.info("authenticate: entering — base_url=%s", self._base_url)
        self._ensure_session()

        def _do_login() -> None:
            resp = self._session.post(
                f"{self._base_url}/api/4.0/login",
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                timeout=_TIMEOUT_S,
            )
            resp.raise_for_status()
            payload = resp.json()
            self._token = payload.get("access_token")
            if not self._token:
                raise ConnectionError(
                    "Looker login returned no access_token",
                    context={"response_keys": list(payload.keys())},
                )
            log.info(
                "authenticate: success — token_type=%s, expires_in=%s",
                payload.get("token_type"),
                payload.get("expires_in"),
            )

        retry_request(
            _do_login,
            max_retries=_MAX_RETRIES,
            backoff_base=_BASE_BACKOFF_S,
            label="Looker authenticate",
        )

    def test_connection(self) -> bool:
        """Verify the API is reachable and token is valid.

        Returns:
            True if GET /api/4.0/user succeeds, False otherwise.
        """
        log.info("test_connection: entering")
        try:
            resp = self._request("GET", "/api/4.0/user")
            ok = resp.status_code == 200
            log.info("test_connection: result=%s", ok)
            return ok
        except Exception as exc:
            log.warning("test_connection: failed — %s", exc)
            return False

    def get_models(self) -> list[dict]:
        """Fetch all LookML models.

        Returns:
            List of model dicts from GET /api/4.0/lookml_models.
        """
        log.info("get_models: entering")
        resp = self._request("GET", "/api/4.0/lookml_models")
        models = resp.json()
        log.info("get_models: done — count=%d", len(models))
        return models

    def get_explore_detail(self, model_name: str, explore_name: str) -> dict:
        """Fetch a fully-resolved explore with all fields.

        Args:
            model_name: LookML model name.
            explore_name: Explore name within the model.

        Returns:
            Explore dict from GET /api/4.0/lookml_models/{model}/explores/{explore}.
        """
        log.info(
            "get_explore_detail: entering — model=%s, explore=%s",
            model_name, explore_name,
        )
        path = f"/api/4.0/lookml_models/{quote(model_name, safe='')}/explores/{quote(explore_name, safe='')}"
        resp = self._request("GET", path)
        detail = resp.json()
        field_count = len(detail.get("fields", {}).get("dimensions", []))
        log.info(
            "get_explore_detail: done — model=%s, explore=%s, fields~=%d",
            model_name, explore_name, field_count,
        )
        return detail

    def get_dashboards(self) -> list[dict]:
        """Fetch all dashboards (summary list).

        Returns:
            List of dashboard summary dicts from GET /api/4.0/dashboards.
        """
        log.info("get_dashboards: entering")
        resp = self._request("GET", "/api/4.0/dashboards")
        dashboards = resp.json()
        log.info("get_dashboards: done — count=%d", len(dashboards))
        return dashboards

    def get_dashboard_detail(self, dashboard_id: str) -> dict:
        """Fetch a single dashboard with elements, fields, and filters.

        Args:
            dashboard_id: Looker dashboard ID or slug.

        Returns:
            Dashboard dict from GET /api/4.0/dashboards/{id}.
        """
        log.info("get_dashboard_detail: entering — id=%s", dashboard_id)
        resp = self._request("GET", f"/api/4.0/dashboards/{quote(dashboard_id, safe='')}")
        detail = resp.json()
        element_count = len(detail.get("dashboard_elements", []))
        log.info(
            "get_dashboard_detail: done — id=%s, elements=%d",
            dashboard_id, element_count,
        )
        return detail

    def close(self) -> None:
        """Release the underlying HTTP session."""
        log.info("close: closing session")
        if self._session is not None:
            try:
                self._session.close()
            except Exception as exc:
                log.warning("close: session.close() raised %s — ignoring", exc)
            finally:
                self._session = None
                self._token = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_session(self) -> None:
        """Create the requests.Session if it doesn't exist yet."""
        if self._session is not None:
            return
        try:
            import requests
        except ImportError as exc:
            raise ConnectionError(
                "requests package is not installed. Run: pip install requests",
            ) from exc
        self._session = requests.Session()

    def _auth_headers(self) -> dict[str, str]:
        if self._token:
            return {"Authorization": f"token {self._token}"}
        return {}

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Make a single API request with retry, auth refresh, and logging.

        Args:
            method: HTTP method (GET, POST, etc.).
            path: API path (e.g. /api/4.0/lookml_models).
            **kwargs: Additional keyword arguments forwarded to requests.

        Returns:
            Response object with a successful (2xx) status code.

        Raises:
            ConnectionError: After _MAX_RETRIES failed attempts.
        """
        self._ensure_session()
        if self._token is None:
            self.authenticate()

        url = f"{self._base_url}{path}"

        def _do_request() -> Any:
            resp = self._session.request(
                method,
                url,
                headers=self._auth_headers(),
                timeout=_TIMEOUT_S,
                **kwargs,
            )
            resp.raise_for_status()
            log.debug("_request: %s %s -> %d", method, path, resp.status_code)
            return resp

        return retry_request(
            _do_request,
            max_retries=_MAX_RETRIES,
            backoff_base=_BASE_BACKOFF_S,
            label=f"Looker {method} {path}",
            on_401=self.authenticate,
        )
