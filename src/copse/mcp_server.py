"""MCP server each agent gets as ``copse``. It knows which agent is calling
from ``COPSE_AGENT_ID`` (set in the agent's environment at spawn)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time

from mcp.server.mcpserver import MCPServer

from copse import agents, autopilot, codemap, gates, git, history, pipeline, sessions, tasks, workspaces
from copse.config import RepoConfig, load_repo_config
from copse.db import DB, Agent, Workspace
from copse.profiles import list_profiles
from copse.providers import copse_invocation

MAX_DIFF_CHARS = 60_000
MAX_STAT_LINES = 60
LAST_ACTIVITY_TAIL_BYTES = 65_536

mcp = MCPServer(
    "copse",
    instructions=(
        "copse runs other coding agents for you, each on its own git branch in its own "
        "worktree. Delegate with `handoff` (wait for the result) or `assign` (continue "
        "working; the result arrives later as a message). Review a worker's branch with "
        "`workspace_diff`, integrate it with `merge_workspace`, clean up with "
        "`remove_workspace`. Workers must finish by calling `report_result`. A `subagent` "
        "profile starts no process: the reply gives you a worktree and a prompt for your "
        "own Agent tool; record the outcome with `complete_subagent`. In an autopilot "
        "session, track the goal with `set_goal`, `get_progress` and `check_milestone`, and "
        "get branches approved with `request_review` before merging."
    ),
)


def _caller(db: DB) -> tuple[Agent | None, Workspace]:
    agent_id = os.environ.get("COPSE_AGENT_ID")
    agent = db.get_agent(agent_id) if agent_id else None
    if agent:
        ws = db.get_workspace(agent.workspace_id)
        if ws:
            return agent, ws
    ws = workspaces.current(db) or workspaces.adopt_root(db, os.getcwd())
    return agent, ws


def _ws(db: DB, ref: str) -> Workspace:
    _, here = _caller(db)
    return workspaces.resolve(db, ref, cwd=here.path)


_busy_worker = pipeline.busy_worker


def _summary(db: DB, ws: Workspace) -> str:
    base = ws.base_branch
    if not base:
        return f"branch {ws.branch}"
    st = git.status(ws.path, base)
    stat = git.diff(ws.path, base, stat=True) or "(no changes)"
    lines = stat.splitlines()
    if len(lines) > MAX_STAT_LINES:
        stat = "\n".join(lines[:MAX_STAT_LINES] + [f"... ({len(lines) - MAX_STAT_LINES} more lines)"])
    dirty = f", {len(st.dirty_files)} uncommitted file(s)" if st.dirty_files else ""
    return (
        f"workspace {ws.id} on branch {ws.branch}: {st.ahead} commit(s) ahead of {base}, "
        f"{st.behind} behind{dirty}\n{stat}"
    )


def _ago(seconds: float) -> str:
    s = max(int(seconds), 0)
    if s < 90:
        return f"{s}s ago"
    if s < 90 * 60:
        return f"{s // 60}m ago"
    return f"{s // 3600}h ago"


def _last_tool(transcript: str) -> str | None:
    """Name of the last tool call in the tail of a session transcript (Claude
    Code's or a native worker's JSONL). Reads only the last few KB."""
    try:
        with open(transcript, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(f.tell() - LAST_ACTIVITY_TAIL_BYTES, 0))
            tail = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        blocks = (entry.get("message") or {}).get("content") if isinstance(entry.get("message"), dict) else None
        for b in reversed(blocks if isinstance(blocks, list) else []):
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name"):
                return str(b["name"])
        calls = entry.get("tool_calls")
        if isinstance(calls, list) and calls and isinstance(calls[-1], dict) and calls[-1].get("name"):
            return str(calls[-1]["name"])
    return None


def _last_activity(a: Agent) -> str:
    """`` last: WebFetch 40s ago`` from the agent's transcript (its mtime is
    the last activity), or empty when it has none."""
    if not a.transcript_path:
        return ""
    try:
        age = time.time() - os.stat(a.transcript_path).st_mtime
    except OSError:
        return ""
    tool = _last_tool(a.transcript_path)
    return f" last: {tool + ' ' if tool else ''}{_ago(age)}"


DEFAULT_WAIT_SECONDS = 240


def _await_worker(db: DB, worker_id: str, wait_seconds: int) -> str:
    """Wait a bounded time for a handoff worker. Tool calls must stay short:
    MCP clients time out long calls, and a timed-out call would strand the
    result. On timeout the worker is detached, so its result is forwarded as
    a message later, and the caller can resume with wait_for_worker."""
    worker = agents.get(db, worker_id)
    wws = db.get_workspace(worker.workspace_id)
    isolated = bool(wws and wws.kind == "worktree")
    if not agents.runs_process(worker):
        # Nothing of copse's to wait on: the caller's own subagent does it.
        if worker.result is None:
            return agents.subagent_brief(worker, wws) if wws else f"Worker {worker.id} has no workspace."
        tail = f"\n\n{_summary(db, wws)}" if isolated and wws else ""
        return f"Worker {worker.id} finished.\n\n{worker.result}{tail}"
    try:
        result = agents.wait_for_result(db, worker.id, wait_seconds)
    except agents.StillRunning as e:
        result = agents.detach(db, worker.id)
        if result is None:
            return (
                f"Worker {worker.id} is still running ({e}). Its result will arrive as a "
                f"message when it finishes. To keep waiting now instead, call "
                f"wait_for_worker(agent_id=\"{worker.id}\")."
            )
    except agents.AgentError as e:
        return f"Worker {worker.id} failed: {e}"
    agents.collect(db, worker.parent_id, worker.id)
    if worker.mode.startswith("handoff"):
        # The caller has the result; assign workers stay up for feedback.
        agents.kill(db, worker.id)
    tail = f"\n\n{_summary(db, wws)}" if isolated and wws else ""
    return f"Worker {worker.id} finished.\n\n{result}{tail}"


@mcp.tool()
async def handoff(
    agent_profile: str = "", task: str = "", isolate: bool = True, branch: str | None = None,
    wait_seconds: int = DEFAULT_WAIT_SECONDS, done_when: str | None = None,
    files: list[str] | None = None, depends_on: list[str] | None = None,
) -> str:
    """Give a task to a new worker agent and wait for its result.

    agent_profile is optional: when empty, the current unverified milestone's
    profile is used, else the repo's default agent.

    Waits up to wait_seconds (default 4 minutes). If the worker isn't done by
    then, this returns "still running": call wait_for_worker to keep waiting,
    or carry on, and the result will arrive as a message.

    With isolate=true (default) the worker gets its own git worktree on a new
    branch cut from YOUR current branch. It only sees work you've committed,
    so commit first if the worker needs your latest changes. The result
    includes the worker's summary and a diffstat of its branch; review with
    workspace_diff and integrate with merge_workspace. Pass a short
    descriptive branch (e.g. "fix/login-redirect") to name the worker's branch.
    With isolate=false the worker shares your working directory; only use that
    for read-only tasks. done_when is a finish line the worker can verify
    (e.g. "uv run pytest tests/test_auth.py passes"); Claude workers keep
    going until it's met.

    files: paths/globs this task expects to touch. If they overlap another
    active worker's declared or actually-changed files, the task isn't
    started (the repo's `overlap` setting can make that a warning instead). depends_on: agent ids or branch
    names of earlier tasks that must be merged into your branch first; if any
    aren't yet, this task is queued instead of starting, and started
    automatically (cut from your branch as it stands then) once
    merge_workspace resolves them. list_tasks shows what's queued.
    """
    def run() -> str:
        db = DB()
        caller, ws = _caller(db)
        if not task.strip():
            return "Give the worker a task."
        try:
            profile = autopilot.resolve_profile(db, caller.id, ws.repo_root, agent_profile)
        except autopilot.AutopilotError as e:
            return str(e)
        try:
            unmet = tasks.unmet_dependencies(db, ws, depends_on)
        except agents.AgentError as e:
            return str(e)
        if unmet:
            t = tasks.enqueue(
                db, caller, ws, profile, task,"handoff", isolate=isolate, branch=branch,
                done_when=done_when, files=files, depends_on=depends_on,
            )
            return f"Queued task {t.id} until {', '.join(unmet)} merge{'s' if len(unmet) == 1 else ''}."
        warning = tasks.overlap_warning(db, ws, files)
        if warning and load_repo_config(ws.repo_root).overlap == "block":
            return (f"Not started: this task {warning}. Two workers editing the same files "
                    "end in conflicts. Fold it into that worker's task (send_message), or pass "
                    "depends_on so it starts once that branch has merged.")
        worker, wws = agents.delegate(
            db, caller, ws, profile, task,"handoff", isolate=isolate, branch=branch,
            done_when=done_when,
        )
        tasks.record_started(
            db, ws, worker, profile, task,"handoff", isolate=isolate, branch=branch,
            done_when=done_when, files=files, depends_on=depends_on,
        )
        if not agents.runs_process(worker):
            return agents.subagent_brief(worker, wws)
        missing = agents.add_dirs_warning(worker, wws)
        result = _await_worker(db, worker.id, wait_seconds)
        for w in (warning, missing):
            if w:
                result += f"\n\nWarning: {w}"
        return result

    return await asyncio.to_thread(run)


@mcp.tool()
async def wait_for_worker(agent_id: str, wait_seconds: int = DEFAULT_WAIT_SECONDS) -> str:
    """Keep waiting for a worker started with handoff that was still running.
    Returns its result, or "still running" again after wait_seconds."""
    def run() -> str:
        db = DB()
        return _await_worker(db, agent_id, wait_seconds)

    return await asyncio.to_thread(run)


@mcp.tool()
async def assign(
    agent_profile: str = "", task: str = "", isolate: bool = True, branch: str | None = None,
    done_when: str | None = None, files: list[str] | None = None,
    depends_on: list[str] | None = None,
) -> str:
    """Start a worker agent on a task and return immediately.

    agent_profile is optional: when empty, the current unverified milestone's
    profile is used, else the repo's default agent.

    When it finishes, its result arrives in your conversation as a message.
    Isolation works as for handoff. Use this to run several workers in parallel.
    Pass a short descriptive branch (e.g. "feat/ls-json") to name the worker's branch.
    done_when is a finish line the worker can verify (e.g. "uv run pytest
    tests/test_ls.py passes"); Claude workers keep going until it's met.

    files: paths/globs this task expects to touch. If they overlap another
    active worker's declared or actually-changed files, the task isn't
    started (the repo's `overlap` setting can make that a warning instead). depends_on: agent ids or branch
    names of earlier tasks that must be merged into your branch first; if any
    aren't yet, this task is queued instead of starting, and started
    automatically (cut from your branch as it stands then) once
    merge_workspace resolves them. list_tasks shows what's queued.
    """
    def run() -> str:
        db = DB()
        caller, ws = _caller(db)
        if not task.strip():
            return "Give the worker a task."
        try:
            profile = autopilot.resolve_profile(db, caller.id, ws.repo_root, agent_profile)
        except autopilot.AutopilotError as e:
            return str(e)
        try:
            unmet = tasks.unmet_dependencies(db, ws, depends_on)
        except agents.AgentError as e:
            return str(e)
        if unmet:
            t = tasks.enqueue(
                db, caller, ws, profile, task,"assign", isolate=isolate, branch=branch,
                done_when=done_when, files=files, depends_on=depends_on,
            )
            return f"Queued task {t.id} until {', '.join(unmet)} merge{'s' if len(unmet) == 1 else ''}."
        warning = tasks.overlap_warning(db, ws, files)
        if warning and load_repo_config(ws.repo_root).overlap == "block":
            return (f"Not started: this task {warning}. Two workers editing the same files "
                    "end in conflicts. Fold it into that worker's task (send_message), or pass "
                    "depends_on so it starts once that branch has merged.")
        worker, wws = agents.delegate(
            db, caller, ws, profile, task,"assign", isolate=isolate, branch=branch,
            done_when=done_when,
        )
        tasks.record_started(
            db, ws, worker, profile, task,"assign", isolate=isolate, branch=branch,
            done_when=done_when, files=files, depends_on=depends_on,
        )
        if not agents.runs_process(worker):
            return agents.subagent_brief(worker, wws)
        text = f"Started worker {worker.id} ({worker.profile}) in workspace {wws.id} on branch {wws.branch}."
        for w in (warning, agents.add_dirs_warning(worker, wws)):
            if w:
                text += f"\nWarning: {w}"
        u = autopilot.usage()
        if u and u["used"] >= load_repo_config(wws.repo_root).usage_limit - 15:
            text += f"\nNote: {autopilot.usage_note(u)}."
        return text

    return await asyncio.to_thread(run)


@mcp.tool()
def send_message(to_agent_id: str, message: str) -> str:
    """Send a message to another agent. It's delivered when that agent is idle."""
    db = DB()
    caller, _ = _caller(db)
    return agents.send_message(db, to_agent_id, message, caller.id if caller else None)


@mcp.tool()
def report_result(result: str) -> str:
    """Workers: call this once when your task is done (after committing), with
    a concise summary of what you did and anything left to check."""
    db = DB()
    caller, _ = _caller(db)
    if not caller:
        return "Not running as a copse agent; nothing to report to."
    return agents.report_result(db, caller.id, result)


@mcp.tool()
def complete_subagent(agent_id: str, result: str) -> str:
    """Record the outcome of a task you ran in your own subagent (a worker
    whose profile uses the `subagent` provider): pass the agent id from the
    handoff/assign reply and the subagent's summary. The worker then shows as
    done; review, merge and remove its workspace as usual."""
    db = DB()
    try:
        agent = agents.complete_subagent(db, agent_id, result)
    except agents.AgentError as e:
        return str(e)
    ws = db.get_workspace(agent.workspace_id)
    if not ws:
        return f"Recorded the result of {agent.id}."
    text = f"Recorded the result of {agent.id}."
    if ws.kind == "worktree":
        text += (f"\n\n{_summary(db, ws)}\n\nReview with workspace_diff(\"{ws.id}\"), then "
                 "merge_workspace and remove_workspace.")
    return text


@mcp.tool()
def list_agents() -> str:
    """List agents in this repository with their status, workspace and branch."""
    db = DB()
    caller, here = _caller(db)
    lines = []
    for ws in db.find_workspaces(here.repo_root):
        for a in db.list_agents(ws.id):
            me = " (you)" if caller and a.id == caller.id else ""
            parent = f" parent={a.parent_id}" if a.parent_id else ""
            lines.append(
                f"{a.id}{me} {a.profile}/{a.provider} {a.status} mode={a.mode}{parent} "
                f"ws={ws.id} branch={ws.branch}{_last_activity(a)}"
            )
    return "\n".join(lines) or "No agents."


@mcp.tool()
def list_tasks() -> str:
    """Coordination tasks (assign/handoff calls given files or depends_on)
    that are queued or were cancelled: what a queued one is waiting on, and
    why a cancelled one was cancelled. Started tasks show in list_agents."""
    db = DB()
    _, here = _caller(db)
    return tasks.list_text(db, here.repo_root)


@mcp.tool()
def list_agent_profiles() -> str:
    """Agent profiles available to handoff/assign."""
    db = DB()
    _, here = _caller(db)
    return "\n".join(f"{p.name} ({p.provider}): {p.description}" for p in list_profiles(here.repo_root))


@mcp.tool()
def workspace_diff(workspace: str, stat_only: bool = False) -> str:
    """Show everything a workspace's branch changes relative to its base
    (commits plus uncommitted edits)."""
    db = DB()
    ws = _ws(db, workspace)
    text = git.diff(ws.path, workspaces.require_base(ws), stat=stat_only) or "(no changes)"
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + "\n... (truncated; use stat_only or read files directly)"
    return f"{_summary(db, ws)}\n\n{text}" if not stat_only else _summary(db, ws)


@mcp.tool()
async def merge_workspace(workspace: str, squash: bool = False) -> str:
    """Merge a workspace's branch into its base branch (for workers: your branch).

    Before the gates run, if the branch is behind its (local) base and the
    worktree is clean, copse merges the base into the branch first, so a
    passing check reflects the code as it will actually be merged: this can
    add a commit to the branch even when nothing ends up merged into the
    base, which is only ever touched once the merge itself succeeds. If that
    sync conflicts, it's aborted, the worktree is left clean, and the reply
    lists the conflicting files. If it succeeds and adds a commit, that
    commit hasn't been reviewed yet (when this repo requires review), so
    nothing is merged; the reply asks for a fresh request_review instead.
    If the branch needs that sync while another worker is still at work in
    the workspace (alive and not yet reported), nothing is done, to avoid
    racing its commits: the reply says to retry once it reports. A worker
    merging its own branch is never blocked by itself.

    Then copse checks the merge gates in the workspace: everything committed,
    a reviewer's approval of this commit (in autopilot sessions, or when the
    repo requires review), pre-commit hooks, and the repo's `checks` commands.
    If a gate fails nothing is merged, and the reply says what to fix.
    Fails without changing anything on conflicts."""
    def run() -> str:
        db = DB()
        caller, _ = _caller(db)
        ws = _ws(db, workspace)
        return pipeline.merge(db, caller, ws, squash=squash)

    return await asyncio.to_thread(run)


@mcp.tool()
async def request_review(workspace: str, focus: str | None = None, profile: str | None = None) -> str:
    """Start a reviewer agent on a worker's branch. It doesn't edit code; its
    verdict arrives as a message and is recorded for merge_workspace, which
    only accepts an approval of the branch's current commit. The repo's
    checks run in a detached process and are delivered to the reviewer as a
    message once they finish (so this returns right away instead of blocking
    on the full suite, and the delivery survives even if this MCP server
    exits first). focus: anything the reviewer should look at especially.
    profile: the reviewer profile to use; defaults to the repo's
    `review_profile` config, else the built-in `reviewer-codex` profile (a
    different model from a Claude worker) when Codex is installed, else
    `reviewer`.
    """
    def start() -> tuple[Agent, Workspace, RepoConfig] | str:
        db = DB()
        caller, _ = _caller(db)
        ws = _ws(db, workspace)
        if ws.kind != "worktree":
            return "Only a worker's workspace (its own branch and worktree) can be reviewed this way."
        cfg = load_repo_config(ws.repo_root)
        try:
            reviewer = agents.request_review(db, caller, ws, profile, focus, cfg)
        except agents.AgentError as e:
            return str(e)
        return reviewer, ws, cfg

    result = await asyncio.to_thread(start)
    if isinstance(result, str):
        return result
    reviewer, ws, cfg = result
    if cfg.checks:
        # Detached (like the existing _flush/_after-launch/_close calls): it
        # must outlive this call and this MCP server process, since the
        # reviewer was told a summary is coming.
        subprocess.Popen(
            [*copse_invocation(), "_deliver-checks", reviewer.id, ws.id],
            start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    return (f"Reviewer {reviewer.id} ({reviewer.profile}/{reviewer.provider}) is reviewing "
            f"{ws.branch}. Its verdict will arrive as a message.")


@mcp.tool()
def submit_review(approved: bool, summary: str) -> str:
    """Reviewers: call this once with your verdict. approved=true only if the
    branch can merge as is. summary: your findings, most severe first."""
    db = DB()
    caller, _ = _caller(db)
    if not caller:
        return "Only a reviewer started with request_review can submit a review."
    return agents.submit_review(db, caller.id, approved, summary)


@mcp.tool()
def remove_workspace(workspace: str, delete_branch: bool = False, force: bool = False) -> str:
    """Remove a workspace's worktree and stop its agents. The branch is kept
    unless delete_branch=true (which only deletes it if merged, unless force).
    If a queued task (see assign/handoff's depends_on) was waiting on this
    workspace's branch and it still had unmerged commits, that task is
    cancelled and its caller is told."""
    db = DB()
    ws = _ws(db, workspace)
    unmerged = False
    if ws.kind == "worktree" and ws.base_branch and os.path.isdir(ws.path):
        try:
            _behind, ahead = git.ahead_behind(ws.path, ws.base_branch)
            unmerged = ahead > 0
        except git.GitError:
            pass
    if unmerged:
        tasks.on_removed_unmerged(db, ws)
    removed = workspaces.remove(db, ws, force=force, delete_branch=delete_branch)
    return f"Removed {ws.id}. {removed.branch_note or 'branch deleted'}"


def _session(db: DB) -> tuple[str, Workspace] | str:
    """The caller's session root and the checkout it works in, or an error."""
    caller, _ = _caller(db)
    if not caller:
        return "Not running as a copse agent."
    root_id = autopilot.root_of(db, caller.id)
    root = db.get_agent(root_id)
    ws = db.get_workspace(root.workspace_id) if root else None
    if not ws or not db.get_autopilot(root_id):
        return "Autopilot isn't on for this session. The user can turn it on with `copse autopilot on`."
    return root_id, ws


@mcp.tool()
def set_goal(goal: str, milestones: list[dict[str, str]], detail: str | None = None) -> str:
    """Autopilot: record what we're building. milestones is an ordered list of
    {"title": ..., "check": ..., "detail": ..., "profile": ...}. The optional
    profile names the default worker profile for `assign` while that milestone
    is the current one (e.g. a cheaper profile for a small milestone). Each check is a shell command
    copse runs from the root of your checkout; it must exit 0 only when that
    milestone is done (e.g. "uv run pytest tests/test_settings.py -q").
    Replaces any earlier goal; milestones that keep their title and check keep
    their last result."""
    db = DB()
    found = _session(db)
    if isinstance(found, str):
        return found
    root_id, _ = found
    items = []
    for m in milestones:
        title = str(m.get("title", "")).strip()
        if not title:
            return "Every milestone needs a title."
        check = str(m.get("check") or "").strip() or None
        items.append((title, check, (str(m.get("detail") or "").strip() or None),
                      (str(m.get("profile") or "").strip() or None)))
    try:
        autopilot.set_goal(db, root_id, goal, items, detail)
    except autopilot.AutopilotError as e:
        return str(e)
    return autopilot.progress(db, root_id)


@mcp.tool()
def handover(to: str, note: str | None = None) -> str:
    """Hand this session over to a new supervisor on another branch or
    worktree (`to`: a branch name, or a worktree path). The goal and milestones,
    your workers, queued tasks and `note` (the rules, branch state, loose ends
    the new supervisor needs) move across, and the new supervisor's first
    message carries the note. Afterwards you have nothing left to supervise:
    tell the user and stop."""
    db = DB()
    caller, _ = _caller(db)
    if not caller:
        return "Not running as a copse agent."
    root_id = autopilot.root_of(db, caller.id)
    root = db.get_agent(root_id)
    ws = db.get_workspace(root.workspace_id) if root else None
    if not ws:
        return "No session to hand over."
    try:
        dest = workspaces.checkout_for_target(db, ws.path, to)
        new = sessions.handover(db, root_id, dest, note, pause_old=False)
    except (git.GitError, workspaces.WorkspaceError, agents.AgentError, ValueError) as e:
        return f"Handover failed: {e}"
    return (f"Handed the session over to {new.id} in {dest.id} ({dest.branch}). Your goal, workers and "
            "queued tasks are now theirs; tell the user and stop.")


@mcp.tool()
def get_progress() -> str:
    """Autopilot: the goal, each milestone with its check and last result."""
    db = DB()
    found = _session(db)
    return found if isinstance(found, str) else autopilot.progress(db, found[0])


@mcp.tool()
async def check_milestone(milestone: int | None = None) -> str:
    """Autopilot: run milestone checks in your checkout (where merges land) and
    record the results. milestone: its number; omit to check all of them.
    This is the only way a milestone becomes done. Checking one milestone also
    re-runs the others currently marked passed, so a merge that broke one of
    them shows up as "REGRESSED" in the result even while another milestone
    is still in progress."""
    def run() -> str:
        db = DB()
        found = _session(db)
        if isinstance(found, str):
            return found
        root_id, ws = found
        if not db.milestones(root_id):
            return "no milestones yet: record the goal with set_goal first"
        return autopilot.check_in_background(db, root_id, ws, milestone)

    return await asyncio.to_thread(run)


def record_milestone_changes(db: DB, root_id: str, ws: Workspace, before: dict,
                             caller, position: int | None, text: str) -> None:
    """History rows for a milestone check: one per milestone whose status it
    changed, and one for the check itself. No tokens on these rows: the
    supervisor's usage belongs to its own report/merge rows."""
    for m in [m for m in db.milestones(root_id) if before.get(m.id) != m.status]:
        history.record_safely(
            db, ws.repo_root, "milestone", agent=caller, branch=ws.branch,
            task=m.title, result=f"{m.status}\n\n{m.output or ''}",
        )
    history.record_safely(
        db, ws.repo_root, "check", agent=caller, branch=ws.branch,
        task=f"check_milestone({position if position is not None else 'all'})",
        result=text,
    )


@mcp.tool()
def need_user(question: str) -> str:
    """Autopilot: you're blocked on a decision only the user can make. copse
    stops asking you to keep going until the user replies. Ask them the
    question right after calling this."""
    db = DB()
    found = _session(db)
    if isinstance(found, str):
        return found
    autopilot.need_user(db, found[0], question)
    return "Noted. Ask the user now; autopilot resumes when they reply."


@mcp.tool()
def transfer_to_repo(target_path: str, branch: str | None = None) -> str:
    """Move this scratch session's work into a real git repository: its commits
    (and any uncommitted changes) are replayed onto a new branch there, as a
    copse workspace to review and merge. Only works from a scratch session
    (copse started outside a git repo)."""
    from copse import scratch

    db = DB()
    _, ws = _caller(db)
    if not scratch.is_scratch(ws.path):
        return "This isn't a scratch session; the work is already in a real repository."
    try:
        t = scratch.transfer(db, ws, target_path, branch)
    except (scratch.ScratchError, git.GitError, workspaces.WorkspaceError) as e:
        return f"Transfer failed: {e}"
    return (f"Moved {t.commits} commit(s) onto branch {t.workspace.branch} in {t.workspace.repo_root} "
            f"(workspace {t.workspace.id}, at {t.workspace.path}). The user can open it with "
            f"`copse` in that repo, then review with `copse diff {t.workspace.name}`.")


def main() -> None:
    mcp.run()
