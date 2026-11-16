"""
Database Connection and Utility Helpers.
"""

import sqlite3
from pathlib import Path
from src.common.config import DB_PATH


def get_db_connection(custom_path: str | Path | None = None) -> sqlite3.Connection:
    """Returns a SQLite connection with 10s timeout and WAL pragma."""
    path = str(custom_path or DB_PATH)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn
