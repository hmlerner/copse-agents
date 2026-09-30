"""Agents: a CLI agent process in a tmux window inside a workspace.

Messaging uses an inbox: a message to a busy agent waits in its
inbox and is delivered the moment the agent goes idle. With hook-capable
providers, delivery happens inside the ``Stop`` hook itself (the hook tells
Claude Code to keep going with the message as its next instruction), so
nothing ever types into a terminal while the agent is mid-turn.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid

from copse import git, tmux, workspaces
from copse.config import RepoConfig
from copse.db import DB, Agent, Workspace
from copse.profiles import load_profile, missing_add_dirs
from copse.providers import LaunchContext, get_provider

log = logging.getLogger(__name__)

WORKER_FOOTER = """

---
You are running as a copse worker (agent id {agent_id}) on branch `{branch}`.
{guidance}
When you have finished:
1. Commit your work to this branch with a clear message (do not push or merge).
2. Call the `report_result` tool from the `copse` MCP server with a concise
   summary: what you changed, anything left undone, and anything the
   supervisor should check.
"""

REVIEW_FOOTER = """

---
You are running as a copse reviewer (agent id {agent_id}) on branch `{branch}`.
Don't edit files. When you have finished, call the `submit_review` tool from
the `copse` MCP server: approved=true only if you'd merge it as is, with a
summary of your findings (most severe first, each with file:line and a fix).
"""


SUBAGENT_FOOTER = """

---
Work only in `{path}`: a git worktree on branch `{branch}` that is yours alone.
Your shell may start in another directory, so begin every Bash command with
`cd {path} && ` (or use `git -C {path}`), and give file tools absolute paths
under that directory. Don't change files anywhere else, and don't switch branches.
{guidance}
When you have finished:
1. Commit your work there on `{branch}` with a clear message (do not push or merge).
2. Don't call any copse tools. End with a concise summary: what you changed,
   anything left undone, and anything the supervisor should check.
"""


class AgentError(RuntimeError):
    pass


class StillRunning(AgentError):
    """The wait ended before the worker reported; it's still working."""


# Modes whose workers must finish with report_result. A handoff whose caller
# stopped waiting becomes "handoff_detached": its result is then forwarded to
# the caller as a message, like an assign.
REPORTING_MODES = ("handoff", "handoff_detached", "assign", "review")
FORWARDING_MODES = ("handoff_detached", "assign", "review")


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def test_guidance(checks: list[str]) -> str:
    """How a worker should test: the tests for its change as it goes, and
    the full suite once. Full-suite runs are the slowest thing a worker does,
    and several workers running them at once slow each other down."""
    targeted = ("Testing: while you work, run only the tests that cover your change (one "
                "test file, or a -k filter), not the whole suite.")
    if checks:
        shown = "; ".join(f"`{c}`" for c in checks)
        return (f"{targeted} Don't run the full suite yourself: copse runs the repo's checks "
                f"({shown}) on your branch once, before it merges, and sends you any failures.")
    return f"{targeted} Run the full suite once, just before you commit."


PERMISSION_GUIDANCE = (
    "Permissions: commands are pre-approved by their first words, so run them plainly and "
    "one at a time from your own worktree (no `cd`, no `&&`, no pipes, no `VAR=x` prefixes). "
    "Never `cd` into or read from the main checkout ({root}): it isn't yours, and it "
    "pauses you for approval. Write files with your Edit and Write tools, never with "
    "shell heredocs or scripts, and don't write to /tmp."
)


def worker_guidance(ws: Workspace) -> str:
    """How a worker should find code (the code map, if any), test it, and
    stay within its pre-approved commands."""
    from copse import codemap
    from copse.config import load_repo_config

    try:
        checks = load_repo_config(ws.repo_root).checks
    except ValueError:
        checks = []
    parts = (codemap.guidance(ws.repo_root), test_guidance(checks),
             PERMISSION_GUIDANCE.format(root=ws.repo_root))
    return "\n".join(p for p in parts if p)


def agent_env(ws: Workspace, agent_id: str, agent: Agent | None = None) -> dict[str, str]:
    env = {**workspaces.workspace_env(ws), "COPSE_AGENT_ID": agent_id}
    if agent is not None:
        # The profile's ``env.NAME: value`` lines: how a Claude Code profile
        # points at another backend (ANTHROPIC_BASE_URL, ...), or any CLI at
        # a key it needs. Set before copse's own variables, which win.
        try:
            env = {**load_profile(agent.profile, ws.repo_root).env, **env}
        except KeyError:
            pass
    if agent is not None and preload_tools(agent, ws):
        # Claude Code defers MCP tools and loads them on demand, which costs a
        # worker an extra round trip at the moment it's told to report (and
        # some never get there). Workers and reviewers only have copse's own
        # tools, so loading them all up front is cheap.
        env["ENABLE_TOOL_SEARCH"] = "false"
    return env


def preload_tools(agent: Agent, ws: Workspace) -> bool:
    """Whether to turn Claude Code's tool search off for this agent: the
    profile's ``tool_search`` setting, else yes for every non-interactive
    Claude agent (a chat may carry the person's own MCP servers, whose tools
    are better left deferred)."""
    if agent.provider != "claude":
        return False
    try:
        setting = load_profile(agent.profile, ws.repo_root).tool_search
    except KeyError:
        setting = None
    if setting is not None:
        return not setting
    return agent.mode != "interactive"


PLAN_FIRST_NOTE = """

Plan first: before you edit any file, read the code you need, then call the
`submit_plan` tool from the `copse` MCP server with a short plan (the files
you'll change and how, and how you'll test it) and stop. Wait for your
supervisor's decision, which arrives as a message. Don't edit files until the
plan is approved; if it comes back with feedback, revise it and call
`submit_plan` again."""


def decorate_worker_prompt(task: str, agent_id: str, ws: Workspace, done_when: str | None,
                           provider, headless: bool, plan_first: bool = False) -> str:
    """The prompt actually sent to a handoff/assign worker's CLI: the raw
    ``task`` plus its finish line, the WORKER_FOOTER reminder to report, and
    (for an interactive Claude worker with a finish line) the ``/goal``
    wrapper. Used at spawn time, and again by ``resume`` to rebuild it when a
    paused worker's CLI session can't be resumed and must restart fresh."""
    from copse import autopilot as pilot

    prompt = task
    if done_when:
        prompt += f"\n\nFinish line: {done_when.strip()}"
    if plan_first:
        prompt += PLAN_FIRST_NOTE
    prompt += WORKER_FOOTER.format(agent_id=agent_id, branch=ws.branch,
                                   guidance=worker_guidance(ws))
    if done_when and provider.name == "claude" and not headless:
        # /goal is an interactive command; headless workers get the finish
        # line above and the Stop hook's reminder to report.
        prompt = pilot.worker_goal(prompt, done_when, ws.branch) or prompt
    return prompt


def spawn(
    db: DB,
    ws: Workspace,
    profile_name: str,
    *,
    prompt: str | None = None,
    provider_name: str | None = None,
    parent_id: str | None = None,
    mode: str = "interactive",
    watch_pane: bool = False,
    background_setup: bool = False,
    done_when: str | None = None,
    autopilot: bool = False,
    plan_first: bool = False,
) -> Agent:
    """Start an agent in ``ws``. Workers (handoff/assign) given a ``done_when``
    finish line run it as a Claude Code ``/goal``. With ``autopilot``, the
    agent is a session root that drives toward a goal (see copse.autopilot)."""
    from copse import autopilot as pilot

    profile = load_profile(profile_name, ws.repo_root)
    provider = get_provider(provider_name or profile.provider)
    agent_id = new_id()
    # Headless is a Claude Code mode; other CLIs ignore the profile field.
    # copse's own loop (native) has no TUI at all, so it always runs that way.
    headless = bool(profile.headless and provider.name == "claude") or provider.name == "native"

    # Stored as the agent's task: the raw text for a handoff/assign worker (so
    # a reviewer reading it later isn't given WORKER_FOOTER or the /goal
    # wrapper), but the fully decorated prompt for a subagent (that text IS
    # what's handed to the supervisor's own Agent tool) or a reviewer.
    raw_task = prompt
    if not provider.launches_process:
        if mode not in ("handoff", "assign"):
            raise AgentError(
                f"profile {profile.name!r} uses the {provider.name} provider, which runs in "
                "a supervisor's own Agent tool: use it through the copse handoff or assign tools"
            )
        prompt = raw_task = subagent_prompt(profile.prompt, prompt or "", ws, done_when)
    elif prompt and mode in ("handoff", "assign"):
        prompt = decorate_worker_prompt(prompt, agent_id, ws, done_when, provider, headless,
                                        plan_first=plan_first)
    elif prompt and mode == "review":
        prompt = raw_task = prompt + REVIEW_FOOTER.format(agent_id=agent_id, branch=ws.branch)

    agent = Agent(
        id=agent_id, workspace_id=ws.id, profile=profile.name, provider=provider.name,
        parent_id=parent_id, mode=mode, status="starting", tmux_window="",
        result=None, created_at=time.time(), task=raw_task, headless=int(headless) or None,
        done_when=done_when,
    )
    db.add_agent(agent)
    if plan_first and provider.launches_process and mode in ("handoff", "assign"):
        db.update_agent(agent_id, plan_first=1)
        agent.plan_first = 1
    if autopilot:
        plan = pilot.enable(db, agent_id, ws)
        if plan and not prompt:
            prompt = agent.task = pilot.kickoff(plan)
            db.update_agent(agent_id, task=prompt)
    try:
        _launch(db, agent, ws, prompt=prompt, resume=None, watch_pane=watch_pane,
                background_setup=background_setup)
    except Exception:
        db.delete_agent(agent_id)
        raise
    return agent


