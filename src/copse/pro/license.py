"""Offline verification of copse Pro entitlements, and the helpers other
modules use to gate features (``features()``, ``has()``, ``require()``).

The backend has two token kinds signed by the same Ed25519 key: short-lived
access tokens (``typ: at+jwt``, opaque to this client, only sent as Bearer)
and entitlements from ``GET /entitlement`` (``typ: copse-entitlement+jwt``,
``token_use: entitlement``), which are what this module verifies. The
header names a ``kid``, which must be one of the keys pinned in
:mod:`copse.pro.keys`. Claims::

    iss, aud="copse-pro", sub, org_id, plan, status, features[], seats,
    iat, exp, kid, jti, token_use="entitlement"

``iss`` must equal the base URL the entitlement was fetched from.

Everything fails closed: any problem -- a malformed token, an unknown key, a
bad signature, the wrong ``alg``/``typ``/``aud``/``iss``/``token_use``, an
expired token outside its grace, unreadable credentials -- means no
entitlement and no features.

Offline grace: an entitlement is fetched after every successful refresh, so
its signed ``iat`` is the time of the last successful refresh. An expired
entitlement is still honoured (flagged ``in_grace``) until ``iat + grace``
(default 7 days); ``grace`` is clamped to ``MAX_GRACE`` (14 days) however it
is configured. The bound comes from a signed claim, so nothing a user can
edit on disk extends it.

Development: with ``COPSE_PRO_DEV=1`` *and* an issuer on localhost, keys may
also be fetched from that server's ``GET /keys`` (JWKS) and are trusted for
tokens issued by that host only. Never in normal mode.

Tokens are never logged or put in exception messages.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import sys
import urllib.parse
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from copse.pro.keys import PINNED_KEYS

log = logging.getLogger(__name__)

AUDIENCE = "copse-pro"
ALG = "EdDSA"
TYP = "copse-entitlement+jwt"
TOKEN_USE = "entitlement"
DEFAULT_GRACE = 7 * 86400
MAX_GRACE = 14 * 86400        # hard cap on grace past the last refresh (signed iat)
LEEWAY = 60                   # clock skew tolerated on exp / iat
MAX_TOKEN_BYTES = 8192
REFRESH_WHEN_LEFT = 0.25      # refresh once less than this share of lifetime remains
_REQUIRED = ("iss", "aud", "sub", "org_id", "plan", "status", "features", "seats", "iat", "exp",
             "kid", "jti", "token_use")
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class LicenseError(Exception):
    """No valid entitlement. The message never contains a token."""


class NotEntitled(LicenseError):
    """A valid entitlement that doesn't include the requested feature."""


@dataclass(frozen=True)
class Entitlement:
    sub: str
    org_id: str
    plan: str
    status: str
    features: frozenset[str]
    seats: int
    iat: int
    exp: int
    kid: str
    in_grace: bool = False
    grace_until: int | None = None
    role: str | None = None          # the caller's role in org_id (owner|admin|member)
    policy_version: int = 0          # the org policy version current when issued


# -- keys ----------------------------------------------------------------------------

_test_keys: dict[str, Ed25519PublicKey] = {}
_keys_lock = threading.Lock()


_dev_keys: dict[str, dict[str, Ed25519PublicKey]] = {}


def _dev_issuer(issuer: str | None) -> bool:
    """True only in dev mode for an issuer on localhost."""
    if os.environ.get("COPSE_PRO_DEV") != "1" or not issuer:
        return False
    try:
        u = urllib.parse.urlsplit(issuer)
        return u.scheme in ("http", "https") and (u.hostname or "").lower() in LOCAL_HOSTS
    except ValueError:
        return False


