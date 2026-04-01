from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from chipcoin.storage.db import _ensure_column, initialize_database


def test_initialize_database_creates_expected_tables() -> None:
    with TemporaryDirectory() as tempdir:
        database_path = Path(tempdir) / "chipcoin.sqlite3"
        connection = initialize_database(database_path)
        try:
            rows = connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                ORDER BY name
                """
            ).fetchall()
        finally:
            connection.close()

    table_names = {row["name"] for row in rows}
    assert {"headers", "blocks", "utxos", "chain_meta"} <= table_names


def test_initialize_database_returns_sqlite_connection() -> None:
    with TemporaryDirectory() as tempdir:
        connection = initialize_database(Path(tempdir) / "chipcoin.sqlite3")
        try:
            assert isinstance(connection, sqlite3.Connection)
        finally:
            connection.close()


def test_ensure_column_ignores_duplicate_column_race() -> None:
    class _Cursor:
        def fetchall(self):
            return []

    class _Connection:
        def execute(self, statement: str):
            if statement.startswith("PRAGMA table_info("):
                return _Cursor()
            if statement.startswith("ALTER TABLE peers ADD COLUMN last_seen"):
                raise sqlite3.OperationalError("duplicate column name: last_seen")
            raise AssertionError(f"unexpected statement: {statement}")

    _ensure_column(_Connection(), table="peers", column="last_seen", definition="INTEGER")
