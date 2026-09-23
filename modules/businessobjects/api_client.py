"""SAP Business Objects REST API client.

Provides authenticated access to the BO REST API for
universe and report metadata extraction.

Requires: requests
"""

from typing import Any
from urllib.parse import quote

import requests

from ..common.logger import get_logger
from ..common.errors import ConnectionError, fail_step, validate_base_url
from ..common.retry import retry_request

log = get_logger("businessobjects.api_client")

DEFAULT_TIMEOUT = 30
MAX_RETRIES = 3
BACKOFF_BASE = 1


class BOApiClient:
    """REST API client for SAP BusinessObjects.

    Usage:
        client = BOApiClient(base_url="http://bo-server:6405/biprws",
                             username="admin", password="pass")
        if client.test_connection():
            universes = client.list_universes()
            detail = client.get_universe_details(universe_id)
        client.close()
    """

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        auth_type: str = "secEnterprise",
        timeout: int = DEFAULT_TIMEOUT,
    ):
        validate_base_url(base_url, label="bo_base_url")
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.auth_type = auth_type
        self.timeout = timeout
        self._session = requests.Session()
        self._logon_token: str | None = None

        log.info("BOApiClient initialized: %s", self.base_url)

    def _request(
        self,
        method: str,
        path: str,
        json_data: dict | None = None,
        params: dict | None = None,
    ) -> dict | list:
        """Make an authenticated API request with retry."""
        url = f"{self.base_url}{path}"

        def _do_request() -> dict | list:
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            if self._logon_token:
                headers["X-SAP-LogonToken"] = self._logon_token

            response = self._session.request(
                method=method,
                url=url,
                json=json_data,
                params=params,
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()

            if response.content:
                return response.json()
            return {}

        return retry_request(
            _do_request,
            max_retries=MAX_RETRIES,
            backoff_base=BACKOFF_BASE,
            label=f"BO {method} {path}",
            on_401=self.authenticate,
        )

    def authenticate(self) -> None:
        """Log on to BO REST API and obtain a logon token."""
        log.info("Authenticating to BO: %s", self.base_url)
        try:
            response = self._session.post(
                f"{self.base_url}/logon/long",
                json={
                    "userName": self.username,
                    "password": self.password,
                    "auth": self.auth_type,
                },
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
            self._logon_token = data.get("logonToken", "")
            if not self._logon_token:
                raise ConnectionError(
                    "No logon token in BO response",
                    context={"response_keys": list(data.keys())},
                )
            log.info("BO authentication successful.")
        except requests.exceptions.RequestException as e:
            raise ConnectionError(
                f"BO authentication failed: {e}",
                context={"base_url": self.base_url},
            )

    def test_connection(self) -> bool:
        """Test connectivity and authentication."""
        log.info("Testing BO connection to %s", self.base_url)
        try:
            self.authenticate()
            # Verify token works with a simple call
            self._request("GET", "/raylight/v1/serverinfo")
            log.info("BO connection test: SUCCESS")
            return True
        except Exception as e:
            log.error("BO connection test: FAILED — %s", e)
            return False

    def list_universes(self) -> list[dict]:
        """List all published universes (.unx)."""
        log.info("Listing BO universes.")
        result = self._request("GET", "/raylight/v1/universes")
        universes = result.get("universes", result) if isinstance(result, dict) else result
        if isinstance(universes, dict):
            universes = universes.get("universe", [])
        log.info("Found %d universes.", len(universes))
        return universes

    def get_universe_details(self, universe_id: str) -> dict:
        """Get detailed metadata for a specific universe.

        Includes tables, joins, classes, objects (dimensions/measures/details).
        """
        log.info("Getting universe details: %s", universe_id)
        result = self._request("GET", f"/raylight/v1/universes/{quote(universe_id, safe='')}")
        log.info("Universe '%s' retrieved.", universe_id)
        return result

    def get_universe_objects(self, universe_id: str) -> list[dict]:
        """Get all business objects (dimensions, measures, details) for a universe."""
        log.info("Getting objects for universe: %s", universe_id)
        detail = self.get_universe_details(universe_id)

        objects = []
        for folder in detail.get("folders", detail.get("classes", [])):
            folder_name = folder.get("name", "")
            for item in folder.get("items", folder.get("objects", [])):
                item["folder_path"] = folder_name
                objects.append(item)

        log.info("Universe '%s' has %d objects.", universe_id, len(objects))
        return objects

    def list_reports(self, folder_id: str | None = None) -> list[dict]:
        """List Web Intelligence reports."""
        log.info("Listing BO reports (folder=%s).", folder_id or "all")
        params = {}
        if folder_id:
            params["folderId"] = folder_id

        result = self._request("GET", "/raylight/v1/documents", params=params)
        documents = result.get("documents", result) if isinstance(result, dict) else result
        if isinstance(documents, dict):
            documents = documents.get("document", [])
        log.info("Found %d reports.", len(documents))
        return documents

    def get_report_details(self, document_id: str) -> dict:
        """Get detailed metadata for a Web Intelligence report.

        Includes data providers, queries, variables.
        """
        log.info("Getting report details: %s", document_id)
        result = self._request("GET", f"/raylight/v1/documents/{quote(document_id, safe='')}")
        log.info("Report '%s' retrieved.", document_id)
        return result

    def get_report_dataproviders(self, document_id: str) -> list[dict]:
        """Get data providers (queries) for a report."""
        log.info("Getting data providers for report: %s", document_id)
        result = self._request(
            "GET",
            f"/raylight/v1/documents/{quote(document_id, safe='')}/dataproviders",
        )
        providers = result.get("dataproviders", result) if isinstance(result, dict) else result
        if isinstance(providers, dict):
            providers = providers.get("dataprovider", [])
        log.info("Report '%s' has %d data providers.", document_id, len(providers))
        return providers

    def logoff(self) -> None:
        """Log off from the BO REST API."""
        if self._logon_token:
            try:
                self._request("POST", "/logoff")
                log.info("BO logoff successful.")
            except Exception as e:
                log.warning("BO logoff error: %s", e)
            self._logon_token = None

    def close(self) -> None:
        """Log off and close the HTTP session."""
        self.logoff()
        try:
            self._session.close()
            log.info("BO client session closed.")
        except Exception as e:
            log.warning("Error closing BO session: %s", e)
