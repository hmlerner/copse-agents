"""The ``cloud`` learning plugin: hosted learning for copse Pro.

``"learning": "cloud"`` in a repo's ``.copse/config.json`` selects it, and
the default ``"auto"`` selects it when the verified entitlement includes the
``learning`` feature (see ``copse.plugins``). It answers ``suggest`` from the
hosted learner and, whenever that can't be used -- no ``learning`` feature in
the verified entitlement, offline, rate-limited, slow (more than
``SUGGEST_TIMEOUT``), or any error at all -- from a local learner when one is
installed as the ``local`` entry point in ``copse.learning`` (copse-pro),
else with no suggestion (None, so copse picks as it would without a plugin).
Outcomes are always handed to that local learner too. It never raises into
copse.

What leaves the machine is fixed by :func:`record_payload` and
:func:`suggest_payload`: kind/size one-hots from :mod:`copse.pro.features`,
the declared weight, profile names and their relative cost, outcome counters,
an opaque ``repo_key`` and ``agent_ref``. Never task text, file paths, repo
paths, branch names, provider or model.

``repo_key`` and ``agent_ref`` are HMACs under the active org's learning key
over the repo's identity (root commit, else normalized origin URL) and the
agent id; see :mod:`copse.pro.orgkey`. Without an org key or a repo
identity, nothing is sent.

Records are sent from a small bounded queue by one background thread, so
copse never waits on the network to record; when the queue is full, remote
records are dropped (the local learner, if any, still has them).
"""

from __future__ import annotations

import atexit
import logging
import queue
import re
import threading
import time
from typing import Callable

from copse import plugins
from copse.learning import LearningPlugin, Outcome, TaskInfo
from copse.pro.features import KIND_WORDS, SIZES, featurize
from copse.pro.orgkey import OrgKey, OrgKeys, repo_identity

log = logging.getLogger(__name__)

FEATURE = "learning"
LOCAL_PLUGIN = "local"         # the copse.learning entry point used as the fallback, if installed
SUGGEST_TIMEOUT = 2.0
RECORD_TIMEOUT = 5.0
QUEUE_SIZE = 256
FLUSH_AT_EXIT = 2.0
FORBIDDEN_BACKOFF = 600.0      # after a 403, don't ask again for this long
ERROR_BACKOFF = 60.0           # after a transport error or a 429
DAILY_LIMIT_BACKOFF = 3600.0   # after the repo's daily event cap
WEIGHTS = ("light", "medium", "heavy")
EVENTS = ("review", "escalated", "merged", "removed_unmerged")
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MAX_CANDIDATES = 16

RECORD_KEYS = frozenset({"org_id", "key_id", "repo_key", "agent_ref", "profile", "weight", "features", "event",
                         "approved", "checks_passed", "review_rounds", "escalations", "tokens",
                         "wall_seconds"})
SUGGEST_KEYS = frozenset({"org_id", "key_id", "repo_key", "weight", "features", "candidates", "candidate_cost"})


# -- keys -----------------------------------------------------------------------------------------


def install_secret(store=None) -> bytes | None:
    """Deprecated: the pre-org-key per-install secret, read-only (never
    created, never used for remote calls). Kept for migration tooling."""
    from copse.pro.orgkey import legacy_install_secret

    return legacy_install_secret(store)


# -- cost ------------------------------------------------------------------------------------------


def cost_rank(name: str, repo_root: str) -> int:
    """0 (free/local) to 3 (frontier), guessed from the profile's model: the
    relative cost sent alongside the candidates so close calls can go to the
    cheaper profile."""
    from copse.profiles import load_profile

    try:
        p = load_profile(name, repo_root)
    except Exception:  # noqa: BLE001 - an unknown profile is an average one
        return 2
    model = (p.model or "").lower()
    if p.base_url and any(h in p.base_url for h in ("localhost", "127.0.0.1")):
        return 0
    if any(s in model for s in ("haiku", "mini", "flash", "small")):
        return 1
    if any(s in model for s in ("opus", "fable", "gpt-5", "pro")):
        return 3
    return 2


# -- payloads ---------------------------------------------------------------------------------------


def one_hot(task: TaskInfo) -> dict[str, bool]:
    f = featurize(task.task, task.files)
    return {**{f"kind_{k}": f.kind == k for k in KIND_WORDS},
            **{f"size_{s}": f.size == s for s in SIZES}}


def _weight(task: TaskInfo) -> str | None:
    return task.weight if task.weight in WEIGHTS else None


def _count(v, hi: int) -> int:
    try:
        return max(0, min(int(v), hi))
    except (TypeError, ValueError):
        return 0


