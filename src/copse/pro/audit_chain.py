"""The ``audit`` events plugin (``copse.events`` group): a local,
tamper-evident log of every action copse took (copse Enterprise).

Only with a verified entitlement carrying the ``audit`` feature: without it
``emit`` drops the event on the spot and writes nothing, not even the key.

Each :class:`copse.events.Event` -- a delegation, a review verdict, an
escalation, a merge, a worktree removal, or a policy refusal -- becomes one
line of ``$COPSE_HOME/audit/<repo-key>.jsonl`` (0600, in a 0700 directory;
the repo key is the repo's basename plus a hash of its path). The log is
local, so the record carries the raw local names: agent id, branch, profile,
provider, model, actor, workspace and the outcome fields. A record is::

    {"seq": N, "ts": "<UTC ISO 8601>", "event": {...},
     "prev_hash": "<sha256 of the previous record's canonical JSON>",
     "hash": "<sha256 of this record's canonical body (seq, ts, event, prev_hash)>",
     "sig": "<Ed25519 signature over hash, hex>"}

Canonical JSON is ``json.dumps(..., sort_keys=True, separators=(",", ":"))``.
The first record anchors with ``prev_hash`` of 64 zeros. The signing key is
per install (``$COPSE_HOME/audit/signing.key``, 0600, created on first use);
``copse audit pubkey`` prints its public half. A head file next to the log
remembers the last seq and hash, so removing records from the end is caught
too. Appends hold an exclusive ``flock``.

``verify`` recomputes the whole chain and checks every signature against
this install's key, reporting the first seq that's wrong: a record edited
in place (hash or signature), one removed, inserted or reordered (seq and
``prev_hash``), or a truncated tail (the head file). ``export`` writes the
records as JSONL or CSV, optionally from a point in time.

Like every events plugin, ``emit`` never raises into copse.
"""

from __future__ import annotations

import csv
import fcntl
import hashlib
import io
import json
import logging
import os
import re
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from copse.events import Event, EventsPlugin

log = logging.getLogger(__name__)

FEATURE = "audit"
ZERO_HASH = "0" * 64
KEY_FILE = "signing.key"
EVENT_KEYS = ("kind", "agent", "branch", "profile", "provider", "model", "actor", "workspace",
              "repo_root", "at", "approved", "merged", "reason")
RECORD_KEYS = ("seq", "ts", "event", "prev_hash", "hash", "sig")
CSV_COLUMNS = ("seq", "ts", *EVENT_KEYS, "prev_hash", "hash", "sig")
TAIL_BYTES = 64 * 1024
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


class AuditError(Exception):
    """A problem with the audit files themselves (never with an event)."""


# -- canonical form -------------------------------------------------------------------------


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def body_hash(seq: int, ts: str, event: dict, prev_hash: str) -> str:
    return sha256(canonical({"seq": seq, "ts": ts, "event": event, "prev_hash": prev_hash}))


def record_hash(record: dict) -> str:
    """What the next record's ``prev_hash`` must be."""
    return sha256(canonical(record))


def _iso(t: float | None = None) -> str:
    dt = datetime.fromtimestamp(time.time() if t is None else t, tz=timezone.utc)
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_time(text: str) -> datetime:
    """An ISO 8601 timestamp as an aware datetime (a naive one is local time)."""
    dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    return dt.astimezone() if dt.tzinfo is None else dt


def event_record(ev: Event) -> dict:
    """The local event as it is written (always all of ``EVENT_KEYS``)."""
    try:
        at = float(ev.at)
    except (TypeError, ValueError):
        at = time.time()
    return {
        "kind": ev.kind, "agent": ev.agent_id, "branch": ev.branch, "profile": ev.profile,
        "provider": ev.provider, "model": ev.model, "actor": ev.actor,
        "workspace": ev.workspace_id, "repo_root": ev.repo_root, "at": at,
        "approved": ev.approved, "merged": ev.merged, "reason": ev.reason,
    }


# -- files ------------------------------------------------------------------------------------


def audit_dir(home: Path | None = None) -> Path:
    """``$COPSE_HOME/audit``, created 0700 (and tightened to it)."""
    if home is None:
        from copse.config import copse_home

        home = copse_home()
    d = home / "audit"
    old = os.umask(0o077)
    try:
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    finally:
        os.umask(old)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode):
        raise AuditError(f"{d} is not a directory")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(d, 0o700)
    return d


def repo_key(repo_root: str) -> str:
    """``<basename>-<16 hex of sha256(path)>``: readable, and unique per path."""
    path = os.path.abspath(repo_root)
    base = _SAFE.sub("_", os.path.basename(path.rstrip("/")) or "repo").strip("_")[:40] or "repo"
    return f"{base}-{sha256(path)[:16]}"


def log_path(repo_root: str, home: Path | None = None) -> Path:
    return audit_dir(home) / f"{repo_key(repo_root)}.jsonl"


def _head_path(path: Path) -> Path:
    return path.with_suffix(".head")