def _pause_when_done(agent_id: str, argv: list[str]) -> list[str]:
    """Wrap an interactive agent's command so that, when it exits for any
    reason, copse pauses its session from inside the same pane. This doesn't
    rely on tmux hooks, which some tmux builds don't fire reliably. The
    wrapper survives Ctrl-C (a trap *handler* resets in the child, so the CLI
    keeps its normal Ctrl-C behaviour), and errors go to hooks.log instead of
    vanishing."""
    from copse.config import copse_home
    from copse.providers import copse_invocation

    log = shlex.quote(str(copse_home() / "hooks.log"))
    ended = " ".join(shlex.quote(a) for a in [*copse_invocation(), "_ended", agent_id])
    script = f'trap : INT; "$@"; code=$?; {ended} >>{log} 2>&1; exit $code'
    return ["/bin/sh", "-c", script, "copse-agent", *argv]


def _profile_for(db: DB, agent: Agent, ws: Workspace):
    """``agent``'s profile as launched: autopilot sessions add their guide,
    and a chat (a supervisor) learns about the code map, if there is one.
    Workers get the code map in their task instead (see worker_guidance)."""
    from dataclasses import replace

    from copse import autopilot as pilot
    from copse.config import load_repo_config

    profile = load_profile(agent.profile, ws.repo_root)
    if db.get_autopilot(agent.id):
        profile = replace(profile, prompt=profile.prompt + pilot.guide(load_repo_config(ws.repo_root)))
    if agent.mode == "interactive":
        from copse import codemap

        note = codemap.guidance(ws.repo_root)
        if note:
            profile = replace(profile, prompt=f"{profile.prompt}\n\n{note}".strip())
    if agent.headless:
        profile = replace(profile, headless=True)
    return profile


def _open_window(db: DB, agent: Agent, ws: Workspace, name: str, argv: list[str],
                 watch_pane: bool) -> str:
    """Run ``argv`` for ``agent`` in a new window of ``ws``'s tmux session."""
    if agent.mode == "interactive":
        argv = _pause_when_done(agent.id, argv)
    tmux.ensure_session(ws.tmux_session, ws.path, workspaces.workspace_env(ws))
    target = tmux.new_window(ws.tmux_session, name, ws.path, argv, agent_env(ws, agent.id, agent),
                             tag=(AGENT_TAG, agent.id))
    db.update_agent(agent.id, tmux_window=target)
    agent.tmux_window = target
    if watch_pane:
        # Best effort: a failed split (or a lock some other process held
        # past _sidebar_lock's timeout) must not fail the agent it sits
        # beside or the hook that triggered this launch.
        try:
            _ensure_sidebar(db, root_of(db, agent.id), ws, target)
        except Exception:
            pass
    tmux.apply_theme(ws.tmux_session)
    return target


SIDEBAR_COLUMNS = 30
SIDEBAR_TAG = tmux.SIDEBAR_TAG
# Set on every agent pane at creation (see _open_window), so a pane can say
# whose it is: pane ids are a per-server counter that restarts at 0 when the
# tmux server does, and a stored id alone can't tell an agent's own pane from
# a newer session's that happens to have the same id (see pane_owners).
AGENT_TAG = tmux.AGENT_TAG


def root_of(db: DB, agent_id: str) -> str:
    """Walk up ``parent_id`` to the top of this agent's tree: the interactive
    session root the sidebar is keyed by (see db.sidebars)."""
    seen: set[str] = set()
    current = agent_id
    while current not in seen:
        seen.add(current)
        a = db.get_agent(current)
        if a is None or a.parent_id is None:
            return current
        current = a.parent_id
    return current  # a parent_id cycle would be a bug elsewhere; don't loop forever


# Stored in place of a pane id once the person quits the sidebar themselves
# (see dismiss_sidebar): sidebar_follow then leaves it gone, where a sidebar
# that died any other way (its session closed under it, a crash) comes back.
SIDEBAR_DISMISSED = "dismissed"
# How long _ensure_sidebar waits for another process's sidebar work.
SIDEBAR_LOCK_TIMEOUT = 5.0


@contextlib.contextmanager
def _sidebar_lock(root_id: str, timeout: float | None = None):
    """Serialize sidebar operations for one session root: _ensure_sidebar,
    sidebar_follow and pause's cleanup can all run from different, concurrent
    processes (background hook invocations, a resume, a pause), and without
    this a relocate can interleave with a create or a kill.

    ``timeout`` (for _ensure_sidebar, called from the launch path a hook
    can trigger) bounds the wait, raising BlockingIOError after it, so a hook
    is never stuck for long behind another's sidebar work. It waits rather
    than giving up at once: the other holder is almost always a quick
    sidebar_follow, and a launch that skipped its sidebar over that would
    leave the session without one."""
    from copse.config import copse_home

    lock_dir = copse_home() / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with open(lock_dir / f"sidebar-{root_id}.lock", "w") as f:
        if timeout is None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _valid_sidebar(pane: str | None, root_id: str) -> bool:
    """Whether ``pane`` is still really this root's sidebar: alive, and
    tagged with this root's id. Pane ids are a per-server counter that
    restarts at 0 after a tmux server restart, so a stale DB row's pane id
    can silently now refer to a completely different, unrelated pane; the
    tag (set once, at creation) is what tells the two apart."""
    return (bool(pane) and pane != SIDEBAR_DISMISSED and tmux.window_alive(pane)
            and tmux.get_pane_tag(pane, SIDEBAR_TAG) == root_id)


def _agent_for_window(db: DB, ws: Workspace, window: str) -> Agent | None:
    """The agent whose pane lives in ``window``, or (for a window with no
    agent record of its own, e.g. the session's default 'shell' window) any
    agent in this workspace -- they all share one lineage, so any of them
    gives the right session root."""
    agents_here = db.list_agents(ws.id)
    for a in agents_here:
        if a.tmux_window and tmux.pane_window(a.tmux_window) == window:
            return a
    return agents_here[0] if agents_here else None


def _ensure_sidebar(db: DB, root_id: str, ws: Workspace, target_pane: str) -> None:
    """Make sure ``root_id``'s one `copse watch --sidebar` pane is beside
    ``target_pane``: move it there if it already exists elsewhere (never
    start a second one -- that would double the dashboard's 2s polling), or
    create it fresh if it's dead, stale (see _valid_sidebar), was dismissed
    (a launch or relaunch brings it back) or has never run."""
    with _sidebar_lock(root_id, timeout=SIDEBAR_LOCK_TIMEOUT):
        existing = db.get_sidebar_pane(root_id)
        if _valid_sidebar(existing, root_id):
            assert existing is not None
            if tmux.pane_window(existing) != tmux.pane_window(target_pane):
                tmux.move_pane(existing, target_pane, SIDEBAR_COLUMNS, _sidebar_position(ws))
            return
        _create_sidebar(db, root_id, ws, target_pane)


def _sidebar_position(ws: Workspace) -> str:
    """"left" or "bottom": the repo's `sidebar` setting (anything else: left)."""
    from copse.config import load_repo_config

    try:
        return "bottom" if load_repo_config(ws.repo_root).sidebar == "bottom" else "left"
    except ValueError:  # unreadable config: the layout is not worth failing over
        return "left"


def _create_sidebar(db: DB, root_id: str, ws: Workspace, target_pane: str) -> str:
    """Start ``root_id``'s sidebar beside ``target_pane``. The caller holds
    _sidebar_lock and has checked there's no valid one already."""
    from copse.providers import copse_invocation

    pane = tmux.split_left(target_pane, ws.path, [*copse_invocation(), "watch", "--sidebar"],
                           workspaces.workspace_env(ws), columns=SIDEBAR_COLUMNS,
                           position=_sidebar_position(ws))
    tmux.set_pane_tag(pane, SIDEBAR_TAG, root_id)
    db.set_sidebar_pane(root_id, pane)
    return pane


def sidebar_root(pane: str | None) -> str | None:
    """The session root whose sidebar is ``pane``, if it's a copse sidebar."""
    return tmux.get_pane_tag(pane, SIDEBAR_TAG) if pane else None


