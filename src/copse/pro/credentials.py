"""Where copse Pro keeps its credentials (entitlement + refresh token).

In order of preference:

* macOS: the login keychain, through ``security``. The secret is written via
  ``security -i`` on stdin, so it never appears on a command line.
* Linux: the Secret Service, through ``secret-tool`` (secret on stdin).
* Otherwise: ``$COPSE_HOME/pro/credentials.json`` (default ``~/.copse``),
  created 0600 inside a 0700 directory. Reading refuses a file or directory
  that is group/world accessible, not owned by the user, or a symlink.

``COPSE_PRO_CREDENTIAL_STORE=keychain|secret-service|file`` forces one.
Secrets are never logged.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

SERVICE = "copse-pro"
ACCOUNT = "default"
MAX_BLOB = 64 * 1024


class CredentialError(Exception):
    """The store couldn't be used safely. Never contains a secret."""


def _encode(creds: dict) -> str:
    return base64.b64encode(json.dumps(creds, separators=(",", ":")).encode()).decode("ascii")


def _decode(blob: str) -> dict | None:
    blob = blob.strip()
    if not blob:
        return None
    if len(blob) > MAX_BLOB:
        raise CredentialError("stored credentials are too large")
    try:
        creds = json.loads(base64.b64decode(blob, validate=True))
    except ValueError as e:
        raise CredentialError("stored credentials are corrupt") from e
    if not isinstance(creds, dict):
        raise CredentialError("stored credentials are corrupt")
    return creds


class KeychainStore:
    """macOS login keychain via the ``security`` CLI."""
    name = "keychain"

    def __init__(self, service: str = SERVICE, account: str = ACCOUNT) -> None:
        self.service, self.account = service, account

    def _run(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["security", *args], input=stdin, capture_output=True,
                              text=True, timeout=30, check=False)

    def load(self) -> dict | None:
        r = self._run(["find-generic-password", "-a", self.account, "-s", self.service, "-w"])
        if r.returncode == 44:   # errSecItemNotFound
            return None
        if r.returncode != 0:
            raise CredentialError(f"keychain read failed (security exit {r.returncode})")
        return _decode(r.stdout)

    def save(self, creds: dict) -> None:
        # The blob is base64 (no quotes, spaces or backslashes), so quoting is safe.
        line = (f'add-generic-password -U -a "{self.account}" -s "{self.service}" '
                f'-l "copse Pro" -w "{_encode(creds)}"\n')
        r = self._run(["-i"], stdin=line)
        if r.returncode != 0 or "error" in (r.stderr or "").lower():
            raise CredentialError(f"keychain write failed (security exit {r.returncode})")

    def delete(self) -> None:
        self._run(["delete-generic-password", "-a", self.account, "-s", self.service])


class SecretServiceStore:
    """The freedesktop Secret Service via ``secret-tool``."""
    name = "secret-service"

    def __init__(self, service: str = SERVICE, account: str = ACCOUNT) -> None:
        self.attrs = ["service", service, "account", account]

    def _run(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["secret-tool", *args], input=stdin, capture_output=True,
                              text=True, timeout=30, check=False)

    def load(self) -> dict | None:
        r = self._run(["lookup", *self.attrs])
        if r.returncode != 0:
            if not r.stdout and not r.stderr:
                return None   # not found
            raise CredentialError(f"secret-tool lookup failed (exit {r.returncode})")
        return _decode(r.stdout)

    def save(self, creds: dict) -> None:
        r = self._run(["store", "--label=copse Pro", *self.attrs], stdin=_encode(creds))
        if r.returncode != 0:
            raise CredentialError(f"secret-tool store failed (exit {r.returncode})")

    def delete(self) -> None:
        self._run(["clear", *self.attrs])


class FileStore:
    """A 0600 file in a 0700 directory; refuses anything looser."""
    name = "file"

    def __init__(self, directory: Path | None = None, account: str = ACCOUNT) -> None:
        if directory is None:
            from copse.config import copse_home

            directory = copse_home() / "pro"
        self.dir = Path(directory)
        name = "credentials" if account == ACCOUNT else account
        self.path = self.dir / f"{name}.json"

    @staticmethod
    def _check(st: os.stat_result, what: str, mode: int) -> None:
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            raise CredentialError(f"{what} is not owned by the current user")
        if stat.S_IMODE(st.st_mode) & 0o077:
            raise CredentialError(
                f"refusing to use {what}: permissions {oct(stat.S_IMODE(st.st_mode))} "
                f"are looser than {oct(mode)}")

    def _check_dir(self) -> None:
        st = os.lstat(self.dir)
        if not stat.S_ISDIR(st.st_mode):
            raise CredentialError("credentials directory is not a directory")
        self._check(st, "credentials directory", 0o700)

    def load(self) -> dict | None:
        if not self.dir.exists():
            return None
        self._check_dir()
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        except OSError as e:
            raise CredentialError("cannot open the credentials file") from e
        with os.fdopen(fd, "r", encoding="ascii", errors="replace") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise CredentialError("credentials file is not a regular file")
            self._check(st, "credentials file", 0o600)
            return _decode(fh.read(MAX_BLOB + 1))

    def save(self, creds: dict) -> None:
        old = os.umask(0o077)
        try:
            self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._check_dir()
            tmp = self.dir / f".credentials.{os.getpid()}.tmp"
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                         0o600)
            try:
                with os.fdopen(fd, "w", encoding="ascii") as fh:
                    fh.write(_encode(creds))
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
                raise
        finally:
            os.umask(old)

    def delete(self) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


LEARNING_SECRET = "learning-secret"


def default_store(account: str = ACCOUNT):
    """The credential store for ``account`` (``default``: login credentials;
    ``learning-secret``: the per-install HMAC key for hosted learning, kept
    apart so logging out doesn't reset it)."""
    forced = os.environ.get("COPSE_PRO_CREDENTIAL_STORE", "").strip().lower()
    if forced == "keychain":
        return KeychainStore(account=account)
    if forced == "secret-service":
        return SecretServiceStore(account=account)
    if forced == "file":
        return FileStore(account=account)
    if forced:
        raise CredentialError(f"unknown COPSE_PRO_CREDENTIAL_STORE {forced!r}")
    if sys.platform == "darwin" and shutil.which("security"):
        return KeychainStore(account=account)
    if sys.platform.startswith("linux") and shutil.which("secret-tool"):
        return SecretServiceStore(account=account)
    return FileStore(account=account)