def _lock_path(path: Path) -> Path:
    return path.with_suffix(".lock")


def _open_private(path: Path, flags: int) -> int:
    old = os.umask(0o077)
    try:
        fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
    finally:
        os.umask(old)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise AuditError(f"{path.name} is not a regular file")
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            raise AuditError(f"{path.name} is not owned by the current user")
        if stat.S_IMODE(st.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextmanager
def _locked(path: Path):
    fd = _open_private(_lock_path(path), os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = _open_private(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
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


# -- the signing key ----------------------------------------------------------------------------


def key_path(home: Path | None = None) -> Path:
    return audit_dir(home) / KEY_FILE


def signing_key(home: Path | None = None, create: bool = True) -> Ed25519PrivateKey | None:
    """This install's key (32 raw bytes, hex, in a 0600 file), created on
    first use unless ``create`` is False (then None when there is none)."""
    path = key_path(home)
    while True:
        try:
            fd = _open_private(path, os.O_RDONLY)
        except FileNotFoundError:
            if not create:
                return None
            fresh = Ed25519PrivateKey.generate()
            try:
                fd = _open_private(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            except FileExistsError:
                continue                     # another process won the race; read theirs
            with os.fdopen(fd, "w", encoding="ascii") as fh:
                fh.write(fresh.private_bytes_raw().hex() + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            return fresh
        with os.fdopen(fd, "r", encoding="ascii", errors="replace") as fh:
            raw = fh.read(256).strip()
        try:
            return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(raw))
        except ValueError as e:
            raise AuditError(f"{path} does not hold an Ed25519 key") from e


def public_key(home: Path | None = None, create: bool = True) -> Ed25519PublicKey | None:
    key = signing_key(home, create)
    return key.public_key() if key else None


def public_key_hex(home: Path | None = None, create: bool = True) -> str | None:
    pub = public_key(home, create)
    return pub.public_bytes_raw().hex() if pub else None


def sign(key: Ed25519PrivateKey, digest: str) -> str:
    return key.sign(digest.encode("ascii")).hex()


def signature_ok(pub: Ed25519PublicKey, digest: str, sig: str) -> bool:
    try:
        pub.verify(bytes.fromhex(sig), digest.encode("ascii"))
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


# -- reading --------------------------------------------------------------------------------------


def _parse(line: bytes) -> dict | None:
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def _last_record(path: Path) -> dict | None:
    """The last complete record of ``path`` (None for no or an empty log)."""
    try:
        fd = _open_private(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        start = max(0, size - TAIL_BYTES)
        fh.seek(start)
        chunk = fh.read()
        if start and b"\n" not in chunk[:-1]:      # one line longer than the tail: read it all
            fh.seek(0)
            chunk = fh.read()
        elif start:
            chunk = chunk[chunk.index(b"\n") + 1:]
    lines = [ln for ln in chunk.split(b"\n") if ln.strip()]
    if not lines:
        return None
    rec = _parse(lines[-1])
    if rec is None or not isinstance(rec.get("seq"), int):
        raise AuditError("the last audit record is unreadable; run `copse audit verify`")
    return rec


def read_lines(path: Path) -> list[bytes]:
    """The log's lines, in order (an empty list without a log)."""
    try:
        fd = _open_private(path, os.O_RDONLY)
    except FileNotFoundError:
        return []
    with os.fdopen(fd, "rb") as fh:
        raw = fh.read()
    return [ln for ln in raw.split(b"\n") if ln.strip()]


def read_records(path: Path) -> list[dict]:
    """Every readable record of the log, unverified."""
    return [r for r in (_parse(ln) for ln in read_lines(path)) if r is not None]


def _read_head(path: Path) -> dict | None:
    try:
        fd = _open_private(_head_path(path), os.O_RDONLY)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as fh:
        head = _parse(fh.read(4096))
    return head if head and isinstance(head.get("seq"), int) else None


# -- appending --------------------------------------------------------------------------------


def append(repo_root: str, event: dict, *, home: Path | None = None,
           key: Ed25519PrivateKey | None = None, ts: str | None = None) -> dict:
    """Append ``event`` to the repo's chain and return the record written."""
    path = log_path(repo_root, home)
    key = key or signing_key(home)
    with _locked(path):
        last = _last_record(path)
        seq = last["seq"] + 1 if last else 1
        prev = record_hash(last) if last else ZERO_HASH
        ts = ts or _iso()
        digest = body_hash(seq, ts, event, prev)
        rec = {"seq": seq, "ts": ts, "event": event, "prev_hash": prev, "hash": digest,
               "sig": sign(key, digest)}
        line = (canonical(rec) + "\n").encode("utf-8")
        fd = _open_private(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)
        _write_atomic(_head_path(path), (canonical({"seq": seq, "hash": record_hash(rec)}) + "\n").encode())
    return rec


# -- verifying ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Report:
    path: Path
    records: int
    broken_seq: int | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.broken_seq is None

    @property
    def exists(self) -> bool:
        return self.records > 0 or self.path.exists()

    def describe(self) -> str:
        if not self.exists:
            return f"no audit log at {self.path}"
        if self.ok:
            return f"{self.path}: {self.records} record(s), chain intact, every signature valid"
        return f"{self.path}: BROKEN at seq {self.broken_seq}: {self.reason}"


def verify(repo_root: str | None = None, *, path: Path | None = None, home: Path | None = None,
           pub: Ed25519PublicKey | None = None) -> Report:
    """Recompute the chain of the repo's log (or ``path``) and check every
    signature against this install's key (or ``pub``)."""
    if path is None:
        if repo_root is None:
            raise AuditError("verify needs a repo or a path")
        path = log_path(repo_root, home)
    lines = read_lines(path)
    if pub is None:
        pub = public_key(home, create=False)
    if lines and pub is None:
        return Report(path, 0, 1, "no signing key in this install, so nothing can be verified")
    prev = ZERO_HASH
    count = 0
    for i, line in enumerate(lines):
        expected = i + 1
        rec = _parse(line)
        if rec is None or any(k not in rec for k in RECORD_KEYS) or not isinstance(rec["event"], dict):
            return Report(path, count, expected, "record is unreadable or missing fields")
        if rec["seq"] != expected:
            return Report(path, count, expected,
                          f"found seq {rec['seq']!r} where {expected} was expected "
                          "(a record was removed, inserted or reordered)")
        if rec["prev_hash"] != prev:
            return Report(path, count, expected,
                          "prev_hash does not match the previous record (the chain is broken)")
        if body_hash(rec["seq"], rec["ts"], rec["event"], rec["prev_hash"]) != rec["hash"]:
            return Report(path, count, expected, "record was altered (hash mismatch)")
        if not isinstance(rec["sig"], str) or not signature_ok(pub, rec["hash"], rec["sig"]):
            return Report(path, count, expected, "signature is invalid")
        prev = record_hash(rec)
        count = expected
    head = _read_head(path)
    if head and head["seq"] > count:
        return Report(path, count, count + 1,
                      f"records after seq {count} are missing (the log was truncated; "
                      f"the last one written was seq {head['seq']})")
    if head and head["seq"] == count and count and head.get("hash") != prev:
        return Report(path, count, count, "the last record does not match the recorded head")
    return Report(path, count)


# -- exporting -----------------------------------------------------------------------------------


def export(repo_root: str | None = None, *, since: datetime | None = None, fmt: str = "jsonl",
           path: Path | None = None, home: Path | None = None) -> str:
    """The repo's records (from ``since`` on) as ``jsonl`` or ``csv`` text."""
    if fmt not in ("jsonl", "csv"):
        raise AuditError(f"unknown export format {fmt!r} (jsonl or csv)")
    if path is None:
        if repo_root is None:
            raise AuditError("export needs a repo or a path")
        path = log_path(repo_root, home)
    records = read_records(path)
    if since is not None:
        if since.tzinfo is None:
            since = since.astimezone()
        kept = []
        for r in records:
            try:
                if parse_time(str(r.get("ts"))) >= since:
                    kept.append(r)
            except ValueError:
                continue
        records = kept
    if fmt == "jsonl":
        return "".join(canonical(r) + "\n" for r in records)
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for r in records:
        ev = r.get("event") if isinstance(r.get("event"), dict) else {}
        row = [r.get("seq"), r.get("ts")] + [ev.get(k) for k in EVENT_KEYS] \
            + [r.get("prev_hash"), r.get("hash"), r.get("sig")]
        w.writerow(["" if v is None else v for v in row])
    return out.getvalue()


# -- the plugin ------------------------------------------------------------------------------------


class AuditChain(EventsPlugin):
    """Append every event to the repo's chain, when entitled."""

    def __init__(self, repo_root: str, *, home: Path | None = None, entitled=None) -> None:
        self.repo_root = repo_root
        self.home = home
        self._entitled = entitled
        self.dropped = 0

    def entitled(self) -> bool:
        if self._entitled is not None:
            return bool(self._entitled())
        from copse.pro import license

        return license.has(FEATURE)

    def emit(self, event: Event) -> None:
        try:
            if not self.entitled():
                return
            append(event.repo_root or self.repo_root, event_record(event), home=self.home)
        except Exception:  # noqa: BLE001 - never fail the operation being reported
            self.dropped += 1
            log.warning("copse: couldn't append to the audit chain", exc_info=True)


def make(repo_root: str) -> AuditChain:
    return AuditChain(repo_root)


__all__ = ["CSV_COLUMNS", "EVENT_KEYS", "FEATURE", "RECORD_KEYS", "ZERO_HASH", "AuditChain",
           "AuditError", "Report", "append", "audit_dir", "canonical", "event_record", "export",
           "key_path", "log_path", "make", "parse_time", "public_key", "public_key_hex",
           "read_records", "repo_key", "signing_key", "verify"]
