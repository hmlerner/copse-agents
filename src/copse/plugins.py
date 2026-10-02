"""Plugin loading: the entry-point groups copse can be extended through.

copse itself is complete without any plugin. A plugin is a Python package
that registers an entry point in one of these groups:

``copse.events``
    is told what happens (a task started, a review verdict, a merge, a
    worktree removed) (``copse.events.EventsPlugin``)
``copse.policy``
    may refuse a delegation or a merge with a reason
    (``copse.policy.PolicyPlugin``)
``copse.account``
    handles ``copse account ...`` (``copse.account.AccountPlugin``)

The entry point's object is a factory ``make(repo_root: str) -> plugin | None``,
called once per repo per process (the result is cached). Install a plugin
next to copse, e.g. ``uv tool install copse-agents --with <plugin>``.

Learning is not a plugin group: it is reachable only through the hosted
copse Pro API, and no installed package can act as a learner. The repo
config's ``learning`` key is ``"auto"`` (the default: ``"cloud"`` when the
verified entitlement includes the ``learning`` feature, else ``"off"``),
``"cloud"`` or ``"off"``; anything else means ``"off"`` (``learning_name``).

Selection. The groups select themselves: when exactly one plugin is installed in the
group it is used, so installing one package is all a repo needs. With
several installed, or to turn one off, the repo config's ``plugins`` object
names the one to use per group: ``"plugins": {"events": "<name>", "policy":
"off"}``. The events group is different: it fans out (``select_all``), so
every installed events plugin hears every event unless the config names the
ones to use (one name, or several separated by commas). copse's own Pro
plugins (``pro`` in the events, policy and account groups, ``audit`` in
events) are always installed and do nothing without an entitlement.

Every plugin call is guarded: a missing, broken or slow-to-import plugin is
logged and treated as absent, and never fails the operation copse was
performing. ``reset()`` forgets everything loaded (tests use it).
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points

from copse.config import RepoConfig

log = logging.getLogger(__name__)

EVENTS = "copse.events"
POLICY = "copse.policy"
ACCOUNT = "copse.account"
GROUPS = (EVENTS, POLICY, ACCOUNT)

OFF = "off"
AUTO = "auto"                  # learning: "cloud" when entitled, else off
CLOUD = "cloud"
LEARNING_FEATURE = "learning"  # the entitlement feature that turns "auto" into "cloud"

# (group, entry point name, repo root) -> the plugin, or None when it couldn't load.
_loaded: dict[tuple[str, str, str], object | None] = {}
# (group, repo root) -> the entry point name chosen when the config names none.
_chosen: dict[tuple[str, str], str | None] = {}


def short(group: str) -> str:
    """``"events"`` for ``"copse.events"``: the key in the ``plugins`` config."""
    return group.removeprefix("copse.")


def installed(group: str) -> list[str]:
    """The names of the plugins installed in ``group``."""
    try:
        return sorted({e.name for e in entry_points(group=group)})
    except Exception:
        log.exception("copse: couldn't list the plugins in %s", group)
        return []


def load(group: str, name: str, repo_root: str) -> object | None:
    """The plugin ``name`` in ``group`` for ``repo_root``: loaded once per
    repo per process, None when it's ``"off"``, not installed or broken."""
    name = (name or OFF).strip()
    if name == OFF:
        return None
    key = (group, name, repo_root)
    if key not in _loaded:
        _loaded[key] = None
        try:
            ep = next((e for e in entry_points(group=group) if e.name == name), None)
            if ep is None:
                log.warning("copse: no %s plugin named %r is installed", short(group), name)
            else:
                _loaded[key] = ep.load()(repo_root)
        except Exception:
            log.exception("copse: couldn't load the %s plugin %r", short(group), name)
    return _loaded[key]


def auto_learning() -> str:
    """What ``"learning": "auto"`` means right now: ``"cloud"`` when the
    verified copse Pro entitlement includes hosted learning, else ``"off"``.
    Fails closed (off) on any problem."""
    try:
        from copse.pro import license

        return CLOUD if LEARNING_FEATURE in license.current().features else OFF
    except Exception:  # noqa: BLE001 - not logged in, no entitlement, anything at all
        return OFF


def learning_name(cfg: RepoConfig) -> str:
    """The learning mode ``cfg`` selects, with ``"auto"`` resolved: ``"cloud"``
    or ``"off"`` (any other configured value is off)."""
    name = (cfg.learning or OFF).strip()
    if name == AUTO:
        return auto_learning()
    return name if name == CLOUD else OFF


def select(group: str, cfg: RepoConfig, repo_root: str) -> object | None:
    """The plugin the repo uses for ``group`` (see the module docstring for
    how one is chosen), or None."""
    configured = cfg.plugins.get(short(group)) if isinstance(cfg.plugins, dict) else None
    if isinstance(configured, str) and configured.strip():
        return load(group, configured, repo_root)
    key = (group, repo_root)
    if key not in _chosen:
        names = installed(group)
        if len(names) > 1:
            log.warning(
                "copse: several %s plugins are installed (%s); name one in .copse/config.json "
                'as "plugins": {"%s": "<name>"}', short(group), ", ".join(names), short(group))
        _chosen[key] = names[0] if len(names) == 1 else None
    name = _chosen[key]
    return load(group, name, repo_root) if name else None


def select_all(group: str, cfg: RepoConfig, repo_root: str) -> list[object]:
    """Every plugin the repo uses for ``group`` (the events group fans out
    this way): the ones the config names (one name, several separated by
    commas, or ``"off"`` for none), else every plugin installed in the group.
    Plugins that can't load are left out."""
    configured = cfg.plugins.get(short(group)) if isinstance(cfg.plugins, dict) else None
    if isinstance(configured, (list, tuple)):
        names = [n.strip() for n in configured if isinstance(n, str) and n.strip()]
    elif isinstance(configured, str) and configured.strip():
        names = [n.strip() for n in configured.split(",") if n.strip()]
    else:
        names = installed(group)
    out: list[object] = []
    for name in names:
        p = load(group, name, repo_root)
        if p is not None:
            out.append(p)
    return out


def reset() -> None:
    """Forget every loaded plugin and choice (the next call loads again)."""
    _loaded.clear()
    _chosen.clear()
    from copse import learning

    learning.reset()


__all__ = ["ACCOUNT", "AUTO", "CLOUD", "EVENTS", "GROUPS", "OFF", "POLICY",
           "auto_learning", "installed", "learning_name", "load", "reset", "select",
           "select_all", "short"]