def record_payload(key: OrgKey, identity: str, task: TaskInfo, outcome: Outcome,
                   review_rounds: int = 0, escalations: int = 0) -> dict | None:
    """The exact body of ``POST /learning/record``, or None if it can't be
    sent without leaking (e.g. a profile name that isn't a plain identifier)."""
    if not task.profile or not PROFILE_RE.match(task.profile) or outcome.event not in EVENTS:
        return None
    body = {
        "org_id": key.org_id,
        "key_id": key.key_id,
        "repo_key": key.repo_key(identity),
        "profile": task.profile,
        "weight": _weight(task),
        "features": one_hot(task),
        "event": outcome.event,
        "checks_passed": bool(outcome.checks_passed),
        "review_rounds": _count(review_rounds, 50),
        "escalations": _count(escalations, 50),
        "tokens": _count(outcome.tokens, 1_000_000_000),
        "wall_seconds": float(max(0.0, min(float(outcome.wall_seconds or 0), 7 * 86400))),
    }
    if task.agent_id:
        body["agent_ref"] = key.agent_ref(task.agent_id)
    if outcome.event == "review" and isinstance(outcome.approved, bool):
        body["approved"] = outcome.approved
    return body


def suggest_payload(key: OrgKey, identity: str, task: TaskInfo, candidates: list[str],
                    cost=None) -> dict | None:
    """The exact body of ``POST /learning/suggest``, or None to send nothing."""
    if not candidates or len(candidates) > MAX_CANDIDATES or len(set(candidates)) != len(candidates):
        return None
    if not all(isinstance(c, str) and PROFILE_RE.match(c) for c in candidates):
        return None
    body = {"org_id": key.org_id, "key_id": key.key_id,
            "repo_key": key.repo_key(identity), "weight": _weight(task),
            "features": one_hot(task), "candidates": list(candidates)}
    if cost is not None:
        costs = {}
        for c in candidates:
            try:
                costs[c] = _count(cost(c), 16)
            except Exception:  # noqa: BLE001
                return body
        body["candidate_cost"] = costs
    return body


# -- the plugin ---------------------------------------------------------------------------------------


class _Unavailable(Exception):
    pass


def local_plugin(repo_root: str) -> LearningPlugin | None:
    """The ``local`` learning plugin, if one is installed (copse-pro)."""
    if LOCAL_PLUGIN not in plugins.installed(plugins.LEARNING):
        return None
    p = plugins.load(plugins.LEARNING, LOCAL_PLUGIN, repo_root)
    return p if isinstance(p, LearningPlugin) else None