def quit_later(root_id: str) -> None:
    """Quit a copse session from its own sidebar: pause it, as ending the
    chat does, which closes its tmux session and hands the person their
    prompt back. Runs detached, since pausing closes the sidebar asking."""
    from copse.providers import copse_invocation

    subprocess.Popen([*copse_invocation(), "_quit", root_id], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def dismiss_sidebar(db: DB, pane: str | None) -> None:
    """The person quit the sidebar in ``pane`` (`copse watch --sidebar`
    returned normally): remember that, so sidebar_follow doesn't bring it
    back on the next window switch. Only if ``pane`` is still its root's
    recorded sidebar; a no-op for any other `copse watch`."""
    if not pane:
        return
    root_id = tmux.get_pane_tag(pane, SIDEBAR_TAG)
    if not root_id:
        return
    with _sidebar_lock(root_id):
        if db.get_sidebar_pane(root_id) == pane:
            db.set_sidebar_pane(root_id, SIDEBAR_DISMISSED)


def sidebar_follow(db: DB, session: str) -> None:
    """Called from the session-window-changed / client-session-changed hooks
    tmux.apply_theme sets on every copse session: its active window just
    changed, or a client just switched into it, so make sure the sidebar is
    there instead of wherever it used to be.

    A sidebar that has died since (it followed the person into a worker's
    session that then closed, `copse watch` crashed, ...) is started again
    here, so it never stays gone for longer than the next switch. Except
    when the person quit it themselves (see dismiss_sidebar), or the root
    never had one (`--no-watch`: no sidebars row at all). Skips entirely
    once its root has been paused (a tombstone pause() sets before it
    starts closing windows)."""
    ws = db.workspace_by_tmux_session(session)
    if ws is None:
        return
    window = tmux.active_window(session)
    if not window:
        return
    agent = _agent_for_window(db, ws, window)
    if agent is None:
        return
    # Only to pick which root's lock to take; re-derived below once it's
    # held, since the window (and so the agent and root) may have changed
    # while this call waited for the lock.
    root_id = root_of(db, agent.id)
    with _sidebar_lock(root_id):
        window = tmux.active_window(session)
        if not window:
            return
        agent = _agent_for_window(db, ws, window)
        if agent is None:
            return
        if root_of(db, agent.id) != root_id:
            return  # the root changed while waiting; let the next call catch up
        root = db.get_agent(root_id)
        if root is None or root.status == "paused":
            return
        sidebar = db.get_sidebar_pane(root_id)
        if sidebar is None or sidebar == SIDEBAR_DISMISSED:
            return
        if not _valid_sidebar(sidebar, root_id):
            target_pane = tmux.agent_pane_in_window(window, None)
            root_ws = db.get_workspace(root.workspace_id)
            if target_pane and root_ws:
                _create_sidebar(db, root_id, root_ws, target_pane)
            return
        if tmux.pane_window(sidebar) == window:
            return  # already here
        # A worker's session changes its active window on its own (its
        # placeholder shell window closing once the agent's is up, a
        # relaunch), and the hook fires just the same with nobody attached.
        # The sidebar follows the person, not the windows: it never leaves a
        # session someone is looking at for one nobody is.
        if not tmux.session_attached(session):
            home = tmux.pane_session(sidebar)
            if home and home != session and tmux.session_attached(home):
                return
        target_pane = tmux.agent_pane_in_window(window, sidebar)
        if not target_pane:
            return
        root_ws = db.get_workspace(root.workspace_id)
        tmux.move_pane(sidebar, target_pane, SIDEBAR_COLUMNS,
                       _sidebar_position(root_ws) if root_ws else "left")


def _add_dirs_warning(profile, provider_name: str) -> str | None:
    missing = missing_add_dirs(profile) if provider_name == "claude" else []
    if not missing:
        return None
    return (f"add_dirs names {', '.join(missing)}, which do not exist; "
            "Claude Code will ignore them")


def add_dirs_warning(agent: Agent, ws: Workspace) -> str | None:
    """A warning if ``agent`` was launched with ``add_dirs`` that do not exist,
    or None. Claude Code ignores those silently, so whoever started the agent
    has to be told: the supervisor, whose only view of a launch is the reply."""
    return _add_dirs_warning(load_profile(agent.profile, ws.repo_root), agent.provider)


def _launch(db: DB, agent: Agent, ws: Workspace, *, prompt: str | None,
            resume: str | None, watch_pane: bool, background_setup: bool = False) -> None:
    """Start (or restart) ``agent``'s CLI in a new tmux window of ``ws``."""
    profile = _profile_for(db, agent, ws)
    provider = get_provider(agent.provider)
    if not provider.launches_process:
        # Nothing to start: the caller's own subagent does the work.
        status = "done" if agent.result is not None else "processing"
        db.set_status(agent.id, status)
        agent.status = status
        return
    # Here rather than in spawn, so a resume checks too: a directory can be
    # deleted between the first launch and a `copse continue`. This reaches a
    # person running copse in a terminal; a launch from the MCP server has no
    # one reading its stderr, so handoff, assign and a queued task's start put
    # the same text in what they tell the supervisor (add_dirs_warning).
    warning = _add_dirs_warning(profile, provider.name)
    if warning:
        print(f"copse: {warning}", file=sys.stderr)
    if agent.headless:
        _launch_headless(db, agent, ws, prompt=prompt, resume=resume, watch_pane=watch_pane)
        return
    if provider.prompt_after_ready:
        # Typed in once it's ready, after any warm-up message.
        for text in ((provider.warmup(profile) if not resume else None), prompt):
            if text:
                db.enqueue(agent.id, text, None)
        prompt = None
    # Recorded before launching: the agent's hooks may fire within milliseconds.
    status = "processing" if prompt else "starting"
    if not provider.uses_hooks:
        status = "unknown"
    db.set_status(agent.id, status)
    agent.status = status

    if provider.name == "claude":
        from copse.providers import trust_folder

        trust_folder(ws.path)  # no trust dialog for nobody to answer
    argv = provider.command(LaunchContext(agent.id, profile, prompt, resume=resume, cwd=ws.path,
                                          mode=agent.mode, plan_first=bool(agent.plan_first)))
    target = _open_window(db, agent, ws, f"{profile.name}-{agent.id[:4]}", argv, watch_pane)

    if provider.name == "shell" and prompt:
        tmux.paste(target, prompt)
    if background_setup:
        # Startup dialogs (folder trust) are handled by a detached helper so
        # the person gets their terminal immediately.
        from copse.providers import copse_invocation

        subprocess.Popen(
            [*copse_invocation(), "_after-launch", agent.id],
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, **agent_env(ws, agent.id)},
        )
    else:
        provider.after_launch(target)
        ready(db, agent.id)


# -- headless workers (claude -p) ------------------------------------------------
#
# A headless worker runs `claude -p`: one turn per process, no TUI. So that the
# rest of copse can treat it like any other agent, its tmux pane runs a small
# runner (``copse _headless``) that stays up between turns:
#
# - the first prompt, and every message sent while no turn is running, go to
#   the agent's inbox; the runner takes the oldest and runs
#   `claude -p --resume <session> <message>` (the first turn gets a fresh
#   --session-id, so the conversation is known without relying on hooks);
# - messages that arrive mid-turn are delivered by the Stop hook, exactly as
#   for interactive Claude Code (it keeps the -p process going);
# - hooks report status and results as usual; the runner marks the agent idle
#   when a turn's process exits;
# - if claude exits with an error, the runner exits too. The pane stays (with
#   claude's error in it) and is dead, which is how copse notices any agent
#   died: wait_for_result reports its last lines, the dashboard shows it
#   stopped, and send_message refuses.

HEADLESS_CONTINUE = (
    "You were paused and have just been restarted. Check `git status` and `git log` "
    "in your working directory to see what you already did, then continue your task."
)


def _launch_headless(db: DB, agent: Agent, ws: Workspace, *, prompt: str | None,
                     resume: str | None, watch_pane: bool) -> None:
    from copse.providers import copse_invocation

    if prompt:
        db.enqueue(agent.id, prompt, None)
    elif resume and agent.mode in REPORTING_MODES and agent.result is None:
        # An interactive chat resumes onto its input box; a headless worker
        # needs a turn to pick its task back up.
        db.enqueue(agent.id, HEADLESS_CONTINUE, None)
    status = "processing" if db.pending_count(agent.id) else "idle"
    db.set_status(agent.id, status)
    agent.status = status
    runner = get_provider(agent.provider).runner
    argv = [*copse_invocation(), runner, agent.id, *(["--resume", resume] if resume else [])]
    _open_window(db, agent, ws, f"{agent.profile}-{agent.id[:4]}", argv, watch_pane)


def _preview(text: str, lines: int = 6) -> str:
    rows = text.strip().splitlines()
    return "\n".join(rows[:lines] + (["..."] if len(rows) > lines else []))


def run_headless(db: DB, agent_id: str, resume: str | None = None, *,
                 poll: float = 0.5, exit_when_idle: bool = False) -> int:
    """The loop in a headless worker's pane (see above). Returns claude's exit
    code when a turn fails, or 0 once the agent is gone. ``exit_when_idle``
    (for tests) returns as soon as there's nothing left to run."""
    from copse.providers import LaunchContext

    agent = db.get_agent(agent_id)
    ws = db.get_workspace(agent.workspace_id) if agent else None
    if agent is None or ws is None:
        return 0
    provider = get_provider(agent.provider)
    session = resume
    turn = 0
    while True:
        agent = db.get_agent(agent_id)
        if agent is None:
            return 0
        msg = db.pop_pending(agent_id)
        if msg is None:
            if exit_when_idle:
                return 0
            time.sleep(poll)
            continue
        turn += 1
        db.set_status(agent_id, "processing")
        new_session = None
        if not session:
            new_session = str(uuid.uuid4())
            db.update_agent(agent_id, session_ref=new_session)
        ctx = LaunchContext(agent_id, _profile_for(db, agent, ws), msg.body, resume=session,
                            cwd=ws.path, session_id=new_session, mode=agent.mode,
                            plan_first=bool(agent.plan_first))
        print(f"\n── copse: turn {turn} ──\n{_preview(msg.body)}\n", flush=True)
        try:
            code = subprocess.call(provider.command(ctx), cwd=ws.path, stdin=subprocess.DEVNULL)
        except OSError as e:
            print(f"copse: couldn't start {provider.name}: {e}", flush=True)
            code = 127
        session = session or new_session
        agent = db.get_agent(agent_id)
        if agent is None:
            return 0
        session = agent.session_ref or session
        if code != 0:
            print(f"\n── copse: claude exited with code {code}; this worker has stopped ──", flush=True)
            return code
        db.set_status(agent_id, "idle", only_if="processing")
        print("── copse: turn finished; waiting for messages ──", flush=True)


