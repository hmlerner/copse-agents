"""What `copse ls` and `copse watch` show: one entry per workspace, with its
agents. Git fields are None when they can't be computed."""

from __future__ import annotations

import logging
import os
import time

from copse import agents, git, status_cache, tmux
from copse import usage as usage_mod
from copse.db import DB, NATIVE_SUBAGENT_STALE, Agent, NativeSubagent, Workspace

log = logging.getLogger(__name__)

# How long a *finished* native subagent still shows "done" in the sidebar
# before disappearing entirely. Display-only, so it lives here rather than
# with NATIVE_SUBAGENT_STALE/_PRUNE_AFTER in db.py, which govern the table
# itself (when a "running" row counts as crashed, and when rows are dropped).
NATIVE_SUBAGENT_LINGER = 30
# Parent states where a subagent can never really be "running" any more:
# a paused or killed parent's own SubagentStop hooks never fire, and
# end_native_subagents (called from agents.pause/kill) may not have caught
# up yet, so this is a display-side backstop.
_PARENT_NOT_RUNNING = ("paused", "exited", "done")
# How long a session root (a supervisor chat) whose terminal is gone still
# shows as stopped before it leaves the sidebar, counted from its last status
# change. It stays resumable (`copse continue`); it just stops cluttering the
# sidebar once none of its workers is running either. A newer session
# running in the same repo cuts this short: launching `copse` pauses the old
# chat, and it shouldn't sit next to the new one.
STOPPED_ROOT_LINGER = NATIVE_SUBAGENT_LINGER
# A worktree that never had an agent is only treated as finished (see
# ``retired``) once it's this old, so one just made by hand isn't hidden
# before anything is started in it.
UNUSED_WORKTREE_GRACE = 600.0


def workspace_entry(db: DB, ws: Workspace, *, detail: bool = False,
                    native_subagents: dict[str, list[NativeSubagent]] | None = None,
                    now: float | None = None,
                    panes: dict[str, bool] | None = None,
                    agent_list: list[Agent] | None = None,
                    alive: set[str] | None = None) -> dict:
    """``agent_list`` limits the agents shown (default: all of ``ws``'s);
    ``alive`` is the ids of the ones known to be running, when the caller
    has already worked that out (see ``snapshot``)."""
    ahead = behind = dirty = None
    if ws.base_branch and os.path.isdir(ws.path):
        try:
            st = status_cache.cached_status(ws.path, ws.base_branch, now=now)
            ahead, behind, dirty = st.ahead, st.behind, len(st.dirty_files)
        except git.GitError:
            pass
    return {
        "id": ws.id,
        "name": ws.name,
        "branch": ws.branch,
        "base_branch": ws.base_branch,
        "path": ws.path,
        "ahead": ahead,
        "behind": behind,
        "dirty": dirty,
        "agents": [
            agent_entry(db, a, detail=detail,
                       native_subagents=None if native_subagents is None else native_subagents.get(a.id, []),
                       now=now, panes=panes, alive=None if alive is None else a.id in alive)
            for a in (db.list_agents(ws.id) if agent_list is None else agent_list)
        ],
    }


def _visible_native_subagents(subs: list[NativeSubagent], now: float) -> list[dict]:
    out = []
    for s in subs:
        if s.ended_at is None:
            if now - s.started_at > NATIVE_SUBAGENT_STALE:
                continue
        elif now - s.ended_at > NATIVE_SUBAGENT_LINGER:
            continue
        out.append({"id": s.id, "agent_type": s.agent_type, "started_at": s.started_at,
                    "ended_at": s.ended_at})
    return out


