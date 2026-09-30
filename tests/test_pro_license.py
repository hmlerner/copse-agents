"""Offline entitlement verification, the credential stores, and the current
entitlement (refresh, offline grace, failing closed)."""
import json
import os
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from copse.pro import auth, credentials, keys, license
from copse.pro.license import LicenseError, NotEntitled
from pro_fixtures import (  # noqa: F401 - fixtures
    BASE, ISS, TEST_KID, b64, backend, claims, pro_env, sign, signing_key, token,
)

DAY = 86400


def verify(token, **kw):
    kw.setdefault("issuer", ISS)
    return license.verify(token, **kw)


# -- verification ------------------------------------------------------------------------


def test_valid_token(token):
    ent = verify(token())
    assert (ent.sub, ent.org_id, ent.plan, ent.status, ent.seats, ent.kid) == \
        ("user_1", "org_1", "pro", "active", 5, TEST_KID)
    assert ent.features == {"learning", "autopilot"} and not ent.in_grace


def test_audience_may_be_a_list(token):
    assert verify(token(aud=["other", "copse-pro"])).plan == "pro"


def test_issuer_must_match_the_backend(token):
    with pytest.raises(LicenseError, match="issuer"):
        verify(token(iss="https://evil.test/api/copse/v1"))
    with pytest.raises(LicenseError, match="issuer"):
        verify(token(), issuer="https://other.pawdelta.test/api/copse/v1")
    assert verify(token(), issuer=ISS + "/").sub == "user_1"


@pytest.mark.parametrize("typ", ["at+jwt", "JWT", None])
def test_only_entitlement_typ_is_accepted(signing_key, typ):
    """An access token (same key, same audience) can't be passed off as an entitlement."""
    with pytest.raises(LicenseError, match="not an entitlement"):
        verify(sign(signing_key, claims(), {"typ": typ}))


@pytest.mark.parametrize("use", ["access", "", None])
def test_token_use_must_be_entitlement(token, use):
    with pytest.raises(LicenseError, match="not an entitlement"):
        verify(token(token_use=use))


@pytest.mark.parametrize("mangle", [
    lambda t: t[:-4] + ("AAAA" if not t.endswith("AAAA") else "BBBB"),       # signature
    lambda t: ".".join([t.split(".")[0], b64(json.dumps(claims(plan="enterprise")).encode()),
                        t.split(".")[2]]),                                     # payload swapped
    lambda t: ".".join([b64(b'{"alg":"EdDSA","kid":"test-kid-1","x":1}'), *t.split(".")[1:]]),
    lambda t: t + ".extra",
    lambda t: t.rsplit(".", 1)[0],
    lambda t: t.replace(".", "!", 1),
    lambda t: "",
    lambda t: "a" * (license.MAX_TOKEN_BYTES + 1),
])
def test_tampered_or_malformed_tokens_are_rejected(token, mangle):
    with pytest.raises(LicenseError):
        verify(mangle(token()))


@pytest.mark.parametrize("alg", ["none", "HS256", "RS256", "ES256", "", None])
def test_wrong_alg_is_rejected(signing_key, alg):
    with pytest.raises(LicenseError, match="algorithm"):
        verify(sign(signing_key, claims(), {"alg": alg}))


def test_unsigned_token_is_rejected():
    header = b64(json.dumps({"alg": "none", "kid": TEST_KID}).encode())
    t = f"{header}.{b64(json.dumps(claims()).encode())}."
    with pytest.raises(LicenseError):
        verify(t)


def test_unknown_kid_is_rejected(signing_key):
    other = Ed25519PrivateKey.generate()
    with pytest.raises(LicenseError, match="unknown key"):
        verify(sign(other, claims(kid="rogue"), {"kid": "rogue"}))


def test_right_kid_wrong_key_is_rejected(signing_key):
    other = Ed25519PrivateKey.generate()
    with pytest.raises(LicenseError, match="signature"):
        verify(sign(other, claims()))


def test_kid_header_and_claim_must_match(signing_key):
    with pytest.raises(LicenseError, match="kid"):
        verify(sign(signing_key, claims(kid="something-else"), {"kid": TEST_KID}))


def test_crit_header_is_rejected(signing_key):
    with pytest.raises(LicenseError, match="critical"):
        verify(sign(signing_key, claims(), {"crit": ["exp"]}))


