"""Shared fixtures and fakes for the copse Pro client tests
(``tests/test_pro_*.py``): a test signing key, signed entitlements, a fake
backend, and an isolated credential store. Test modules import what they
use; importing ``pro_env`` (autouse) applies the isolation to the module.
"""

import base64
import json
import os
import re
import threading
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from copse.learning import LearningPlugin
from copse.pro import license

TEST_KID = "test-kid-1"
ISS = "https://pawdelta.test/api/copse/v1"
BASE = ISS


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def claims(**over):
    now = int(time.time())
    c = {"iss": ISS, "aud": "copse-pro", "sub": "user_1", "org_id": "org_1", "plan": "pro",
         "status": "active", "features": ["learning", "autopilot"], "seats": 5, "iat": now,
         "exp": now + 3600, "kid": TEST_KID, "jti": "j1", "token_use": "entitlement"}
    c.update(over)
    return {k: v for k, v in c.items() if v is not ...}


def sign(key: Ed25519PrivateKey, payload: dict, header: dict | None = None) -> str:
    h = {"alg": "EdDSA", "typ": "copse-entitlement+jwt", "kid": payload.get("kid", TEST_KID)}
    h.update(header or {})
    signing_input = f"{b64(json.dumps(h).encode())}.{b64(json.dumps(payload).encode())}"
    return f"{signing_input}.{b64(key.sign(signing_input.encode()))}"


@pytest.fixture
def signing_key():
    key = Ed25519PrivateKey.generate()
    with license._test_signing_key(TEST_KID, key.public_key()):
        yield key


@pytest.fixture
def token(signing_key):
    def make(**over):
        return sign(signing_key, claims(**over))
    return make


@pytest.fixture(autouse=True)
def pro_env(monkeypatch, copse_home):
    """A private home, the file credential store, a test base URL, no dev
    mode, and no cached entitlement or dev keys (see also tests/conftest.py)."""
    monkeypatch.setenv("COPSE_PRO_BASE_URL", BASE)
    license.clear_cache()
    license.clear_dev_keys()
    yield
    license.clear_cache()
    license.clear_dev_keys()


class FakeTransport:
    """Routes "METHOD /path" to a queue of (status, body) responses (the last
    one repeats), an exception to raise, or a callable(form, headers)."""

    def __init__(self, routes=None, base=BASE):
        self.base = base
        self.routes = {k: (v if callable(v) else list(v)) for k, v in (routes or {}).items()}
        self.calls = []
        self.lock = threading.Lock()

    def request(self, method, url, form, headers):
        assert url.startswith(self.base), url
        key = f"{method} {url[len(self.base):]}"
        with self.lock:
            self.calls.append((key, dict(form or {}), dict(headers)))
        r = self.routes[key]
        if callable(r):
            return r(form or {}, headers)
        with self.lock:
            r = r.pop(0) if len(r) > 1 else r[0]
        if isinstance(r, Exception):
            raise r
        return r

    def paths(self):
        return [c[0] for c in self.calls]


