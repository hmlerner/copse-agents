"""Policy: let an installed plugin refuse a delegation or a merge.

Before ``assign``/``handoff`` start (or queue) a worker, copse asks the
repo's policy plugin ``check_assign``; before ``merge_workspace`` (and the
pipeline's own merge) touches the base branch, ``check_merge``. Each
returns a ``Decision``: ``allow()``, or ``deny(reason)``, and the reason is
what the caller sees ("Not started: ..." / "Not merged: ...").

A plugin is a package registering an entry point in the ``copse.policy``
group whose object is a factory ``make(repo_root) -> PolicyPlugin | None``;
see ``copse.plugins`` for how one is selected. Without a plugin everything
is allowed, and so is anything a plugin fails to decide (an exception is
logged and treated as allow).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from copse import plugins
from copse.config import RepoConfig
from copse.db import Agent, Workspace

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
    try:
        p = plugin(cfg, repo_root)
        if p is None:
            return allow()
        d = ask(p)
        if isinstance(d, Decision):
            return d
        return allow() if d in (None, True) else deny(str(d) if d is not False else "")
    except Exception:
        log.exception("copse: the policy plugin failed to check a %s; allowing it", what)
        return allow()


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
                 branch: str | None = None, actor: Agent | None = None) -> Decision:
    """The plugin's decision on starting ``task`` with ``profile`` (allow
    without a plugin)."""
    provider, model = _profile_fields(profile, repo_root)
    info = AssignInfo(
        repo_root=repo_root, task=task, files=tuple(files or ()), weight=weight,
        profile=profile, provider=provider, model=model, mode=mode, branch=branch,
        actor=actor.id if actor else "user",
    )
    return _decide("delegation", cfg, repo_root, lambda p: p.check_assign(info))


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
           "check_assign", "check_merge", "deny", "plugin"]
