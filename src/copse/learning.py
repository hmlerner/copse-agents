"""Hosted learning: which profiles fit which tasks, learned by copse Pro's API.

Nothing is learned on this machine, and there is no plugin interface for it:
no installed package can act as a learner. copse reports what happened to each
worker task (a review verdict, an escalation to the supervisor, a merge, the
worktree removed unmerged) to the hosted learner (``copse.pro.learning``), and
asks it to pick a profile when ``assign``/``handoff`` get none and no
milestone names one.

The repo config's ``learning`` key is ``"auto"`` (the default: hosted learning
when the copse Pro entitlement includes it, else off), ``"cloud"`` or
``"off"``; any other value means off.

Every call into the learner is guarded: an unreachable or failing API never
fails the review, merge or delegation it was told about.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from copse import plugins
from copse.config import RepoConfig
from copse.db import DB, Agent, Workspace

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskInfo:
    """A task as copse describes it to a plugin. ``agent_id`` and the
    profile fields are empty when a profile is still being chosen."""
    repo_root: str
    task: str = ""
    files: tuple[str, ...] = ()
    weight: str | None = None
    agent_id: str | None = None
    profile: str | None = None
    provider: str | None = None
    model: str | None = None
    started_at: float | None = None


@dataclass(frozen=True)
class Outcome:
    """One event in a worker task's life. ``event`` is "review" (with
    ``approved``), "escalated" (the supervisor was asked to step in),
    "merged" or "removed_unmerged" (the task is over)."""
    event: str
    approved: bool | None = None
    checks_passed: bool = False
    tokens: int = 0
    wall_seconds: float = 0.0
    at: float = field(default_factory=time.time)


class LearningPlugin(ABC):
    """What a learning plugin implements."""

    @abstractmethod
    def record(self, task: TaskInfo, outcome: Outcome) -> None:
        """Take note of ``outcome`` for ``task``."""

    @abstractmethod
    def suggest(self, task: TaskInfo, candidates: list[str]) -> str | None:
        """One of ``candidates`` for ``task``, or None to leave it to copse."""

    def report(self, reset: bool = False) -> str:
        """What ``copse learning`` prints (``--reset``: forget this repo)."""
        return "this learning plugin has nothing to report"


_learners: dict[str, LearningPlugin] = {}


def plugin(cfg: RepoConfig, repo_root: str) -> LearningPlugin | None:
    """The hosted learner (copse Pro's ``CloudLearner``) when the repo's
    ``learning`` setting resolves to ``"cloud"``, else None. Never loads an
    installed package."""
    if plugins.learning_name(cfg) != plugins.CLOUD:
        return None
    if repo_root not in _learners:
        from copse.pro.learning import CloudLearner

        _learners[repo_root] = CloudLearner(repo_root)
    return _learners[repo_root]


def reset() -> None:
    """Forget the learners made so far (tests use it)."""
    _learners.clear()


def _task_files(db: DB, worker: Agent, repo_root: str) -> tuple[tuple[str, ...], str | None]:
    """The files and the weight declared for ``worker``'s task."""
    import json

    for t in db.list_tasks(repo_root):
        if t.agent_id == worker.id:
            files: tuple[str, ...] = ()
            if t.files:
                try:
                    files = tuple(f for f in json.loads(t.files) if isinstance(f, str))
                except ValueError:
                    pass
            return files, t.weight
    return (), None


def _tokens(db: DB, worker: Agent) -> int:
    try:
        from copse.usage import agent_usage

        u = agent_usage(db, worker)
        return int(u.total) if u else 0
    except Exception:
        return 0


def _task_info(db: DB, worker: Agent, ws: Workspace) -> TaskInfo:
    from copse.profiles import load_profile

    model = None
    try:
        model = load_profile(worker.profile, ws.repo_root).model
    except Exception:
        pass
    files, weight = _task_files(db, worker, ws.repo_root)
    return TaskInfo(
        repo_root=ws.repo_root, task=worker.task or "", files=files, weight=weight,
        agent_id=worker.id, profile=worker.profile, provider=worker.provider, model=model,
        started_at=worker.created_at,
    )


def note(db: DB, cfg: RepoConfig, worker: Agent | None, ws: Workspace, *,
         approved: bool | None = None, escalated: bool = False,
         merged: bool | None = None, checks_passed: bool = False) -> None:
    """Tell the plugin about one event in ``worker``'s task. Does nothing
    without a plugin, and never raises."""
    try:
        p = plugin(cfg, ws.repo_root)
        if p is None or worker is None:
            return
        info = _task_info(db, worker, ws)
        if approved is not None:
            p.record(info, Outcome("review", approved=approved))
        if escalated:
            p.record(info, Outcome("escalated"))
        if merged is not None:
            p.record(info, Outcome(
                "merged" if merged else "removed_unmerged", checks_passed=checks_passed,
                tokens=_tokens(db, worker), wall_seconds=max(0.0, time.time() - worker.created_at),
            ))
    except Exception:
        log.exception("copse: the learning plugin failed to record an outcome")


def choose(db: DB, cfg: RepoConfig, repo_root: str, task: str | None = None,
           files: list[str] | None = None, candidates: list[str] | None = None,
           weight: str | None = None) -> str | None:
    """The plugin's pick among ``candidates`` (default: the repo's
    ``learning_candidates``), or None without a plugin or candidates."""
    names = [c for c in (candidates if candidates is not None else cfg.learning_candidates)
             if isinstance(c, str)]
    if not names:
        return None
    try:
        p = plugin(cfg, repo_root)
        if p is None:
            return None
        pick = p.suggest(TaskInfo(repo_root=repo_root, task=task or "",
                                  files=tuple(files or ()), weight=weight), names)
        return pick if pick in names else None
    except Exception:
        log.exception("copse: the learning plugin failed to suggest a profile")
        return None


__all__ = ["LearningPlugin", "Outcome", "TaskInfo", "choose", "note", "plugin", "reset"]