class FakeBackend(FakeTransport):
    """A stateful stand-in for the real backend's token endpoints: rotating
    refresh tokens where reuse revokes the family, 15-minute access tokens,
    and signed entitlements."""

    def __init__(self, signing_key, *, delay=0.0, plan="pro"):
        super().__init__({
            "POST /token/refresh": self._refresh, "GET /entitlement": self._entitlement,
            "GET /me": self._me, "POST /billing/checkout": self._checkout,
            "POST /billing/portal": self._portal, "POST /token/revoke": self._revoke,
        })
        self.key, self.delay, self.plan = signing_key, delay, plan
        self.n = 0
        self.refresh_tokens: dict[str, str] = {}
        self.access_tokens: set[str] = set()
        self.revoked = False
        self.state = threading.Lock()
        self.org_keys: dict[str, tuple[str, bytes]] = {}   # org_id -> (key_id, key)
        self.key_fetches: list[str] = []
        self.key_status: tuple[int, dict] | None = None     # force an error answer

    def org_key(self, org_id):
        if org_id not in self.org_keys:
            self.org_keys[org_id] = ("k1", os.urandom(32))
        return self.org_keys[org_id]

    def request(self, method, url, form, headers):
        m = re.fullmatch(re.escape(self.base) + r"/orgs/([^/]+)/learning-key", url)
        if method == "GET" and m and f"GET /orgs/{m.group(1)}/learning-key" not in self.routes:
            with self.lock:
                self.calls.append((f"GET /orgs/{m.group(1)}/learning-key", {}, dict(headers)))
            if not self._bearer(headers):
                return 401, {"error": "invalid_token"}
            if self.key_status:
                return self.key_status
            self.key_fetches.append(m.group(1))
            key_id, key = self.org_key(m.group(1))
            return 200, {"org_id": m.group(1), "key_id": key_id,
                         "key": base64.b64encode(key).decode(), "created_at": 1}
        return super().request(method, url, form, headers)

    def issue(self):
        with self.state:
            self.n += 1
            a, r = f"at_{self.n}", f"cpr_{self.n}"
            self.access_tokens.add(a)
            self.refresh_tokens[r] = "active"
        return {"access_token": a, "token_type": "Bearer", "expires_in": 900, "refresh_token": r}

    def _refresh(self, form, headers):
        assert form.get("client_id") == "copse-cli" and form.get("grant_type") == "refresh_token"
        r = form.get("refresh_token")
        with self.state:
            st = self.refresh_tokens.get(r)
            if self.revoked or st is None:
                return 400, {"error": "invalid_grant"}
            if st != "active":
                self.revoked = True          # reuse: theft, revoke the family
                return 400, {"error": "invalid_grant"}
            self.refresh_tokens[r] = "used"
        time.sleep(self.delay)
        return 200, self.issue()

    def _bearer(self, headers):
        tok = headers.get("Authorization", "").removeprefix("Bearer ")
        return not self.revoked and tok in self.access_tokens

    def _entitlement(self, form, headers):
        if not self._bearer(headers):
            return 401, {"error": "invalid_token"}
        return 200, {"entitlement": sign(self.key, claims(plan=self.plan)), "plan": self.plan,
                     "status": "active", "features": ["learning", "autopilot"], "seats": 5,
                     "expires_at": int(time.time()) + 3600}

    def _me(self, form, headers):
        if not self._bearer(headers):
            return 401, {"error": "invalid_token"}
        return 200, {"sub": "user_1", "email": "dev@example.test", "org_id": "org_1",
                     "plan": self.plan, "status": "active", "seats": 5}

    def _checkout(self, form, headers):
        if not self._bearer(headers):
            return 401, {"error": "invalid_token"}
        return 200, {"url": "https://checkout.stripe.test/c/abc", "id": "cs_1"}

    def _portal(self, form, headers):
        if not self._bearer(headers):
            return 401, {"error": "invalid_token"}
        return 200, {"url": "https://billing.stripe.test/p/xyz"}

    def _revoke(self, form, headers):
        assert form.get("client_id") == "copse-cli"
        self.revoked = True
        return 200, {}


@pytest.fixture
def backend(signing_key):
    return FakeBackend(signing_key)


class SpyLocal(LearningPlugin):
    """A stand-in for copse-pro's local learner: remembers what it was told
    and suggests the last candidate, so its answers are recognisable."""

    def __init__(self):
        self.recorded: list[tuple[str | None, str]] = []
        self.suggested = 0

    def record(self, task, outcome):
        self.recorded.append((task.agent_id, outcome.event))

    def suggest(self, task, candidates):
        self.suggested += 1
        return candidates[-1]

    def report(self, reset=False):
        return f"spy: {len(self.recorded)} outcome(s)"

    def done(self, agent_id) -> bool:
        return any(a == agent_id and e in ("merged", "removed_unmerged") for a, e in self.recorded)


ROOT_SHA = "a" * 40


@pytest.fixture
def fixed_identity(monkeypatch):
    """Every repo path gets the same root-commit identity (no git needed)."""
    from copse.pro import orgkey

    monkeypatch.setattr(orgkey, "repo_identity", lambda root: ROOT_SHA)
    for mod in ("copse.pro.learning", "copse.pro.team_events"):
        monkeypatch.setattr(f"{mod}.repo_identity", lambda root: ROOT_SHA)
    return ROOT_SHA