def ready(db: DB, agent_id: str) -> None:
    """after_launch saw the CLI's input box. For CLIs with no hook that says
    so, this is when it's ready (and when queued messages can go in)."""
    agent = db.get_agent(agent_id)
    if agent and not get_provider(agent.provider).announces_start:
        handle_hook(db, agent_id, "session-start", {})


# -- sessions: pause and continue ----------------------------------------------

RESUME_NOTE = (
    "\n\n(You were paused and have just been restarted. Before continuing, check "
    "`git status` and `git log` in your working directory to see what you already did.)"
)


def tree(db: DB, root_id: str) -> list[Agent]:
    """``root_id`` and every agent it started, directly or indirectly."""
    out, queue = [], [root_id]
    while queue:
        a = db.get_agent(queue.pop(0))
        if a is None:
            continue
        out.append(a)
        queue.extend(c.id for c in db.children(a.id))
    return out


def pause(db: DB, root_id: str, *, stop_procs: bool = True,
          stop_local_models: bool = True) -> list[Agent]:
    """Stop a supervisor and everything it started, keeping their work.

    Worktrees, branches, queued messages and each CLI's own session stay; the
    processes stop, so nothing keeps acting while nobody's watching. Agents
    that already reported are left marked done. Returns the agents paused.
    Without ``stop_procs``, processes Claude Code's daemon hosts are left for
    a detached cull (see copse.cull), which stops those of paused agents,
    rather than waiting here for them to exit. The Ollama server copse
    started is stopped too once no other running session uses it (see
    copse.native.serve.stop_unused); ``stop_local_models=False`` when
    another session is about to start and would only start it again.

    Records every status first and closes windows last: this can run inside
    one of the windows it closes (see _pause_when_done)."""
    paused, sessions, windows = [], set(), []
    owners = pane_owners(db)
    for a in tree(db, root_id):
        ws = db.get_workspace(a.workspace_id)
        if ws:
            sessions.add(ws.tmux_session)
        if a.tmux_window and owns_pane(db, a, owners):
            windows.append(a.tmux_window)
        # Its own SubagentStop hooks will never fire once its process stops.
        db.end_native_subagents(a.id)
        if a.mode != "interactive" and a.result is not None:
            db.set_status(a.id, "done")
        else:
            db.set_status(a.id, "paused")
            paused.append(a)
    # The sidebar follows the person around, so it may not be sitting in any
    # of the windows about to close: find it wherever it is and stop it too,
    # rather than leaving it running with nothing left to show it. Status was
    # already set to "paused" above (a tombstone), and the lock keeps this
    # from interleaving with a concurrent sidebar_follow or _ensure_sidebar.
    # Checked (and, if it's really ours, killed) around the window closes,
    # not after: a sidebar living in a window being closed here would
    # otherwise already look dead by the time _valid_sidebar ran, and get
    # skipped instead of having its now-stale DB row cleared.
    with _sidebar_lock(root_id):
        sidebar = db.get_sidebar_pane(root_id)
        sidebar_is_ours = _valid_sidebar(sidebar, root_id)
        for w in windows:
            # Dead or alive: a dead pane (the chat that just exited) would
            # otherwise hold the window, and the session, open.
            tmux.kill_window(w)
        if sidebar_is_ours:
            assert sidebar is not None
            tmux.kill_pane(sidebar)
            db.clear_sidebar_pane(root_id)
    # Closing the windows doesn't stop sessions Claude Code's daemon hosts.
    # Skipped for this process and its ancestors: this can run in the chat's pane.
    if stop_procs:
        from copse import procs

        procs.stop([a.id for a in tree(db, root_id)], grace=2.0)
    root = db.get_agent(root_id)
    root_ws = db.get_workspace(root.workspace_id) if root else None
    for session in sessions:
        # The chat's own session always closes, so an attached terminal gets
        # its prompt back every time. Workers' sessions close once they hold
        # nothing but the idle starter shell.
        if (root_ws and session == root_ws.tmux_session) or set(tmux.windows(session)) <= {"shell"}:
            tmux.kill_session(session)
    if stop_local_models:
        _stop_local_models(db)
    return paused


def _stop_local_models(db: DB) -> None:
    from copse.native import serve

    try:
        lines = serve.stop_unused(db)
        if lines:
            with open(serve.log_path(), "a", encoding="utf-8") as f:
                f.writelines(f"== copse: {line}\n" for line in lines)
    except Exception:  # noqa: BLE001 -- freeing memory must never break a pause
        pass


def latest_paused(db: DB, ws: Workspace) -> Agent | None:
    """The most recently paused interactive agent (session root) in ``ws``."""
    roots = [a for a in db.list_agents(ws.id) if a.mode == "interactive" and a.status == "paused"]
    return max(roots, key=lambda a: a.status_since or 0, default=None)


def resume(db: DB, root_id: str, *, watch_pane: bool = True,
           only: set[str] | None = None) -> list[Agent]:
    """Bring a paused session back: the supervisor and its paused workers
    restart in their own workspaces. Claude Code picks up its previous
    conversation (--resume); other CLIs restart on their original task.
    With ``only``, just those agents (if paused) restart."""
    resumed = []
    for a in tree(db, root_id):
        if a.status != "paused" or (only is not None and a.id not in only):
            continue
        ws = db.get_workspace(a.workspace_id)
        if ws is None or not os.path.isdir(ws.path):
            continue
        provider = get_provider(a.provider)
        ref = a.session_ref if provider.name in ("claude", "antigravity", "native") and a.session_ref else None
        if ref and not provider.can_resume(ref):
            ref = None  # nothing was ever said in it: start that agent fresh
        if ref:
            prompt = None
        elif a.task and a.mode in ("handoff", "assign") and provider.launches_process:
            # a.task is the raw task now (see decorate_worker_prompt); a fresh
            # start needs the same decoration it got the first time. A row
            # from before this change stored the already-decorated text, so
            # it gets decorated a second time here; harmless, if redundant.
            prompt = decorate_worker_prompt(
                a.task, a.id, ws, a.done_when, provider, bool(a.headless),
                plan_first=bool(a.plan_first) and a.plan_state != "approved") + RESUME_NOTE
        else:
            prompt = (a.task + RESUME_NOTE) if a.task else None
        if a.dismissed_at is not None:
            db.update_agent(a.id, dismissed_at=None)  # running again: show it again
        _launch(db, a, ws, prompt=prompt, resume=ref,
                watch_pane=watch_pane and a.id == root_id, background_setup=True)
        resumed.append(a)
    return resumed


def get(db: DB, agent_id: str) -> Agent:
    agent = db.get_agent(agent_id)
    if not agent:
        # Allow unambiguous prefixes, like git does for hashes.
        matches = [a for a in db.list_agents() if a.id.startswith(agent_id)]
        if len(matches) == 1:
            return matches[0]
        raise AgentError(f"no agent {agent_id!r}")
    return agent


def ended(db: DB, agent_id: str) -> None:
    """An interactive agent's CLI exited (the person closed the chat): pause
    its whole session. The chat's window, dashboard and workers stop; their
    work is kept for `copse --continue`."""
    agent = db.get_agent(agent_id)
    for _ in range(20):  # a CLI that exits instantly can beat the window id to the DB
        if agent is None or agent.tmux_window:
            break
        time.sleep(0.1)
        agent = db.get_agent(agent_id)
    if agent is None or agent.status in ("paused", "done"):
        return
    pause(db, agent_id)


def find_running(db: DB, ws: Workspace, profile: str) -> Agent | None:
    """The newest live interactive agent of ``profile`` in ``ws``, if any."""
    for a in reversed(db.list_agents(ws.id)):
        if a.profile == profile and a.mode == "interactive" and is_alive(a):
            return a
    return None


def runs_process(agent: Agent) -> bool:
    """False for agents whose work happens outside copse (the subagent provider)."""
    from copse.providers import PROVIDERS

    provider = PROVIDERS.get(agent.provider)
    return provider is None or provider.launches_process


def is_alive(agent: Agent, panes: dict[str, bool] | None = None) -> bool:
    """``panes`` is a pre-fetched ``tmux.list_panes()`` result, shared by a
    whole snapshot so callers don't each shell out for their own agent's
    pane. Omit it to check this one agent's pane directly."""
    if not runs_process(agent):
        # No process to watch: it's at work until its result is recorded.
        return agent.result is None and agent.status not in ("paused", "done")
    if not agent.tmux_window:
        return False
    if panes is not None:
        return panes.get(agent.tmux_window, False)
    return tmux.window_alive(agent.tmux_window)


