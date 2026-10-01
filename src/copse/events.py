"""Events: tell an installed plugin what happens to worker tasks.

copse emits an ``Event`` when a task is delegated (``assign`` or
``handoff``), when a reviewer gives a verdict (``review``), when the
supervisor has to step in (``escalated``), when a branch is merged
(``merge``), when a worktree is removed (``remove``), and when the repo's
policy refuses a delegation or a merge (``deny_assign``, ``deny_merge``,
with the reason). An event names the repo, the worker (id, branch, profile,
provider and model), the workspace and the actor that caused it (an agent
id, or ``"user"`` for a command run by hand), with a timestamp. It carries
no diff and no prompt or task text.

A plugin is a package registering an entry point in the ``copse.events``
group whose object is a factory ``make(repo_root) -> EventsPlugin | None``;
see ``copse.plugins`` for how plugins are selected. Every selected plugin
hears every event (copse's own ``pro`` team feed and ``audit`` chain run
side by side). Emitting is guarded: a plugin that's missing or raises never
fails the operation being reported, and never keeps another plugin from
hearing the event.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from copse import plugins
from copse.config import RepoConfig
from copse.db import Agent, Workspace

log = logging.getLogger(__name__)

GROUP = plugins.EVENTS
KINDS = ("assign", "handoff", "review", "escalated", "merge", "remove", "deny_assign",
         "deny_merge")


@dataclass(frozen=True)
class Event:
    """One thing that happened. ``approved`` is set for a ``review``;
    ``merged`` for a ``remove`` (whether the branch had been merged);
    ``reason`` for a ``deny_*`` (what the policy said)."""
    kind: str
    repo_root: str
    agent_id: str | None = None
    branch: str | None = None
    profile: str | None = None
    provider: str | None = None
    model: str | None = None
    actor: str | None = None
    at: float = field(default_factory=time.time)
    approved: bool | None = None
    merged: bool | None = None
    workspace_id: str | None = None
    reason: str | None = None


class EventsPlugin(ABC):
    """What an events plugin implements."""

    @abstractmethod
    def emit(self, event: Event) -> None:
        """Take note of ``event``."""


def plugins_for(cfg: RepoConfig, repo_root: str) -> list[EventsPlugin]:
    """Every events plugin the repo uses (see ``copse.plugins.select_all``)."""
    return plugins.select_all(GROUP, cfg, repo_root)  # type: ignore[return-value]


def plugin(cfg: RepoConfig, repo_root: str) -> EventsPlugin | None:
    """The first of the repo's events plugins, or None."""
    found = plugins_for(cfg, repo_root)
    return found[0] if found else None


def _dispatch(cfg: RepoConfig, event: Event) -> None:
    """Hand ``event`` to every plugin; one that raises doesn't stop the rest."""
    for p in plugins_for(cfg, event.repo_root):
        try:
            p.emit(event)
        except Exception:
            log.exception("copse: the events plugin %s failed on a %s event",
                          type(p).__name__, event.kind)


def _model(profile: str | None, repo_root: str) -> str | None:
    if not profile:
        return None
    try:
        from copse.profiles import load_profile

        return load_profile(profile, repo_root).model
    except Exception:
        return None


def emit(cfg: RepoConfig, kind: str, ws: Workspace, worker: Agent | None = None, *,
         actor: Agent | str | None = None, approved: bool | None = None,
         merged: bool | None = None) -> None:
    """Tell the repo's events plugins that ``kind`` happened to ``worker`` in
    ``ws``. Does nothing without a plugin, and never raises."""
    try:
        who = actor.id if isinstance(actor, Agent) else (actor or "user")
        _dispatch(cfg, Event(
            kind=kind, repo_root=ws.repo_root, agent_id=worker.id if worker else None,
            branch=ws.branch, profile=worker.profile if worker else None,
            provider=worker.provider if worker else None,
            model=_model(worker.profile if worker else None, ws.repo_root),
            actor=who, approved=approved, merged=merged, workspace_id=ws.id,
        ))
    except Exception:
        log.exception("copse: the events plugin failed on a %s event", kind)


def emit_denial(cfg: RepoConfig, repo_root: str, what: str, reason: str, *,
                branch: str | None = None, profile: str | None = None,
                provider: str | None = None, model: str | None = None,
                actor: str | None = None, agent_id: str | None = None,
                workspace_id: str | None = None) -> None:
    """Tell the repo's events plugins that the policy refused a ``what``
    (``"assign"`` or ``"merge"``) with ``reason``. Never raises."""
    try:
        _dispatch(cfg, Event(
            kind=f"deny_{what}", repo_root=repo_root, agent_id=agent_id, branch=branch,
            profile=profile, provider=provider, model=model, actor=actor or "user",
            workspace_id=workspace_id, reason=reason,
        ))
    except Exception:
        log.exception("copse: the events plugin failed on a deny_%s event", what)


__all__ = ["GROUP", "KINDS", "Event", "EventsPlugin", "emit", "emit_denial", "plugin",
           "plugins_for"]