@pytest.mark.parametrize("aud", ["copse", "copse-pro-evil", ["other"], None, ""])
def test_wrong_audience_is_rejected(token, aud):
    with pytest.raises(LicenseError, match="audience"):
        verify(token(aud=aud))


@pytest.mark.parametrize("claim", ["iss", "aud", "sub", "org_id", "plan", "status", "features", "seats",
                                   "iat", "exp", "kid", "jti", "token_use"])
def test_missing_claims_are_rejected(token, claim):
    with pytest.raises(LicenseError, match="missing"):
        verify(token(**{claim: ...}))


@pytest.mark.parametrize("over", [
    {"seats": True}, {"seats": -1}, {"seats": "5"}, {"features": "learning"},
    {"features": [1]}, {"sub": ""}, {"status": 3}, {"iat": "0"}, {"exp": 1.5},
    {"role": 5}, {"role": ""}, {"policy_version": -1}, {"policy_version": "3"},
    {"iat": 2_000_000_000, "exp": 1_000_000_000},
])
def test_malformed_claims_are_rejected(token, over):
    with pytest.raises(LicenseError):
        verify(token(**over))


def test_expired_token_is_rejected_without_grace(token):
    now = time.time()
    with pytest.raises(LicenseError, match="expired"):
        verify(token(iat=int(now - 7200), exp=int(now - 3600)), grace=0)


def test_leeway_on_expiry(token):
    now = time.time()
    assert not verify(token(iat=int(now - 3600), exp=int(now - 10)), grace=0).in_grace


def test_future_iat_is_rejected(token):
    now = time.time()
    with pytest.raises(LicenseError, match="not valid yet"):
        verify(token(iat=int(now + 3600), exp=int(now + 7200)))


# -- offline grace ----------------------------------------------------------------------------


def test_grace_is_measured_from_the_last_refresh(token):
    iat = 1_000_000_000
    t = token(iat=iat, exp=iat + DAY)
    ent = verify(t, now=iat + 3 * DAY)
    assert ent.in_grace and ent.grace_until == iat + 7 * DAY
    assert verify(t, now=iat + 7 * DAY - 1).in_grace
    with pytest.raises(LicenseError, match="expired"):
        verify(t, now=iat + 7 * DAY + 1)


def test_grace_never_passes_the_hard_max(token):
    iat = 1_000_000_000
    t = token(iat=iat, exp=iat + DAY)
    # A huge configured grace is capped at MAX_GRACE past the last refresh.
    assert verify(t, now=iat + license.MAX_GRACE - 1, grace=10**9).in_grace
    with pytest.raises(LicenseError, match="expired"):
        verify(t, now=iat + license.MAX_GRACE + 1, grace=10**9)


def test_negative_grace_means_none(token):
    iat = 1_000_000_000
    with pytest.raises(LicenseError):
        verify(token(iat=iat, exp=iat + DAY), now=iat + DAY + 3600, grace=-5)


# -- the test-only key hook ------------------------------------------------------------------


def test_test_hook_cannot_shadow_a_pinned_kid(monkeypatch):
    monkeypatch.setitem(keys.PINNED_KEYS, "prod", "00" * 32)
    with pytest.raises(RuntimeError):
        with license._test_signing_key("prod", Ed25519PrivateKey.generate().public_key()):
            pass


def test_test_key_is_gone_after_the_hook(token):
    t = token()
    with license._test_signing_key("x", Ed25519PrivateKey.generate().public_key()):
        pass
    assert "x" not in license._test_keys
    assert verify(t).sub == "user_1"


def test_pinned_keys_are_trusted(monkeypatch):
    from cryptography.hazmat.primitives import serialization

    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setitem(license.PINNED_KEYS, "pinned-1", raw.hex())
    assert verify(sign(key, claims(kid="pinned-1"))).kid == "pinned-1"


# -- credential file permissions ---------------------------------------------------------------


def test_file_store_creates_private_files(tmp_path):
    store = credentials.FileStore(tmp_path / "pro")
    store.save({"access_token": "a", "refresh_token": "r"})
    assert oct(os.stat(tmp_path / "pro").st_mode & 0o777) == "0o700"
    assert oct(os.stat(store.path).st_mode & 0o777) == "0o600"
    assert store.load() == {"access_token": "a", "refresh_token": "r"}
    store.delete()
    assert store.load() is None


def test_file_store_refuses_a_readable_file(tmp_path):
    store = credentials.FileStore(tmp_path / "pro")
    store.save({"access_token": "a"})
    os.chmod(store.path, 0o644)
    with pytest.raises(credentials.CredentialError, match="permissions"):
        store.load()


