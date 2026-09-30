"""The ``pro`` events plugin (``copse.events`` group): feed a team org's audit
log with what copse does.

Only for team orgs: without a verified entitlement carrying the ``team``
feature, events are dropped on the spot and nothing is stored or sent.

Each :class:`copse.events.Event` becomes :func:`event_payload` -- kind,
HMAC refs (``copse-ref-v1``, under the org's learning key; see
:mod:`copse.pro.orgkey`) for the agent, branch and actor (the literal actor
``"user"`` stays ``"user"``; a branch ref needs a repo identity),
profile/provider/model when they are plain identifiers, the timestamp and
the review/merge flags. Never a raw branch name, agent id, repo path or task.

Delivery: ``emit`` puts the payload on a small bounded queue; a background
thread moves it into a durable spool (``$COPSE_HOME/pro/events-spool.jsonl``,
0600, capped at ``MAX_SPOOL_EVENTS`` / ``MAX_SPOOL_BYTES``, oldest dropped
first) and sends the spool in batches of at most 100 to
``POST /orgs/{org_id}/events``, backing off exponentially on failure. The
spool survives offline periods and restarts; if the queue is full, ``emit``
writes to the spool itself. Refs are computed before anything touches the
spool, so without an org key (offline on first use, 403) events are dropped,
never stored raw and never keyed with anything else. Only one process at a time sends (a
non-blocking ``flock``), so concurrent copse processes don't double-send.
"""

from __future__ import annotations

import atexit
import fcntl
import json
import logging
import os
import queue
import random
import re
import threading
import time
from contextlib import contextmanager

from copse.events import Event, EventsPlugin

from copse.pro.orgkey import OrgKey, OrgKeys, repo_identity
from copse.pro._files import private_dir, read_private, write_private

log = logging.getLogger(__name__)

FEATURE = "team"
KINDS = ("assign", "handoff", "review", "escalated", "merge", "remove")
PAYLOAD_KEYS = ("kind", "agent_ref", "branch_ref", "profile", "provider", "model", "actor_ref",
                "at", "approved", "merged")
BATCH = 100
BATCH_BYTES = 48 * 1024          # backend caps the body at 64 KiB
QUEUE_SIZE = 64
MAX_SPOOL_EVENTS = 5000
MAX_SPOOL_BYTES = 2 * 1024 * 1024
MAX_BACKOFF = 300.0
SEND_TIMEOUT = 10.0
FLUSH_AT_EXIT = 2.0
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}(/[A-Za-z0-9][A-Za-z0-9._:-]{0,63})?$")
ORG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# -- payload ---------------------------------------------------------------------------------------


def agent_ref(key: OrgKey, agent_id: str) -> str:
    return key.ref("agent\0" + agent_id)


def branch_ref(key: OrgKey, identity: str, branch: str) -> str:
    return key.ref("branch\0" + identity + "\0" + branch)


def _ident(v, rx: re.Pattern) -> str | None:
    return v if isinstance(v, str) and rx.match(v) else None


def _flag(v) -> bool | None:
    return v if isinstance(v, bool) else None


def event_payload(key: OrgKey, identity: str | None, ev: Event) -> dict | None:
    """The exact wire form of ``ev`` (always all of ``PAYLOAD_KEYS``), or
    None for an unknown kind. ``identity`` is the repo identity (None: no
    branch ref)."""
    if ev.kind not in KINDS:
        return None
    actor = ev.actor
    try:
        at = float(ev.at)
    except (TypeError, ValueError):
        at = time.time()
    if not 1_000_000_000 <= at <= 10_000_000_000:
        at = time.time()
    return {
        "kind": ev.kind,
        "agent_ref": agent_ref(key, ev.agent_id) if ev.agent_id else None,
        "branch_ref": branch_ref(key, identity, ev.branch) if ev.branch and identity else None,
        "profile": _ident(ev.profile, PROFILE_RE),
        "provider": _ident(ev.provider, PROVIDER_RE),
        "model": _ident(ev.model, MODEL_RE),
        "actor_ref": ("user" if actor == "user" else agent_ref(key, actor) if actor else None),
        "at": at,
        "approved": _flag(ev.approved),
        "merged": _flag(ev.merged),
    }


# -- the spool -----------------------------------------------------------------------------------------