def pane_owners(db: DB, panes: dict[str, bool] | None = None) -> dict[str, str]:
    """Which agent each recorded pane id belongs to. tmux reuses pane ids once
    its server restarts (a reboot, `tmux kill-server`), so an old agent's
    stored pane can now belong to a newer agent: only the newest agent
    recorded on a pane can be the one running in it.

    A pane that carries a tag (AGENT_TAG, or SIDEBAR_TAG for the dashboard)
    has the last word over the DB: it names its agent itself, or says it's
    nobody's. That covers a pane the DB doesn't know about yet (a launch
    records the pane id a moment after the pane exists) and the sidebar,
    which no agent row ever names. ``panes`` is a pre-fetched
    ``tmux.list_panes()`` snapshot, which already carries the tags; without
    one, this fetches its own."""
    owners = {a.tmux_window: a.id for a in db.list_agents() if a.tmux_window}  # oldest first
    tags = getattr(panes, "tags", None)
    if tags is None:
        tags = tmux.list_panes().tags
    for pane, found in tags.items():
        owners[pane] = found.get(AGENT_TAG, "")  # a sidebar is no agent's pane
    return owners


def owns_pane(db: DB, agent: Agent, owners: dict[str, str] | None = None) -> bool:
    """False when a newer agent has since been recorded on ``agent``'s pane
    id: that pane, if alive, is the newer agent's, never ``agent``'s. Check
    this before touching an agent's window (killing it, reading it).
    ``owners`` is a pre-fetched ``pane_owners`` result, for loops."""
    if not agent.tmux_window:
        return True
    return (pane_owners(db) if owners is None else owners).get(agent.tmux_window) == agent.id


def format_message(db: DB, body: str, sender_id: str | None) -> str:
    if not sender_id:
        return body
    sender = db.get_agent(sender_id)
    who = f"{sender.profile} agent {sender_id}" if sender else f"agent {sender_id}"
    return f"[Message from {who}. Reply with the copse send_message tool, to_agent_id={sender_id}]\n\n{body}"


def message_lead(db: DB, agent: Agent, sender_id: str | None) -> str | None:
    """The line copse types (not pastes) before a message it delivers into an
    agent's chat, so the agent can tell a copse delivery from pasted text of
    unknown origin (see tmux.paste). None for a plain shell, where it would
    become part of the command."""
    if agent.provider == "shell":
        return None
    if not sender_id:
        return "copse delivered this message:"
    sender = db.get_agent(sender_id)
    who = f"{sender.profile} agent {sender_id}" if sender else f"agent {sender_id}"
    return f"copse delivered this message from {who}:"


def send_message(db: DB, to_id: str, body: str, sender_id: str | None = None) -> str:
    """Deliver now if the agent is idle; otherwise queue until it is.
    Returns ``"delivered"`` or ``"queued"``."""
    agent = get(db, to_id)
    if not runs_process(agent):
        raise AgentError(
            f"agent {agent.id} is a subagent run by its supervisor's own Agent tool, so copse "
            "can't message it. Its supervisor can continue it with that tool, or start a new "
            f"subagent in the same worktree; then record the outcome with complete_subagent."
        )
    if not is_alive(agent):
        raise AgentError(f"agent {agent.id} is not running")
    provider = get_provider(agent.provider)
    text = format_message(db, body, sender_id)
    if agent.headless:
        # Its runner starts the next turn with this, or the Stop hook hands it
        # over if a turn is still running.
        db.enqueue(agent.id, text, sender_id)
        return "delivered" if agent.status == "idle" else "queued"
    if not provider.uses_hooks:
        tmux.paste(agent.tmux_window, text, lead=message_lead(db, agent, sender_id))
        return "delivered"
    message_id = db.enqueue(agent.id, text, sender_id)
    if _deliver_to_inbox(db, agent, message_id, text, sender_id):
        return "delivered"
    reconcile(db, agent)
    if db.message_delivered(message_id):
        return "delivered"  # the idle correction above already flushed it
    return "delivered" if flush(db, agent.id) else "queued"


def reconcile(db: DB, agent: Agent, samples: int = 2, gap: float = 0.7) -> Agent:
    """Correct a status the hooks left stale. Claude Code runs no Stop hook
    when a turn is interrupted (Esc), so the agent can sit idle while we still
    think it's busy; after a permission prompt is approved the status stays
    'waiting' until the tool finishes; and an 'idle' status can outlive the
    turn that followed it. The screen must agree across ``samples`` reads
    before we override the hooks."""
    screen_status(db, agent, samples, gap)
    return agent


def screen_status(db: DB, agent: Agent, samples: int = 2, gap: float = 0.7) -> str | None:
    """``reconcile``, returning the status the screen showed consistently
    ('idle', 'processing' or 'waiting'), or None when it can't be read or
    didn't agree across samples.

    An 'idle' agent is only read with ``samples`` >= 2, and only moved to
    'processing' when its provider sees the busy marker right by the input
    box (not just anywhere on screen, where a transcript can quote it). A
    single cheap read, as the dashboard does, leaves it alone. Moving an
    agent to idle also delivers anything queued for it, but only when
    ``samples`` >= 2: a single-sample read must never pop a message into a
    terminal as a side effect of just rendering the dashboard."""
    provider = get_provider(agent.provider)
    if (agent.headless or not provider.uses_hooks
            or agent.status not in ("starting", "idle", "processing", "waiting")):
        return None  # a headless pane shows output, not a TUI to read
    if agent.status == "idle" and samples < 2:
        return None
    seen = set()
    for i in range(samples):
        if i:
            time.sleep(gap)
        try:
            screen = tmux.capture(agent.tmux_window, lines=40)
        except tmux.TmuxError:
            return None
        state = provider.screen_state(screen)
        if agent.status == "idle" and state == "busy" and not provider.busy_in_footer(screen):
            state = None
        seen.add(state)
    if len(seen) != 1:
        return None
    state = seen.pop()
    new = {"idle": "idle", "busy": "processing", "waiting": "waiting"}.get(state or "")
    if agent.status == "starting" and new != "waiting":
        # Its hooks say when it's ready; the screen only adds that it's
        # stuck on a dialog before they could (e.g. folder trust).
        return None
    if new and new != agent.status:
        db.set_status(agent.id, new, only_if=agent.status)
        agent.status = new
        if new == "idle" and samples >= 2 and db.pending_count(agent.id):
            try:
                flush(db, agent.id)
            except tmux.TmuxError:
                pass  # stays queued; its Stop hook hands it over
    return new


def _deliver_to_inbox(db: DB, agent: Agent, message_id: int, text: str,
                      sender_id: str | None) -> bool:
    """Send a queued message through Claude Code's own inbox (see
    copse.inbox), where it doesn't wait for the agent to go idle. Returns
    whether it went; if not, it stays queued for the pane."""
    from copse import inbox

    if not inbox.usable(agent):
        return False
    sender = db.get_agent(sender_id) if sender_id else None
    who = f"copse {sender.profile} {sender.id}" if sender else "copse"
    if not inbox.send(agent, text, sender=who):
        return False
    db.mark_delivered(message_id)
    return True


def flush(db: DB, agent_id: str) -> bool:
    """Deliver the agent's oldest pending message: through its inbox if it
    has one, else typed into its pane once it's idle. Returns True if
    something was delivered."""
    from copse import inbox

    agent = db.get_agent(agent_id)
    if agent is None or agent.headless:
        return False  # its runner takes messages from the inbox itself
    if inbox.usable(agent):
        delivered = False
        while (msg := db.pop_pending(agent_id)) is not None:
            if inbox.send(agent, msg.body, sender="copse"):
                delivered = True
            else:
                db.enqueue(agent_id, msg.body, msg.sender_id)  # back in the queue, for the pane
                break
        if delivered:
            return True
    if db.pending_count(agent_id) == 0 or not db.claim_idle(agent_id):
        return False
    if _paste_blocked(agent):
        # Leave it queued: the next flush (a later message, or a resumed
        # session's start-up) gets another chance.
        db.set_status(agent_id, "idle", only_if="processing")
        return False
    msg = db.pop_pending(agent_id)
    if not msg:
        db.set_status(agent_id, "idle", only_if="processing")
        return False
    agent = db.get_agent(agent_id)
    assert agent is not None
    tmux.paste(agent.tmux_window, msg.body, lead=message_lead(db, agent, msg.sender_id))
    return True


def _paste_blocked(agent: Agent) -> bool:
    """Whether pasting into ``agent``'s pane right now would land somewhere
    other than its chat: over text the person is mid-typing (interactive
    only), into Claude Code's background-session launcher, or into a pane
    whose foreground session has changed to something else entirely (any
    mode -- a blind paste in either case reaches the wrong conversation or
    starts a brand-new one). A background view gets one Escape and a
    re-check before giving up."""
    provider = get_provider(agent.provider)
    interactive = agent.mode == "interactive"
    try:
        screen = tmux.capture(agent.tmux_window, lines=40, escapes=True)
    except tmux.TmuxError:
        return False
    reason = provider.paste_blocked(screen, interactive)
    if reason == "background":
        tmux.send_keys(agent.tmux_window, "Escape")
        time.sleep(0.3)
        try:
            screen = tmux.capture(agent.tmux_window, lines=40, escapes=True)
        except tmux.TmuxError:
            return False
        reason = provider.paste_blocked(screen, interactive)
    return reason is not None


# -- subagent provider -------------------------------------------------------------


def subagent_prompt(profile_prompt: str, task: str, ws: Workspace, done_when: str | None) -> str:
    """What the supervisor passes to its Agent tool for a subagent worker."""
    parts = [profile_prompt.strip(), "Task:\n" + task.strip()]
    if done_when:
        parts.append(f"Finish line: {done_when.strip()}")
    body = "\n\n".join(p for p in parts if p)
    return body + SUBAGENT_FOOTER.format(path=ws.path, branch=ws.branch,
                                         guidance=worker_guidance(ws))