def test_file_store_refuses_a_loose_directory(tmp_path):
    store = credentials.FileStore(tmp_path / "pro")
    store.save({"access_token": "a"})
    os.chmod(store.dir, 0o755)
    with pytest.raises(credentials.CredentialError, match="permissions"):
        store.load()
    with pytest.raises(credentials.CredentialError):
        store.save({"access_token": "b"})


def test_file_store_refuses_a_symlink(tmp_path):
    store = credentials.FileStore(tmp_path / "pro")
    store.save({"access_token": "a"})
    target = tmp_path / "elsewhere"
    os.replace(store.path, target)
    os.symlink(target, store.path)
    with pytest.raises(credentials.CredentialError):
        store.load()


def test_file_store_rejects_corrupt_contents(tmp_path):
    store = credentials.FileStore(tmp_path / "pro")
    store.save({})
    fd = os.open(store.path, os.O_WRONLY | os.O_TRUNC)
    os.write(fd, b"not base64 !!")
    os.close(fd)
    with pytest.raises(credentials.CredentialError, match="corrupt"):
        store.load()


def test_default_store_honours_the_override(monkeypatch):
    assert isinstance(credentials.default_store(), credentials.FileStore)
    monkeypatch.setenv("COPSE_PRO_CREDENTIAL_STORE", "bogus")
    with pytest.raises(credentials.CredentialError):
        credentials.default_store()


def test_keychain_writes_the_secret_on_stdin_not_argv(monkeypatch):
    calls = []

    class R:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(credentials.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)) or R())
    credentials.KeychainStore().save({"access_token": "SECRET"})
    argv, kw = calls[0]
    assert argv == ["security", "-i"]
    assert "SECRET" not in " ".join(argv)
    assert credentials._encode({"access_token": "SECRET"}) in kw["input"]


# -- features() / require() and the current entitlement ---------------------------------------------


def logged_in(backend, entitlement, *, access_valid=True):
    """Stored credentials as if logged in through ``backend``."""
    t = backend.issue()
    store = credentials.default_store()
    store.save({"access_token": t["access_token"], "refresh_token": t["refresh_token"],
                "access_expires_at": time.time() + (900 if access_valid else -1),
                "entitlement": entitlement, "base_url": BASE})
    return store


def client(transport):
    return auth.Client(BASE, transport)


def test_features_fail_closed_without_credentials():
    assert license.features() == frozenset()
    assert not license.has("learning")
    with pytest.raises(LicenseError, match="not logged in"):
        license.require("learning")


def test_features_and_require_with_stored_credentials(backend, token):
    logged_in(backend, token())
    assert license.features() == {"learning", "autopilot"}
    assert license.require("learning").plan == "pro"
    with pytest.raises(NotEntitled):
        license.require("sso")


def test_features_fail_closed_on_a_loose_credentials_file(backend, token):
    store = logged_in(backend, token())
    os.chmod(store.path, 0o644)
    assert license.features() == frozenset()


def test_features_fail_closed_on_a_forged_token(backend, token, monkeypatch):
    logged_in(backend, token()[:-6] + "AAAAAA")
    monkeypatch.setattr(auth, "UrllibTransport", lambda: backend)
    backend.routes["GET /entitlement"] = [(503, {"error": "unavailable"})]
    assert license.features() == frozenset()


def test_features_fail_closed_on_a_stolen_entitlement_from_another_issuer(backend, signing_key):
    logged_in(backend, sign(signing_key, claims(iss="https://evil.test")))
    backend.routes["GET /entitlement"] = [auth.TransportError("down")]
    assert license.features() == frozenset()


def test_expired_entitlement_offline_falls_back_to_grace(backend, token):
    now = time.time()
    store = logged_in(backend, token(iat=int(now - 2 * DAY), exp=int(now - DAY)), access_valid=False)
    backend.routes["POST /token/refresh"] = [auth.TransportError("down")]
    ent = license.current(store=store, client=client(backend))
    assert ent.in_grace


def test_expired_entitlement_is_refreshed_when_online(backend, token):
    now = time.time()
    store = logged_in(backend, token(iat=int(now - 2 * DAY), exp=int(now - DAY)), access_valid=False)
    old_refresh = store.load()["refresh_token"]
    backend.plan = "enterprise"
    ent = license.current(store=store, client=client(backend))
    assert ent.plan == "enterprise" and not ent.in_grace
    assert backend.paths() == ["POST /token/refresh", "GET /entitlement"]
    creds = store.load()
    assert creds["refresh_token"] != old_refresh and license.verify(creds["entitlement"], issuer=ISS)


