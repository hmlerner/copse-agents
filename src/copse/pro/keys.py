"""Pinned Ed25519 public keys that may sign copse Pro entitlements, by ``kid``.

Only keys listed here are trusted; a token naming any other ``kid`` is
rejected. There is deliberately no way to add a key at runtime from the
environment, a config file or the network.

Rotation: add the next key here and release *before* the backend signs with
it; switch the backend to the new ``kid``; drop the old ``kid`` in a later
release, once every token it signed is past ``exp`` + ``license.MAX_GRACE``.
To revoke a compromised key, release with its ``kid`` removed (installs that
don't upgrade keep trusting it until they do, which is inherent to offline
verification -- short token lifetimes bound the exposure).

Values are the raw 32-byte public keys, hex-encoded.
"""

from __future__ import annotations

PINNED_KEYS: dict[str, str] = {
    # pawdelta-web production signing key (KMS alias/pawdelta-web-copse-signing,
    # ECC_NIST_EDWARDS25519), created 2026-09-30. kid is its RFC 7638 thumbprint.
    "sQ88-LFjWYgISL3A6CcjMe9BEzThVtgGgKpbXH9vCIk":
        "370e58a97ce3c85fcacf18693ed1607addefbcb63794e0fea4e615eee310fba0",
}
