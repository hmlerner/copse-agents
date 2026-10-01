"""Air-gap mode (copse Enterprise): no outbound traffic, local models only.

On when ``"airgap": true`` is in ``.copse/config.json`` (``load_repo_config``
arms this module for the rest of the process) or ``COPSE_AIRGAP=1`` is in
the environment. Every path that would leave the machine asks here first:

* ``copse.pro`` refuses every backend request (login, refresh, entitlement,
  JWKS, learning, team policy, audit events); see :func:`guard`. Learning
  falls back to the local learner, the team policy comes from an offline
  file (``.copse/policy.json``, same schema as the org policy), events are
  not recorded, and the entitlement is the offline license installed with
  ``copse account license install`` (never refreshed).
* Delegation only reaches profiles whose model is on this machine or the
  private network: the native provider with a ``base_url`` on a loopback or
  private address, or a profile marked ``local: true``. Hosted providers
  (claude, codex, antigravity, ...) are refused; see :func:`check_profile`.

Air-gap mode is a feature of the copse Enterprise plan (``airgap`` in the
entitlement). Without it, copse still blocks everything (fail safe) and
warns that the plan doesn't include it.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import urllib.parse

log = logging.getLogger(__name__)

ENV = "COPSE_AIRGAP"
FEATURE = "airgap"
POLICY_FILE = "policy.json"          # the offline team policy, under .copse/
LOCAL_PROVIDERS = ("native",)        # providers whose endpoint can be checked for locality
_TRUE = ("1", "true", "yes", "on")

_armed = False
_warned = False


class AirGapError(Exception):
    """An outbound request was refused because air-gap mode is on."""


# -- state ------------------------------------------------------------------------------------


def arm() -> None:
    """Turn air-gap mode on for the rest of this process (a repo config with
    ``"airgap": true`` was loaded)."""
    global _armed
    _armed = True
    warn_if_unlicensed()


def reset() -> None:
    """Test-only: forget :func:`arm`."""
    global _armed, _warned
    _armed = _warned = False


def enabled(cfg=None) -> bool:
    """Whether air-gap mode is on: the environment, an armed process, or
    ``cfg.airgap`` (a ``RepoConfig``) when one is given."""
    if _armed or (os.environ.get(ENV) or "").strip().lower() in _TRUE:
        return True
    return bool(getattr(cfg, "airgap", False))


def source(cfg=None) -> str:
    """Where air-gap mode was turned on, for messages."""
    if (os.environ.get(ENV) or "").strip().lower() in _TRUE:
        return f"{ENV}=1"
    if _armed or getattr(cfg, "airgap", False):
        return '"airgap": true in .copse/config.json'
    return "off"


def licensed() -> bool:
    """Whether the entitlement (the offline license, in air-gap mode) includes
    the ``airgap`` feature. Offline check; fails closed."""
    from copse.pro import license

    return license.has(FEATURE)


def warning() -> str | None:
    """The warning to show when air-gap mode is on without the plan for it."""
    if not enabled() or licensed():
        return None
    return ("air-gap mode is on but your copse plan doesn't include it (feature "
            f"{FEATURE!r}): all outbound traffic is still blocked, as a safety measure. "
            "Install a copse Enterprise license with `copse account license install <file>`.")


def warn_if_unlicensed() -> None:
    global _warned
    if _warned:
        return
    _warned = True
    msg = warning()
    if msg:
        log.warning("copse: %s", msg)


# -- outbound requests --------------------------------------------------------------------------


def is_local_host(host: str | None) -> bool:
    """Loopback or private-network: ``localhost`` (or ``*.localhost``), an IP
    in 127/8, ::1, 10/8, 172.16/12, 192.168/16, or another private or
    link-local range. A hostname that isn't an IP literal is not local (it
    can't be checked offline); mark the profile ``local: true`` instead."""
    host = (host or "").strip().lower().strip("[]")
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def is_local_url(url: str | None) -> bool:
    try:
        return is_local_host(urllib.parse.urlsplit(url or "").hostname)
    except ValueError:
        return False


def guard(url: str | None, what: str = "request") -> None:
    """Raise :class:`AirGapError` for a ``what`` to ``url`` that would leave
    the machine while air-gap mode is on. Loopback and private-network URLs
    pass. A missing or unparsable URL is refused."""
    if not enabled():
        return
    if is_local_url(url):
        return
    try:
        host = urllib.parse.urlsplit(url or "").hostname or "?"
    except ValueError:
        host = "?"
    raise AirGapError(f"air-gap mode: refusing the {what} to {host} "
                      f"(air-gap mode is on via {source()})")


# -- profiles -------------------------------------------------------------------------------------


def profile_allowed(profile) -> tuple[bool, str]:
    """Whether ``profile`` (a ``copse.profiles.Profile``) may run in air-gap
    mode: ``(True, "")`` or ``(False, why)``."""
    if getattr(profile, "local", False):
        return True, ""
    provider = getattr(profile, "provider", None) or "claude"
    base_url = getattr(profile, "base_url", None)
    if provider in LOCAL_PROVIDERS:
        if is_local_url(base_url):
            return True, ""
        where = base_url or "no base_url"
        return False, (f"its model endpoint ({where}) is not on this machine or the private "
                       "network; use a loopback or private-network base_url, or mark the "
                       "profile `local: true` if it is")
    return False, (f"provider {provider!r} is a hosted service; only local models (the native "
                   "provider on a loopback or private-network base_url, or a profile marked "
                   "`local: true`) may run")


def check_profile(name: str | None, repo_root: str | None) -> tuple[bool, str]:
    """:func:`profile_allowed` for the profile ``name`` in ``repo_root``. A
    profile that can't be loaded is refused (fail safe)."""
    if not name:
        return False, "air-gap mode: delegations must name a local profile"
    try:
        from copse.profiles import load_profile

        p = load_profile(name, repo_root)
    except Exception as e:  # noqa: BLE001 - unknown profile: refuse
        return False, f"air-gap mode: profile {name!r} couldn't be loaded ({e})"
    ok, why = profile_allowed(p)
    if ok:
        return True, ""
    return False, f"air-gap mode: profile {name!r} is refused: {why}"


def hosted_profiles(repo_root: str | None) -> list[str]:
    """Names of the configured profiles that air-gap mode refuses."""
    from copse.profiles import list_profiles

    try:
        profiles = list_profiles(repo_root)
    except Exception:  # noqa: BLE001
        return []
    return [p.name for p in profiles if not profile_allowed(p)[0]]


# -- the offline policy file -------------------------------------------------------------------------


def policy_path(repo_root: str | None):
    """``.copse/policy.json`` for ``repo_root`` (the main worktree's, for a
    linked worktree)."""
    from pathlib import Path

    from copse.config import CONFIG_DIR, config_root

    root = config_root(repo_root) if repo_root else Path.cwd()
    return Path(root) / CONFIG_DIR / POLICY_FILE


__all__ = ["ENV", "FEATURE", "POLICY_FILE", "AirGapError", "arm", "check_profile", "enabled",
           "guard", "hosted_profiles", "is_local_host", "is_local_url", "licensed", "policy_path",
           "profile_allowed", "reset", "source", "warning"]
