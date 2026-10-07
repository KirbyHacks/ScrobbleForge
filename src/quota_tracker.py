import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional, Tuple


class QuotaTracker:
    """
    Manages persistent state and rolling 24-hour Last.fm quota tracking.
    Uses SQLite WAL (Write-Ahead Logging) mode for safe concurrent access.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS scrobbles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER NOT NULL,
                    artist TEXT NOT NULL,
                    title TEXT NOT NULL
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_scrobbles_timestamp
                ON scrobbles (timestamp)
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS app_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            conn.commit()

    def record_scrobble(self, artist: str, title: str, timestamp: Optional[int] = None):
        """Records a scrobble timestamp for rolling window tracking."""
        ts = timestamp if timestamp is not None else int(time.time())
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO scrobbles (timestamp, artist, title) VALUES (?, ?, ?)",
                (ts, artist, title)
            )
            conn.commit()

    def get_rolling_24h_count(self, now: Optional[int] = None) -> int:
        """Counts scrobbles submitted in the rolling 24-hour window."""
        current_time = now if now is not None else int(time.time())
        cutoff = current_time - 86400
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM scrobbles WHERE timestamp >= ?", (cutoff,))
            return cursor.fetchone()[0]

    def can_scrobble(self, max_daily_limit: int = 2750, now: Optional[int] = None) -> Tuple[bool, int]:
        """
        Determines whether the engine is allowed to scrobble or must pause.
        Returns:
            (allowed: bool, wait_seconds: int)
        """
        current_time = now if now is not None else int(time.time())
        count = self.get_rolling_24h_count(now=current_time)

        if count < max_daily_limit:
            return True, 0

        # Calculate exact seconds until oldest scrobbles in window age past 24 hours
        cutoff = current_time - 86400
        excess = (count - max_daily_limit) + 1
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT timestamp FROM scrobbles WHERE timestamp >= ? ORDER BY timestamp ASC LIMIT ?",
                (cutoff, excess)
            )
            rows = cursor.fetchall()
            if rows:
                oldest_in_excess = rows[-1][0]
                wait_seconds = max(1, (oldest_in_excess + 86400) - current_time)
                return False, wait_seconds

        return False, 60

    def get_total_scrobbles(self) -> int:
        """Returns total historical scrobbles recorded in the local ledger."""
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM scrobbles")
            return cursor.fetchone()[0]

    def set_state(self, key: str, value: str):
        """Sets a persistent key-value property."""
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO app_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value))
            )
            conn.commit()

    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Reads a persistent key-value property."""
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM app_state WHERE key = ?", (key,))
            row = cursor.fetchone()
            if row:
                return row[0]
            return default

    def prune_old_records(self, retention_days: int = 14):
        """Prunes records older than retention period to keep database compact."""
        cutoff = int(time.time()) - (retention_days * 86400)
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM scrobbles WHERE timestamp < ?", (cutoff,))
            conn.commit()
