"""Task coordination between parallel workers.

``assign``/``handoff`` may declare ``files`` (paths/globs a task expects to
touch) and ``depends_on`` (earlier tasks, by agent id or branch name, that
must be merged first). A task with unmet dependencies is recorded here as
'pending' (no worker started yet) and started once ``merge_workspace``
resolves its dependencies, cut from the updated base branch. A task with
declared ``files`` is checked against other active workers' declared and
actually-changed files, so the caller learns about likely collisions without
being blocked by them.
"""

from __future__ import annotations

import fnmatch
import json
import os
import time
from pathlib import PurePath

from copse import agents, git
from copse.db import DB, Agent, Task, Workspace


def new_id() -> str:
    return agents.new_id()


def _dumps(items: list[str] | None) -> str | None:
    return json.dumps(list(items)) if items else None


def _loads(text: str | None) -> list[str]:
    return json.loads(text) if text else []


# -- overlap warnings ---------------------------------------------------------


def _normpath(p: str) -> str:
    """``p`` with a leading './' and redundant separators collapsed, so
    "./src/a.py" compares equal to "src/a.py"."""
    return os.path.normpath(p) if p else p


def _glob_match(a: str, b: str) -> bool:
    """Whether globs/paths ``a`` and ``b`` could refer to the same file(s)."""
    a, b = _normpath(a), _normpath(b)
    if a == b:
        return True
    if fnmatch.fnmatch(b, a) or fnmatch.fnmatch(a, b):
        return True
    if "**" in a or "**" in b:
        try:
            return PurePath(b).match(a) or PurePath(a).match(b)
        except ValueError:
            return False
    return False


def _changed_files(ws: Workspace) -> list[str]:
    """Files ``ws``'s branch has touched relative to its base, cheaply (no
    process beyond a couple of git calls)."""
    if not ws.base_branch:
        return []
    try:
        mb = git.merge_base(ws.path, git.base_ref(ws.path, ws.base_branch))
        names = git.out(["diff", "--name-only", mb], ws.path)
        untracked = git.out(["ls-files", "--others", "--exclude-standard"], ws.path)
    except git.GitError:
        return []
    return [f for f in (*names.splitlines(), *untracked.splitlines()) if f]


def active_tasks(db: DB, repo_root: str) -> list[Task]:
    """Started tasks in ``repo_root`` whose worker hasn't reported yet."""
    out = []
    for t in db.list_tasks(repo_root, state="started"):
        if not t.agent_id:
            continue
        agent = db.get_agent(t.agent_id)
        if agent and agent.result is None:
            out.append(t)
    return out


def overlap_warning(db: DB, ws: Workspace, files: list[str] | None) -> str | None:
    """A warning if ``files`` overlaps another active task's declared or
    actually-changed files, or None. Still starts the worker regardless: this
    is informational, not a block."""
    if not files:
        return None
    for t in active_tasks(db, ws.repo_root):
        other_agent = db.get_agent(t.agent_id) if t.agent_id else None
        other_ws = db.get_workspace(other_agent.workspace_id) if other_agent else None
        candidates = list(_loads(t.files))
        if other_ws:
            candidates += _changed_files(other_ws)
        for mine in files:
            for theirs in candidates:
                if _glob_match(mine, theirs):
                    branch = other_ws.branch if other_ws else "?"
                    return (f"overlaps with {t.agent_id} ({branch}) on {theirs}; "
                            "consider depends_on or merging first")
    return None


# -- dependencies ---------------------------------------------------------------


def _dep_branch(db: DB, dep: str) -> str:
    """A dependency identifier's branch: ``dep`` may be an agent id (its
    workspace's branch) or already a branch name."""
    agent = db.get_agent(dep)
    if agent:
        ws = db.get_workspace(agent.workspace_id)
        if ws:
            return ws.branch
    return dep


def _dep_task(db: DB, repo_root: str, dep: str) -> Task | None:
    """The task copse is tracking for ``dep`` (an agent id or branch name), if
    any -- including one that was cancelled: a cancelled dependency can never
    become merged, so ``unmet_dependencies`` must treat it as a hard failure
    rather than something to keep waiting on. An untracked dependency (e.g. a
    branch never assigned through copse) falls back to a git ancestry check
    in ``unmet_dependencies``."""
    branch = _dep_branch(db, dep)
    candidates = [t for t in db.list_tasks(repo_root) if t.agent_id == dep or t.branch == branch]
    return max(candidates, key=lambda t: t.created_at) if candidates else None


