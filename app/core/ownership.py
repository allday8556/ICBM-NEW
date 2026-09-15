"""One ICBM process per data directory (ADR-0006).

Ownership of a data directory is an exclusive, non-blocking OS lock held on an open handle of
``<data_dir>/runtime/owner.lock`` for the owner's whole lifetime. ``runtime`` is the
application-owned part of the data root: the database, the sessions and this lock (Issue #52
comment 5688854287). The primitive is:

* Windows — ``msvcrt.locking`` on one byte at a fixed offset past the metadata. Windows byte-range
  locks are mandatory, so the locked byte sits away from the metadata contenders read.
* POSIX — ``fcntl.flock(LOCK_EX | LOCK_NB)``.

The operating system decides ownership. Because the lock belongs to the open file, every spelling
of a path that reaches the same directory (relative, ``..``, symlink, junction) contends for the
same lock; nothing keys ownership on a path string. Process death releases the lock. The lock file
is never deleted and its JSON contents are diagnostic only. Without an OS locking primitive,
acquisition fails closed.

Code handed a lease checks it with ``require_ownership`` before its first side effect on a
target directory, so a lease for one directory can never authorise a mutation of another.
"""

import errno
import json
import os
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

RUNTIME_DIR_NAME = "runtime"
LOCK_FILE_NAME = "owner.lock"
DATA_DIR_IN_USE = "DATA_DIR_IN_USE"
DATA_DIR_LOCK_UNSUPPORTED = "DATA_DIR_LOCK_UNSUPPORTED"
DATA_DIR_LOCK_FAILED = "DATA_DIR_LOCK_FAILED"
DATA_DIR_NOT_OWNED = "DATA_DIR_NOT_OWNED"

_WINDOWS_LOCK_OFFSET = 1 << 20
_METADATA_LIMIT = 4096
_CONTENDED = frozenset({errno.EACCES, errno.EAGAIN, errno.EDEADLK})


def runtime_dir(data_dir: Path) -> Path:
    """The application-owned part of a data root: the database, the sessions and the lock."""
    return data_dir / RUNTIME_DIR_NAME


def owner_lock_path(data_dir: Path) -> Path:
    return runtime_dir(data_dir) / LOCK_FILE_NAME


