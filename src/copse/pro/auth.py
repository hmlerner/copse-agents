"""The copse Pro backend client: device-flow login (RFC 8628), refresh-token
rotation, entitlements, account info and billing links.

Backend contract (``/api/copse/v1``): OAuth calls are form-encoded POSTs that
all carry ``client_id=copse-cli``; ``/device/token`` and ``/token/refresh``
return a 15-minute access token (opaque here, sent as Bearer) and a refresh
token that is rotated on every use. ``GET /entitlement`` (Bearer) returns
the signed entitlement that :mod:`copse.pro.license` verifies. Presenting a
refresh token twice is treated by the backend as theft and revokes the
whole session, so rotation is serialized across processes with a file lock
and the rotated tokens are saved before anything else is attempted.

Transport rules (``UrllibTransport``):

* https only; ``http://`` is allowed solely to localhost/127.0.0.1/::1 and
  only when ``COPSE_PRO_DEV=1``. No credentials in URLs.
* TLS verification is always on (system trust store, hostname checks); there
  is no switch to turn it off.
* Every request has a timeout; responses larger than ``MAX_RESPONSE`` are
  refused; only 307/308 redirects to the same scheme, host and port are
  followed.

Base URL: ``COPSE_PRO_BASE_URL`` or ``DEFAULT_BASE_URL``. Tokens are never
logged, and server error text is sanitized before it is shown.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from copse.pro import license

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://pawdelta.com/api/copse/v1"
CLIENT_ID = "copse-cli"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
SLOW_DOWN_STEP = 5
TIMEOUT = 15.0
MAX_RESPONSE = 256 * 1024
LOCAL_HOSTS = license.LOCAL_HOSTS
USER_AGENT = "copse-pro"
ACCESS_SLACK = 60          # treat an access token this close to expiry as expired
MAX_ACCESS_TTL = 3600      # never trust a server-claimed access lifetime beyond this
LOCK_TIMEOUT = 60.0


class AuthError(Exception):
    """A failed backend call. ``code`` is an OAuth-style error code;
    ``revoked`` means the session is gone and the user must log in again."""

    def __init__(self, message: str, code: str = "error", revoked: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.revoked = revoked


class TransportError(AuthError):
    """The backend couldn't be reached (network, TLS, timeout, bad response)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="transport")


def _sanitize(text, limit: int = 200) -> str:
    return re.sub(r"[^\x20-\x7e]", "?", str(text))[:limit]


# -- URLs -----------------------------------------------------------------------------------


def check_url(url: str) -> urllib.parse.SplitResult:
    """Parse ``url`` and enforce the transport rules; raise :class:`AuthError`."""
    try:
        u = urllib.parse.urlsplit(url)
        host = (u.hostname or "").lower()
        u.port   # raises on a bad port
    except ValueError as e:
        raise AuthError("invalid copse Pro URL", code="bad_url") from e
    if not host or u.username or u.password:
        raise AuthError("invalid copse Pro URL", code="bad_url")
    if u.scheme == "https":
        return u
    if u.scheme == "http" and host in LOCAL_HOSTS and os.environ.get("COPSE_PRO_DEV") == "1":
        return u
    raise AuthError("copse Pro URLs must use https", code="bad_url")