def test_fresh_access_token_skips_rotation(backend, token):
    now = time.time()
    store = logged_in(backend, token(iat=int(now - 2 * DAY), exp=int(now - DAY)))
    license.current(store=store, client=client(backend))
    assert backend.paths() == ["GET /entitlement"]


def test_revoked_session_fails_closed_and_forgets_credentials(backend, token):
    now = time.time()
    store = logged_in(backend, token(iat=int(now - 2 * DAY), exp=int(now - DAY)), access_valid=False)
    backend.revoked = True
    with pytest.raises(LicenseError, match="revoked"):
        license.current(store=store, client=client(backend))
    assert store.load() is None


def test_expired_past_grace_offline_is_rejected(backend, token):
    now = time.time()
    store = logged_in(backend, token(iat=int(now - 30 * DAY), exp=int(now - 29 * DAY)),
                      access_valid=False)
    backend.routes["POST /token/refresh"] = [auth.TransportError("down")]
    with pytest.raises(LicenseError, match="expired"):
        license.current(store=store, client=client(backend))


# -- development keys (JWKS from a localhost backend) ------------------------------------------


@pytest.fixture
def dev_key(monkeypatch):
    """A backend key served only by a JWKS endpoint, never pinned."""
    from cryptography.hazmat.primitives import serialization

    key = Ed25519PrivateKey.generate()
    x = b64(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    kid = license.jwk_thumbprint(x)
    fetched = []

    def fetch(base, transport=None):
        fetched.append(base)
        return {"keys": [{"kty": "OKP", "crv": "Ed25519", "x": x, "kid": kid, "alg": "EdDSA"}]}

    monkeypatch.setattr(auth, "fetch_jwks", fetch)
    return key, kid, fetched


LOCAL = "http://localhost:8000/api/copse/v1"


def test_dev_keys_are_trusted_for_a_localhost_issuer_in_dev_mode(dev_key, monkeypatch):
    key, kid, fetched = dev_key
    monkeypatch.setenv("COPSE_PRO_DEV", "1")
    t = sign(key, claims(kid=kid, iss=LOCAL))
    assert license.verify(t, issuer=LOCAL).kid == kid
    assert fetched == [LOCAL]


def test_dev_keys_are_never_used_outside_dev_mode(dev_key):
    key, kid, fetched = dev_key
    with pytest.raises(LicenseError, match="unknown key"):
        license.verify(sign(key, claims(kid=kid, iss=LOCAL)), issuer=LOCAL)
    assert fetched == []


def test_dev_keys_are_never_used_for_a_remote_issuer(dev_key, monkeypatch):
    key, kid, fetched = dev_key
    monkeypatch.setenv("COPSE_PRO_DEV", "1")
    with pytest.raises(LicenseError, match="unknown key"):
        license.verify(sign(key, claims(kid=kid)), issuer=ISS)
    assert fetched == []


def test_dev_keys_are_scoped_to_their_host(dev_key, monkeypatch):
    key, kid, fetched = dev_key
    monkeypatch.setenv("COPSE_PRO_DEV", "1")
    t = sign(key, claims(kid=kid, iss=LOCAL))
    license.verify(t, issuer=LOCAL)
    # the same token presented against the production issuer finds no key
    with pytest.raises(LicenseError):
        license.verify(t, issuer=ISS)


def test_jwks_entries_must_carry_their_own_thumbprint():
    from cryptography.hazmat.primitives import serialization

    raw = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    x = b64(raw)
    good = {"kty": "OKP", "crv": "Ed25519", "x": x, "kid": license.jwk_thumbprint(x)}
    assert list(license.parse_jwks({"keys": [good]})) == [good["kid"]]
    assert license.parse_jwks({"keys": [{**good, "kid": "chosen-by-attacker"}]}) == {}
    assert license.parse_jwks({"keys": [{**good, "crv": "P-256"}]}) == {}
    assert license.parse_jwks({"nope": 1}) == {}


def test_errors_never_contain_the_token(token, caplog):
    t = token(aud="wrong")
    with pytest.raises(LicenseError) as e:
        verify(t)
    assert t not in str(e.value) and t.split(".")[1] not in str(e.value)
    assert t not in caplog.text