class DataDirOwnershipError(RuntimeError):
    def __init__(
        self,
        reason_code: str,
        message: str,
        *,
        data_dir: Path,
        holder: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.data_dir = data_dir
        self.holder = holder


class DataDirInUseError(DataDirOwnershipError):
    """Another process holds the OS lock on this data directory."""


class OwnershipUnavailableError(DataDirOwnershipError):
    """No usable OS lock: refuse to run rather than run without single-owner protection."""


class OwnershipMismatchError(DataDirOwnershipError):
    """A mutation target is not covered by an active lease on that very directory."""


class _LockPrimitive(Protocol):
    name: str

    def try_lock(self, fd: int) -> bool: ...

    def unlock(self, fd: int) -> None: ...


class _WindowsLock:
    name = "msvcrt.locking"

    def try_lock(self, fd: int) -> bool:
        if sys.platform != "win32":  # pragma: no cover - only selected on Windows
            raise RuntimeError("msvcrt locking is Windows-only")
        import msvcrt

        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in _CONTENDED:
                return False
            raise
        return True

    def unlock(self, fd: int) -> None:
        if sys.platform != "win32":  # pragma: no cover - only selected on Windows
            raise RuntimeError("msvcrt locking is Windows-only")
        import msvcrt

        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


class _PosixLock:
    name = "fcntl.flock"

    def try_lock(self, fd: int) -> bool:
        if sys.platform == "win32":  # pragma: no cover - only selected on POSIX
            raise RuntimeError("fcntl.flock is POSIX-only")
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in _CONTENDED:
                return False
            raise
        return True

    def unlock(self, fd: int) -> None:
        if sys.platform == "win32":  # pragma: no cover - only selected on POSIX
            raise RuntimeError("fcntl.flock is POSIX-only")
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _detect_platform_lock() -> _LockPrimitive | None:
    if sys.platform == "win32":
        return _WindowsLock()
    try:
        import fcntl  # noqa: F401
    except ImportError:
        return None
    return _PosixLock()


def read_owner_metadata(lock_path: Path) -> dict[str, Any] | None:
    """Best-effort diagnostic read of the lock file. Never used to decide ownership."""
    try:
        with lock_path.open("rb") as handle:
            value = json.loads(handle.read(_METADATA_LIMIT).decode("utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _same_file(fd: int, path: Path) -> bool:
    try:
        on_disk = os.stat(path)
    except OSError:
        return False
    held = os.fstat(fd)
    return (held.st_dev, held.st_ino) == (on_disk.st_dev, on_disk.st_ino)


def _write_metadata(fd: int, metadata: dict[str, Any]) -> None:
    payload = json.dumps(metadata, ensure_ascii=False).encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, payload)
    os.fsync(fd)


class DataDirLease:
    """An acquired ownership lease. Hold it for the owning process's whole lifetime."""

    def __init__(
        self,
        *,
        data_dir: Path,
        lock_path: Path,
        fd: int,
        primitive: _LockPrimitive,
        metadata: dict[str, Any],
    ) -> None:
        self.data_dir = data_dir
        self.lock_path = lock_path
        self.metadata = metadata
        self._fd: int | None = fd
        self._primitive = primitive

    @property
    def active(self) -> bool:
        return self._fd is not None

    def covers(self, directory: Path) -> bool:
        """True when ``directory`` is the data root this lease locks, or the runtime directory
        that holds its database (compared as files)."""
        fd = self._fd
        return fd is not None and (
            _same_file(fd, owner_lock_path(directory)) or _same_file(fd, directory / LOCK_FILE_NAME)
        )

    def verify(self) -> tuple[bool, str]:
        fd = self._fd
        if fd is None:
            return False, "ownership lease was released"
        if not _same_file(fd, self.lock_path):
            return False, f"{self.lock_path} no longer matches the held handle"
        return True, f"pid {self.metadata['pid']} holds {self._primitive.name} on {self.lock_path}"

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            self._primitive.unlock(fd)
        finally:
            os.close(fd)  # the lock file stays; only the OS lock identifies an owner

    def __enter__(self) -> "DataDirLease":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def acquire_data_dir(data_dir: Path, *, app_version: str) -> DataDirLease:
    """Take exclusive ownership of the data root ``data_dir`` or fail fast. Never blocks and
    never takes over. The lock is ``<data_dir>/runtime/owner.lock``."""
    return _acquire(data_dir, runtime_dir(data_dir), app_version=app_version)


def acquire_database_dir(database_dir: Path, *, app_version: str) -> DataDirLease:
    """Ownership for a caller that knows only a database (the migration environment): the lock
    in the directory that holds it. For a data root's own database, in ``<root>/runtime``, that
    is the very lock ``acquire_data_dir(root)`` takes, so the two always contend."""
    return _acquire(database_dir, database_dir, app_version=app_version)


def _acquire(data_dir: Path, lock_dir: Path, *, app_version: str) -> DataDirLease:
    resolved = data_dir.resolve()
    primitive = _detect_platform_lock()
    if primitive is None:
        raise OwnershipUnavailableError(
            DATA_DIR_LOCK_UNSUPPORTED,
            f"no OS file-locking primitive is available; refusing to use {resolved} without "
            "single-owner protection",
            data_dir=resolved,
        )
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / LOCK_FILE_NAME
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
    try:
        acquired = primitive.try_lock(fd)
    except OSError as exc:
        os.close(fd)
        raise OwnershipUnavailableError(
            DATA_DIR_LOCK_FAILED, f"could not lock {lock_path}: {exc}", data_dir=resolved
        ) from exc
    if not acquired:
        os.close(fd)
        raise DataDirInUseError(
            DATA_DIR_IN_USE,
            f"data directory {resolved} is already owned by another ICBM process",
            data_dir=resolved,
            holder=read_owner_metadata(lock_path),
        )
    if not _same_file(fd, lock_path):
        primitive.unlock(fd)
        os.close(fd)
        raise OwnershipUnavailableError(
            DATA_DIR_LOCK_FAILED,
            f"{lock_path} was replaced while it was being locked",
            data_dir=resolved,
        )
    metadata: dict[str, Any] = {
        "pid": os.getpid(),
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "hostname": socket.gethostname(),
        "resolved_data_dir": str(resolved),
        "app_version": app_version,
        "lock": primitive.name,
    }
    _write_metadata(fd, metadata)
    return DataDirLease(
        data_dir=resolved, lock_path=lock_path, fd=fd, primitive=primitive, metadata=metadata
    )


def require_ownership(lease: DataDirLease | None, target_dir: Path) -> DataDirLease:
    """The single ADR-0006 gate for production mutation targets.

    ``lease`` must be active and lock ``target_dir`` itself (compared as files, so every spelling
    of the directory is accepted). Call it before the first side effect on ``target_dir``:
    creating it, opening a database engine, configuring a log file or migrating. It never acquires
    a lock and never creates anything, so a missing or mismatched lease fails closed.
    """
    if lease is None or not lease.active:
        raise OwnershipMismatchError(
            DATA_DIR_NOT_OWNED, f"no active ownership lease for {target_dir}", data_dir=target_dir
        )
    if not lease.covers(target_dir):
        raise OwnershipMismatchError(
            DATA_DIR_NOT_OWNED,
            f"ownership lease for {lease.data_dir} does not cover {target_dir}",
            data_dir=target_dir,
        )
    return lease
