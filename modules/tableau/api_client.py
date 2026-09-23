"""Tableau Server / Cloud Metadata API client.

Uses Personal Access Tokens (PAT) for authentication and the GraphQL
Metadata API for semantic extraction.  All methods log entry/exit and
wrap errors in ConnectionError or ExtractionError so callers get
structured failures.

Usage::

    client = TableauApiClient(
        server_url="https://tableau.example.com",
        token_name="my-token-name",
        token_secret="my-token-secret",
        site_name="MySite",        # "" for the Default site
    )
    client.authenticate()
    datasources = client.get_datasources()
    client.close()
"""

from typing import Any, Optional

import requests

from ..common.logger import get_logger
from ..common.errors import ConnectionError as KGConnectionError, fail_step, validate_base_url
from ..common.retry import retry_request

log = get_logger(__name__)

# Tableau REST API version used for auth and site queries.
_API_VERSION = "3.21"

# Default timeout for every HTTP request (seconds).
_DEFAULT_TIMEOUT = 30

# Retry policy: attempt count and backoff delays in seconds.
_MAX_RETRIES = 3
_BACKOFF_SECONDS = (1, 2, 4)

# ---------------------------------------------------------------------------
# GraphQL query templates
# ---------------------------------------------------------------------------

_GQL_CALCULATED_FIELDS = """
{
  calculatedFieldsConnection {
    nodes {
      id
      name
      formula
      dataType
      role
      description
      datasource {
        id
        name
        __typename
      }
    }
  }
}
"""

_GQL_DATASOURCES = """
{
  embeddedDatasourcesConnection {
    nodes {
      id
      name
      description
      extractLastRefreshTime
      extractLastUpdateTime
      fields {
        id
        name
        dataType
        description
        isHidden
        ... on CalculatedField {
          formula
        }
      }
      workbook {
        id
        name
        projectName
      }
    }
  }
}
"""

