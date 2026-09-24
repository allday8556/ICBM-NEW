"""SQLite WAL database access with a process-scoped write coordinator (ADR-0001, ADR-0002)."""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, make_url, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import AppError, ErrorClass


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


DATABASE_WRITE_REENTRANT = "DATABASE_WRITE_REENTRANT"


class DatabaseWriteReentryError(AppError):
    """A write unit was opened on a thread that already holds one.

    The write coordinator is not re-entrant. A nested write would wait for itself forever, so it
    is refused at once and the enclosing unit rolls back: fail closed, never a hang (Gate 2 G2-B
    carry-forward from the G2-A cross-audit)."""

    error_class = ErrorClass.FATAL


class Database:
    """The single unit-of-work boundary for application DB access.

    Reads use independent sessions (WAL allows concurrent readers). Writes are serialised by a
    process-scoped lock so request threads and the job-worker thread never contend inside
    SQLite. Keep write blocks short and never perform external I/O while holding one. A write
    opened inside another on the same thread is refused with :class:`DatabaseWriteReentryError`.
    """

    def __init__(self, database_url: str) -> None:
        self.url = database_url
        self.engine = create_sqlite_engine(database_url)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)
        self._write_lock = threading.Lock()
        # The thread holding the write unit, if any. Only that thread can ever read its own ident
        # here, so the check needs no lock of its own.
        self._writer: int | None = None

    @contextmanager
    def read(self) -> Iterator[Session]:
        with self._sessions() as session:
            yield session

    @contextmanager
    def write(self) -> Iterator[Session]:
        if self._writer == threading.get_ident():
            raise DatabaseWriteReentryError(
                DATABASE_WRITE_REENTRANT,
                "a write unit is already open on this thread; a nested write is refused, never "
                "waited for",
            )
        with self._write_lock:
            self._writer = threading.get_ident()
            try:
                with self._sessions.begin() as session:
                    yield session
            finally:
                self._writer = None

    def ping(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def journal_mode(self) -> str:
        with self.engine.connect() as connection:
            return str(connection.exec_driver_sql("PRAGMA journal_mode").scalar()).lower()

    def dispose(self) -> None:
        self.engine.dispose()