def jwk_thumbprint(x: str) -> str:
    """RFC 7638 thumbprint of an Ed25519 OKP key (the backend's kid)."""
    canonical = json.dumps({"crv": "Ed25519", "kty": "OKP", "x": x},
                           separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(hashlib.sha256(canonical.encode()).digest()).rstrip(b"=").decode()


def parse_jwks(jwks: dict) -> dict[str, Ed25519PublicKey]:
    """Ed25519 keys from a JWKS, keeping only those whose kid is their own
    RFC 7638 thumbprint."""
    out: dict[str, Ed25519PublicKey] = {}
    for jwk in (jwks.get("keys") if isinstance(jwks, dict) else None) or []:
        if not isinstance(jwk, dict) or jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
            continue
        x, kid = jwk.get("x"), jwk.get("kid")
        if not isinstance(x, str) or not isinstance(kid, str) or jwk_thumbprint(x) != kid:
            continue
        try:
            out[kid] = Ed25519PublicKey.from_public_bytes(_b64decode(x))
        except (LicenseError, ValueError):
            continue
    return out


def _dev_key(kid: str, issuer: str) -> Ed25519PublicKey | None:
    if issuer not in _dev_keys:
        from copse.pro import auth

        try:
            _dev_keys[issuer] = parse_jwks(auth.fetch_jwks(issuer))
        except Exception:  # noqa: BLE001 - fail closed
            log.info("could not fetch development keys")
            return None
        log.warning("COPSE_PRO_DEV: trusting keys served by %s", issuer)
    return _dev_keys[issuer].get(kid)


def _trusted_key(kid: str, issuer: str | None = None) -> Ed25519PublicKey:
    raw = PINNED_KEYS.get(kid)
    if raw is not None:
        try:
            return Ed25519PublicKey.from_public_bytes(bytes.fromhex(raw))
        except ValueError as e:
            raise LicenseError("pinned key is malformed") from e
    if kid in _test_keys:
        return _test_keys[kid]
    if _dev_issuer(issuer):
        key = _dev_key(kid, issuer)
        if key is not None:
            return key
    raise LicenseError("entitlement signed by an unknown key")


@contextmanager
def _test_signing_key(kid: str, public_key: Ed25519PublicKey):
    """Test-only: trust ``public_key`` as ``kid`` for the duration. Refuses to
    run outside pytest and refuses to shadow a pinned kid. Not configurable
    from the environment, files or the network."""
    if "pytest" not in sys.modules:
        raise RuntimeError("test signing keys are only available under pytest")
    if kid in PINNED_KEYS:
        raise RuntimeError("a test key may not shadow a pinned kid")
    with _keys_lock:
        _test_keys[kid] = public_key
    try:
        yield
    finally:
        with _keys_lock:
            _test_keys.pop(kid, None)
        clear_cache()


def clear_dev_keys() -> None:
    _dev_keys.clear()


# -- verification ----------------------------------------------------------------------


def _b64decode(part: str) -> bytes:
    if not part or any(c not in _B64URL for c in part) or len(part) % 4 == 1:
        raise LicenseError("malformed entitlement")
    try:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
    except (binascii.Error, ValueError) as e:
        raise LicenseError("malformed entitlement") from e


_B64URL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def _json_object(raw: bytes) -> dict:
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise LicenseError("malformed entitlement") from e
    if not isinstance(obj, dict):
        raise LicenseError("malformed entitlement")
    return obj


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def verify(token: str, *, issuer: str | None, now: float | None = None,
           grace: float = DEFAULT_GRACE) -> Entitlement:
    """Verify ``token`` offline and return its entitlement, or raise
    :class:`LicenseError`. ``issuer`` is the base URL it must come from
    (``None`` skips that check); ``grace=0`` disables the offline grace."""
    now = time.time() if now is None else now
    if not isinstance(token, str) or len(token) > MAX_TOKEN_BYTES:
        raise LicenseError("malformed entitlement")
    parts = token.split(".")
    if len(parts) != 3:
        raise LicenseError("malformed entitlement")
    header = _json_object(_b64decode(parts[0]))
    if header.get("alg") != ALG:
        raise LicenseError("entitlement uses an unsupported algorithm")
    if header.get("typ") != TYP:
        raise LicenseError("token is not an entitlement")
    if "crit" in header:
        raise LicenseError("entitlement has unsupported critical headers")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise LicenseError("entitlement names no key")
    key = _trusted_key(kid, issuer)
    sig = _b64decode(parts[2])
    try:
        key.verify(sig, f"{parts[0]}.{parts[1]}".encode("ascii"))
    except InvalidSignature as e:
        raise LicenseError("entitlement signature is invalid") from e
    # Only now is the payload trusted enough to parse.
    claims = _json_object(_b64decode(parts[1]))
    missing = [c for c in _REQUIRED if c not in claims]
    if missing:
        raise LicenseError(f"entitlement is missing claims: {', '.join(missing)}")
    if claims["kid"] != kid:
        raise LicenseError("entitlement kid mismatch")
    if claims["token_use"] != TOKEN_USE:
        raise LicenseError("token is not an entitlement")
    if issuer is not None and claims["iss"] != issuer.rstrip("/"):
        raise LicenseError("entitlement is from a different issuer")
    aud = claims["aud"]
    if not (aud == AUDIENCE or (isinstance(aud, list) and AUDIENCE in aud)):
        raise LicenseError("entitlement is for a different audience")
    feats = claims["features"]
    if not (all(isinstance(claims[c], str) and claims[c] for c in ("sub", "org_id", "plan", "status"))
            and isinstance(feats, list) and all(isinstance(f, str) for f in feats)
            and _is_int(claims["seats"]) and claims["seats"] >= 0
            and _is_int(claims["iat"]) and _is_int(claims["exp"])
            and claims["exp"] > claims["iat"]):
        raise LicenseError("entitlement claims are malformed")
    role, policy_version = claims.get("role"), claims.get("policy_version", 0)
    if not ((role is None or (isinstance(role, str) and role)) and _is_int(policy_version)
            and policy_version >= 0):
        raise LicenseError("entitlement claims are malformed")
    iat, exp = claims["iat"], claims["exp"]
    if iat > now + LEEWAY:
        raise LicenseError("entitlement is not valid yet")
    in_grace, grace_until = False, None
    if now > exp + LEEWAY:
        grace = min(max(0.0, float(grace)), MAX_GRACE)
        grace_until = int(iat + grace)
        if now > grace_until:
            raise LicenseError("entitlement has expired")
        in_grace = True
    return Entitlement(
        sub=claims["sub"], org_id=claims["org_id"], plan=claims["plan"], status=claims["status"],
        features=frozenset(feats), seats=claims["seats"], iat=iat, exp=exp, kid=kid,
        in_grace=in_grace, grace_until=grace_until, role=role, policy_version=policy_version)


def needs_refresh(ent: Entitlement, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return ent.in_grace or (ent.exp - now) < REFRESH_WHEN_LEFT * (ent.exp - ent.iat)


# -- the current entitlement ---------------------------------------------------------------

_cache: tuple[float, Entitlement] | None = None
CACHE_SECONDS = 300


def clear_cache() -> None:
    global _cache
    _cache = None


def current(*, refresh: bool = True, now: float | None = None, store=None, client=None) -> Entitlement:
    """The entitlement from the stored credentials, fetching a new one (and
    rotating the refresh token if needed) when it is expired or near expiry
    (``refresh``). Offline, an expired entitlement is used within its grace.
    Raises :class:`LicenseError` when there is none."""
    global _cache
    now = time.time() if now is None else now
    default = store is None and client is None
    if default and _cache and _cache[0] > now:
        return _cache[1]
    from copse.pro import auth, credentials

    try:
        store = store or credentials.default_store()
        creds = store.load()
    except credentials.CredentialError as e:
        raise LicenseError(str(e)) from e
    if not creds or not (creds.get("entitlement") or creds.get("refresh_token")):
        raise LicenseError("not logged in to copse Pro (run `copse account login`)")
    try:
        issuer = client.base if client is not None else auth.base_url(creds.get("base_url"))
    except auth.AuthError as e:
        raise LicenseError("stored copse Pro URL is not allowed") from e
    ent = None
    try:
        ent = verify(creds.get("entitlement") or "", issuer=issuer, now=now)
    except LicenseError:
        if not refresh:
            raise
    if refresh and (ent is None or needs_refresh(ent, now)) and creds.get("refresh_token"):
        try:
            ent = auth.refresh(client or auth.Client(issuer), store, creds, now=now)
        except auth.AuthError as e:
            if e.revoked:
                raise LicenseError("copse Pro session was revoked; log in again") from e
            log.info("entitlement refresh failed (%s); using the stored entitlement", e.code)
    if ent is None:
        ent = verify(creds.get("entitlement") or "", issuer=issuer, now=now)   # raises with the reason
    if default:
        _cache = (min(now + CACHE_SECONDS, ent.grace_until or ent.exp), ent)
    return ent


def features() -> frozenset[str]:
    """The entitled features, or an empty set when there is no valid
    entitlement for any reason (fail closed)."""
    try:
        return current().features
    except Exception:  # noqa: BLE001 - fail closed on anything
        return frozenset()


def has(feature: str) -> bool:
    return feature in features()


def require(feature: str) -> Entitlement:
    """The entitlement if it includes ``feature``; raises otherwise."""
    try:
        ent = current()
    except LicenseError:
        raise
    except Exception as e:  # noqa: BLE001 - fail closed on anything
        raise LicenseError("could not check the copse Pro entitlement") from e
    if feature not in ent.features:
        raise NotEntitled(f"your copse Pro plan ({ent.plan}) does not include {feature!r}")
    return ent