class Spool:
    """A capped JSONL file of ``{"org_id", "event"}`` entries, oldest first.
    Every read-modify-write holds an exclusive ``flock``."""

    def __init__(self, directory=None, max_events: int | None = None,
                 max_bytes: int | None = None) -> None:
        self.dir = directory
        self.max_events = max_events or MAX_SPOOL_EVENTS
        self.max_bytes = max_bytes or MAX_SPOOL_BYTES
        self.dropped = 0

    def _paths(self):
        d = self.dir or private_dir()
        return d / "events-spool.jsonl", d / "events-spool.lock"

    @contextmanager
    def _locked(self, path_lock, blocking: bool = True):
        old = os.umask(0o077)
        try:
            fd = os.open(path_lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        finally:
            os.umask(old)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _read(self, path) -> list[dict]:
        raw = read_private(path, self.max_bytes * 2) or b""
        out = []
        for line in raw.splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and isinstance(e.get("org_id"), str) \
                    and isinstance(e.get("event"), dict):
                out.append(e)
        return out

    def _write(self, path, entries: list[dict]) -> None:
        lines = [json.dumps(e, separators=(",", ":")) for e in entries]
        while lines and (len(lines) > self.max_events
                         or sum(len(x) + 1 for x in lines) > self.max_bytes):
            lines.pop(0)
            self.dropped += 1
        write_private(path, ("\n".join(lines) + "\n" if lines else "").encode())
        if self.dropped:
            log.warning("copse Pro event spool full; %d oldest event(s) dropped", self.dropped)

    def append(self, org_id: str, events: list[dict]) -> None:
        path, lock = self._paths()
        with self._locked(lock):
            self._write(path, self._read(path) + [{"org_id": org_id, "event": e} for e in events])

    def entries(self) -> list[dict]:
        path, lock = self._paths()
        with self._locked(lock):
            return self._read(path)

    def head(self) -> tuple[str, list[dict]] | None:
        """The oldest batch: up to ``BATCH`` consecutive events of one org
        within ``BATCH_BYTES``."""
        entries = self.entries()
        if not entries:
            return None
        org, batch, size = entries[0]["org_id"], [], 0
        for e in entries:
            n = len(json.dumps(e["event"]))
            if e["org_id"] != org or len(batch) >= BATCH or (batch and size + n > BATCH_BYTES):
                break
            batch.append(e["event"])
            size += n
        return org, batch

    def pop(self, org_id: str, count: int) -> None:
        """Remove the first ``count`` entries if they are still ``org_id``'s."""
        path, lock = self._paths()
        with self._locked(lock):
            entries = self._read(path)
            k = 0
            while k < min(count, len(entries)) and entries[k]["org_id"] == org_id:
                k += 1
            self._write(path, entries[k:])

    @contextmanager
    def sender(self):
        """Yields True for the one process allowed to send right now."""
        d = self.dir or private_dir()
        old = os.umask(0o077)
        try:
            fd = os.open(d / "events-sender.lock",
                         os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        finally:
            os.umask(old)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# -- the plugin ------------------------------------------------------------------------------------------


class ProEvents(EventsPlugin):
    def __init__(self, repo_root: str, *, store=None, client=None, key_store=None,
                 spool: Spool | None = None, entitlement=None, start_thread: bool = True) -> None:
        self.repo_root = repo_root
        self._store, self._client = store, client
        self.keys = OrgKeys(store=store, client=client, key_store=key_store)
        self._entitlement = entitlement
        self._identities: dict[str, str | None] = {}
        self.dropped_no_key = 0
        self.spool = spool or Spool()
        self.queue: queue.Queue = queue.Queue(maxsize=QUEUE_SIZE)
        self._wake = threading.Event()
        self._delay = 0.0
        self._next_try = 0.0
        self._thread = None
        self._start_thread = start_thread

    def _ensure_sender(self) -> None:
        """Start the sender thread on the first event of a team org, so a
        copse without a team entitlement never runs one."""
        if self._thread is not None or not self._start_thread:
            return
        self._thread = threading.Thread(target=self._run, name="copse-pro-events", daemon=True)
        self._thread.start()
        self._wake.set()          # send whatever an earlier run left in the spool
        atexit.register(self.flush, FLUSH_AT_EXIT)

    # -- emitting --------------------------------------------------------------------------

    def team_org(self) -> str | None:
        """The org to report to, or None without a team entitlement (offline
        check only, so an offline grace period still spools)."""
        from copse.pro import license

        try:
            ent = (self._entitlement() if self._entitlement
                   else license.current(refresh=False, store=self._store))
        except Exception:  # noqa: BLE001
            return None
        return ent.org_id if FEATURE in ent.features and ORG_RE.match(ent.org_id) else None

    def emit(self, event: Event) -> None:
        try:
            org = self.team_org()
            if org is None or event.kind not in KINDS:
                return
            self._ensure_sender()
            try:
                self.queue.put_nowait((org, event))    # refs are computed by the sender
            except queue.Full:
                key = self.keys.cached(org)            # never lose an audit event to a full queue
                if key is None:
                    self.dropped_no_key += 1
                else:
                    self.spool.append(org, [event_payload(key, self._identity(event.repo_root), event)])
            self._wake.set()
        except Exception:  # noqa: BLE001 - never fail the operation being reported
            log.warning("copse Pro: couldn't record an audit event", exc_info=True)

    # -- sending -------------------------------------------------------------------------------

    def _identity(self, repo_root: str) -> str | None:
        if self._identities.get(repo_root) is None:
            self._identities[repo_root] = repo_identity(repo_root)
        return self._identities[repo_root]

    def _drain_queue(self) -> None:
        pending: dict[str, list[dict]] = {}
        order: list[str] = []
        while True:
            try:
                org, event = self.queue.get_nowait()
            except queue.Empty:
                break
            try:
                key = self.keys.get(org)
                body = event_payload(key, self._identity(event.repo_root), event)
            except Exception as e:  # noqa: BLE001 - no org key: send nothing
                log.info("copse Pro: no org key (%s); audit event dropped", type(e).__name__)
                self.dropped_no_key += 1
                body = None
            finally:
                self.queue.task_done()
            if body is None:
                continue
            if org not in pending:
                order.append(org)
            pending.setdefault(org, []).append(body)
        for org in order:
            self.spool.append(org, pending[org])

    def _post(self, org: str, batch: list[dict]) -> int:
        from copse.pro import auth, credentials

        if self._store is None:
            self._store = credentials.default_store()
        if self._client is None:
            creds = self._store.load() or {}
            self._client = auth.Client(creds.get("base_url"), auth.UrllibTransport(timeout=SEND_TIMEOUT))
        try:
            status, _ = auth.authed(self._client, self._store, "POST", f"/orgs/{org}/events",
                                    auth.JSONBody({"events": batch}))
        except auth.AuthError:
            return 0
        return status

    def send_pending(self) -> bool:
        """Send spooled batches until the spool is empty (True) or a send
        fails (False, with the backoff advanced)."""
        with self.spool.sender() as mine:
            if not mine:
                return False
            while True:
                head = self.spool.head()
                if head is None:
                    self._delay = 0.0
                    return True
                org, batch = head
                status = self._post(org, batch)
                if status in (200, 201, 202):
                    self.spool.pop(org, len(batch))
                    self._delay = 0.0
                elif status in (400, 413, 422):
                    log.warning("copse Pro: backend rejected %d audit event(s) (HTTP %d); dropped",
                                len(batch), status)
                    self.spool.pop(org, len(batch))
                else:
                    self._delay = min(MAX_BACKOFF, max(1.0, self._delay * 2))
                    self._next_try = time.monotonic() + self._delay * random.uniform(0.8, 1.2)
                    return False

    def _run(self) -> None:
        while True:
            wait = max(0.0, self._next_try - time.monotonic()) if self._delay else None
            self._wake.wait(wait)
            self._wake.clear()
            try:
                self._drain_queue()
                if time.monotonic() >= self._next_try:
                    self.send_pending()
            except Exception:  # noqa: BLE001
                log.warning("copse Pro event sender error", exc_info=True)
                self._delay = min(MAX_BACKOFF, max(1.0, self._delay * 2))
                self._next_try = time.monotonic() + self._delay

    def flush(self, timeout: float = FLUSH_AT_EXIT) -> bool:
        """Move queued events to the spool and try to send them, within
        ``timeout``. The spool keeps whatever isn't sent."""
        try:
            self._drain_queue()
        except Exception:  # noqa: BLE001
            log.warning("copse Pro: couldn't spool audit events", exc_info=True)
            return False
        if self._thread is None:
            return self.send_pending()
        self._next_try = 0.0
        self._wake.set()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if not self.spool.entries():
                    return True
            except Exception:  # noqa: BLE001
                return False
            time.sleep(0.02)
        return False


def make(repo_root: str) -> ProEvents:
    return ProEvents(repo_root)
