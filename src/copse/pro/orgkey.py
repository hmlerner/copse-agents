"""Per-org learning keys and repo identity: how opaque refs are derived.

Every ref copse Pro sends is an HMAC-SHA256 under the *org's* learning key,
served by ``GET /orgs/{org_id}/learning-key`` (Bearer) as
``{org_id, key_id, key (base64, 32 bytes), created_at}``. The org is the
active org in the entitlement (the personal org by default). Members of one
org therefore derive the same refs, and different orgs derive unlinkable
ones. Domain separation::

    repo_key  = HMAC(key, "copse-repo-v1:"  + repo identity)          (64 hex)
    agent_ref = HMAC(key, "copse-agent-v1:" + agent id)[:32]           (learning)
    ref       = HMAC(key, "copse-ref-v1:"   + value)                   (team events)

Keys are cached in the credential store (entry ``learning-keys``) by org_id
and key_id, and refetched when the entitlement's org changes or the server
names a different key_id. Without a key (offline on first use, 403, ...)
callers stay local and send nothing: there is no fallback to any other key.

Repo identity is the repo's root commit (the lexicographically smallest when
there are several), so every clone of a repo, wherever it lives, has the
same identity. A repo with no commits falls back to its normalized ``origin``
URL (host lowercased, ``.git`` stripped, ssh and https forms equal). With
neither there is no identity, and nothing about the repo is sent.

Migration: the per-install secret used before org keys is only ever read
(:func:`legacy_install_secret`), never created or used for remote calls.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import re
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass

log = logging.getLogger(__name__)

KEYS_ACCOUNT = "learning-keys"
KEY_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
ORG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
GIT_TIMEOUT = 5.0
UNAVAILABLE_BACKOFF = 60.0

REPO_PREFIX = b"copse-repo-v1:"
AGENT_PREFIX = b"copse-agent-v1:"
REF_PREFIX = b"copse-ref-v1:"


class OrgKeyUnavailable(Exception):
    """No usable org key: stay local and send nothing."""


@dataclass(frozen=True)
class OrgKey:
    org_id: str
    key_id: str
    key: bytes

    def _mac(self, prefix: bytes, value: str) -> str:
        return hmac.new(self.key, prefix + value.encode("utf-8"), hashlib.sha256).hexdigest()

    def repo_key(self, identity: str) -> str:
        return self._mac(REPO_PREFIX, identity)

    def agent_ref(self, agent_id: str) -> str:
        return self._mac(AGENT_PREFIX, agent_id)[:32]

    def ref(self, value: str) -> str:
        return self._mac(REF_PREFIX, value)


# -- repo identity ----------------------------------------------------------------------------


def _git(repo_root: str, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", repo_root, *args], capture_output=True, text=True,
                           timeout=GIT_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def normalize_remote(url: str) -> str | None:
    """``host/owner/repo`` for an ssh, scp-style or http(s) git URL: host
    lowercased, credentials, port, trailing slashes and ``.git`` dropped."""
    url = (url or "").strip()
    if not url:
        return None
    m = re.match(r"^(?:[^@/]+@)?([^:/]+):(?!//)(.+)$", url)      # scp-like: git@host:owner/repo
    if m and "://" not in url:
        host, path = m.group(1), m.group(2)
    else:
        try:
            u = urllib.parse.urlsplit(url)
        except ValueError:
            return None
        if u.scheme not in ("ssh", "git", "http", "https", "git+ssh", "ssh+git") or not u.hostname:
            return None
        host, path = u.hostname, u.path
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    path = path.strip("/")
    if not path or not host:
        return None
    return f"{host.lower()}/{path}"


def repo_identity(repo_root: str) -> str | None:
    """The root commit SHA (smallest of several), else the normalized origin
    URL, else None."""
    out = _git(repo_root, "rev-list", "--max-parents=0", "HEAD")
    shas = sorted(s for s in (out or "").split() if SHA_RE.match(s))
    if shas:
        return shas[0]
    origin = _git(repo_root, "config", "--get", "remote.origin.url")
    return normalize_remote(origin) if origin else None


# -- org keys ----------------------------------------------------------------------------------


def _decode_key(raw) -> bytes | None:
    if not isinstance(raw, str):
        return None
    padded = raw + "=" * (-len(raw) % 4)
    for decode in (lambda: base64.b64decode(padded, validate=True),
                   lambda: base64.b64decode(padded, altchars=b"-_", validate=True)):
        try:
            key = decode()
        except (binascii.Error, ValueError):
            continue
        return key if len(key) == 32 else None
    return None


class OrgKeys:
    """Fetches and caches org keys. Thread-safe; one instance per plugin."""

    def __init__(self, *, store=None, client=None, key_store=None, now=time.monotonic) -> None:
        self._store, self._client, self._key_store = store, client, key_store
        self._lock = threading.Lock()
        self._unavailable: dict[str, float] = {}
        self._stale: set[str] = set()
        self._now = now

    def _deps(self):
        from copse.pro import auth, credentials

        if self._store is None:
            self._store = credentials.default_store()
        if self._key_store is None:
            self._key_store = credentials.default_store(KEYS_ACCOUNT)
        if self._client is None:
            creds = self._store.load() or {}
            self._client = auth.Client(creds.get("base_url"), auth.UrllibTransport(timeout=5.0))
        return self._client, self._store, self._key_store

    def cached(self, org_id: str) -> OrgKey | None:
        """The cached key for ``org_id`` (no network), unless marked stale."""
        if org_id in self._stale:
            return None
        _, _, ks = self._deps()
        try:
            entry = ((ks.load() or {}).get("keys") or {}).get(org_id)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(entry, dict):
            return None
        key, key_id = _decode_key(entry.get("key")), entry.get("key_id")
        if key is None or not isinstance(key_id, str) or not KEY_ID_RE.match(key_id):
            return None
        return OrgKey(org_id, key_id, key)

    def get(self, org_id: str) -> OrgKey:
        """The org key, from the cache or the backend; raises
        :class:`OrgKeyUnavailable`."""
        if not isinstance(org_id, str) or not ORG_RE.match(org_id):
            raise OrgKeyUnavailable("invalid org id")
        k = self.cached(org_id)
        if k is not None:
            return k
        with self._lock:
            k = self.cached(org_id)
            if k is not None:
                return k
            if self._now() < self._unavailable.get(org_id, 0):
                raise OrgKeyUnavailable("recently unavailable")
            try:
                k = self._fetch(org_id)
            except OrgKeyUnavailable:
                self._unavailable[org_id] = self._now() + UNAVAILABLE_BACKOFF
                raise
            self._stale.discard(org_id)
            return k

    def _fetch(self, org_id: str) -> OrgKey:
        from copse.pro import auth

        client, store, ks = self._deps()
        try:
            status, body = auth.authed(client, store, "GET", f"/orgs/{org_id}/learning-key")
        except auth.AuthError as e:
            raise OrgKeyUnavailable(e.code) from e
        if status != 200:
            raise OrgKeyUnavailable(f"HTTP {status}")
        key, key_id = _decode_key(body.get("key")), body.get("key_id")
        if body.get("org_id") != org_id or key is None or not isinstance(key_id, str) \
                or not KEY_ID_RE.match(key_id):
            raise OrgKeyUnavailable("malformed learning key")
        data = ks.load() or {}
        keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
        keys[org_id] = {"key_id": key_id, "key": base64.b64encode(key).decode("ascii"),
                        "created_at": body.get("created_at")}
        ks.save({**data, "keys": keys})
        return OrgKey(org_id, key_id, key)

    def note_key_id(self, org_id: str, key_id) -> None:
        """The server named ``key_id`` for ``org_id``: if it isn't the cached
        one, drop the cache so the next call refetches."""
        if not isinstance(key_id, str):
            return
        k = self.cached(org_id)
        if k is None or k.key_id != key_id:
            self._stale.add(org_id)
            self._unavailable.pop(org_id, None)


def legacy_install_secret(store=None) -> bytes | None:
    """The pre-org-key per-install secret, if one exists. Read-only (never
    created) and never used for anything sent to the backend."""
    from copse.pro import credentials

    try:
        store = store or credentials.default_store(credentials.LEARNING_SECRET)
        secret = (store.load() or {}).get("secret")
    except Exception:  # noqa: BLE001
        return None
    return bytes.fromhex(secret) if isinstance(secret, str) and re.fullmatch(r"[0-9a-f]{64}", secret) else None
