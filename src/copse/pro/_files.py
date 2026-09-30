"""Private files under ``$COPSE_HOME/pro``: 0600 files in a 0700 directory,
refused when group/world accessible, not the user's, or a symlink."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from copse.pro.credentials import CredentialError, FileStore


def private_dir() -> Path:
    from copse.config import copse_home

    d = copse_home() / "pro"
    old = os.umask(0o077)
    try:
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    finally:
        os.umask(old)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode):
        raise CredentialError(f"{d} is not a directory")
    FileStore._check(st, "copse Pro directory", 0o700)
    return d


def read_private(path: Path, limit: int) -> bytes | None:
    """The file's bytes (at most ``limit``), or None if it doesn't exist."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as e:
        raise CredentialError(f"cannot open {path.name}") from e
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise CredentialError(f"{path.name} is not a regular file")
        FileStore._check(st, path.name, 0o600)
        return fh.read(limit)


def write_private(path: Path, data: bytes) -> None:
    """Atomically replace ``path`` with ``data``, created 0600."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    old = os.umask(0o077)
    try:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
    finally:
        os.umask(old)