def agent_entry(db: DB, a: Agent, *, detail: bool = False,
                native_subagents: list[NativeSubagent] | None = None,
                now: float | None = None,
                panes: dict[str, bool] | None = None,
                alive: bool | None = None) -> dict:
    # Usage is a display extra: a bad transcript must never break the sidebar.
    try:
        u = usage_mod.agent_usage(db, a)
    except Exception:
        log.exception("copse: couldn't read usage for %s", a.id)
        u = None
    if not agents.runs_process(a):
        status = a.status  # a supervisor's own subagent: no terminal to check
    elif agents.is_alive(a, panes) if alive is None else alive:
        a = agents.reconcile(db, a, samples=1)
        status = a.status
    else:
        status = "exited"
    entry = {"id": a.id, "profile": a.profile, "provider": a.provider,
             "status": status, "mode": a.mode}
    if u and u.total:
        entry["tokens"] = usage_mod.short_summary(u)
    unread = db.unread_count(a.id)
    if unread:
        entry["unread"] = unread
    if detail:
        subs = db.native_subagents(a.id) if native_subagents is None else native_subagents
        visible = _visible_native_subagents(subs, now if now is not None else time.time())
        if status in _PARENT_NOT_RUNNING:
            visible = [s for s in visible if s["ended_at"] is not None]
        entry.update(
            parent_id=a.parent_id,
            status_since=a.status_since,
            pending=db.pending_count(a.id),
            reported=a.result is not None,
            window=a.tmux_window,
            headless=bool(a.headless),
            subagents=visible,
        )
    return entry


def live_agents(db: DB, panes: dict[str, bool]) -> set[str]:
    """Ids of every agent that is running (see agents.owns_pane)."""
    owners = agents.pane_owners(db, panes)
    return {a.id for a in db.list_agents()
            if agents.is_alive(a, panes) and agents.owns_pane(db, a, owners)}


def _stopped_root(db: DB, a: Agent, alive: set[str], now: float,
                  newest_live_root: float = 0.0) -> bool:
    """A session root (a supervisor chat) whose terminal is gone, with none
    of its workers still running, that has been stopped long enough to leave
    the sidebar, or has been replaced by a newer session (one started at
    ``newest_live_root``)."""
    if a.mode != "interactive" or a.parent_id or not agents.runs_process(a) or a.id in alive:
        return False
    members = agents.tree(db, a.id)
    if any(m.id in alive for m in members[1:]):
        return False  # its workers still show, so it does too
    superseded = newest_live_root > a.created_at
    if not superseded and now - max(a.status_since or 0, a.created_at) <= STOPPED_ROOT_LINGER:
        return False
    if a.status not in ("paused", "done"):
        # It ended without pausing its session (agents.ended runs from inside
        # the chat's own pane, so it never runs when tmux itself goes away).
        # Record what agents.pause would have, so `copse continue` can bring
        # it back and sessions.enforce's retention eventually drops it.
        for m in members:
            if m.status in ("paused", "done"):
                continue
            db.end_native_subagents(m.id)
            db.set_status(m.id, "done" if m.mode != "interactive" and m.result is not None
                          else "paused")
    return True


def _finished(a: Agent) -> bool:
    return a.dismissed_at is not None or a.status == "done" or a.result is not None


def retired(db: DB, ws: Workspace, alive: set[str], now: float | None = None,
            everyone: list[Agent] | None = None) -> bool:
    """A worker's worktree whose branch is merged into its base (it has no
    commit the base lacks) and whose agents are all finished: closed, done or
    reported, none still running. Uncommitted changes don't count against
    it: the sidebar hides it all the same, but ``cull.prune_retired`` only
    removes a clean one."""
    now = time.time() if now is None else now
    if ws.kind != "worktree" or not ws.base_branch or not os.path.isdir(ws.path):
        return False
    everyone = db.list_agents(ws.id) if everyone is None else everyone
    if any(a.id in alive or not _finished(a) for a in everyone):
        return False
    if not everyone and now - ws.created_at <= UNUSED_WORKTREE_GRACE:
        return False
    try:
        st = status_cache.cached_status(ws.path, ws.base_branch, now=now)
    except git.GitError:
        return False
    return st.ahead == 0