def unmet_dependencies(db: DB, caller_ws: Workspace, depends_on: list[str] | None) -> list[str]:
    """``depends_on`` entries not yet merged into ``caller_ws``'s branch.

    Raises ``agents.AgentError`` if any dependency's task was cancelled: it
    can never merge, so there's nothing left to wait for.

    A dependency a task hasn't diverged from yet (no commits beyond its base)
    would trivially satisfy a plain git ancestry check even though it hasn't
    been merged, so a dependency copse is tracking as a task is judged by its
    recorded state (only 'merged' counts) instead; only a dependency copse
    never started falls back to ancestry."""
    if not depends_on:
        return []
    unmet = []
    for dep in depends_on:
        task = _dep_task(db, caller_ws.repo_root, dep)
        if task is not None:
            if task.state == "cancelled":
                raise agents.AgentError(f"dependency {dep} was cancelled and will never merge")
            if task.state != "merged":
                unmet.append(dep)
        elif not git.ok(["merge-base", "--is-ancestor", _dep_branch(db, dep), "HEAD"], caller_ws.path):
            unmet.append(dep)
    return unmet


def _dep_matches(dep: str, ws: Workspace, worker_id: str | None) -> bool:
    return dep == ws.branch or (worker_id is not None and dep == worker_id)


def _refers_to(dep: str, t: Task) -> bool:
    """Whether a ``depends_on`` entry names task ``t``: its worker's agent id
    (once it has one) or its declared branch."""
    return dep == t.branch or (t.agent_id is not None and dep == t.agent_id)


def _cancel(db: DB, task_id: str, reason: str) -> None:
    """Cancel a still-pending task and tell its caller, then cascade the
    cancellation to any pending task depending on it, recursively: a task
    waiting on one that can never merge can itself never merge. Re-fetches
    and checks state so cancelling the same task twice (reachable via more
    than one dependency path) is a no-op the second time."""
    t = db.get_task(task_id)
    if t is None or t.state != "pending":
        return
    db.update_task(t.id, state="cancelled")
    if t.caller_id:
        try:
            agents.send_message(
                db, t.caller_id, f"Cancelled queued task {t.id}: {reason}", sender_id=None,
            )
        except agents.AgentError:
            pass
    for dependent in db.list_tasks(t.repo_root, state="pending"):
        if any(_refers_to(d, t) for d in _loads(dependent.depends_on)):
            _cancel(db, dependent.id, f"its dependency {t.id} ({t.branch or t.id}) was cancelled")


# -- queueing and starting -------------------------------------------------------


def enqueue(
    db: DB, caller: Agent | None, caller_ws: Workspace, profile: str, task_text: str, mode: str,
    *, isolate: bool, branch: str | None, done_when: str | None,
    files: list[str] | None, depends_on: list[str] | None, plan_first: bool | None = None,
    weight: str | None = None,
) -> Task:
    """Record a task that can't start yet: no worker, no workspace, just what
    it takes to start it once its dependencies are merged."""
    t = Task(
        id=new_id(), repo_root=caller_ws.repo_root, agent_id=None,
        caller_id=caller.id if caller else None, caller_ws_id=caller_ws.id,
        profile=profile, task_text=task_text, mode=mode, isolate=int(isolate),
        branch=branch, done_when=done_when, files=_dumps(files),
        depends_on=_dumps(depends_on), state="pending", created_at=time.time(), weight=weight,
    )
    db.add_task(t)
    if plan_first is not None:
        db.update_task(t.id, plan_first=int(plan_first))
    return t


