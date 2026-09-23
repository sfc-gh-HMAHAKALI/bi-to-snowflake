"""Denodo Data Catalog REST API client.

Provides authenticated access to the Denodo Data Catalog for
metadata enrichment (tags, descriptions, business terms).

Requires: requests
"""

from typing import Any
from urllib.parse import quote

import requests

from ..common.logger import get_logger
from ..common.errors import ConnectionError, fail_step, validate_base_url
from ..common.retry import retry_request

log = get_logger("denodo.catalog_client")

DEFAULT_TIMEOUT = 30
MAX_RETRIES = 3
BACKOFF_BASE = 1


class CatalogClient:
    """REST API client for Denodo Data Catalog.

    Usage:
        client = CatalogClient(base_url="https://catalog.example.com",
                               username="admin", password="pass")
        if client.test_connection():
            dbs = client.browse_databases()
            metadata = client.export_catalog_metadata("mydb")
        client.close()
    """

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        timeout: int = DEFAULT_TIMEOUT,
    ):
        validate_base_url(base_url, label="catalog_base_url")
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self._session = requests.Session()
        self._token: str | None = None

        log.info("CatalogClient initialized: %s", self.base_url)

    def _request(
        self,
        method: str,
        path: str,
        json_data: dict | None = None,
        params: dict | None = None,
    ) -> dict | list:
        """Make an authenticated API request with retry."""
        url = f"{self.base_url}{path}"

        def _do_request():
            headers = {"Content-Type": "application/json"}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"

            log.debug("API %s %s", method, url)

            response = self._session.request(
                method=method,
                url=url,
                json=json_data,
                params=params,
                headers=headers,
                timeout=self.timeout,
            )

            log.debug("Response: %d %s", response.status_code, response.reason)
            response.raise_for_status()

            if response.content:
                return response.json()
            return {}

        return retry_request(
            _do_request,
            max_retries=MAX_RETRIES,
            backoff_base=BACKOFF_BASE,
            label=f"{method} {path}",
            on_401=self.authenticate,
        )

    def authenticate(self) -> None:
        """Authenticate with basic auth and obtain a session token."""
        log.info("Authenticating to Catalog: %s", self.base_url)
        try:
            response = self._session.post(
                f"{self.base_url}/public/api/login",
                json={"login": self.username, "password": self.password},
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
            self._token = data.get("token") or data.get("access_token")
            log.info("Catalog authentication successful.")
        except Exception as e:
            raise ConnectionError(
                f"Catalog authentication failed: {e}",
                context={"base_url": self.base_url},
            )

    def test_connection(self) -> bool:
        """Test connectivity and authentication."""
        log.info("Testing Catalog connection to %s", self.base_url)
        try:
            self.authenticate()
            self.browse_databases()
            log.info("Catalog connection test: SUCCESS")
            return True
        except Exception as e:
            log.error("Catalog connection test: FAILED — %s", e)
            return False

    def browse_databases(self) -> list[dict]:
        """List available databases in the catalog."""
        log.info("Browsing catalog databases.")
        result = self._request("GET", "/public/api/catalog/databases")
        dbs = result if isinstance(result, list) else result.get("databases", [])
        log.info("Found %d databases.", len(dbs))
        return dbs

    def synchronize_catalog(self, database: str) -> dict:
        """Trigger catalog synchronization for a database.

        This refreshes the catalog metadata from VDP.
        """
        log.info("Synchronizing catalog for database: %s", database)
        result = self._request(
            "POST",
            f"/public/api/catalog/databases/{quote(database, safe='')}/synchronize",
        )
        log.info("Catalog sync triggered for '%s'.", database)
        return result

    def export_catalog_metadata(self, database: str, view_name: str | None = None) -> list[dict]:
        """Export catalog metadata (tags, descriptions, business terms).

        Args:
            database: Database name.
            view_name: Optional specific view to export. If None, exports all.

        Returns:
            List of metadata dicts with tags and descriptions.
        """
        log.info(
            "Exporting catalog metadata: database=%s, view=%s",
            database, view_name or "ALL",
        )

        params = {}
        if view_name:
            params["viewName"] = view_name

        result = self._request(
            "GET",
            f"/public/api/catalog/databases/{quote(database, safe='')}/elements",
            params=params,
        )

        elements = result if isinstance(result, list) else result.get("elements", [])
        log.info("Exported %d catalog elements.", len(elements))
        return elements

    def get_element_tags(self, database: str, view_name: str) -> list[str]:
        """Get tags assigned to a specific catalog element."""
        log.debug("Getting tags for %s.%s", database, view_name)
        try:
            elements = self.export_catalog_metadata(database, view_name)
            if elements:
                return elements[0].get("tags", [])
            return []
        except Exception as e:
            log.warning("Failed to get tags for %s.%s: %s", database, view_name, e)
            return []

    def get_element_description(self, database: str, view_name: str) -> str:
        """Get the catalog description for a specific element."""
        log.debug("Getting description for %s.%s", database, view_name)
        try:
            elements = self.export_catalog_metadata(database, view_name)
            if elements:
                return elements[0].get("description", "")
            return ""
        except Exception as e:
            log.warning("Failed to get description for %s.%s: %s", database, view_name, e)
            return ""

    def close(self) -> None:
        """Close the HTTP session."""
        try:
            self._session.close()
            log.info("Catalog client session closed.")
        except Exception as e:
            log.warning("Error closing catalog session: %s", e)