_GQL_DASHBOARDS = """
{
  dashboardsConnection {
    nodes {
      id
      name
      path
      workbook {
        id
        name
      }
      sheets {
        id
        name
        datasourceFields {
          datasource {
            id
            name
          }
          field {
            id
            name
            dataType
            ... on CalculatedField {
              formula
            }
          }
        }
      }
    }
  }
}
"""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class TableauApiClient:
    """Authenticated client for the Tableau Server / Cloud Metadata API.

    Args:
        server_url: Base URL of the Tableau Server, e.g.
            ``"https://tableau.example.com"``.  Trailing slashes are stripped.
        token_name: Personal Access Token name.
        token_secret: Personal Access Token secret.
        site_name: Tableau site content URL.  Use ``""`` for the Default site.
        timeout: HTTP request timeout in seconds (default 30).
    """

    def __init__(
        self,
        server_url: str,
        token_name: str,
        token_secret: str,
        site_name: str = "",
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> None:
        log.info(
            "TableauApiClient.__init__: server=%s site=%s token_name=%s",
            server_url,
            site_name,
            token_name,
        )
        validate_base_url(server_url, label="tableau_server_url")
        self._server_url = server_url.rstrip("/")
        self._token_name = token_name
        self._token_secret = token_secret
        self._site_name = site_name
        self._timeout = timeout
        self._session: Optional[requests.Session] = None
        self.token: Optional[str] = None
        self.site_id: Optional[str] = None

    # ------------------------------------------------------------------ auth

    def authenticate(self) -> None:
        """Authenticate with the Tableau Server using a Personal Access Token.

        Sets ``self.token`` and ``self.site_id`` on success.  Retries up to
        ``_MAX_RETRIES`` times with exponential backoff on transient failures.

        Raises:
            ConnectionError: If all retry attempts fail.
        """
        log.info(
            "TableauApiClient.authenticate: entry — server=%s site=%s",
            self._server_url,
            self._site_name,
        )

        url = f"{self._server_url}/api/{_API_VERSION}/auth/signin"
        payload = {
            "credentials": {
                "personalAccessTokenName": self._token_name,
                "personalAccessTokenSecret": self._token_secret,
                "site": {"contentUrl": self._site_name},
            }
        }

        def _do_auth():
            session = self._get_session()
            resp = session.post(url, json=payload, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
            credentials = data.get("credentials", {})
            self.token = credentials.get("token")
            self.site_id = credentials.get("site", {}).get("id")
            if not self.token or not self.site_id:
                raise KGConnectionError(
                    "Authentication succeeded but token/site_id missing in response",
                    context={"response_keys": list(credentials.keys())},
                )
            session.headers.update({"X-Tableau-Auth": self.token})
            log.info(
                "TableauApiClient.authenticate: exit — site_id=%s",
                self.site_id,
            )

        retry_request(
            _do_auth,
            max_retries=_MAX_RETRIES,
            backoff_base=_BACKOFF_SECONDS[0],
            label="authenticate",
        )

    # ----------------------------------------------------------- connectivity

    def test_connection(self) -> bool:
        """Probe server health without consuming a full auth round-trip.

        Returns:
            True if the server responds with a 2xx status on the API info
            endpoint, False otherwise.  Never raises.
        """
        log.info("TableauApiClient.test_connection: entry")
        url = f"{self._server_url}/api/{_API_VERSION}/serverinfo"
        try:
            session = self._get_session()
            resp = session.get(url, timeout=self._timeout)
            ok = resp.status_code < 400
            log.info(
                "TableauApiClient.test_connection: exit — status=%d ok=%s",
                resp.status_code,
                ok,
            )
            return ok
        except Exception as exc:
            log.warning("TableauApiClient.test_connection: request failed — %s", exc)
            return False

    # --------------------------------------------------------------- GraphQL

    def graphql_query(self, query: str) -> dict:
        """Execute a Metadata API GraphQL query.

        Automatically refreshes auth on HTTP 401.  Retries transient 5xx
        errors up to ``_MAX_RETRIES`` times with backoff.

        Args:
            query: GraphQL query string (without ``query`` wrapper — just the
                selection set, e.g. ``"{ calculatedFieldsConnection { ... } }"``).

        Returns:
            The parsed JSON response dict.

        Raises:
            ConnectionError: On network failure or persistent server errors.
        """
        log.info("TableauApiClient.graphql_query: entry — query_len=%d", len(query))

        if not self.token:
            raise KGConnectionError(
                "Not authenticated — call authenticate() first",
                context={"token_present": False},
            )

        url = f"{self._server_url}/api/metadata/graphql"
        payload = {"query": query}

        def _do_query():
            session = self._get_session()
            resp = session.post(url, json=payload, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
            log.info(
                "TableauApiClient.graphql_query: exit — response_keys=%s",
                list(data.keys()),
            )
            return data

        return retry_request(
            _do_query,
            max_retries=_MAX_RETRIES,
            backoff_base=_BACKOFF_SECONDS[0],
            label="graphql_query",
            on_401=self.authenticate,
        )

    # --------------------------------------------- high-level query helpers

    def get_calculated_fields(self) -> list[dict]:
        """Retrieve all calculated fields across all datasources.

        Returns:
            List of calculated field dicts from the Metadata API.  Empty list
            if none exist.  Errors are logged but not re-raised.

        Raises:
            ConnectionError: On network/auth failure.
        """
        log.info("TableauApiClient.get_calculated_fields: entry")
        try:
            data = self.graphql_query(_GQL_CALCULATED_FIELDS)
            nodes = (
                data.get("data", {})
                    .get("calculatedFieldsConnection", {})
                    .get("nodes", [])
            )
            log.info(
                "TableauApiClient.get_calculated_fields: exit — count=%d",
                len(nodes),
            )
            return nodes
        except KGConnectionError:
            raise
        except Exception as exc:
            fail_step("get_calculated_fields", exc)
            return []

    def get_datasources(self) -> list[dict]:
        """Retrieve all embedded datasources (workbook-embedded).

        Returns:
            List of embedded datasource dicts.  Empty list on non-fatal error.

        Raises:
            ConnectionError: On network/auth failure.
        """
        log.info("TableauApiClient.get_datasources: entry")
        try:
            data = self.graphql_query(_GQL_DATASOURCES)
            nodes = (
                data.get("data", {})
                    .get("embeddedDatasourcesConnection", {})
                    .get("nodes", [])
            )
            log.info(
                "TableauApiClient.get_datasources: exit — count=%d",
                len(nodes),
            )
            return nodes
        except KGConnectionError:
            raise
        except Exception as exc:
            fail_step("get_datasources", exc)
            return []

    def get_dashboards(self) -> list[dict]:
        """Retrieve all dashboards with their sheets and field references.

        Returns:
            List of dashboard dicts.  Empty list on non-fatal error.

        Raises:
            ConnectionError: On network/auth failure.
        """
        log.info("TableauApiClient.get_dashboards: entry")
        try:
            data = self.graphql_query(_GQL_DASHBOARDS)
            nodes = (
                data.get("data", {})
                    .get("dashboardsConnection", {})
                    .get("nodes", [])
            )
            log.info(
                "TableauApiClient.get_dashboards: exit — count=%d",
                len(nodes),
            )
            return nodes
        except KGConnectionError:
            raise
        except Exception as exc:
            fail_step("get_dashboards", exc)
            return []

    # ----------------------------------------------------------------- lifecycle

    def close(self) -> None:
        """Sign out from Tableau Server and release the HTTP session."""
        log.info("TableauApiClient.close: entry")
        if self.token and self.site_id:
            url = f"{self._server_url}/api/{_API_VERSION}/auth/signout"
            try:
                session = self._get_session()
                resp = session.post(url, timeout=self._timeout)
                log.info(
                    "TableauApiClient.close: signout status=%d",
                    resp.status_code,
                )
            except Exception as exc:
                log.warning("TableauApiClient.close: signout request failed — %s", exc)
        if self._session is not None:
            self._session.close()
            self._session = None
        self.token = None
        self.site_id = None
        log.info("TableauApiClient.close: exit")

    def __enter__(self) -> "TableauApiClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ----------------------------------------------------------------- internal

    def _get_session(self) -> requests.Session:
        """Return the shared requests Session, creating it on first call."""
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({
                "Content-Type": "application/json",
                "Accept": "application/json",
            })
        return self._session
