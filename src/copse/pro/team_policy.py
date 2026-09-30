"""The ``pro`` policy plugin (``copse.policy`` group): enforce a team org's
policy on delegations and merges.

* No team entitlement (not logged in, no ``team`` feature): everything is
  allowed; this plugin only enforces what an org has set.
* With one, the org's policy is used at the entitlement's
  ``policy_version``: from the on-disk cache (``$COPSE_HOME/pro/
  policy-<org>.json``, 0600) when it is that version or newer, else fetched
  from ``GET /orgs/{org_id}/policy`` and cached. If the fetch fails, the last
  good copy is used; if there has never been one, everything is denied with
  a message saying how to fix it.
* ``check_assign`` denies a provider or model outside the allowed lists (and
  an undeclared one when a list is set). ``check_merge`` denies a merge that
  wasn't asked for by the user (the pipeline's auto-merge, a supervisor or
  any other agent) when ``require_human_review`` is set.

copse itself fails closed if this plugin raises.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass

from copse.policy import AssignInfo, Decision, MergeInfo, PolicyPlugin, allow, deny

from copse.pro._files import private_dir, read_private, write_private

log = logging.getLogger(__name__)

FEATURE = "team"
FETCH_TIMEOUT = 5.0
MAX_CACHE = 64 * 1024
ORG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class PolicyUnavailable(Exception):
    pass


@dataclass(frozen=True)
class OrgPolicy:
    org_id: str
    version: int
    allowed_providers: tuple[str, ...] | None = None
    allowed_models: tuple[str, ...] | None = None
    require_human_review: bool = False
    max_parallel_workers: int | None = None
    fetched_at: float = 0.0

    def to_json(self) -> dict:
        return {"org_id": self.org_id, "version": self.version, "fetched_at": self.fetched_at,
                "policy": {"allowed_providers": _list(self.allowed_providers),
                           "allowed_models": _list(self.allowed_models),
                           "require_human_review": self.require_human_review,
                           "max_parallel_workers": self.max_parallel_workers}}


def _list(v):
    return None if v is None else list(v)


def _names(v, what: str) -> tuple[str, ...] | None:
    if v is None:
        return None
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v) or len(v) > 256:
        raise PolicyUnavailable(f"malformed {what}")
    return tuple(v)


def parse_policy(org_id: str, body: dict, fetched_at: float | None = None) -> OrgPolicy:
    """An :class:`OrgPolicy` from the backend's answer: either flat
    ``{allowed_providers, ..., version}`` or ``{version, policy: {...}}``."""
    if not isinstance(body, dict):
        raise PolicyUnavailable("malformed policy")
    p = body.get("policy") if isinstance(body.get("policy"), dict) else body
    version = body.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise PolicyUnavailable("malformed policy version")
    if body.get("org_id") not in (None, org_id):
        raise PolicyUnavailable("policy is for another org")
    review = p.get("require_human_review", False)
    mpw = p.get("max_parallel_workers")
    if not isinstance(review, bool) or not (mpw is None or (isinstance(mpw, int)
                                                            and not isinstance(mpw, bool) and mpw >= 1)):
        raise PolicyUnavailable("malformed policy")
    return OrgPolicy(org_id=org_id, version=version,
                     allowed_providers=_names(p.get("allowed_providers"), "allowed_providers"),
                     allowed_models=_names(p.get("allowed_models"), "allowed_models"),
                     require_human_review=review, max_parallel_workers=mpw,
                     fetched_at=float(body.get("fetched_at") or fetched_at or time.time()))


def cache_path(org_id: str):
    if not ORG_RE.match(org_id):
        raise PolicyUnavailable("invalid org id")
    return private_dir() / f"policy-{org_id}.json"


def load_cached(org_id: str) -> OrgPolicy | None:
    try:
        raw = read_private(cache_path(org_id), MAX_CACHE)
        return parse_policy(org_id, json.loads(raw)) if raw else None
    except Exception as e:  # noqa: BLE001 - an unreadable cache is no cache
        log.warning("ignoring the cached team policy (%s)", e)
        return None


def save_cached(p: OrgPolicy) -> None:
    write_private(cache_path(p.org_id), json.dumps(p.to_json()).encode())


def fetch_policy(org_id: str, client=None, store=None) -> OrgPolicy:
    from copse.pro import auth, credentials

    store = store or credentials.default_store()
    if client is None:
        creds = store.load() or {}
        client = auth.Client(creds.get("base_url"), auth.UrllibTransport(timeout=FETCH_TIMEOUT))
    if not ORG_RE.match(org_id):
        raise PolicyUnavailable("invalid org id")
    try:
        status, body = auth.authed(client, store, "GET", f"/orgs/{org_id}/policy")
    except auth.AuthError as e:
        raise PolicyUnavailable(e.code) from e
    if status != 200:
        raise PolicyUnavailable(f"HTTP {status}")
    p = parse_policy(org_id, body, time.time())
    save_cached(p)
    return p


def current_policy(ent, client=None, store=None) -> OrgPolicy:
    """The policy for ``ent``'s org at (at least) ``ent.policy_version``, else
    the last good copy; raises :class:`PolicyUnavailable` if there is none."""
    cached = load_cached(ent.org_id)
    if cached is not None and cached.version >= ent.policy_version:
        return cached
    try:
        return fetch_policy(ent.org_id, client, store)
    except Exception as e:  # noqa: BLE001
        if cached is not None:
            log.warning("team policy v%s unavailable (%s); using cached v%s",
                        ent.policy_version, e, cached.version)
            return cached
        raise PolicyUnavailable(str(e)) from e


class ProPolicy(PolicyPlugin):
    def __init__(self, repo_root: str, *, store=None, client=None, entitlement=None) -> None:
        self.repo_root = repo_root
        self._store, self._client = store, client
        self._entitlement = entitlement

    def _team_entitlement(self):
        from copse.pro import license

        try:
            ent = self._entitlement() if self._entitlement else license.current(store=self._store,
                                                                                client=self._client)
        except license.LicenseError:
            return None
        except Exception:  # noqa: BLE001 - no verified entitlement: nothing to enforce
            log.warning("copse Pro: couldn't check the entitlement; no team policy applies",
                        exc_info=True)
            return None
        return ent if FEATURE in ent.features else None

    def policy(self) -> OrgPolicy | Decision:
        """The org policy to enforce, ``allow()`` with no team entitlement, or
        a denial when a policy is required but has never been fetched."""
        ent = self._team_entitlement()
        if ent is None:
            return allow()
        try:
            return current_policy(ent, self._client, self._store)
        except PolicyUnavailable as e:
            return deny(f"copse Pro team policy for org {ent.org_id} has never been fetched "
                        f"({e}); connect to the network and run `copse account org policy`")

    def check_assign(self, info: AssignInfo) -> Decision:
        p = self.policy()
        if isinstance(p, Decision):
            return p
        who = f"profile {info.profile!r}" if info.profile else "this delegation"
        if p.allowed_providers is not None and info.provider not in p.allowed_providers:
            got = f"provider {info.provider!r}" if info.provider else "an undeclared provider"
            return deny(f"{who} uses {got}; org {p.org_id} allows only "
                        f"{', '.join(p.allowed_providers) or 'no providers'}")
        if p.allowed_models is not None and info.model not in p.allowed_models:
            got = f"model {info.model!r}" if info.model else "no declared model"
            return deny(f"{who} uses {got}; org {p.org_id} allows only "
                        f"{', '.join(p.allowed_models) or 'no models'} (set `model` in the profile)")
        return allow()

    def check_merge(self, info: MergeInfo) -> Decision:
        p = self.policy()
        if isinstance(p, Decision):
            return p
        if p.require_human_review and info.actor != "user":
            return deny(f"org {p.org_id} requires a human review before merging; "
                        "a person must run this merge (not the pipeline or an agent)")
        return allow()


def make(repo_root: str) -> ProPolicy:
    return ProPolicy(repo_root)