def subagent_brief(agent: Agent, ws: Workspace) -> str:
    """The handoff/assign reply for a subagent worker: what the caller does next."""
    base = f" (cut from {ws.base_branch})" if ws.base_branch else ""
    return (
        f"copse made workspace {ws.id} for agent {agent.id} ({agent.profile}) but started no "
        "process: your own subagent does this task.\n"
        f"  path:   {ws.path}\n"
        f"  branch: {ws.branch}{base}\n"
        f"  agent:  {agent.id}\n\n"
        "Next:\n"
        "1. Run it with your Agent tool (a general-purpose subagent), passing the prompt "
        "between the markers exactly as written. With several tasks, you can run their "
        "subagents in parallel.\n"
        f"2. When it returns, call complete_subagent(agent_id=\"{agent.id}\", result=<its "
        "summary>). Until then copse shows it as working.\n"
        f"3. Review with workspace_diff(\"{ws.id}\"), then merge_workspace and "
        "remove_workspace as for any worker.\n\n"
        f"----- prompt for your Agent tool -----\n{agent.task}\n----- end of prompt -----"
    )


def complete_subagent(db: DB, agent_id: str, result: str) -> Agent:
    """Record a subagent worker's outcome: it shows as done, and its workspace
    is reviewed, merged and removed like any other."""
    agent = get(db, agent_id)
    if runs_process(agent):
        raise AgentError(
            f"agent {agent.id} runs its own {agent.provider} process; it reports with "
            "report_result itself"
        )
    db.set_result(agent.id, result)
    db.set_status(agent.id, "done")
    return db.get_agent(agent.id) or agent


def report_result(db: DB, agent_id: str, result: str, forward: bool = True) -> str:
    """Record a worker's or reviewer's result. A piped worker's report goes
    to the pipeline (which reviews and merges the branch); otherwise, and
    with ``forward``, it's sent to the parent as a message."""
    from copse import history, pipeline, usage as usage_mod

    agent = get(db, agent_id)
    db.set_result(agent.id, result)
    ws = db.get_workspace(agent.workspace_id)
    # Usage and history are extras: never let them stop the result arriving.
    try:
        u = usage_mod.agent_usage(db, agent)
    except Exception:
        log.exception("copse: couldn't read usage for %s", agent.id)
        u = None
    if ws:
        history.record_safely(
            db, ws.repo_root, "review" if agent.mode == "review" else "worker_result",
            agent=agent, usage=u, branch=ws.branch, task=agent.task, result=result,
        )
    forwarded = f"{result}\n\n{usage_mod.summary_line(u)}" if u and u.total else result
    if ws and agent.mode in ("handoff", "handoff_detached", "assign"):
        warm_checks(ws)
        if forward and pipeline.on_report(db, agent, ws, forwarded):
            return ("result recorded. copse is having your branch reviewed; it will merge it, "
                    "or send you the review's findings to fix.")
    if forward and agent.mode in FORWARDING_MODES and agent.parent_id and db.get_agent(agent.parent_id):
        where = f" on branch `{ws.branch}` (workspace {ws.id})" if ws else ""
        send_message(
            db, agent.parent_id,
            f"Assigned task finished{where}.\n\n{forwarded}",
            sender_id=agent.id,
        )
        return "result recorded and sent to your supervisor"
    return "result recorded"


def warm_checks(ws: Workspace) -> None:
    """A worker just reported on ``ws``: run the repo's checks on its branch
    now, detached, so the cached result is ready for the review and the merge
    gate. Only for a clean worktree (a dirty one can't be cached) in a repo
    with checks."""
    from copse.config import load_repo_config
    from copse.providers import copse_invocation

    try:
        if not load_repo_config(ws.repo_root).checks or git.dirty_files(ws.path):
            return
    except (ValueError, git.GitError):
        return
    _detach([*copse_invocation(), "_warm-checks", ws.id])


