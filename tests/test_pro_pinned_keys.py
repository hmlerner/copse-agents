"""Each pinned key's kid must be the RFC 7638 thumbprint of that key, as the
backend derives it, or every token it signs is rejected."""
import base64
import hashlib
import json

from copse.pro.keys import PINNED_KEYS


def _thumbprint(raw_hex: str) -> str:
    x = base64.urlsafe_b64encode(bytes.fromhex(raw_hex)).rstrip(b"=").decode()
    jwk = json.dumps({"crv": "Ed25519", "kty": "OKP", "x": x}, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(hashlib.sha256(jwk.encode()).digest()).rstrip(b"=").decode()


def test_every_pinned_kid_is_its_keys_thumbprint():
    assert PINNED_KEYS, "no production key pinned"
    for kid, raw in PINNED_KEYS.items():
        assert len(bytes.fromhex(raw)) == 32
        assert _thumbprint(raw) == kid


def test_the_production_key_is_pinned_exactly():
    assert PINNED_KEYS["sQ88-LFjWYgISL3A6CcjMe9BEzThVtgGgKpbXH9vCIk"] == \
        "370e58a97ce3c85fcacf18693ed1607addefbcb63794e0fea4e615eee310fba0"
