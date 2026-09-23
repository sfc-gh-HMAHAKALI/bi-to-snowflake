"""Power BI REST API + Scanner API client with OAuth2 authentication."""

import time
from typing import Any
from urllib.parse import quote

from ..common.errors import ConnectionError, fail_step
from ..common.logger import get_logger
from ..common.retry import retry_request

log = get_logger("powerbi.api_client")

_BASE_URL = "https://api.powerbi.com/v1.0/myorg"
_AUTH_URL_TEMPLATE = (
    "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
)
_DEFAULT_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
_REQUEST_TIMEOUT = 30          # seconds
_MAX_RETRIES = 3
_RETRY_BACKOFF = 2.0           # seconds between retries
_SCAN_POLL_INTERVAL = 5        # seconds between Scanner API status polls
_SCAN_MAX_POLLS = 24           # ~2 minutes of polling


class PowerBIApiClient:
    """Authenticated client for the Power BI REST and Scanner APIs.

    Uses Azure AD client-credentials OAuth2 to obtain a bearer token.
    All HTTP operations retry up to 3 times with exponential backoff.

    Usage::

        client = PowerBIApiClient(client_id, client_secret, tenant_id)
        client.authenticate()
        workspaces = client.list_workspaces()
        client.close()
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        tenant_id: str,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._tenant_id = tenant_id
        self._token: str | None = None
        self._session: Any = None  # requests.Session, lazy-imported

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def _get_session(self) -> Any:
        """Return a shared requests.Session, creating it if necessary."""
        if self._session is None:
            try:
                import requests  # type: ignore[import]
            except ImportError as exc:
                raise ConnectionError(
                    "requests library not installed — run `pip install requests`",
                    {"hint": "pip install requests"},
                ) from exc
            self._session = requests.Session()
            self._session.timeout = _REQUEST_TIMEOUT
        return self._session

    def close(self) -> None:
        """Close the underlying HTTP session."""
        log.info("PowerBIApiClient.close: entry")
        if self._session is not None:
            self._session.close()
            self._session = None
        log.info("PowerBIApiClient.close: session closed")

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """Obtain an OAuth2 bearer token via Azure AD client credentials.

        Retries up to 3 times on transient failures.

        Raises:
            ConnectionError: If all retry attempts fail.
        """
        log.info(
            "authenticate: entry — tenant=%s, client_id=%s",
            self._tenant_id,
            self._client_id,
        )
        auth_url = _AUTH_URL_TEMPLATE.format(tenant_id=quote(self._tenant_id, safe=''))
        payload = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": _DEFAULT_SCOPE,
        }
        session = self._get_session()

        def _do_auth():
            resp = session.post(auth_url, data=payload, timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            self._token = data["access_token"]
            session.headers.update({"Authorization": f"Bearer {self._token}"})
            log.info("authenticate: success")

        retry_request(
            _do_auth,
            max_retries=_MAX_RETRIES,
            backoff_base=_RETRY_BACKOFF,
            label="authenticate",
        )

    def test_connection(self) -> bool:
        """Test connectivity by listing workspaces (minimal call).

        Returns:
            True if the API is reachable and the token is valid, False otherwise.
        """
        log.info("test_connection: entry")
        try:
            self.list_workspaces()
            log.info("test_connection: success")
            return True
        except Exception as exc:
            log.warning("test_connection: failed — %s", exc)
            return False

    # ------------------------------------------------------------------
    # Internal HTTP helper
    # ------------------------------------------------------------------

    def _get(self, url: str, params: dict | None = None) -> Any:
        """Perform a GET with retry logic.

        Args:
            url: Full URL to request.
            params: Optional query parameters.

        Returns:
            Parsed JSON response body.

        Raises:
            ConnectionError: If all retries fail.
        """
        if self._token is None:
            raise ConnectionError(
                "Not authenticated — call authenticate() first",
                {"url": url},
            )
        session = self._get_session()

        def _do_get():
            log.debug("_get: GET %s", url)
            resp = session.get(url, params=params, timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()

        return retry_request(
            _do_get,
            max_retries=_MAX_RETRIES,
            backoff_base=_RETRY_BACKOFF,
            label=f"GET {url}",
            on_401=self.authenticate,
        )

    def _post(self, url: str, body: dict) -> Any:
        """Perform a POST with retry logic.

        Args:
            url: Full URL.
            body: JSON-serialisable request body.

        Returns:
            Parsed JSON response body.

        Raises:
            ConnectionError: If all retries fail.
        """
        if self._token is None:
            raise ConnectionError(
                "Not authenticated — call authenticate() first",
                {"url": url},
            )
        session = self._get_session()

        def _do_post():
            log.debug("_post: POST %s", url)
            resp = session.post(url, json=body, timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()

        return retry_request(
            _do_post,
            max_retries=_MAX_RETRIES,
            backoff_base=_RETRY_BACKOFF,
            label=f"POST {url}",
            on_401=self.authenticate,
        )

    # ------------------------------------------------------------------
    # Workspace & dataset discovery
    # ------------------------------------------------------------------

    def list_workspaces(self) -> list[dict]:
        """List all workspaces (groups) accessible by the service principal.

        Returns:
            List of workspace dicts with id, name, type, state.
        """
        log.info("list_workspaces: entry")
        data = self._get(f"{_BASE_URL}/groups")
        workspaces = data.get("value", [])
        log.info("list_workspaces: exit — count=%d", len(workspaces))
        return workspaces

    def list_datasets(self, group_id: str) -> list[dict]:
        """List all datasets in a workspace.

        Args:
            group_id: The workspace (group) GUID.

        Returns:
            List of dataset dicts with id, name, configuredBy, etc.
        """
        log.info("list_datasets: entry — group_id=%s", group_id)
        data = self._get(f"{_BASE_URL}/groups/{quote(group_id, safe='')}/datasets")
        datasets = data.get("value", [])
        log.info("list_datasets: exit — count=%d", len(datasets))
        return datasets

    # ------------------------------------------------------------------
    # DMV (Data Model Views) via DAX query execution
    # ------------------------------------------------------------------

    def execute_dax_query(
        self,
        group_id: str,
        dataset_id: str,
        dax_query: str,
    ) -> list[dict]:
        """Execute a DAX query against a dataset and return row results.

        Args:
            group_id: Workspace GUID.
            dataset_id: Dataset GUID.
            dax_query: DAX query string (e.g. INFO.VIEW.MEASURES()).

        Returns:
            List of row dicts from the first result table.
        """
        log.info(
            "execute_dax_query: entry — group=%s, dataset=%s, query_len=%d",
            group_id,
            dataset_id,
            len(dax_query),
        )
        url = f"{_BASE_URL}/groups/{quote(group_id, safe='')}/datasets/{quote(dataset_id, safe='')}/executeQueries"
        body = {
            "queries": [{"query": dax_query}],
            "serializerSettings": {"includeNulls": True},
        }
        response = self._post(url, body)

        rows: list[dict] = []
        try:
            results = response.get("results", [])
            if results:
                tables = results[0].get("tables", [])
                if tables:
                    rows = tables[0].get("rows", [])
        except Exception as exc:
            fail_step("parse_dax_query_response", exc)

        log.info("execute_dax_query: exit — rows=%d", len(rows))
        return rows

    # ------------------------------------------------------------------
    # Convenience DMV wrappers
    # ------------------------------------------------------------------

    def get_all_measures(self, group_id: str, dataset_id: str) -> list[dict]:
        """Retrieve all measures via INFO.VIEW.MEASURES() DMV.

        Args:
            group_id: Workspace GUID.
            dataset_id: Dataset GUID.

        Returns:
            List of measure dicts. Never raises — returns empty list on failure.
        """
        log.info(
            "get_all_measures: entry — group=%s, dataset=%s",
            group_id,
            dataset_id,
        )
        try:
            rows = self.execute_dax_query(
                group_id,
                dataset_id,
                "EVALUATE INFO.VIEW.MEASURES()",
            )
            log.info("get_all_measures: exit — count=%d", len(rows))
            return rows
        except Exception as exc:
            fail_step("get_all_measures", exc)
            return []

    def get_all_columns(self, group_id: str, dataset_id: str) -> list[dict]:
        """Retrieve all columns via INFO.VIEW.COLUMNS() DMV.

        Args:
            group_id: Workspace GUID.
            dataset_id: Dataset GUID.

        Returns:
            List of column dicts. Never raises — returns empty list on failure.
        """
        log.info(
            "get_all_columns: entry — group=%s, dataset=%s",
            group_id,
            dataset_id,
        )
        try:
            rows = self.execute_dax_query(
                group_id,
                dataset_id,
                "EVALUATE INFO.VIEW.COLUMNS()",
            )
            log.info("get_all_columns: exit — count=%d", len(rows))
            return rows
        except Exception as exc:
            fail_step("get_all_columns", exc)
            return []

    def get_all_relationships(self, group_id: str, dataset_id: str) -> list[dict]:
        """Retrieve all relationships via INFO.VIEW.RELATIONSHIPS() DMV.

        Args:
            group_id: Workspace GUID.
            dataset_id: Dataset GUID.

        Returns:
            List of relationship dicts. Never raises — returns empty list on failure.
        """
        log.info(
            "get_all_relationships: entry — group=%s, dataset=%s",
            group_id,
            dataset_id,
        )
        try:
            rows = self.execute_dax_query(
                group_id,
                dataset_id,
                "EVALUATE INFO.VIEW.RELATIONSHIPS()",
            )
            log.info("get_all_relationships: exit — count=%d", len(rows))
            return rows
        except Exception as exc:
            fail_step("get_all_relationships", exc)
            return []

    # ------------------------------------------------------------------
    # Scanner API (admin-level workspace metadata)
    # ------------------------------------------------------------------

    def scan_workspaces(self, workspace_ids: list[str]) -> dict:
        """Trigger a workspace scan via the Power BI Scanner API and poll
        until complete.

        Requires the service principal to have Tenant admin rights or the
        ``Tenant.Read.All`` / ``Tenant.ReadWrite.All`` scope.

        Args:
            workspace_ids: List of workspace GUIDs to scan (max 100 per call).

        Returns:
            Dict with scan results keyed by workspace ID, plus a 'status' key.
        """
        log.info(
            "scan_workspaces: entry — workspace_count=%d",
            len(workspace_ids),
        )

        scan_url = (
            "https://api.powerbi.com/v1.0/myorg/admin/workspaces/getInfo"
            "?lineage=true&datasourceDetails=true&datasetSchema=true"
            "&datasetExpressions=true"
        )
        body = {"workspaces": workspace_ids}

        try:
            trigger_resp = self._post(scan_url, body)
        except ConnectionError as exc:
            fail_step("scan_workspaces_trigger", exc)
            return {"status": "error", "error": str(exc), "workspaces": {}}

        scan_id = trigger_resp.get("id")
        if not scan_id:
            return {
                "status": "error",
                "error": "No scan ID returned by trigger",
                "workspaces": {},
            }

        log.info("scan_workspaces: scan triggered — scan_id=%s", scan_id)

        # ---- Poll for completion ----
        status_url = (
            f"https://api.powerbi.com/v1.0/myorg/admin/workspaces/scanStatus/{quote(scan_id, safe='')}"
        )
        result_url = (
            f"https://api.powerbi.com/v1.0/myorg/admin/workspaces/scanResult/{quote(scan_id, safe='')}"
        )

        for poll in range(1, _SCAN_MAX_POLLS + 1):
            time.sleep(_SCAN_POLL_INTERVAL)
            try:
                status_data = self._get(status_url)
                status = status_data.get("status", "").lower()
                log.debug(
                    "scan_workspaces: poll %d/%d — status=%s",
                    poll,
                    _SCAN_MAX_POLLS,
                    status,
                )
                if status == "succeeded":
                    break
                if status in ("failed", "cancelled"):
                    log.error("scan_workspaces: scan ended with status=%s", status)
                    return {
                        "status": status,
                        "scan_id": scan_id,
                        "workspaces": {},
                    }
            except Exception as exc:
                fail_step(f"scan_workspaces_poll:{poll}", exc)
        else:
            log.warning("scan_workspaces: timed out after %d polls", _SCAN_MAX_POLLS)
            return {
                "status": "timeout",
                "scan_id": scan_id,
                "workspaces": {},
            }

        # ---- Fetch results ----
        try:
            result_data = self._get(result_url)
            workspaces_raw = result_data.get("workspaces", [])
        except Exception as exc:
            fail_step("scan_workspaces_fetch_results", exc)
            return {"status": "error", "scan_id": scan_id, "workspaces": {}}

        # Index by workspace ID for easy lookup
        indexed: dict[str, dict] = {}
        for ws in workspaces_raw:
            ws_id = ws.get("id", "")
            if ws_id:
                indexed[ws_id] = ws

        log.info(
            "scan_workspaces: exit — scan_id=%s, workspaces_returned=%d",
            scan_id,
            len(indexed),
        )
        return {
            "status": "succeeded",
            "scan_id": scan_id,
            "workspaces": indexed,
        }

    # ------------------------------------------------------------------
    # Report & Page APIs (visual-level provenance)
    # ------------------------------------------------------------------

    def list_reports(self, group_id: str) -> list[dict]:
        """List all reports in a workspace.

        Args:
            group_id: The workspace (group) GUID.

        Returns:
            List of report dicts with id, name, datasetId, webUrl, etc.
        """
        log.info("list_reports: entry — group_id=%s", group_id)
        data = self._get(f"{_BASE_URL}/groups/{quote(group_id, safe='')}/reports")
        reports = data.get("value", [])
        log.info("list_reports: exit — count=%d", len(reports))
        return reports

    def get_report_pages(self, group_id: str, report_id: str) -> list[dict]:
        """Get pages (tabs) for a report.

        Args:
            group_id: Workspace GUID.
            report_id: Report GUID.

        Returns:
            List of page dicts with name, displayName, order.
        """
        log.info("get_report_pages: entry — group_id=%s, report_id=%s", group_id, report_id)
        data = self._get(
            f"{_BASE_URL}/groups/{quote(group_id, safe='')}"
            f"/reports/{quote(report_id, safe='')}/pages"
        )
        pages = data.get("value", [])
        log.info("get_report_pages: exit — count=%d", len(pages))
        return pages