def _detach(args: list[str]) -> None:
    """Start a copse helper that outlives this process."""
    subprocess.Popen(args, start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for_result(db: DB, agent_id: str, timeout: float, poll: float = 2.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        agent = db.get_agent(agent_id)
        if agent is None:
            raise AgentError(f"agent {agent_id} was removed before reporting")
        if agent.result is not None:
            return agent.result
        if not is_alive(agent):
            screen = ""
            try:
                screen = tmux.capture(agent.tmux_window, lines=40)
            except tmux.TmuxError:
                pass
            raise AgentError(f"agent {agent_id} exited without reporting. Last output:\n{screen}")
        time.sleep(poll)
    agent = db.get_agent(agent_id)
    hint = " It is waiting for a permission approval: attach to its workspace to answer." if agent and agent.status == "waiting" else ""
    raise StillRunning(
        f"agent {agent_id} hasn't reported yet after {int(timeout)}s "
        f"(status: {agent.status if agent else '?'}).{hint}"
    )


def detach(db: DB, agent_id: str) -> str | None:
    """Stop waiting synchronously on a handoff worker: from now on its result
    is forwarded to its parent as a message. Returns the result instead if it
    arrived in the meantime (so it's never lost between the two paths)."""
    db.update_agent(agent_id, mode="handoff_detached")
    agent = db.get_agent(agent_id)
    return agent.result if agent else None


def collect(db: DB, parent_id: str | None, worker_id: str) -> None:
    """The parent received the worker's result directly; drop the duplicate
    copy that forwarding may have queued in the parent's inbox."""
    if parent_id:
        db.drop_pending(parent_id, worker_id)


def submit_review(db: DB, caller_id: str, approved: bool, summary: str) -> str:
    """A reviewer's verdict: recorded for the merge gate, handed to the
    pipeline if the branch is piped, else sent to the supervisor. The
    reviewer is closed shortly after."""
    from copse import autopilot, gates, pipeline

    caller = db.get_agent(caller_id)
    ws = db.get_workspace(caller.workspace_id) if caller else None
    if not caller or caller.mode != "review" or ws is None:
        return "Only a reviewer started with request_review can submit a review."
    sha = gates.head(ws)
    db.add_review(ws.id, sha, caller.id, approved, summary)
    if approved:
        db.bump_progress(autopilot.root_of(db, caller.id))
    pipeline.note_review(db, ws, approved)
    verdict = "APPROVED" if approved else "CHANGES REQUESTED"
    text = f"Review of {ws.branch} (workspace {ws.id}) at {sha[:8]}: {verdict}\n\n{summary}"
    handled = pipeline.on_review(db, caller, ws, approved, summary)
    report_result(db, caller.id, text, forward=not handled)
    close_later(caller.id)
    if handled:
        return f"Review recorded ({verdict}); copse takes it from here. You're done."
    return f"Review recorded ({verdict}) and sent to your supervisor. You're done."


def close_later(agent_id: str, delay: float = 5.0) -> None:
    """Stop an agent shortly, from a detached process: used by an agent's own
    tool call, which must return before its CLI goes away."""
    from copse.providers import copse_invocation

    subprocess.Popen(
        [*copse_invocation(), "_close", agent_id, "--delay", str(delay)],
        start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _stop(db: DB, agent: Agent) -> None:
    """Stop an agent: its window, and every process of its that outlives the
    window (Claude Code's daemon can host the real session; see copse.procs).
    A pane id a newer agent has since been given is left alone (see owns_pane)."""
    from copse import procs

    window = agent.tmux_window if owns_pane(db, agent) else ""
    pane_pids = tmux.window_pids(window) if window else []
    if window:
        tmux.kill_window(window)
    procs.stop([agent.id], {agent.id: pane_pids})
    db.end_native_subagents(agent.id)


def pause_worker(db: DB, agent: Agent) -> None:
    """Stop one worker but keep its work, as ``pause`` does for a session: its
    worktree, branch, queued messages and CLI session stay, and ``resume``
    brings it back."""
    _stop(db, agent)
    db.set_status(agent.id, "paused")


def kill(db: DB, agent_id: str) -> None:
    agent = get(db, agent_id)
    _stop(db, agent)
    db.delete_agent(agent.id)


def close(db: DB, agent_id: str, panes: dict[str, bool] | None = None) -> Agent:
    """Hide an agent from the sidebar for good, stopping it first if it's
    still running. Unlike ``kill`` its record stays, and nothing on disk is
    touched: its worktree and branch keep any unmerged work, `copse ls` still
    lists it, and a paused session can still be continued (which shows it
    again). Returns the agent as it was before closing."""
    agent = get(db, agent_id)
    if is_alive(agent, panes) and owns_pane(db, agent):
        _stop(db, agent)
        db.set_status(agent.id, "done" if agent.result is not None else "paused")
    else:
        from copse import procs

        procs.stop([agent.id])  # anything left running after its window went
    db.update_agent(agent.id, dismissed_at=time.time())
    return agent


# -- delegation (used by the MCP tools) ------------------------------------


def _branch_from_task(profile: str, task: str, agent_hint: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", task.lower())[:5]
    return f"copse/{profile}/{'-'.join(words) or 'task'}-{agent_hint}"


def delegate(
    db: DB,
    caller: Agent | None,
    caller_ws: Workspace,
    profile: str,
    task: str,
    mode: str,
    *,
    isolate: bool = True,
    branch: str | None = None,
    done_when: str | None = None,
    plan_first: bool | None = None,
) -> tuple[Agent, Workspace]:
    """Start a worker. ``plan_first`` (None: the repo's ``plan_first`` config)
    makes it get a plan approved before it may edit. With ``isolate``, the worker gets a new worktree whose
    branch starts from the caller's current branch, so it sees the caller's
    committed work, and nobody edits the same files. Refuses beyond the
    repo's ``max_agents`` workers running at once."""
    from copse import autopilot as pilot
    from copse.config import load_repo_config

    try:
        pilot.check_capacity(db, caller.id if caller else None, load_repo_config(caller_ws.repo_root))
    except pilot.AutopilotError as e:
        raise AgentError(str(e)) from e
    if plan_first is None:
        plan_first = load_repo_config(caller_ws.repo_root).plan_first
    if isolate:
        caller_ws = workspaces.refresh_branch(db, caller_ws)
        # merge_into applies to the supervisor's workers; a worker's own
        # sub-workers still branch from (and merge back into) its branch.
        merge_into = load_repo_config(caller_ws.repo_root).merge_into
        base = merge_into if merge_into and not (caller and caller.parent_id) else caller_ws.branch
        branch = branch or _branch_from_task(profile, task, new_id()[:4])
        start = base if git.branch_exists(caller_ws.repo_root, base) else "HEAD"
        created = workspaces.create(
            db, caller_ws.path, branch, base, fetch=False, start=start,
        )
        if created.setup and not created.setup.ok:
            raise AgentError(f"workspace setup failed:\n{created.setup.log}")
        ws = created.workspace
    else:
        ws = caller_ws
    # Startup dialogs are handled by the detached _after-launch helper, so
    # handoff/assign return as soon as the window exists instead of polling
    # the new pane for up to 30 seconds.
    agent = spawn(
        db, ws, profile, prompt=task, parent_id=caller.id if caller else None, mode=mode,
        done_when=done_when, background_setup=True, plan_first=bool(plan_first),
    )
    return agent, ws


TASK_TRIM = 2000  # a worker's task can be long; the reviewer needs the gist, not the whole thing


def workspace_worker(db: DB, ws: Workspace) -> Agent | None:
    """The worker whose task produced the code in ``ws``, if any: the earliest
    handoff/assign agent in the workspace (excludes reviewers, which run there
    too). Used to give a reviewer the original task and finish line."""
    workers = [a for a in db.list_agents(ws.id) if a.mode in ("handoff", "handoff_detached", "assign")]
    return workers[0] if workers else None


def is_linear_since(ws: Workspace, prev_sha: str) -> bool:
    """Whether ``prev_sha`` is a plain ancestor of HEAD with no merge commits
    between them, i.e. ``git diff prev_sha..HEAD`` alone captures everything
    new since that commit. False after a rebase (prev_sha is no longer
    reachable from HEAD) or a merge (the range isn't just the worker's own
    commits)."""
    if not git.ok(["merge-base", "--is-ancestor", prev_sha, "HEAD"], ws.path):
        return False
    return not git.out(["rev-list", "--merges", f"{prev_sha}..HEAD"], ws.path)


def default_review_profile(cfg: RepoConfig, worker: Agent | None) -> str:
    """The reviewer profile to use when none was asked for explicitly:
    ``cfg.review_profile`` if set, else the built-in Codex reviewer when
    Codex is installed and the worker being reviewed ran on Claude (so the
    review comes from a different model), else the built-in local reviewer
    when the worker ran on Claude, Codex is missing and the local model
    answers its endpoint probe, else ``cfg.reviewer``."""
    if cfg.review_profile:
        return cfg.review_profile
    if worker and worker.provider == "claude":
        if shutil.which("codex"):
            return "reviewer-codex"
        if _local_reviewer_available():
            return "reviewer-local"
    return cfg.reviewer


def _local_reviewer_available() -> bool:
    """Whether the built-in ``reviewer-local`` profile's model is served
    right now. Any failure counts as not available."""
    try:
        from copse.native import runner

        endpoint = runner.endpoint_for(load_profile("reviewer-local"))
        ok, detail = runner.probe(endpoint, timeout=1.0)
    except Exception:
        return False
    return bool(ok) and "is available" in detail


def request_review(db: DB, caller: Agent | None, ws: Workspace, profile: str | None = None,
                   focus: str | None = None, cfg: RepoConfig | None = None) -> Agent:
    """Start a reviewer in a worker's workspace right away. Its verdict is
    recorded for the merge gate and forwarded to ``caller`` as a message.
    ``profile`` picks the reviewer profile; None uses
    ``default_review_profile``.

    Raises ``AgentError`` if the chosen profile (explicit or picked) doesn't
    exist, or if it uses the codex provider but codex isn't on PATH -- rather
    than spawning a reviewer doomed to fail in a dead pane.

    ``cfg.checks`` are NOT run here (that would block the caller on the full
    suite): the caller runs them in the background and delivers a pass/fail
    summary to the reviewer as a message once they finish, via
    ``deliver_check_summary``. If the workspace already has a review at an
    earlier commit, the reviewer is pointed at just what changed since then,
    when that's a plain diff (see ``is_linear_since``)."""
    from copse import gates
    from copse.config import load_repo_config

    cfg = cfg or load_repo_config(ws.repo_root)
    worker = workspace_worker(db, ws)
    profile = profile or default_review_profile(cfg, worker)
    try:
        chosen = load_profile(profile, ws.repo_root)
    except KeyError as e:
        raise AgentError(str(e)) from e
    if chosen.provider == "codex" and not shutil.which("codex"):
        raise AgentError(
            f"reviewer profile {profile!r} uses the codex provider, but codex isn't on PATH; "
            "install it, or set review_profile (or pass profile) to a different reviewer"
        )

    base = ws.base_branch or "the base branch"
    task = (f"Review the changes on branch `{ws.branch}` (workspace {ws.id}) against `{base}`: "
            f"use the copse workspace_diff tool, or run `git diff {base}...HEAD` (no `$(...)`: "
            "it isn't pre-approved). "
            "Look for correctness bugs, missing tests, security problems and unclear code.")

    if worker and worker.task:
        task += f"\n\nThe worker's original task:\n{worker.task.strip()[:TASK_TRIM]}"
    if worker and worker.done_when:
        task += f"\n\nIts finish line: {worker.done_when.strip()}"

    if cfg and cfg.checks:
        task += ("\n\nThe repo's checks are running now; a pass/fail summary will arrive as a "
                 "message shortly. Review the diff meanwhile, and don't call submit_review "
                 "until you've received it. If about 10 minutes pass with no such message, "
                 "submit anyway and say in your summary that the check results never arrived; "
                 "don't run the whole suite yourself to compensate.")

    prev = db.last_review(ws.id)
    sha = gates.head(ws)
    if prev and prev.sha != sha:
        prior = f"\n\nA previous review at {prev.sha[:8]} found:\n{(prev.summary or '').strip()}\n\n"
        if is_linear_since(ws, prev.sha):
            task += (prior + f"Focus on what changed since then (`git diff {prev.sha}..HEAD`), "
                     "plus a final sanity pass over the rest; you don't need to re-review it "
                     "from scratch.")
        else:
            task += (prior + "The branch has diverged since then (for example a rebase or a "
                     "merge), so review the whole current diff rather than just the recent "
                     "changes.")

    if focus:
        task += f"\n\nFocus: {focus}"
    return spawn(db, ws, profile, prompt=task, parent_id=caller.id if caller else None,
                 mode="review", background_setup=True)


def deliver_check_summary(db: DB, reviewer_id: str, ws: Workspace, cfg: RepoConfig) -> None:
    """Run ``cfg.checks`` for ``ws`` and deliver a pass/fail summary to the
    reviewer's inbox: delivered right away if it's idle, or handed over at its
    next Stop, exactly like any other queued message (see ``send_message``).
    Meant to run from a detached process started by request_review, so it
    outlives the MCP server call that kicked it off. Always delivers
    something, even if a check crashes, since the reviewer was told to wait
    for this before approving -- unless the reviewer is no longer there to
    receive it by the time the checks finish."""
    from copse import gates

    if db.get_agent(reviewer_id) is None:
        return
    sha = gates.head(ws)  # label with the commit the checks ran on
    try:
        summary = gates.check_summary(db, ws, cfg)
    except Exception as e:
        summary = f"(running the checks crashed: {e})"

    reviewer = db.get_agent(reviewer_id)
    if reviewer is None or reviewer.result is not None or reviewer.status == "done":
        return  # it submitted its review, was closed, or was removed while the checks ran

    text = (f"Checks for {ws.branch} at {sha[:8]}:\n\n{summary}" if summary
            else "No checks are configured for this repo.")
    db.enqueue(reviewer_id, text, None)
    flush(db, reviewer_id)


# -- hook entry point --------------------------------------------------------


PLAN_GATED_TOOLS = ("Edit", "Write", "NotebookEdit")


def submit_plan(db: DB, agent_id: str, plan: str) -> str:
    """A plan_first worker proposes its plan: it goes to the parent as a
    message and the worker waits (plan_state 'proposed') for approve_plan."""
    agent = get(db, agent_id)
    if not agent.plan_first:
        return "This task isn't plan-first; go ahead and do it."
    if agent.plan_state == "approved":
        return "Your plan is already approved; go ahead."
    if not plan.strip():
        return "Give the plan."
    if not agent.parent_id:
        raise AgentError("no supervisor to send the plan to")
    db.update_agent(agent.id, plan_state="proposed")
    send_message(db, agent.parent_id,
                 f"Worker {agent.id} proposes this plan and is waiting for your decision "
                 f"(approve_plan agent_id={agent.id}):\n\n{plan.strip()}", sender_id=agent.id)
    return "Plan sent. Stop now and wait for your supervisor's decision; don't edit files until it's approved."


def approve_plan(db: DB, caller_id: str | None, agent_id: str, feedback: str = "",
                 approved: bool = True) -> str:
    """The worker's parent decides on its proposed plan; the worker is told."""
    agent = get(db, agent_id)
    if not agent.plan_first:
        raise AgentError(f"{agent.id} isn't a plan-first worker")
    if caller_id is None or agent.parent_id != caller_id:
        raise AgentError(f"only {agent.id}'s supervisor can decide on its plan")
    if agent.plan_state != "proposed":
        raise AgentError(f"{agent.id} has no plan awaiting a decision")
    feedback = feedback.strip()
    if approved:
        db.update_agent(agent.id, plan_state="approved")
        text = "Your plan is approved: go ahead and implement it."
    else:
        db.update_agent(agent.id, plan_state="revise")
        text = "Your plan needs changes: revise it and call submit_plan again. Don't edit files yet."
    if feedback:
        text += f"\n\nSupervisor's feedback: {feedback}"
    send_message(db, agent.id, text, sender_id=caller_id)
    return f"{'Approved' if approved else 'Asked for a revised plan from'} {agent.id}."


def pre_tool_decision(db: DB, agent: Agent, payload: dict) -> dict | None:
    """Claude Code's PreToolUse hook: approve a Bash command when every
    simple command in it matches one of the profile's ``allowed_tools``
    rules (``cd`` into the worker's own worktree counts), the same reading
    the native loop gives those rules. Claude Code matches a rule against a
    compound command as a whole, so ``cd sub && git status`` would prompt
    even with ``Bash(git status:*)`` allowed. None leaves the decision to
    Claude Code as usual. The one denial: a file edit by a plan_first worker
    whose plan isn't approved yet."""
    if payload.get("tool_name") in PLAN_GATED_TOOLS and agent.plan_first and agent.plan_state != "approved":
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "copse: this task is plan-first. Don't edit files yet: call the submit_plan tool "
                "(copse MCP server) with your plan and wait for your supervisor's approval, "
                "which arrives as a message."),
        }}
    if payload.get("tool_name") != "Bash":
        return None
    command = str((payload.get("tool_input") or {}).get("command", ""))
    ws = db.get_workspace(agent.workspace_id)
    try:
        profile = load_profile(agent.profile, ws.repo_root if ws else None)
    except Exception:
        return None
    from copse.native.permissions import Permissions, uncovered_part

    specs = [spec for name, spec in Permissions(profile.permission_mode, profile.allowed_tools).rules
             if name == "Bash"]
    if not specs:
        return None
    if not any(spec in (None, "", "*") for spec in specs):
        if uncovered_part(specs, command, cd_root=ws.path if ws else None) is not None:
            return None
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": "copse: every part of the command matches the profile's allowed_tools",
    }}


