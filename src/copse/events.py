"""Events: tell an installed plugin what happens to worker tasks.

copse emits an ``Event`` when a task is delegated (``assign`` or
``handoff``), when a reviewer gives a verdict (``review``), when the
supervisor has to step in (``escalated``), when a branch is merged
(``merge``) and when a worktree is removed (``remove``). An event names the
repo, the worker (id, branch, profile, provider and model) and the actor
that caused it (an agent id, or ``"user"`` for a command run by hand), with
a timestamp. It carries no diff and no prompt or task text.

A plugin is a package registering an entry point in the ``copse.events``
group whose object is a factory ``make(repo_root) -> EventsPlugin | None``;
see ``copse.plugins`` for how one is selected. Emitting is guarded: a
plugin that's missing or raises never fails the operation being reported.
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
KINDS = ("assign", "handoff", "review", "escalated", "merge", "remove")


@dataclass(frozen=True)
class Event:
    """One thing that happened. ``approved`` is set for a ``review``;
    ``merged`` for a ``remove`` (whether the branch had been merged)."""
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


class EventsPlugin(ABC):
    """What an events plugin implements."""

    @abstractmethod
    def emit(self, event: Event) -> None:
        """Take note of ``event``."""


def plugin(cfg: RepoConfig, repo_root: str) -> EventsPlugin | None:
    return plugins.select(GROUP, cfg, repo_root)  # type: ignore[return-value]


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
    """Tell the repo's events plugin that ``kind`` happened to ``worker`` in
    ``ws``. Does nothing without a plugin, and never raises."""
    try:
        p = plugin(cfg, ws.repo_root)
        if p is None:
            return
        who = actor.id if isinstance(actor, Agent) else (actor or "user")
        p.emit(Event(
            kind=kind, repo_root=ws.repo_root, agent_id=worker.id if worker else None,
            branch=ws.branch, profile=worker.profile if worker else None,
            provider=worker.provider if worker else None,
            model=_model(worker.profile if worker else None, ws.repo_root),
            actor=who, approved=approved, merged=merged,
        ))
    except Exception:
        log.exception("copse: the events plugin failed on a %s event", kind)


__all__ = ["GROUP", "KINDS", "Event", "EventsPlugin", "emit", "plugin"]
