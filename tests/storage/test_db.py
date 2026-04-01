import sqlite3
from tempfile import TemporaryDirectory
from pathlib import Path

from chipcoin.storage.db import initialize_database


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