def handle_hook(db: DB, agent_id: str, event: str, payload: dict) -> dict | None:
    """Called from ``copse _hook <event>`` inside the agent's own process tree.
    Returns JSON for Claude Code to read on stdout, or None."""
    agent = db.get_agent(agent_id)
    if agent is None:
        return None
    sid = payload.get("session_id")
    if sid and sid != agent.session_ref:
        db.update_agent(agent_id, session_ref=str(sid))
    transcript = payload.get("transcript_path")
    if transcript and agent.provider == "claude" and transcript != agent.transcript_path:
        db.update_agent(agent_id, transcript_path=str(transcript))

    if event == "session-start":
        from copse import inbox

        inbox.record_from_environment(db, agent_id)
        db.set_status(agent_id, "idle", only_if="starting")
        # 'waiting' before the session even started was its trust dialog
        # (see screen_status), which has now been answered.
        db.set_status(agent_id, "idle", only_if="waiting")
        if db.pending_count(agent_id) and not agent.headless:
            # Claude Code hasn't drawn its input box yet; deliver shortly after,
            # from a detached process so this hook returns immediately.
            import subprocess

            from copse.providers import copse_invocation

            subprocess.Popen(
                [*copse_invocation(), "_flush", agent_id, "--delay",
                 str(get_provider(agent.provider).ready_delay)],
                start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    elif event == "prompt-submit":
        db.set_status(agent_id, "processing")
        # Queued messages (worker results) are typed into an idle chat too;
        # only a prompt copse didn't deliver is the user speaking.
        prompt = str(payload.get("prompt", ""))
        if agent.mode == "interactive" and not db.recently_delivered(agent_id, prompt):
            from copse import autopilot as pilot

            pilot.user_spoke(db, agent)
    elif event == "notification":
        text = str(payload.get("message", "")).lower()
        if "permission" in text or "approval" in text:
            db.set_status(agent_id, "waiting")
    elif event == "pre-tool":
        return pre_tool_decision(db, agent, payload)
    elif event == "tool-done":
        db.set_status(agent_id, "processing", only_if="waiting")
    elif event == "stop":
        msg = db.pop_pending(agent_id)
        if msg:
            db.set_status(agent_id, "processing")
            return {"decision": "block", "reason": msg.body}
        needs_report = agent.mode in REPORTING_MODES and agent.result is None
        if needs_report and not payload.get("stop_hook_active"):
            db.set_status(agent_id, "processing")
            if agent.mode == "review":
                return {
                    "decision": "block",
                    "reason": "You haven't called the copse `submit_review` tool yet. "
                    "Call it now with your verdict and findings.",
                }
            return {
                "decision": "block",
                "reason": "You haven't called the copse `report_result` tool yet. "
                "If your task is finished, commit your work and call it now. "
                "If you are blocked, call it with a description of what's blocking you.",
            }
        if needs_report:
            db.set_status(agent_id, "idle")
            tell_parent_unreported(db, agent)
            return None
        if agent.mode == "interactive":
            from copse import autopilot as pilot

            decision = pilot.on_stop(db, agent, payload)
            if decision:
                db.set_status(agent_id, "processing")
                return decision
        db.set_status(agent_id, "idle")
    elif event == "codex-notify":
        # Codex's notify command runs when a turn completes; it can't block the
        # stop, so a queued message is typed into the pane instead.
        from copse import quota

        try:
            quota.refresh_codex()
        except OSError:
            pass
        if payload.get("type") == "agent-turn-complete":
            db.set_status(agent_id, "idle")
            if db.pending_count(agent_id) and not agent.headless:
                flush(db, agent_id)
    elif event == "stop-failure":
        # The turn ended on an API error; no Stop hook follows.
        db.set_status(agent_id, "idle")
        if "rate_limit" in json.dumps(payload):
            from copse import autopilot as pilot

            pilot.limit_reached(db, agent)
    elif event == "subagent-start":
        # A crash can skip SubagentStop, so this doesn't touch agent.status:
        # the sidebar hides a subagent that's been "running" too long instead.
        sub_id = payload.get("agent_id")
        if sub_id:
            db.start_native_subagent(str(sub_id), agent_id, payload.get("agent_type"))
    elif event == "subagent-stop":
        sub_id = payload.get("agent_id")
        if sub_id:
            db.stop_native_subagent(str(sub_id))
    return None


def agent_for_session(db: DB, session_id: object) -> str | None:
    """The newest agent whose CLI session is ``session_id``, if any."""
    if not session_id:
        return None
    matches = [a for a in db.list_agents() if a.session_ref == str(session_id)]
    return matches[-1].id if matches else None


def tell_parent_unreported(db: DB, agent: Agent) -> None:
    """A worker stopped again after being reminded to report, still without a
    result. It won't be reminded again on its own, and its supervisor has
    usually gone idle waiting for it with nothing left to wake it, so tell the
    supervisor (queued if it's busy, delivered now if it's idle)."""
    if agent.mode not in FORWARDING_MODES or not agent.parent_id:
        return  # a synchronous handoff's caller is still waiting on it
    if db.get_agent(agent.parent_id) is None:
        return
    ws = db.get_workspace(agent.workspace_id)
    where = f" (branch `{ws.branch}`, workspace {ws.id})" if ws else ""
    tool = "submit_review" if agent.mode == "review" else "report_result"
    body = (
        f"Worker {agent.id}{where} stopped without calling {tool}, and won't be reminded "
        f"again on its own. Check on it: workspace_diff to see what it did, send_message to "
        f"ask it to finish or report, or remove_workspace if the work is abandoned."
    )
    try:
        send_message(db, agent.parent_id, body, agent.id)
    except (AgentError, tmux.TmuxError):
        # The parent isn't running (or can't take messages): the notice is
        # deliberately dropped here, since there's nothing left to tell.
        pass


def hook_main(db: DB, agent_id: str, event: str, stdin_text: str, trusted: bool = True) -> str:
    """``trusted`` is False when ``agent_id`` came from the environment,
    which can be stale (see ClaudeCode._hook): then an agent already known
    by the payload's session id wins over it."""
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if not trusted:
        agent_id = agent_for_session(db, payload.get("session_id")) or agent_id
    out = handle_hook(db, agent_id, event, payload)
    return json.dumps(out) if out else ""
