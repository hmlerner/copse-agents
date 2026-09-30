"""Plugin loading: the entry-point groups copse can be extended through.

copse itself is complete without any plugin. A plugin is a Python package
that registers an entry point in one of these groups:

``copse.learning``
    records how worker tasks turn out and suggests profiles
    (``copse.learning.LearningPlugin``)
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

Selection. The learning group is opt-in: the repo config's ``learning`` key
names the plugin, and its default ``"off"`` loads nothing. The other groups
select themselves: when exactly one plugin is installed in the group it is
used, so installing one package is all a repo needs. With several installed,
or to turn one off, the repo config's ``plugins`` object names the one to
use per group: ``"plugins": {"events": "<name>", "policy": "off"}``.

Every plugin call is guarded: a missing, broken or slow-to-import plugin is
logged and treated as absent, and never fails the operation copse was
performing. ``reset()`` forgets everything loaded (tests use it).
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points

from copse.config import RepoConfig

log = logging.getLogger(__name__)

LEARNING = "copse.learning"
EVENTS = "copse.events"
POLICY = "copse.policy"
ACCOUNT = "copse.account"
GROUPS = (LEARNING, EVENTS, POLICY, ACCOUNT)

OFF = "off"

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


def select(group: str, cfg: RepoConfig, repo_root: str) -> object | None:
    """The plugin the repo uses for ``group`` (see the module docstring for
    how one is chosen), or None."""
    if group == LEARNING:
        return load(group, cfg.learning or OFF, repo_root)
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


def reset() -> None:
    """Forget every loaded plugin and choice (the next call loads again)."""
    _loaded.clear()
    _chosen.clear()


__all__ = ["ACCOUNT", "EVENTS", "GROUPS", "LEARNING", "OFF", "POLICY", "installed", "load",
           "reset", "select", "short"]
