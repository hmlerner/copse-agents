"""Paused sessions: listing them, and keeping only a few.

A session is an interactive agent (usually a supervisor) plus every agent it
started. Closing its chat pauses it: processes stop, and the work (branches,
worktrees, CLI conversations) stays on disk for `copse continue`. Paused
sessions use no memory, but their worktrees use disk, so each repo keeps its
KEEP most recent paused sessions for at most MAX_AGE_DAYS.

Cleaning up never merges or commits anything. Branches always stay. A worker's
worktree is removed only when it has no uncommitted changes.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from copse import agents, git, pool, scratch, tmux, workspaces
from copse.db import DB, Agent, Workspace

# Free-tier retention. Read from the environment so a paid add-on (or a person
# who wants more) can raise them without changing this module.
KEEP = int(os.environ.get("COPSE_KEEP_SESSIONS", "3"))
MAX_AGE_DAYS = float(os.environ.get("COPSE_SESSION_DAYS", "7"))


@dataclass
class Session:
    root: Agent
    workspace: Workspace          # where the root (the chat) runs
    members: list[Agent]          # root + everything it started
    branches: list[str]           # worker branches, for display

    @property
    def paused_at(self) -> float:
        return self.root.status_since or self.root.created_at


def paused(db: DB, repo_root: str) -> list[Session]:
    """Paused sessions whose chat ran in ``repo_root``, newest first."""
    out = []
    for ws in db.find_workspaces(repo_root):
        for a in db.list_agents(ws.id):
            if a.mode == "interactive" and a.status == "paused":
                members = agents.tree(db, a.id)
                branches = []
                for m in members[1:]:
                    mws = db.get_workspace(m.workspace_id)
                    if mws and mws.kind == "worktree" and mws.branch not in branches:
                        branches.append(mws.branch)
                out.append(Session(a, ws, members, branches))
    return sorted(out, key=lambda s: s.paused_at, reverse=True)


HANDOVER = (
    "[copse handover] You are taking over supervising this work from {old}, which ran on branch "
    "{branch}. The goal and milestones, the workers below and any queued tasks are now yours; "
    "call get_progress and list_agents first.{note}{workers}"
)


def handover_prompt(db: DB, old_root: Agent, note: str | None) -> str:
    """What the new supervisor is told on start: the handoff note and the workers it inherits."""
    ws = db.get_workspace(old_root.workspace_id)
    lines = []
    for w in db.children(old_root.id):
        wws = db.get_workspace(w.workspace_id)
        lines.append(f"- {w.id} ({w.profile}, {w.status}) on {wws.branch if wws else '?'}"
                     f", based on {wws.base_branch if wws else '?'}")
    return HANDOVER.format(
        old=old_root.id, branch=ws.branch if ws else "?",
        note=f"\n\nHandoff note:\n{note.strip()}" if note and note.strip() else "",
        workers="\n\nWorkers you inherit:\n" + "\n".join(lines) if lines else "",
    )


def handover(db: DB, old_root_id: str, dest: Workspace, note: str | None = None, *,
             pause_old: bool = True) -> Agent:
    """Hand a supervisor session to a new supervisor running in ``dest``.

    Carries over the goal and milestones (with their last results), the
    workers (their ``parent_id``), queued and started tasks, and a handoff note
    that becomes the new supervisor's first message. The old supervisor's
    autopilot is switched off and, with ``pause_old``, the old session paused
    (not when the old supervisor itself is calling: it ends its own turn)."""
    from copse import autopilot as pilot

    old = db.get_agent(old_root_id)
    if old is None:
        raise agents.AgentError(f"no session {old_root_id}")
    ap = db.get_autopilot(old_root_id)
    prompt = handover_prompt(db, old, note)
    new = agents.spawn(db, dest, old.profile, prompt=prompt, provider_name=old.provider,
                       watch_pane=True, background_setup=True, autopilot=bool(ap and ap.enabled))
    if ap and ap.enabled and ap.goal:
        old_ms = db.milestones(old_root_id)
        pilot.set_goal(db, new.id, ap.goal, [(m.title, m.check_cmd, m.detail, m.profile) for m in old_ms],
                       ap.detail)
        for prev, m in zip(old_ms, db.milestones(new.id)):
            if prev.status != "pending":
                db.record_check(m.id, prev.status == "passed", prev.output or "", prev.checked_sha,
                                passed_sha=prev.passed_sha)
        db.update_autopilot(old_root_id, enabled=0)   # the old session stops syncing goals.md
        pilot.sync_goals_file(db, new.id)
    with db.tx() as c:
        c.execute("UPDATE agents SET parent_id=? WHERE parent_id=?", (new.id, old_root_id))
        c.execute("UPDATE tasks SET caller_id=?, caller_ws_id=? WHERE caller_id=? "
                  "AND state IN ('pending', 'started')", (new.id, dest.id, old_root_id))
    if pause_old:
        agents.pause(db, old_root_id, stop_local_models=False)
    return new


def _forget(db: DB, s: Session) -> None:
    """Drop a session: its records, and any of its workers' worktrees that are
    clean and not used by anything else. Branches and dirty worktrees stay."""
    worker_workspaces = {}
    panes = tmux.list_panes()
    owners = agents.pane_owners(db, panes)
    for a in s.members:
        # Only a window that is still really this agent's: pausing already
        # closed its windows, and its recorded pane id may since have been
        # given to a newer session's pane (see agents.owns_pane) -- which,
        # on a freshly started tmux server, is usually the very chat being
        # launched right now.
        if a.tmux_window and agents.is_alive(a, panes) and agents.owns_pane(db, a, owners):
            tmux.kill_window(a.tmux_window)
        if a.id != s.root.id:
            ws = db.get_workspace(a.workspace_id)
            if ws and ws.kind == "worktree":
                worker_workspaces[ws.id] = ws
    member_ids = {a.id for a in s.members}
    for a in s.members:
        db.delete_agent(a.id)
    for ws in worker_workspaces.values():
        if any(a.id not in member_ids for a in db.list_agents(ws.id)):
            continue  # someone else still works here
        if os.path.isdir(ws.path) and git.dirty_files(ws.path):
            continue  # uncommitted work: keep it, quietly
        try:
            workspaces.remove(db, ws, force=False, delete_branch=False)
        except (workspaces.WorkspaceError, git.GitError):
            pass


def enforce(db: DB, repo_root: str, now: float | None = None) -> int:
    """Keep the KEEP newest paused sessions in ``repo_root`` that are younger
    than MAX_AGE_DAYS. Returns how many were dropped."""
    now = now or time.time()
    dropped = 0
    for i, s in enumerate(paused(db, repo_root)):
        too_old = now - s.paused_at > MAX_AGE_DAYS * 86400
        if i >= KEEP or too_old:
            _forget(db, s)
            dropped += 1
    if dropped:
        # _forget deletes agent rows; a dropped agent's usage mark (if its
        # history rows are gone too, e.g. never reported) is now dead weight.
        db.prune_usage_marks()
    try:
        # Sweeping and trimming happen in the detached fill process, so a
        # large trim's rmtree never blocks whoever's calling enforce (e.g.
        # `copse start`).
        pool.fill_in_background(repo_root)
    except (git.GitError, ValueError, OSError):
        pass
    return dropped


def prune_scratch(db: DB, now: float | None = None) -> int:
    """Delete scratch sessions older than MAX_AGE_DAYS whose work was
    transferred (or that never had any), with no agent still running there."""
    now = now or time.time()
    removed = 0
    for ws in scratch.sessions(db):
        if now - ws.created_at <= MAX_AGE_DAYS * 86400:
            continue
        if any(agents.is_alive(a) for a in db.list_agents(ws.id)):
            continue
        has_work = scratch.commit_count(ws) > 0 or bool(git.dirty_files(ws.path))
        if has_work and not scratch.transferred_to(ws.path):
            continue  # untransferred work stays
        for s in paused(db, ws.repo_root):
            _forget(db, s)
        tmux.kill_session(ws.tmux_session)
        db.delete_workspace(ws.id)
        shutil.rmtree(ws.path, ignore_errors=True)
        removed += 1
    return removed


def disk_usage(path: str | Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total
