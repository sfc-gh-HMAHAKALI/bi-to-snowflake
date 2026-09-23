"""Denodo Virtual DataPort JDBC connection manager.

Provides stable, retry-capable JDBC connections to Denodo VDP
for metadata extraction (view columns, dependencies, details).

Requires: jaydebeapi + JVM + Denodo JDBC driver JAR.
"""

import time
from typing import Any

from ..common.logger import get_logger
from ..common.errors import ConnectionError, fail_step

log = get_logger("denodo.vdp_client")

# Default JDBC driver class
VDP_DRIVER_CLASS = "com.denodo.vdp.jdbc.Driver"
DEFAULT_TIMEOUT = 30
MAX_RETRIES = 3
BACKOFF_BASE = 1  # seconds


class VDPClient:
    """JDBC connection manager for Denodo Virtual DataPort.

    Usage:
        client = VDPClient(host="vdp.example.com", port=9999,
                           database="mydb", username="user", password="pass",
                           driver_jar="/path/to/denodo-vdp-jdbcdriver.jar")
        if client.test_connection():
            columns = client.get_all_view_columns("my_view")
        client.close()
    """

    def __init__(
        self,
        host: str,
        port: int = 9999,
        database: str = "",
        username: str = "",
        password: str = "",
        driver_jar: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.host = host
        self.port = port
        self.database = database
        self.username = username
        self.password = password
        self.driver_jar = driver_jar
        self.timeout = timeout
        self._conn = None
        self._jdbc_url = f"jdbc:vdb://{host}:{port}/{database}"

        log.info(
            "VDPClient initialized: %s:%d/%s",
            host, port, database,
        )

    def _connect(self) -> None:
        """Establish JDBC connection with retry."""
        try:
            import jaydebeapi
        except ImportError:
            raise ConnectionError(
                "jaydebeapi not installed. Run: pip install jaydebeapi",
                context={"package": "jaydebeapi"},
            )

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                log.info(
                    "Connecting to VDP (attempt %d/%d): %s",
                    attempt, MAX_RETRIES, self._jdbc_url,
                )

                jars = [self.driver_jar] if self.driver_jar else []
                self._conn = jaydebeapi.connect(
                    VDP_DRIVER_CLASS,
                    self._jdbc_url,
                    [self.username, self.password],
                    jars,
                )

                log.info("VDP connection established successfully.")
                return

            except Exception as e:
                wait = BACKOFF_BASE * (2 ** (attempt - 1))
                log.warning(
                    "VDP connection attempt %d failed: %s. Retrying in %ds...",
                    attempt, e, wait,
                )
                if attempt == MAX_RETRIES:
                    raise ConnectionError(
                        f"Failed to connect to VDP after {MAX_RETRIES} attempts: {e}",
                        context={
                            "host": self.host,
                            "port": self.port,
                            "database": self.database,
                            "last_error": str(e),
                        },
                    )
                time.sleep(wait)

    def _ensure_connection(self) -> None:
        """Ensure we have an active connection, reconnecting if needed."""
        if self._conn is None:
            self._connect()
        else:
            # Test if connection is still alive
            try:
                cursor = self._conn.cursor()
                cursor.execute("SELECT 1")
                cursor.close()
            except Exception:
                log.warning("VDP connection lost. Reconnecting...")
                self._conn = None
                self._connect()

    def _execute_query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute a query and return results as list of dicts."""
        self._ensure_connection()
        log.debug("Executing SQL: %s", sql[:200])

        try:
            cursor = self._conn.cursor()
            cursor.execute(sql, params)

            if cursor.description is None:
                cursor.close()
                return []

            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            cursor.close()

            results = [dict(zip(columns, row)) for row in rows]
            log.debug("Query returned %d rows.", len(results))
            return results

        except Exception as e:
            log.error("Query execution failed: %s", e)
            # Try reconnecting once
            self._conn = None
            self._ensure_connection()
            try:
                cursor = self._conn.cursor()
                cursor.execute(sql, params)
                columns = [desc[0] for desc in cursor.description] if cursor.description else []
                rows = cursor.fetchall() if columns else []
                cursor.close()
                return [dict(zip(columns, row)) for row in rows]
            except Exception as e2:
                raise ConnectionError(
                    f"Query failed after reconnect: {e2}",
                    context={"sql": sql[:200], "original_error": str(e)},
                )

    def test_connection(self) -> bool:
        """Test if we can connect to VDP."""
        log.info("Testing VDP connection to %s", self._jdbc_url)
        try:
            self._connect()
            result = self._execute_query("SELECT 1")
            log.info("VDP connection test: SUCCESS")
            return True
        except Exception as e:
            log.error("VDP connection test: FAILED — %s", e)
            return False

    def get_all_view_columns(self, view_name: str) -> list[dict]:
        """Get all columns for a view via VDP metadata query.

        Returns list of {column_name, column_type, column_size, ...}.
        """
        log.info("Getting columns for view: %s", view_name)
        sql = """
            SELECT column_name, column_type_name, column_size,
                   column_sql_type_id, column_description
            FROM CATALOG_VDP_METADATA_VIEWS()
            WHERE input_view_name = ?
        """
        try:
            results = self._execute_query(sql, (view_name,))
            log.info("View '%s' has %d columns.", view_name, len(results))
            return results
        except Exception as e:
            record = fail_step(f"get_columns({view_name})", e)
            log.warning("Failed to get columns for %s: %s", view_name, e)
            return []

    def get_view_dependencies(self, view_name: str) -> list[dict]:
        """Get dependency tree for a view (what views/tables it depends on).

        Returns list of {dependency_name, dependency_type, depth}.
        """
        log.info("Getting dependencies for view: %s", view_name)
        sql = """
            SELECT dependency_name, dependency_type, depth
            FROM CATALOG_VDP_METADATA_VIEWS_DEPENDENCIES()
            WHERE input_view_name = ?
        """
        try:
            results = self._execute_query(sql, (view_name,))
            log.info("View '%s' has %d dependencies.", view_name, len(results))
            return results
        except Exception as e:
            fail_step(f"get_dependencies({view_name})", e)
            return []

    def get_view_column_detail(self, view_name: str, column_name: str) -> dict | None:
        """Get detailed metadata for a single column."""
        log.debug("Getting detail for %s.%s", view_name, column_name)
        sql = """
            SELECT *
            FROM CATALOG_VDP_METADATA_VIEWS()
            WHERE input_view_name = ?
              AND column_name = ?
        """
        try:
            results = self._execute_query(sql, (view_name, column_name))
            return results[0] if results else None
        except Exception as e:
            log.warning("Failed to get detail for %s.%s: %s", view_name, column_name, e)
            return None

    def get_all_elements(self, database: str | None = None) -> list[dict]:
        """Get all views/tables in the VDP database.

        Returns list of {view_name, view_type, database_name, folder}.
        """
        db = database or self.database
        log.info("Getting all elements in database: %s", db)
        sql = """
            SELECT view_name, view_type, database_name, folder
            FROM CATALOG_VDP_METADATA_VIEWS()
            WHERE database_name = ?
            GROUP BY view_name, view_type, database_name, folder
        """
        try:
            results = self._execute_query(sql, (db,))
            log.info("Database '%s' has %d views/tables.", db, len(results))
            return results
        except Exception as e:
            fail_step(f"get_all_elements({db})", e)
            return []

    def close(self) -> None:
        """Close the JDBC connection."""
        if self._conn is not None:
            try:
                self._conn.close()
                log.info("VDP connection closed.")
            except Exception as e:
                log.warning("Error closing VDP connection: %s", e)
            self._conn = None
