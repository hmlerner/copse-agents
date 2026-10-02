"""Settings sync (copse Pro and higher): this person's user-wide preferences in
``~/.copse/config.json`` follow them across machines through ``GET/PUT
/me/settings``.

* Only the keys in :data:`SYNCED_KEYS` ever leave the machine, and only
  str (<= 32 chars), bool or int values; everything else in the file stays local.
* Last writer wins. ``$COPSE_HOME/pro/settings-sync.json`` (0600) remembers the
  server's ``updated_at`` and the synced values as of the last sync. If the
  local values differ from that snapshot, they were changed since: they are
  pushed. Otherwise a newer server copy is written into the config file
  (other keys are kept).
* Needs the ``settings_sync`` feature in the verified entitlement (checked
  offline), and never runs in air-gap mode. Offline, unentitled or on any
  error nothing happens and nothing raises: a later sync catches up.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field

from copse.pro._files import private_dir, read_private, write_private

log = logging.getLogger(__name__)

FEATURE = "settings_sync"
SYNCED_KEYS = ("delegation", "sidebar", "message_delivery", "pr_footer", "delete_merged_branches",
               "max_agents", "autopilot", "plan_first", "stale_after", "usage_limit",
               "review_rounds")
TIMEOUT = 3.0
MAX_STATE = 64 * 1024
PATH = "/me/settings"


@dataclass
class Result:
    action: str                       # "pulled", "pushed", "unchanged" or "skipped"
    changes: dict = field(default_factory=dict)   # key -> (old, new); None means absent
    reason: str = ""                  # why it was skipped


def _valid(v) -> bool:
    return isinstance(v, (bool, int)) or (isinstance(v, str) and len(v) <= 32)


def _subset(data: dict) -> dict:
    """The allowlisted, well-typed part of ``data``: all that may be sent."""
    return {k: data[k] for k in SYNCED_KEYS if k in data and _valid(data[k])}


def _state_path():
    return private_dir() / "settings-sync.json"


def _load_state() -> dict | None:
    try:
        raw = read_private(_state_path(), MAX_STATE)
        st = json.loads(raw) if raw else None
        if isinstance(st, dict) and isinstance(st.get("settings"), dict):
            return st
    except Exception:  # noqa: BLE001 - an unreadable state is no state
        pass
    return None


def _save_state(updated_at, settings: dict) -> None:
    write_private(_state_path(), json.dumps({"updated_at": updated_at, "settings": settings}).encode())


def entitled(store=None) -> bool:
    from copse import airgap

    if airgap.enabled():
        return False
    try:
        from copse.pro import license

        return FEATURE in license.current(refresh=False, store=store).features
    except Exception:  # noqa: BLE001 - no verified entitlement: sync nothing
        return False


def _call(client, store, method: str, form=None):
    from copse.pro import auth, credentials

    store = store or credentials.default_store()
    if client is None:
        creds = store.load() or {}
        client = auth.Client(creds.get("base_url"), auth.UrllibTransport(timeout=TIMEOUT))
    status, body = auth.authed(client, store, method, PATH, form)
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("settings"), dict):
        raise RuntimeError(f"HTTP {status}")
    ua = body.get("updated_at")
    return _subset(body["settings"]), (float(ua) if isinstance(ua, (int, float)) and not isinstance(ua, bool) else None)


def _sync(client=None, store=None) -> Result:
    from copse import config
    from copse.pro import auth

    if not entitled(store):
        return Result("skipped", reason="not entitled to settings sync (or air-gap mode)")
    local = _subset(config.user_settings())
    state = _load_state()
    server, ua = _call(client, store, "GET")

    def put() -> Result:
        got, stamp = _call(client, store, "PUT", auth.JSONBody({"settings": local}))
        _save_state(stamp, local)
        return Result("pushed", {k: (got.get(k), v) for k, v in local.items() if got.get(k) != v})

    if state is not None and _subset(state["settings"]) != local:
        return put()                                   # changed here since the last sync
    if ua is None or (state is not None and state.get("updated_at") is not None
                      and ua <= state["updated_at"]):
        if state is None and local:
            return put()                               # first sync, nothing on the server yet
        if state is None:
            _save_state(ua, local)
        return Result("unchanged")
    if state is None and server == local:
        _save_state(ua, local)
        return Result("unchanged")
    # The server's copy is newer: adopt it, dropping only keys we had synced before.
    merged = config._read_json(config.user_config_path())
    previous = set(_subset(state["settings"])) if state else set()
    changes = {}
    for k in previous - set(server):
        if k in merged:
            changes[k] = (merged.pop(k), None)
    for k, v in server.items():
        changes[k] = (merged.get(k), v)
        merged[k] = v
    changes = {k: c for k, c in changes.items() if c[0] != c[1]}
    if changes:
        path = config.user_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    _save_state(ua, server)
    return Result("pulled" if changes else "unchanged", changes)


def sync(client=None, store=None) -> Result:
    """Sync now (a pull, or a push if this machine changed something). Never
    raises: a failure is a ``skipped`` result saying why."""
    try:
        return _sync(client, store)
    except Exception as e:  # noqa: BLE001 - offline, unauthorised, a bad answer...
        log.info("settings sync skipped (%s)", e)
        return Result("skipped", reason=str(e) or type(e).__name__)


def pull(client=None, store=None) -> Result:
    """Apply newer server values to ``~/.copse/config.json``; a local change
    made since the last sync is pushed instead."""
    return sync(client, store)


def push(client=None, store=None) -> Result:
    """Send this machine's synced keys now (last writer wins)."""
    try:
        if not entitled(store):
            return Result("skipped", reason="not entitled to settings sync (or air-gap mode)")
        from copse import config
        from copse.pro import auth

        local = _subset(config.user_settings())
        got, stamp = _call(client, store, "PUT", auth.JSONBody({"settings": local}))
        _save_state(stamp, local)
        return Result("pushed")
    except Exception as e:  # noqa: BLE001
        log.info("settings push skipped (%s)", e)
        return Result("skipped", reason=str(e) or type(e).__name__)


def push_soon(timeout: float = TIMEOUT) -> None:
    """Push in the background and wait at most ``timeout`` seconds for it
    (a slow network never holds the caller). Never raises."""
    try:
        if not entitled():
            return
        t = threading.Thread(target=push, daemon=True, name="copse-settings-sync")
        t.start()
        t.join(timeout)
    except Exception:  # noqa: BLE001
        pass
