"""Policy: let an installed plugin refuse a delegation or a merge.

Before ``assign``/``handoff`` start (or queue) a worker, copse asks the
repo's policy plugin ``check_assign``; before ``merge_workspace`` (and the
pipeline's own merge) touches the base branch, ``check_merge``. Each
returns a ``Decision``: ``allow()``, or ``deny(reason)``, and the reason is
what the caller sees ("Not started: ..." / "Not merged: ...").

A plugin is a package registering an entry point in the ``copse.policy``
group whose object is a factory ``make(repo_root) -> PolicyPlugin | None``;
see ``copse.plugins`` for how one is selected. Without a plugin everything
is allowed. A policy fails closed: once a plugin is in play, an exception,
an unreadable answer, or a plugin the repo config names that can't be loaded
refuses the delegation or merge, since a policy that errors open (skipping a
required human review, say) is worse than one that blocks.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from copse import plugins
from copse.config import RepoConfig
from copse.db import DB, Agent, Workspace

log = logging.getLogger(__name__)

GROUP = plugins.POLICY


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""


def allow() -> Decision:
    return Decision(True)


def deny(reason: str) -> Decision:
    return Decision(False, reason.strip() or "refused by the repo's policy")


@dataclass(frozen=True)
class AssignInfo:
    """A delegation about to start, as copse describes it to the plugin.
    ``mode`` is "assign" or "handoff"; ``actor`` the caller's agent id."""
    repo_root: str
    task: str = ""
    files: tuple[str, ...] = ()
    weight: str | None = None
    profile: str | None = None
    provider: str | None = None
    model: str | None = None
    mode: str = "assign"
    branch: str | None = None
    actor: str | None = None
    running_workers: int | None = None  # this repo's workers still at work, before this one


@dataclass(frozen=True)
class MergeInfo:
    """A merge about to happen: ``branch`` into ``base_branch``, the work of
    ``agent_id`` (the workspace's worker, if any), asked for by ``actor``."""
    repo_root: str
    workspace_id: str
    branch: str
    base_branch: str | None
    agent_id: str | None = None
    profile: str | None = None
    provider: str | None = None
    model: str | None = None
    actor: str | None = None


class PolicyPlugin(ABC):
    """What a policy plugin implements."""

    @abstractmethod
    def check_assign(self, info: AssignInfo) -> Decision:
        """Whether this delegation may start."""

    @abstractmethod
    def check_merge(self, info: MergeInfo) -> Decision:
        """Whether this branch may be merged into its base."""


def plugin(cfg: RepoConfig, repo_root: str) -> PolicyPlugin | None:
    return plugins.select(GROUP, cfg, repo_root)  # type: ignore[return-value]


def _decide(what: str, cfg: RepoConfig, repo_root: str, ask) -> Decision:
    configured = cfg.plugins.get(plugins.short(GROUP)) if isinstance(cfg.plugins, dict) else None
    configured = configured.strip() if isinstance(configured, str) else ""
    try:
        p = plugin(cfg, repo_root)
    except Exception:
        log.exception("copse: couldn't load the policy plugin")
        p = None
    if p is None:
        if configured and configured != plugins.OFF:
            return deny(f"the policy plugin {configured!r} named in .copse/config.json "
                        "couldn't be loaded")
        return allow()
    try:
        d = ask(p)
    except Exception:
        log.exception("copse: the policy plugin failed to check a %s; refusing it", what)
        return deny(f"the policy plugin failed while checking this {what}")
    if isinstance(d, Decision):
        return d
    if d is True:
        return allow()
    return deny(d if isinstance(d, str) else f"the policy plugin gave no decision on this {what}")


def _profile_fields(profile: str | None, repo_root: str) -> tuple[str | None, str | None]:
    """(provider, model) of ``profile``, best effort."""
    if not profile:
        return None, None
    try:
        from copse.profiles import load_profile

        p = load_profile(profile, repo_root)
        return p.provider, p.model
    except Exception:
        return None, None


def check_assign(cfg: RepoConfig, repo_root: str, profile: str, task: str, mode: str, *,
                 files: list[str] | None = None, weight: str | None = None,
                 branch: str | None = None, actor: Agent | None = None,
                 running_workers: int | None = None) -> Decision:
    """The plugin's decision on starting ``task`` with ``profile`` (allow
    without a plugin)."""
    provider, model = _profile_fields(profile, repo_root)
    info = AssignInfo(
        repo_root=repo_root, task=task, files=tuple(files or ()), weight=weight,
        profile=profile, provider=provider, model=model, mode=mode, branch=branch,
        actor=actor.id if actor else "user", running_workers=running_workers,
    )
    return _decide("delegation", cfg, repo_root, lambda p: p.check_assign(info))


def running_workers(db: DB, repo_root: str) -> int:
    """How many of ``repo_root``'s workers are still at work (not paused,
    done or dismissed, and not reviewers)."""
    from copse import agents

    roots = {w.id: w.repo_root for w in (db.get_workspace(a.workspace_id) for a in db.list_agents())
             if w is not None}
    return sum(1 for a in db.list_agents()
               if roots.get(a.workspace_id) == repo_root and a.parent_id
               and a.mode in agents.REPORTING_MODES and a.mode != "review"
               and a.status not in ("paused", "done") and a.dismissed_at is None)


def check_merge(cfg: RepoConfig, ws: Workspace, worker: Agent | None,
                actor: Agent | None = None) -> Decision:
    """The plugin's decision on merging ``ws``'s branch (allow without a
    plugin)."""
    _provider, model = _profile_fields(worker.profile if worker else None, ws.repo_root)
    info = MergeInfo(
        repo_root=ws.repo_root, workspace_id=ws.id, branch=ws.branch, base_branch=ws.base_branch,
        agent_id=worker.id if worker else None, profile=worker.profile if worker else None,
        provider=worker.provider if worker else None, model=model,
        actor=actor.id if actor else "user",
    )
    return _decide("merge", cfg, ws.repo_root, lambda p: p.check_merge(info))


__all__ = ["GROUP", "AssignInfo", "Decision", "MergeInfo", "PolicyPlugin", "allow",
           "check_assign", "check_merge", "deny", "plugin", "running_workers"]