def snapshot(db: DB, repo_root: str | None, panes: dict[str, bool] | None = None) -> list[dict]:
    """What the sidebar shows: agents closed with `copse close`, and stopped
    sessions with nothing left running, are left out, as is a worktree
    whose agents are all left out that way, and a finished worktree whose
    branch is already merged (see ``retired``)."""
    now = time.time()
    by_parent = db.all_native_subagents()
    if panes is None:
        panes = tmux.list_panes()
    alive = live_agents(db, panes)
    found = [(ws, db.list_agents(ws.id)) for ws in db.find_workspaces(repo_root)]
    by_id = {a.id: a for _, everyone in found for a in everyone}
    newest_live_root = max((a.created_at for a in by_id.values()
                            if a.id in alive and not a.parent_id and a.mode == "interactive"),
                           default=0.0)
    hidden_roots = {a.id for a in by_id.values()
                    if _stopped_root(db, a, alive, now, newest_live_root)}

    def hidden(a: Agent) -> bool:
        # A stopped session leaves with everything it started that isn't
        # running: its paused workers come back with `copse continue`.
        if a.id in hidden_roots:
            return True
        if a.id in alive:
            return False
        seen = set()
        while a.parent_id and a.parent_id not in seen:
            seen.add(a.id)
            parent = by_id.get(a.parent_id) or db.get_agent(a.parent_id)
            if parent is None:
                return False
            a = parent
        return a.id in hidden_roots

    out = []
    for ws, everyone in found:
        shown = [a for a in everyone if a.dismissed_at is None and not hidden(a)]
        if everyone and not shown and ws.kind != "main":
            continue
        if retired(db, ws, alive, now, everyone):
            continue
        entry = workspace_entry(db, ws, detail=True, native_subagents=by_parent, now=now,
                                panes=panes, agent_list=shown, alive=alive)
        review = db.last_review(ws.id)
        # The latest reviewer verdict, so the sidebar can flag rows that need you.
        entry["review"] = None if review is None else ("approved" if review.approved else "changes")
        entry["autopilot"], entry["asking"] = _autopilot_flags(db, shown)
        out.append(entry)
    return out


def _autopilot_flags(db: DB, shown: list[Agent]) -> tuple[bool, str | None]:
    """Whether these agents' session runs on autopilot (it reviews its own
    workers, so their reports don't wait on the person), and the id of the one
    among them that is a session root with an open need_user question."""
    on, asking = False, None
    for a in shown:
        ap = db.get_autopilot(agents.root_of(db, a.id))
        on = on or bool(ap and ap.enabled)
        if ap and a.parent_id is None and ap.state == "blocked":
            asking = a.id
    return on, asking


def autopilot_entry(db: DB, repo_root: str | None, panes: dict[str, bool] | None = None) -> dict | None:
    """The goal and milestones of the newest running autopilot session in
    ``repo_root``, for the sidebar. ``panes`` should be the same
    ``tmux.list_panes()`` result passed to ``snapshot`` for this refresh, so
    liveness isn't checked with a second tmux subprocess."""
    from copse import autopilot, quota

    if not repo_root:
        return None
    roots = [a for ws in db.find_workspaces(repo_root) for a in db.list_agents(ws.id)
             if a.mode == "interactive" and a.status not in ("paused", "done")
             and db.get_autopilot(a.id) and agents.is_alive(a, panes)]
    if not roots:
        return None
    root = max(roots, key=lambda a: a.created_at)
    ap = db.get_autopilot(root.id)
    assert ap is not None
    return {
        "enabled": bool(ap.enabled),
        "goal": ap.goal,
        "state": ap.state,
        "note": ap.note,
        "usage_resets_at": ap.usage_resets_at,
        "milestones": [{"position": m.position, "title": m.title, "status": m.status,
                        "check": m.check_cmd} for m in db.milestones(root.id)],
        "usage": autopilot.usage(),
        "quota": quota.notes(repo_root, native=False),
        "workers": len(autopilot.working_workers(db, root.id)),
    }
