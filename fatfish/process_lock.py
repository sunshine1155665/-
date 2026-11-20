"""Small cross-process advisory locks for single-owner resources.

The stack has several things that must exist **at most once** at a time:

* the CosyVoice process (a second one doubles VRAM use and the GPU simply
  refuses to allocate),
* the embedding server (a second copy costs another ~1 GB of RAM),
* the QQ adapter (two adapters answer the same message twice),
* a ComfyUI photo job (two jobs fight over the same 8 GB card).

Port binding alone is not enough on Windows: ``SO_REUSEADDR`` lets a second
listener bind a port that is already being served, so two processes can both
believe they own 9880/9881.  ``SO_REUSEPORT`` is not the issue here; the
Windows ``SO_REUSEADDR`` semantic is.  An OS file lock has no such ambiguity
and is released automatically when the process exits, even after a crash.

This module is deliberately stdlib-only and side-effect free at import time:
``scripts/start_all.py`` and the CosyVoice virtualenv scripts both import it
before any project dependency is checked.

Usage::

    from fatfish.process_lock import acquire, lock_path

    lock = acquire(lock_path(config.data_dir, "tts"))
    if lock is None:
        raise SystemExit("another TTS server already holds the lock")
    try:
        serve()
    finally:
        lock.release()

For a scoped lock use :func:`held`, which raises :class:`LockBusy` instead of
returning ``None``.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Iterator

__all__ = [
    "LockBusy",
    "ProcessLock",
    "acquire",
    "held",
    "lock_path",
    "normalize_path",
]


class LockBusy(RuntimeError):
    """Raised by :func:`held` when the lock is already owned elsewhere."""


# Windows byte-range locks are attached to a file handle, so two handles in the
# same process normally conflict as well.  This registry makes that guarantee
# explicit and portable (POSIX ``flock`` behaviour for a second open file
# description in the same process is less obvious), and it also makes the
# in-process path deterministic for tests.
_REGISTRY_LOCK = threading.Lock()
_HELD: set[str] = set()


def normalize_path(path: str | Path) -> str:
    """Return a stable registry key, case-insensitive on Windows."""
    return os.path.normcase(os.path.abspath(str(path)))


def lock_path(data_dir: str | Path, name: str) -> Path:
    """Conventional lock location: ``<data_dir>/run/<name>.lock``."""
    suffix = "" if str(name).endswith(".lock") else ".lock"
    return Path(data_dir) / "run" / f"{name}{suffix}"


@dataclass(slots=True)
class ProcessLock:
    """A held OS-level lock plus the file handle that keeps it alive."""

    path: Path
    handle: IO[bytes] = field(repr=False)
    _released: bool = field(default=False, init=False, repr=False)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        handle, self.handle = self.handle, None  # type: ignore[assignment]
        try:
            try:
                handle.seek(0)  # type: ignore[union-attr]
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[union-attr]
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[union-attr]
            except (OSError, ImportError):
                pass
        finally:
            try:
                handle.close()  # type: ignore[union-attr]
            except OSError:
                pass
            with _REGISTRY_LOCK:
                _HELD.discard(normalize_path(self.path))

    def __enter__(self) -> "ProcessLock":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def _try_os_lock(handle: IO[bytes]) -> bool:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, ImportError):
        return False
    return True


def acquire(
    path: str | Path,
    *,
    timeout: float | None = 0.0,
    poll: float = 0.05,
) -> ProcessLock | None:
    """Try to take an exclusive lock, returning ``None`` when it is busy.

    ``timeout=0`` means "one attempt and give up" -- the right thing for a
    request handler that should return a friendly ``busy`` instead of hanging.
    ``timeout=None`` waits forever, for service startup.  A positive timeout
    retries until the deadline.
    """
    target = Path(path)
    key = normalize_path(target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))

    while True:
        with _REGISTRY_LOCK:
            if key not in _HELD:
                handle: IO[bytes] | None = None
                try:
                    handle = target.open("a+b")
                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        # msvcrt.locking locks bytes; an empty file has none.
                        handle.write(b"\0")
                        handle.flush()
                    if _try_os_lock(handle):
                        _HELD.add(key)
                        return ProcessLock(target, handle)
                except OSError:
                    pass
                if handle is not None:
                    try:
                        handle.close()
                    except OSError:
                        pass
        if deadline is not None and time.monotonic() >= deadline:
            return None
        # A tiny sleep outside the registry lock keeps contention cheap.
        time.sleep(min(max(0.01, poll), 0.2))


@contextmanager
def held(path: str | Path, *, timeout: float | None = 0.0) -> Iterator[ProcessLock]:
    """Context manager form that raises :class:`LockBusy` when unavailable."""
    lock = acquire(path, timeout=timeout)
    if lock is None:
        raise LockBusy(f"resource busy: {path}")
    try:
        yield lock
    finally:
        lock.release()