def base_url(override: str | None = None) -> str:
    url = (override or os.environ.get("COPSE_PRO_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    check_url(url)
    return url


# -- transport --------------------------------------------------------------------------------


class JSONBody(dict):
    """A request body to send as JSON rather than urlencoded."""


class Transport(Protocol):
    def request(self, method: str, url: str, form: dict | None,
                headers: dict) -> tuple[int, dict]:
        """Send ``form`` (urlencoded, or JSON for a :class:`JSONBody`; None
        for GET) to ``url``; return (status, JSON object)."""


def _origin(u: urllib.parse.SplitResult) -> tuple[str, str, int | None]:
    return (u.scheme, (u.hostname or "").lower(), u.port)


class _SameOriginRedirects(urllib.request.HTTPRedirectHandler):
    """Follow only 307/308 (which keep the method and body) to the same
    scheme, host and port; refuse every other redirect."""
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = check_url(urllib.parse.urljoin(req.full_url, newurl))
        if _origin(new) != _origin(urllib.parse.urlsplit(req.full_url)):
            raise TransportError("refusing a redirect to another host")
        if code not in (307, 308):
            raise TransportError(f"refusing an HTTP {code} redirect")
        return urllib.request.Request(new.geturl(), data=req.data, method=req.get_method(),
                                      headers=dict(req.header_items()))


class UrllibTransport:
    def __init__(self, timeout: float = TIMEOUT, max_bytes: int = MAX_RESPONSE) -> None:
        self.timeout, self.max_bytes = timeout, max_bytes
        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        # build_opener would add default handlers (incl. file:// and ftp://);
        # assemble exactly what's needed instead.
        self.opener = urllib.request.OpenerDirector()
        for h in (urllib.request.HTTPSHandler(context=ctx), urllib.request.HTTPHandler(),
                  _SameOriginRedirects(), urllib.request.HTTPErrorProcessor(),
                  urllib.request.HTTPDefaultErrorHandler(), urllib.request.UnknownHandler()):
            self.opener.add_handler(h)

    def request(self, method: str, url: str, form: dict | None,
                headers: dict) -> tuple[int, dict]:
        check_url(url)
        hdrs = {"Accept": "application/json", "User-Agent": USER_AGENT, **headers}
        data = None
        if isinstance(form, JSONBody):
            data = json.dumps(form, separators=(",", ":")).encode()
            hdrs["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            resp = self.opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            resp = e
        except AuthError:
            raise
        except (urllib.error.URLError, OSError, ValueError) as e:
            reason = getattr(e, "reason", e)
            raise TransportError(f"cannot reach copse Pro backend: {_sanitize(reason)}") from None
        with resp:
            status = resp.status if hasattr(resp, "status") else resp.code
            try:
                body = resp.read(self.max_bytes + 1)
            except OSError as e:
                raise TransportError("error reading the backend response") from e
        if len(body) > self.max_bytes:
            raise TransportError("backend response too large")
        try:
            obj = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, ValueError):
            raise TransportError(f"backend returned a non-JSON response (HTTP {status})") from None
        if not isinstance(obj, dict):
            raise TransportError("backend returned an unexpected response")
        return status, obj


# -- client -------------------------------------------------------------------------------------


class Client:
    def __init__(self, base: str | None = None, transport: Transport | None = None) -> None:
        self.base = base_url(base)
        self.transport = transport or UrllibTransport()

    def call(self, method: str, path: str, form: dict | None = None,
             token: str | None = None) -> tuple[int, dict]:
        url = self.base + path
        check_url(url)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.transport.request(method, url, form, headers)

    def post(self, path: str, form: dict, token: str | None = None) -> tuple[int, dict]:
        return self.call("POST", path, form, token)

    def get(self, path: str, token: str | None = None) -> tuple[int, dict]:
        return self.call("GET", path, None, token)


def _error(status: int, body: dict) -> AuthError:
    code = body.get("error") if isinstance(body.get("error"), str) else f"http_{status}"
    code = re.sub(r"[^a-z0-9_]", "", code.lower())[:64] or f"http_{status}"
    desc = body.get("error_description")
    msg = f"{code}: {_sanitize(desc)}" if isinstance(desc, str) and desc else code
    return AuthError(msg, code=code, revoked=code == "invalid_grant")


def fetch_jwks(base: str, transport: Transport | None = None) -> dict:
    """The server's JWKS. Only :mod:`license` calls this, and only in dev mode
    for a localhost issuer."""
    status, body = Client(base, transport).get("/keys")
    if status != 200:
        raise _error(status, body)
    return body


# -- device flow ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceAuthorization:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_in: int
    interval: int


def start_device_flow(client: Client) -> DeviceAuthorization:
    status, body = client.post("/device/authorize", {"client_id": CLIENT_ID})
    if status != 200:
        raise _error(status, body)
    try:
        auth = DeviceAuthorization(
            device_code=str(body["device_code"]), user_code=_sanitize(body["user_code"], 32),
            verification_uri=str(body["verification_uri"]),
            verification_uri_complete=body.get("verification_uri_complete"),
            expires_in=int(body["expires_in"]), interval=max(1, int(body.get("interval", 5))))
    except (KeyError, TypeError, ValueError) as e:
        raise AuthError("malformed device authorization response", code="bad_response") from e
    # The user will open these in a browser: never show anything but https.
    for uri in (auth.verification_uri, auth.verification_uri_complete):
        if uri is not None and urllib.parse.urlsplit(str(uri)).scheme != "https":
            if os.environ.get("COPSE_PRO_DEV") != "1":
                raise AuthError("backend sent a non-https verification URL", code="bad_response")
    return auth


def _token_set(body: dict, now: float, old_refresh: str | None = None) -> dict:
    access, refresh_tok = body.get("access_token"), body.get("refresh_token")
    if not isinstance(access, str) or not access or not isinstance(refresh_tok, str) \
            or not refresh_tok:
        raise AuthError("backend response is missing tokens", code="bad_response")
    if str(body.get("token_type", "Bearer")).lower() != "bearer":
        raise AuthError("backend returned an unexpected token type", code="bad_response")
    if old_refresh is not None and refresh_tok == old_refresh:
        raise AuthError("backend did not rotate the refresh token", code="bad_response")
    try:
        ttl = int(body.get("expires_in", 900))
    except (TypeError, ValueError):
        ttl = 0
    ttl = max(0, min(ttl, MAX_ACCESS_TTL))
    return {"access_token": access, "refresh_token": refresh_tok, "access_expires_at": now + ttl}


def poll_device_token(client: Client, auth: DeviceAuthorization, *,
                      sleep: Callable[[float], None] | None = None,
                      clock: Callable[[], float] | None = None) -> dict:
    """Poll until the user approves, honouring ``interval``, ``slow_down``
    (the server's new ``interval``, else +5 s; RFC 8628 3.5) and
    ``expired_token``. Returns the token set."""
    sleep, clock = sleep or time.sleep, clock or time.time
    interval = auth.interval
    deadline = clock() + auth.expires_in
    while True:
        sleep(interval)
        if clock() > deadline:
            raise AuthError("the login code expired; run login again", code="expired_token")
        try:
            status, body = client.post("/device/token", {
                "grant_type": DEVICE_GRANT, "device_code": auth.device_code,
                "client_id": CLIENT_ID})
        except TransportError:
            interval += SLOW_DOWN_STEP   # back off on network trouble too
            continue
        if status == 200:
            return _token_set(body, clock())
        err = _error(status, body)
        if err.code == "authorization_pending":
            continue
        if err.code == "slow_down":
            new = body.get("interval")
            ok = isinstance(new, int) and not isinstance(new, bool) and 0 < new <= 300
            interval = max(interval + SLOW_DOWN_STEP, new) if ok else interval + SLOW_DOWN_STEP
            continue
        if err.code == "expired_token":
            raise AuthError("the login code expired; run login again", code="expired_token")
        if err.code == "access_denied":
            raise AuthError("login was denied", code="access_denied")
        raise err


ORG_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _fetch_entitlement(client: Client, access: str, now: float, org_id: str | None = None):
    """(token, verified entitlement), or 401 when the access token was refused.
    ``org_id`` selects a team org (default: the personal org)."""
    path = "/entitlement"
    if org_id is not None:
        if not ORG_ID_RE.match(org_id):
            raise AuthError("invalid org id", code="bad_request")
        path += "?" + urllib.parse.urlencode({"org_id": org_id})
    status, body = client.get(path, token=access)
    if status == 401:
        return 401
    if status != 200:
        raise _error(status, body)
    tok = body.get("entitlement")
    if not isinstance(tok, str):
        raise AuthError("backend returned no entitlement", code="bad_response")
    ent = license.verify(tok, issuer=client.base, now=now, grace=0)
    if org_id is not None and ent.org_id != org_id:
        raise AuthError("backend returned an entitlement for another org", code="bad_response")
    return tok, ent


def login(client: Client, store, *, show: Callable[[str], None] = print,
          sleep: Callable[[float], None] | None = None,
          clock: Callable[[], float] | None = None) -> license.Entitlement:
    clock = clock or time.time
    auth = start_device_flow(client)
    show(f"To log in to copse Pro, open {auth.verification_uri}\n"
         f"and enter the code: {auth.user_code}")
    creds = poll_device_token(client, auth, sleep=sleep, clock=clock)
    creds["base_url"] = client.base
    with refresh_lock():
        store.save(creds)     # keep the refresh token before anything else can fail
        try:
            got = _fetch_entitlement(client, creds["access_token"], clock())
            if got == 401:
                raise AuthError("backend refused its own access token", code="invalid_token")
        except Exception:
            _revoke_quietly(client, creds.get("refresh_token"))
            store.delete()
            raise
        creds["entitlement"] = got[0]
        store.save(creds)
    license.clear_cache()
    return got[1]


# -- refresh ---------------------------------------------------------------------------------------


def lock_path() -> Path:
    from copse.config import copse_home

    return copse_home() / "pro" / "refresh.lock"


@contextmanager
def refresh_lock(timeout: float = LOCK_TIMEOUT):
    """An exclusive, cross-process lock (``flock``) around every read-rotate-
    save of the refresh token."""
    path = lock_path()
    old = os.umask(0o077)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    finally:
        os.umask(old)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise AuthError("timed out waiting for another copse process to refresh",
                                    code="lock_timeout") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _rotate(client: Client, store, cur: dict, now: float) -> dict:
    """Spend the refresh token once and save the new pair immediately. Call
    with the refresh lock held."""
    old = cur.get("refresh_token")
    if not old:
        raise AuthError("not logged in", code="invalid_grant", revoked=True)
    status, body = client.post("/token/refresh", {
        "grant_type": "refresh_token", "refresh_token": old, "client_id": CLIENT_ID})
    if status != 200:
        err = _error(status, body)
        if err.revoked:
            store.delete()
            license.clear_cache()
        raise err
    new = {**cur, **_token_set(body, now, old_refresh=old), "base_url": client.base}
    store.save(new)
    return new


def _usable_entitlement(cur: dict, issuer: str, now: float) -> license.Entitlement | None:
    try:
        ent = license.verify(cur.get("entitlement") or "", issuer=issuer, now=now, grace=0)
    except license.LicenseError:
        return None
    return None if license.needs_refresh(ent, now) else ent


def refresh(client: Client, store, seen: dict | None = None, *, now: float | None = None,
            force: bool = False) -> license.Entitlement:
    """Get a fresh entitlement, rotating the refresh token when the access
    token is expired (or ``force``). ``seen`` is the credentials the caller
    read before deciding to refresh: if another process rotated them since,
    its result is used instead of spending a refresh token again."""
    now = time.time() if now is None else now
    with refresh_lock():
        cur = store.load() or {}
        if not cur.get("refresh_token"):
            raise AuthError("not logged in to copse Pro", code="invalid_grant", revoked=True)
        elsewhere = seen is not None and cur.get("refresh_token") != seen.get("refresh_token")
        if elsewhere:
            ent = _usable_entitlement(cur, client.base, now)
            if ent is not None:
                license.clear_cache()
                return ent
        fresh_access = bool(cur.get("access_token")) and \
            float(cur.get("access_expires_at") or 0) > now + ACCESS_SLACK
        rotated = False
        if not fresh_access or (force and not elsewhere):
            cur, rotated = _rotate(client, store, cur, now), True
        org = cur.get("org_id")
        got = _fetch_entitlement(client, cur["access_token"], now, org)
        if got == 401 and not rotated:
            cur = _rotate(client, store, cur, now)
            got = _fetch_entitlement(client, cur["access_token"], now, org)
        if got == 401:
            raise AuthError("backend refused a fresh access token", code="invalid_token")
        cur["entitlement"] = got[0]
        store.save(cur)
    license.clear_cache()
    return got[1]


def switch_org(client: Client, store, org_id: str | None, *,
               now: float | None = None) -> license.Entitlement:
    """Make ``org_id`` (None: the personal org) the org whose entitlement is
    stored and used. Nothing changes unless the backend issues a valid
    entitlement for it."""
    now = time.time() if now is None else now
    if org_id is not None and not ORG_ID_RE.match(org_id):
        raise AuthError("invalid org id", code="bad_request")
    with refresh_lock():
        cur = store.load() or {}
        if not cur.get("refresh_token"):
            raise AuthError("not logged in to copse Pro (run `copse account login`)",
                            code="not_logged_in")
        if float(cur.get("access_expires_at") or 0) <= now + ACCESS_SLACK:
            cur = _rotate(client, store, cur, now)
        got = _fetch_entitlement(client, cur["access_token"], now, org_id)
        if got == 401:
            cur = _rotate(client, store, cur, now)
            got = _fetch_entitlement(client, cur["access_token"], now, org_id)
        if got == 401:
            raise AuthError("backend refused a fresh access token", code="invalid_token")
        if got[1].org_id != (org_id or got[1].org_id):
            raise AuthError("backend returned an entitlement for another org", code="bad_response")
        cur["entitlement"] = got[0]
        if org_id is None:
            cur.pop("org_id", None)
        else:
            cur["org_id"] = org_id
        store.save(cur)
    license.clear_cache()
    return got[1]


def authed(client: Client, store, method: str, path: str, form: dict | None = None,
           *, now: float | None = None) -> tuple[int, dict]:
    """Call a Bearer endpoint, refreshing the access token first if it is
    about to expire and once more if the backend answers 401."""
    now = time.time() if now is None else now
    cur = store.load() or {}
    if not cur.get("refresh_token"):
        raise AuthError("not logged in to copse Pro (run `copse account login`)",
                        code="not_logged_in")
    if float(cur.get("access_expires_at") or 0) <= now + ACCESS_SLACK:
        refresh(client, store, cur, now=now)
        cur = store.load() or {}
    status, body = client.call(method, path, form, token=cur.get("access_token"))
    if status == 401:
        refresh(client, store, cur, now=now, force=True)
        cur = store.load() or {}
        status, body = client.call(method, path, form, token=cur.get("access_token"))
    return status, body


# -- logout, account, billing -------------------------------------------------------------------------


def _revoke_quietly(client: Client | None, token: str | None) -> None:
    if client is None or not token:
        return
    try:
        client.post("/token/revoke", {"token": token, "token_type_hint": "refresh_token",
                                      "client_id": CLIENT_ID})
    except AuthError as e:
        log.info("server-side revocation failed (%s)", e.code)


def logout(client: Client | None, store) -> None:
    """Best-effort server-side revocation (which ends the whole session),
    then always forget local credentials."""
    with refresh_lock():
        try:
            creds = store.load() or {}
        except Exception:  # noqa: BLE001 - still delete below
            creds = {}
        _revoke_quietly(client, creds.get("refresh_token"))
        store.delete()
    license.clear_cache()


def me(client: Client, store) -> dict:
    status, body = authed(client, store, "GET", "/me")
    if status != 200:
        raise _error(status, body)
    return {k: _sanitize(body[k]) for k in ("sub", "email", "org_id", "plan", "status", "seats")
            if k in body}


def _billing_url(client: Client, store, path: str) -> str:
    status, body = authed(client, store, "POST", path, {})
    if status != 200:
        raise _error(status, body)
    url = body.get("url")
    if not isinstance(url, str) or urllib.parse.urlsplit(url).scheme != "https":
        raise AuthError("backend returned no valid billing URL", code="bad_response")
    return _sanitize(url, 2048)


def checkout_url(client: Client, store) -> str:
    """A Stripe checkout URL for upgrading (``POST /billing/checkout``)."""
    return _billing_url(client, store, "/billing/checkout")


def portal_url(client: Client, store) -> str:
    """The billing portal URL (``POST /billing/portal``)."""
    return _billing_url(client, store, "/billing/portal")
