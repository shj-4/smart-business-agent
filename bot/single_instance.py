"""Cross-process lock for the one Telegram polling process.

The lock file is intentionally persistent; the OS releases the byte-range lock
when the owning process exits or crashes. Its contents carry no credentials.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO

_lock_handle: BinaryIO | None = None


class BotAlreadyRunning(RuntimeError):
    """Raised when another process already owns the polling lock."""


def acquire_polling_lock(path: str | os.PathLike[str]) -> None:
    """Acquire an exclusive cross-process lock and keep it for process lifetime."""
    global _lock_handle
    if _lock_handle is not None:
        return

    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BotAlreadyRunning("another polling process owns the lock") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise BotAlreadyRunning("another polling process owns the lock") from exc
        _lock_handle = handle
    except Exception:
        handle.close()
        raise


def release_polling_lock() -> None:
    """Release the lock (primarily useful in tests and orderly shutdown)."""
    global _lock_handle
    if _lock_handle is None:
        return
    handle, _lock_handle = _lock_handle, None
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