class CloudLearner(LearningPlugin):
    def __init__(self, repo_root: str, local: LearningPlugin | None = None, *, client=None,
                 store=None, key_store=None, org=None, cost: Callable[[str], int] | None = None,
                 start_thread: bool = True) -> None:
        self.repo_root = repo_root
        self.local = local if local is not None else local_plugin(repo_root)
        self.cost = cost or (lambda name: cost_rank(name, repo_root))
        self._client, self._store = client, store
        self.keys = OrgKeys(store=store, client=client, key_store=key_store)
        self._org_override = org
        self._identity: str | None = None
        self._backoff_until = 0.0
        self._lock = threading.Lock()
        self._counts: dict[str, list[int]] = {}      # agent id -> [review rounds, escalations]
        self.queue: queue.Queue = queue.Queue(maxsize=QUEUE_SIZE)
        self.dropped = 0
        self._thread = None
        if start_thread:
            self._thread = threading.Thread(target=self._sender, name="copse-pro-learning",
                                            daemon=True)
            self._thread.start()
            atexit.register(self.flush, FLUSH_AT_EXIT)

    # -- plumbing ----------------------------------------------------------------------

    def _deps(self):
        from copse.pro import auth, credentials

        if self._store is None:
            self._store = credentials.default_store()
        if self._client is None:
            creds = self._store.load() or {}
            self._client = auth.Client(creds.get("base_url"),
                                       auth.UrllibTransport(timeout=RECORD_TIMEOUT))
        return self._client, self._store

    def identity(self) -> str | None:
        if self._identity is None:
            self._identity = repo_identity(self.repo_root)
        return self._identity

    def org(self) -> str | None:
        """The active org when the verified entitlement includes hosted
        learning, else None (always None in air-gap mode, so every answer
        comes from the local learner). Offline verification only."""
        from copse import airgap

        if airgap.enabled() or time.time() < self._backoff_until:
            return None
        try:
            if self._org_override is not None:
                return self._org_override()
            from copse.pro import license

            ent = license.current(refresh=False, store=self._store)
            return ent.org_id if FEATURE in ent.features and not ent.in_grace else None
        except Exception:  # noqa: BLE001 - fail closed: send nothing
            return None

    def active(self) -> bool:
        return self.org() is not None

    def key(self, org: str) -> OrgKey:
        return self.keys.get(org)

    def _post(self, org: str, path: str, body: dict) -> dict:
        from copse.pro import auth

        client, store = self._deps()
        try:
            status, resp = auth.authed(client, store, "POST", path, auth.JSONBody(body))
        except auth.TransportError:
            self._backoff_until = time.time() + ERROR_BACKOFF
            raise _Unavailable("offline")
        if isinstance(resp, dict) and "key_id" in resp:
            self.keys.note_key_id(org, resp.get("key_id"))
        if status == 200:
            return resp
        if status == 409 and resp.get("error") in ("stale_key", "key_rotated"):
            self.keys.note_key_id(org, resp.get("key_id") or "")
        elif status == 403:
            self._backoff_until = time.time() + FORBIDDEN_BACKOFF
        elif status == 429:
            daily = resp.get("error") == "daily_limit"
            self._backoff_until = time.time() + (DAILY_LIMIT_BACKOFF if daily else ERROR_BACKOFF)
        raise _Unavailable(f"HTTP {status}")

    # -- recording -------------------------------------------------------------------------

    def _rounds(self, task: TaskInfo, outcome: Outcome) -> tuple[int, int]:
        """(review rounds, escalations) so far for ``task``'s agent, this
        outcome included: from the local learner's ledger when it keeps one,
        else counted here for the life of this process."""
        if not task.agent_id:
            return 0, 0
        counts = self._counts.setdefault(task.agent_id, [0, 0])
        if outcome.event == "review":
            counts[0] += 1
        elif outcome.event == "escalated":
            counts[1] += 1
        store = getattr(self.local, "store", None)
        try:
            row = store.get(task.agent_id) if store is not None else None
            if row is not None:
                return int(row.review_rounds), int(row.escalations)
        except Exception:  # noqa: BLE001 - the ledger is the local learner's business
            pass
        return counts[0], counts[1]

    def record(self, task: TaskInfo, outcome: Outcome) -> None:
        if self.local is not None:
            try:
                self.local.record(task, outcome)
            except Exception:  # noqa: BLE001
                log.warning("local learning record failed", exc_info=True)
        try:
            rounds, escalations = self._rounds(task, outcome)
            if not self.active():
                return
            item = (task, outcome, rounds, escalations)
            try:
                self.queue.put_nowait(item)      # the payload is built by the sender
            except queue.Full:
                self.dropped += 1
        except Exception:  # noqa: BLE001
            log.info("hosted learning record skipped", exc_info=True)

    def _send_one(self, item) -> None:
        try:
            org = self.org()
            identity = self.identity() if org else None
            if org is None or identity is None:
                return
            task, outcome, rounds, escalations = item
            body = record_payload(self.key(org), identity, task, outcome, rounds, escalations)
            if body is not None:
                self._post(org, "/learning/record", body)
        except Exception as e:  # noqa: BLE001
            log.info("hosted learning record not sent (%s)", type(e).__name__)

    def _sender(self) -> None:
        while True:
            body = self.queue.get()
            try:
                self._send_one(body)
            finally:
                self.queue.task_done()

    def flush(self, timeout: float = FLUSH_AT_EXIT) -> bool:
        """Wait up to ``timeout`` for queued records to be sent (or, with no
        sender thread, send them inline). True when the queue drained."""
        if self._thread is None:
            while True:
                try:
                    body = self.queue.get_nowait()
                except queue.Empty:
                    return True
                self._send_one(body)
                self.queue.task_done()
        deadline = time.monotonic() + timeout
        while self.queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return not self.queue.unfinished_tasks

    # -- suggesting ---------------------------------------------------------------------------

    def _remote_pick(self, task: TaskInfo, candidates: list[str]) -> str | None:
        org = self.org()
        identity = self.identity() if org else None
        if org is None or identity is None:
            return None
        body = suggest_payload(self.key(org), identity, task, candidates, self.cost)
        if body is None:
            return None
        resp = self._post(org, "/learning/suggest", body)
        pick = resp.get("profile")
        return pick if pick in candidates else None

    def suggest(self, task: TaskInfo, candidates: list[str]) -> str | None:
        pick = None
        try:
            if candidates and self.active():
                result: list = []
                th = threading.Thread(target=lambda: result.append(self._safe_remote(task, candidates)),
                                      daemon=True)
                th.start()
                th.join(SUGGEST_TIMEOUT)
                pick = result[0] if result else None
        except Exception:  # noqa: BLE001
            pick = None
        if pick is not None:
            return pick
        if self.local is None:
            return None
        try:
            return self.local.suggest(task, candidates)
        except Exception:  # noqa: BLE001
            log.warning("local learning suggest failed", exc_info=True)
            return None

    def _safe_remote(self, task, candidates):
        try:
            return self._remote_pick(task, candidates)
        except Exception as e:  # noqa: BLE001
            log.info("hosted suggest unavailable (%s)", type(e).__name__)
            return None

    # -- reporting ------------------------------------------------------------------------------

    def report(self, reset: bool = False) -> str:
        if self.local is None:
            local = "no local learner installed (copse-pro adds one); nothing is learned offline"
        else:
            try:
                local = self.local.report(reset=reset)
            except Exception as e:  # noqa: BLE001
                local = f"local learner unavailable ({type(e).__name__})"
        state = ("active" if self.active() else
                 "inactive (not logged in, offline, or your plan lacks hosted learning)")
        return f"cloud learning: {state}\n{local}"


def make(repo_root: str) -> CloudLearner:
    return CloudLearner(repo_root)
