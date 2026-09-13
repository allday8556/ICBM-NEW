"""SQLite WAL database access with a process-scoped write coordinator (ADR-0001, ADR-0002)."""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, make_url, text
from sqlalchemy.orm import Session, sessionmaker


def sqlite_database_dir(database_url: str) -> Path:
    """Directory holding a file-backed SQLite database: the data directory whose ownership a
    writer must hold (ADR-0006). Any other URL fails closed, since ICBM v1 keeps its state in one
    SQLite file (ADR-0001)."""
    url = make_url(database_url)
    database = url.database
    if url.get_backend_name() != "sqlite" or not database or database == ":memory:":
        raise ValueError(f"not a file-backed SQLite database: {database_url}")
    return Path(database).parent


def create_sqlite_engine(database_url: str) -> Engine:
    engine = create_engine(database_url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _configure(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()

    return engine


class Database:
    """The single unit-of-work boundary for application DB access.

    Reads use independent sessions (WAL allows concurrent readers). Writes are serialised by a
    process-scoped lock so request threads and the job-worker thread never contend inside
    SQLite. Keep write blocks short and never perform external I/O while holding one.
    """

    def __init__(self, database_url: str) -> None:
        self.url = database_url
        self.engine = create_sqlite_engine(database_url)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)
        self._write_lock = threading.Lock()

    @contextmanager
    def read(self) -> Iterator[Session]:
        with self._sessions() as session:
            yield session

    @contextmanager
    def write(self) -> Iterator[Session]:
        with self._write_lock, self._sessions.begin() as session:
            yield session

    def ping(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def journal_mode(self) -> str:
        with self.engine.connect() as connection:
            return str(connection.exec_driver_sql("PRAGMA journal_mode").scalar()).lower()

    def dispose(self) -> None:
        self.engine.dispose()