def record_started(
    db: DB, caller_ws: Workspace, worker: Agent, profile: str, task_text: str, mode: str,
    *, isolate: bool, branch: str | None, done_when: str | None,
    files: list[str] | None, depends_on: list[str] | None, weight: str | None = None,
) -> Task:
    """Record a task that started right away, so its ``files`` can be checked
    for overlap against later tasks."""
    t = Task(
        id=new_id(), repo_root=caller_ws.repo_root, agent_id=worker.id,
        caller_id=worker.parent_id, caller_ws_id=caller_ws.id, profile=profile,
        task_text=task_text, mode=mode, isolate=int(isolate), branch=branch,
        done_when=done_when, files=_dumps(files), depends_on=_dumps(depends_on),
        state="started", created_at=time.time(), started_at=time.time(), weight=weight,
    )
    db.add_task(t)
    return t


def start_queued(db: DB, task: Task) -> Agent:
    """Start a queued task now that its dependencies are met, cutting its
    branch from the caller workspace's current (updated) base."""
    caller_ws = db.get_workspace(task.caller_ws_id)
    if caller_ws is None:
        raise agents.AgentError(f"workspace {task.caller_ws_id} for queued task {task.id} is gone")
    caller = db.get_agent(task.caller_id) if task.caller_id else None
    worker, _wws = agents.delegate(
        db, caller, caller_ws, task.profile, task.task_text, task.mode,
        isolate=bool(task.isolate), branch=task.branch, done_when=task.done_when,
        plan_first=None if task.plan_first is None else bool(task.plan_first),
    )
    db.update_task(task.id, agent_id=worker.id, state="started", started_at=time.time())
    return worker


def on_merged(db: DB, ws: Workspace) -> None:
    """``ws``'s branch was just merged into its base: mark its own task
    'merged' (so dependents judge it correctly, and it stops counting as
    started), then start any pending task that was only waiting on it and now
    has every dependency merged, and tell its caller."""
    worker = agents.workspace_worker(db, ws)
    dep_ref = worker.id if worker else ws.branch
    if worker:
        for t in db.list_tasks(ws.repo_root, state="started"):
            if t.agent_id == worker.id:
                db.update_task(t.id, state="merged")
    for t in db.list_tasks(ws.repo_root, state="pending"):
        deps = _loads(t.depends_on)
        if not any(_dep_matches(d, ws, worker.id if worker else None) for d in deps):
            continue
        caller_ws = db.get_workspace(t.caller_ws_id)
        if not caller_ws:
            continue
        try:
            if unmet_dependencies(db, caller_ws, deps):
                continue
        except agents.AgentError as e:
            _cancel(db, t.id, str(e))
            continue
        try:
            new_worker = start_queued(db, t)
        except agents.AgentError:
            continue
        if t.caller_id:
            try:
                text = f"Started {new_worker.id} (was waiting on {dep_ref})."
                new_ws = db.get_workspace(new_worker.workspace_id)
                warning = agents.add_dirs_warning(new_worker, new_ws) if new_ws else None
                if warning:
                    text += f"\nWarning: {warning}"
                agents.send_message(db, t.caller_id, text, sender_id=None)
            except agents.AgentError:
                pass


def on_removed_unmerged(db: DB, ws: Workspace) -> None:
    """``ws`` was removed while its branch still had commits not in its base:
    cancel any pending task depending on it -- and, recursively, any pending
    task that in turn depends on those -- and tell each one's caller."""
    worker = agents.workspace_worker(db, ws)
    dep_ref = worker.id if worker else ws.branch
    for t in db.list_tasks(ws.repo_root, state="pending"):
        if any(_dep_matches(d, ws, worker.id if worker else None) for d in _loads(t.depends_on)):
            _cancel(db, t.id, f"it was waiting on {dep_ref}, which was removed unmerged.")


def list_text(db: DB, repo_root: str) -> str:
    """Pending and cancelled tasks: started ones already show in list_agents."""
    lines = []
    for t in db.list_tasks(repo_root):
        if t.state not in ("pending", "cancelled"):
            continue
        deps = _loads(t.depends_on)
        parts = [t.id, t.state, t.profile, f"branch={t.branch or '(auto)'}"]
        if deps:
            parts.append(f"depends_on={','.join(deps)}")
        if t.files:
            parts.append(f"files={','.join(_loads(t.files))}")
        lines.append(" ".join(parts))
    return "\n".join(lines) or "No pending or cancelled tasks."
