"""copse command line."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import typer

from copse import agents, git, tmux, view, workspaces
from copse import history as history_mod
from copse.usage import format_tokens
from copse.config import write_template
from copse.db import DB, Workspace
from copse.profiles import list_profiles

app = typer.Typer(add_completion=False, help="""copse: run coding agents in parallel, each on its own git branch.

Run `copse` with no arguments to open (or reopen) a supervisor chat here, with
the live dashboard underneath. Outside a git repo it starts a scratch session;
`copse transfer <repo>` moves that work into a real repository later.

Paid features (hosted learning, per-worktree services, team policies, CI):
`copse account` shows what you have and how to get the rest.""")
agent_app = typer.Typer(no_args_is_help=True, help="Manage agents.")
app.add_typer(agent_app, name="agent")


def _fail(msg: str) -> None:
    typer.secho(msg, fg="red", err=True)
    raise typer.Exit(1)


def _ws(db: DB, ref: Optional[str]) -> Workspace:
    try:
        if ref:
            return workspaces.resolve(db, ref)
        ws = workspaces.current(db)
        if ws:
            return ws
    except workspaces.WorkspaceError as e:
        _fail(str(e))
    _fail("not inside a copse workspace; pass a workspace name")
    raise AssertionError


def _attach(ws: Workspace, window: str | None = None) -> None:
    if not tmux.has_session(ws.tmux_session):
        tmux.ensure_session(ws.tmux_session, ws.path, workspaces.workspace_env(ws))
    if window:
        tmux.select_window(window)
    if os.environ.get("TMUX"):
        subprocess.run([*tmux._base(), "switch-client", "-t", window or f"={ws.tmux_session}"])
        return
    subprocess.run(tmux.attach_command(ws.tmux_session))
    _after_detach(ws)


def _after_detach(ws: Workspace) -> None:
    """Back at the user's own prompt: say what happened and what's still running."""
    from copse import scratch

    if tmux.has_session(ws.tmux_session):
        typer.echo("Detached; everything is still running. Run `copse` here to reopen.")
        return
    typer.echo("copse session paused: nothing is running, and all work is saved.")
    typer.echo("  `copse continue` picks it up where you left off; `copse` starts fresh.")
    if scratch.is_scratch(ws.path) and not scratch.transferred_to(ws.path):
        typer.echo(f"  Scratch work is saved in {ws.path}; `copse transfer <repo>` moves it into a repo.")


def _run(fn, *args, **kwargs):
    from copse.scratch import ScratchError

    try:
        return fn(*args, **kwargs)
    except (git.GitError, workspaces.WorkspaceError, agents.AgentError,
            tmux.TmuxError, ScratchError, KeyError, ValueError) as e:
        _fail(str(e).strip("'\""))


# -- workspaces --------------------------------------------------------------


@app.command()
def init(
    yes: bool = typer.Option(False, "--yes", "-y", help="Write the config without asking."),
) -> None:
    """Set copse up in this repo: detect setup and test commands, write .copse/config.json, check the tools.

    Reads the repo's lockfiles and manifests to fill in `setup` (what a new
    worktree needs), `checks` (what must pass before a branch merges) and
    `copy` (git-ignored env files), then runs the same checks as `copse
    doctor`. An existing config is left alone."""
    from copse import detect, doctor as doctor_mod
    from copse.config import CONFIG_DIR, CONFIG_FILE

    try:
        root = git.main_repo_root(os.getcwd())
    except git.GitError:
        _fail("not in a git repo. Run `git init` first, or just run `copse`: "
              "outside a repo it starts a scratch session you can transfer later.")
    path = Path(root) / CONFIG_DIR / CONFIG_FILE
    found = detect.detect(root)
    typer.echo(f"detected: {', '.join(found.stacks) or 'no known stack'}")
    values = {"setup": found.setup, "checks": found.checks, "copy": found.copy}
    for key, cmds in values.items():
        typer.echo(f"  {key:<7} {'; '.join(cmds) if cmds else '-'}")
    for note in found.notes:
        typer.secho(f"  ! {note}", fg="yellow")
    if path.exists():
        typer.echo(f"kept {path} (it already exists; add anything above it's missing)")
    else:
        if sys.stdin.isatty() and not yes:
            typer.confirm(f"write {path.relative_to(root)}?", default=True, abort=True)
        write_template(root, values)
        typer.secho(f"✓ wrote {path}", fg="green")
    typer.echo("")
    results = [c for c in doctor_mod.checks(root) if c.level != doctor_mod.OK]
    # Optional pieces (Codex, a local model, ...) are one line on a first run.
    optional = [c.name for c in results if doctor_mod.is_optional(c)]
    shown = [c for c in results if not doctor_mod.is_optional(c)]
    if shown:
        typer.echo(doctor_mod.render(shown))
    if optional:
        typer.echo(f"optional, not set up: {', '.join(optional)} (`copse doctor` says how)")
    if any(c.level == doctor_mod.FAIL for c in results):
        raise typer.Exit(1)
    typer.secho("Ready. Commit .copse/config.json, then run `copse` and tell the supervisor what to build.",
                fg="green")


@app.command()
def demo(
    local: bool = typer.Option(False, "--local", help="Workers and reviewers on a local model (Ollama) instead of Claude/Codex."),
    attach: bool = typer.Option(True, "--attach/--no-attach"),
) -> None:
    """Watch copse work on a tiny practice repo: two workers in parallel, reviews, gated merges, verified milestones.

    Creates a small Python repo under ~/.copse/demo/ with two failing test
    files and a two-milestone goal, then starts the supervisor there with
    autopilot on. Takes a few minutes; nothing is created where you run it."""
    from copse import demo as demo_mod

    root = demo_mod.create(local=local)
    typer.echo(f"demo repo: {root}")
    os.chdir(root)
    start(agent="supervisor", prompt=None, provider=None, attach=attach, watch=True,
          autopilot=True, branch=None, worktree=None)


@app.command()
def new(
    branch: Optional[str] = typer.Argument(None, help="Branch for the workspace (created if needed). Required unless --pr is given."),
    base: Optional[str] = typer.Option(None, "--base", "-b", help="Base branch (default: repo default). Not allowed with --pr."),
    pr: Optional[int] = typer.Option(None, "--pr", help="Check out this GitHub PR's head branch (via `gh`), based on the PR's base branch. Don't also pass BRANCH or --base; the head branch is always fetched."),
    agent: Optional[str] = typer.Option(None, "--agent", "-a", help="Agent profile to start (default from config; 'none' for no agent)."),
    prompt: Optional[str] = typer.Option(None, "--prompt", "-p", help="First message for the agent."),
    provider: Optional[str] = typer.Option(None, help="Override the profile's provider (claude, codex, antigravity, shell)."),
    no_fetch: bool = typer.Option(False, "--no-fetch", help="Don't fetch the base branch first."),
    no_setup: bool = typer.Option(False, "--no-setup", help="Skip setup commands."),
    attach: bool = typer.Option(False, "--attach", help="Attach to the tmux session afterward."),
) -> None:
    """Create a worktree on a new branch and start an agent in it."""
    if pr is not None:
        if branch:
            _fail("pass either BRANCH or --pr, not both (--pr uses the PR's head branch)")
        if base:
            _fail("--base can't be combined with --pr (the PR's base branch is used)")
    elif not branch:
        _fail("missing BRANCH (or pass --pr <number>)")
    db = DB()
    if pr is not None:
        created = _run(workspaces.create_from_pr, db, os.getcwd(), pr, run_setup=not no_setup)
    else:
        created = _run(
            workspaces.create, db, os.getcwd(), branch, base,
            fetch=False if no_fetch else None, run_setup=not no_setup,
        )
    ws = created.workspace
    typer.secho(f"✓ {ws.id}", fg="green", bold=True)
    typer.echo(f"  branch  {ws.branch} ({created.how}, from {created.start_point})")
    typer.echo(f"  path    {ws.path}")
    typer.echo(f"  ports   {ws.port_base}-{ws.port_base + 9}  (COPSE_PORT_BASE)")
    if created.copied:
        typer.echo(f"  copied  {', '.join(created.copied)}")
    if created.setup:
        if created.setup.ok:
            typer.echo("  setup   ok")
        else:
            typer.secho(f"  setup   FAILED\n{created.setup.log}", fg="yellow")
            typer.echo(f"  (workspace kept; fix and re-run with `copse setup {ws.name}`)")

    from copse.config import load_repo_config

    profile = agent or load_repo_config(ws.repo_root).default_agent
    window = None
    if profile != "none":
        a = _run(agents.spawn, db, ws, profile, prompt=prompt, provider_name=provider)
        window = a.tmux_window
        typer.echo(f"  agent   {a.id} ({a.profile}/{a.provider})")
    typer.echo(f"\n  copse attach {ws.name}")
    if attach:
        _attach(ws, window)


@app.command()
def start(
    agent: str = typer.Option("supervisor", "--agent", "-a", help="Agent profile."),
    prompt: Optional[str] = typer.Option(None, "--prompt", "-p"),
    provider: Optional[str] = typer.Option(None),
    attach: bool = typer.Option(True, "--attach/--no-attach"),
    watch: bool = typer.Option(True, "--watch/--no-watch", help="Show the copse watch dashboard in a pane under the agent."),
    autopilot: Optional[bool] = typer.Option(None, "--autopilot/--no-autopilot", help="The supervisor drives toward a goal until it's verified (default: on, or `autopilot` in .copse/config.json)."),
    branch: Optional[str] = typer.Option(None, "--branch", "-b", help="Run in the worktree for this branch, creating the branch and worktree if needed."),
    worktree: Optional[str] = typer.Option(None, "--worktree", "-w", help="Run in the worktree at this path (created with --branch if it doesn't exist)."),
) -> None:
    """Start a fresh chat with an agent here (default: a supervisor), with the dashboard alongside.

    If a session is still running here, you choose: open it, start the new
    one in its own worktree (both run at once), or pause it and start fresh.
    Without a terminal to ask in, it's paused, and `copse continue` brings
    it back. With --branch or --worktree it runs in that worktree instead,
    which gets the repo's .copse config."""
    from copse.config import load_repo_config

    db = DB()
    if branch or worktree:
        ws = _run(workspaces.checkout_for, db, os.getcwd(), branch=branch, worktree=worktree)
    else:
        ws = _here_or_scratch(db, reuse_scratch=False)
        running = _running_session(db, ws)
        if running and attach and sys.stdin.isatty():
            choice = _ask_about_running(running)
            if choice == "o":
                _attach(ws, running.tmux_window)
                return
            if choice == "n":
                ws = _run(_session_worktree, db, ws)
                typer.echo(f"✓ new session in its own worktree: {ws.path} ({ws.branch})")
    from copse.providers import NOT_SUPERVISOR, SUPERVISOR_PROVIDERS

    if provider and agent == "supervisor" and provider in NOT_SUPERVISOR:
        _fail(f"the {provider} provider can't run the supervisor; use one of: "
              f"{', '.join(SUPERVISOR_PROVIDERS)}")
    _preflight(agent, provider, ws.repo_root)
    # Nothing slow before the chat starts: the paused session's leftover
    # processes, old paused sessions' worktrees and the pool refill are all
    # handled by the detached cull.
    _pause_running(db, ws, stop_procs=False)
    if autopilot is None:
        autopilot = agent == "supervisor" and _run(load_repo_config, ws.repo_root).autopilot
    a = _run(agents.spawn, db, ws, agent, prompt=prompt, provider_name=provider,
             watch_pane=watch, background_setup=True, autopilot=autopilot)
    # After the chat's window exists and is recorded: the cull's session
    # retention closes dropped sessions' windows by their stored pane ids,
    # which a freshly started tmux server hands out again from %0.
    _cull_detached(ws.repo_root)
    typer.echo(f"✓ {a.profile} agent {a.id} in {ws.id} ({ws.branch})")
    _local_models_detached(ws.repo_root)
    _settings_sync_detached()
    if autopilot:
        _say_autopilot(db, a.id)
    if attach:
        _attach(ws, a.tmux_window)


def _preflight(agent: str, provider: Optional[str], repo_root: str) -> None:
    """Stop before launching when the chat can't start (no tmux, no CLI)."""
    from copse import doctor as doctor_mod
    from copse.profiles import load_profile

    name = provider or _run(load_profile, agent, repo_root).provider
    problems = doctor_mod.preflight(name)
    if problems:
        for p in problems:
            typer.secho(f"✗ {p}", fg="red", err=True)
        _fail("copse can't start yet. `copse doctor` checks everything else.")


def _local_models_detached(repo_root: str) -> None:
    """Start the local model server the native profiles need, if it isn't
    running, without making the person wait (see copse.native.serve). Says so
    on one line when there's something to start."""
    from copse.config import load_repo_config
    from copse.native import serve
    from copse.providers import copse_invocation

    try:
        pending = serve.needed(repo_root, load_repo_config(repo_root))
    except Exception:  # noqa: BLE001 -- a convenience; never block the start
        return
    if not pending:
        return
    who = ", ".join(sorted({n for s in pending for n in s.profiles}))
    typer.echo(f"  local models: starting ollama in the background for {who} (log: {serve.log_path()})")
    subprocess.Popen([*copse_invocation(), "_local-models", "--repo", repo_root], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _settings_sync_detached() -> None:
    """Pull this person's synced settings (copse Pro) without making them
    wait; the helper does nothing unless the plan includes settings sync."""
    from copse.providers import copse_invocation

    subprocess.Popen([*copse_invocation(), "_sync-settings"], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _cull_detached(repo_root: str | None = None) -> None:
    """Clean up leftover agent processes and stale workers (and, given
    ``repo_root``, apply its paused-session retention) without making the
    person wait for it (see copse.cull, copse.sessions.enforce)."""
    from copse.providers import copse_invocation

    repo = ["--repo", repo_root] if repo_root else []
    subprocess.Popen([*copse_invocation(), "_cull", *repo], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _say_autopilot(db: DB, root_id: str) -> None:
    from copse import autopilot as pilot

    ap = db.get_autopilot(root_id)
    if ap and ap.goal:
        done, total = pilot.counts(db, root_id)
        typer.echo(f"  autopilot: {ap.goal} ({done} of {total} milestones verified)")
    else:
        typer.echo("  autopilot: on. Tell the supervisor what we're building.")
    typer.echo("  `copse autopilot off` hands the wheel back to you.")


def _running_session(db: DB, ws: Workspace):
    """The live session (its interactive agent) in this checkout, if any."""
    for a in db.list_agents(ws.id):
        if a.mode == "interactive" and a.status not in ("paused", "done") and agents.is_alive(a):
            return a
    return None


def _ask_about_running(running) -> str:
    import click

    typer.echo(f"A copse session is already running here ({running.id}).")
    typer.echo("  [o] open it   [n] new session in its own worktree   [p] pause it and start fresh")
    return typer.prompt("Which", type=click.Choice(["o", "n", "p"]), default="o", show_choices=False)


def _session_worktree(db: DB, ws: Workspace) -> Workspace:
    """A worktree for a second session in this repo: branch copse/session-N,
    cut from what this checkout has checked out, so the two sessions never
    share files or a branch."""
    n = 2
    while git.ok(["rev-parse", "--verify", "--quiet", f"refs/heads/copse/session-{n}"], ws.repo_root):
        n += 1
    return workspaces.create(db, ws.repo_root, f"copse/session-{n}", base=ws.branch or None,
                             start=git.out(["rev-parse", "HEAD"], ws.path), apply_prefix=False).workspace


def _pause_running(db: DB, ws: Workspace, stop_procs: bool = True) -> None:
    """At most one live session per checkout: pause any that's still running.
    ``stop_procs=False`` when a detached cull follows (see agents.pause)."""
    for a in db.list_agents(ws.id):
        if a.mode == "interactive" and a.status not in ("paused", "done") and agents.is_alive(a):
            # A session starts here next: keep its local models loaded.
            agents.pause(db, a.id, stop_procs=stop_procs, stop_local_models=False)
            typer.echo(f"Paused the session that was still running here ({a.id}); "
                       f"`copse continue {a.id}` brings it back.")


def _describe(s) -> str:
    workers = len(s.members) - 1
    ago = _ago(time.time() - s.paused_at)
    what = f", {workers} worker(s)" if workers else ""
    branches = f": {', '.join(s.branches[:3])}" + (" …" if len(s.branches) > 3 else "") if s.branches else ""
    return f"{s.root.id}  paused {ago} ago{what}{branches}"


def _ago(seconds: float) -> str:
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


@app.command("continue")
def continue_cmd(
    session_id: Optional[str] = typer.Argument(None, help="Session to resume (default: the most recent)."),
    attach: bool = typer.Option(True, "--attach/--no-attach"),
) -> None:
    """Pick up a paused session: the chat and its workers resume where they stopped."""
    from copse import scratch, sessions

    db = DB()
    cwd = os.getcwd()
    try:
        git.main_repo_root(cwd)
        ws = _run(workspaces.adopt_root, db, cwd)
    except git.GitError:
        ws = scratch.for_origin(db, cwd)
        if ws is None:
            _fail("no scratch session started from this folder to continue; run `copse` to start one")
    available = sessions.paused(db, ws.repo_root)
    if not available:
        live = agents.find_running(db, ws, "supervisor")
        if live and attach:
            typer.echo(f"Session {live.id} is already running; reopening it.")
            _attach(ws, live.tmux_window)
            return
        _fail("no paused sessions here. Run `copse` to start a fresh one.")
    if session_id:
        matches = [s for s in available if s.root.id.startswith(session_id)]
        if len(matches) != 1:
            _fail(f"no single paused session matches {session_id!r}. Available:\n  "
                  + "\n  ".join(_describe(s) for s in available))
        chosen = matches[0]
    else:
        chosen = available[0]
    others = [s for s in available if s.root.id != chosen.root.id]
    _pause_running(db, ws)
    resumed = _run(agents.resume, db, chosen.root.id)
    typer.secho(f"↺ continuing {_describe(chosen)} ({len(resumed)} agent(s) restarted)", fg="green")
    _local_models_detached(ws.repo_root)
    if others:
        typer.echo("Other paused sessions (copse continue <id>):")
        for s in others:
            typer.echo(f"  {_describe(s)}")
    if attach:
        root = db.get_agent(chosen.root.id)
        _attach(chosen.workspace, root.tmux_window)


@app.command("sessions")
def sessions_cmd() -> None:
    """List paused sessions in this repo, with the disk their worktrees use."""
    from copse import sessions

    db = DB()
    root = _run(git.main_repo_root, os.getcwd())
    found = sessions.paused(db, root)
    if not found:
        typer.echo("no paused sessions")
        return
    for s in found:
        # Members can share a workspace (a worker and its reviewer): count each once.
        spaces = {m.workspace_id: db.get_workspace(m.workspace_id) for m in s.members[1:]}
        size = sum(sessions.disk_usage(w.path) for w in spaces.values()
                   if w and w.kind == "worktree" and os.path.isdir(w.path))
        typer.echo(f"{_describe(s)}  ({size / 1e6:.0f} MB in worktrees)")
    typer.echo(f"Keeps the newest {sessions.KEEP} for up to {sessions.MAX_AGE_DAYS} days. `copse prune` cleans up now.")


@app.command()
def prune() -> None:
    """Clean up now: old paused sessions, merged worktrees and leftover tmux sessions.

    Drops paused sessions beyond the newest few or older than a week, and old
    scratch sessions with nothing left to transfer. Removes the worktrees of
    finished workers whose branch is already merged, copse tmux sessions that
    only hold idle shells and no running agent, leftover copse tmux servers,
    stale locks and empty worktree folders. A removed worktree's branch goes
    too once fully merged (unless delete_merged_branches is false); nothing is
    merged, unmerged branches are kept, and worktrees with uncommitted changes
    stay."""
    from copse import sessions

    db = DB()
    dropped = 0
    try:
        dropped = sessions.enforce(db, git.main_repo_root(os.getcwd()))
    except git.GitError:
        pass
    removed = sessions.prune_scratch(db)
    typer.echo(f"dropped {dropped} paused session(s), removed {removed} old scratch session(s)")
    from copse import cull

    for line in cull.prune(db):
        typer.echo(line)


def _here_or_scratch(db: DB, reuse_scratch: bool) -> Workspace:
    """This checkout, or (outside any git repo) a scratch session for this folder."""
    from copse import scratch

    cwd = os.getcwd()
    try:
        git.main_repo_root(cwd)
    except git.GitError:
        existing = scratch.for_origin(db, cwd) if reuse_scratch else None
        if existing:
            typer.echo(f"↺ scratch session {existing.id}")
            return existing
        ws = _run(scratch.create, db, cwd)
        typer.secho(f"No git repository here, so this is a scratch session, tracked in {ws.path}", fg="cyan")
        typer.echo("Nothing is created in this folder. When you're ready, move the work into a real repo:")
        typer.echo("  copse transfer ~/path/to/repo    (or ask the supervisor to do it)")
        return ws
    _offer_pending_scratch(db, cwd)
    return _run(workspaces.adopt_root, db, cwd)


def _offer_pending_scratch(db: DB, cwd: str) -> None:
    """Inside a real repo: offer to bring in scratch work that hasn't moved yet."""
    from copse import scratch

    if scratch.is_scratch(cwd) or not sys.stdin.isatty():
        return
    for s in scratch.pending(db)[:3]:
        n = scratch.commit_count(s)
        dirty = " + uncommitted changes" if git.dirty_files(s.path) else ""
        started = scratch.origin_of(s.path) or "?"
        if typer.confirm(f"Bring scratch session {s.name} ({n} commit(s){dirty}, started in {started}) into this repo?", default=False):
            _do_transfer(db, s, cwd, None)


def _do_transfer(db: DB, s: Workspace, target: str, branch: Optional[str]) -> None:
    from copse import scratch

    t = _run(scratch.transfer, db, s, target, branch)
    extra = " (uncommitted work was committed first)" if t.snapshot else ""
    typer.secho(f"✓ moved {t.commits} commit(s){extra} onto branch {t.workspace.branch}", fg="green")
    typer.echo(f"  workspace {t.workspace.id} at {t.workspace.path}")
    typer.echo(f"  review: copse diff {t.workspace.name}   merge: copse merge {t.workspace.name}   PR: copse pr {t.workspace.name}")


@app.command()
def transfer(
    target: Optional[str] = typer.Argument(None, help="A folder inside the real git repo (default: here)."),
    source: Optional[str] = typer.Option(None, "--from", help="Scratch session name or id (default: the one you're in, or the newest)."),
    branch: Optional[str] = typer.Option(None, "--branch", "-b", help="Branch to create (default: copse/from-<session>)."),
) -> None:
    """Move a scratch session's work into a real repository, on its own branch."""
    from copse import scratch

    db = DB()
    here = os.getcwd()
    if source:
        s = _ws(db, source)
    elif scratch.is_scratch(here):
        s = _ws(db, None)
    else:
        options = scratch.pending(db)
        if not options:
            _fail("no scratch sessions with work to transfer")
        s = options[0]
    dest = target or here
    if scratch.is_scratch(dest):
        _fail("give the path of the real repository to move the work into, e.g. copse transfer ~/Projects/myapp")
    _do_transfer(db, s, dest, branch)


@app.command()
def handover(
    to: str = typer.Option(..., "--to", help="Branch or worktree path for the new supervisor (created if needed)."),
    note: Optional[str] = typer.Option(None, "--note", "-n", help="Handoff note: the new supervisor's first message includes it."),
    attach: bool = typer.Option(True, "--attach/--no-attach"),
) -> None:
    """Hand this repo's supervisor session to a new supervisor on another branch or worktree.

    The goal and milestones, workers, queued tasks and your note move to the
    new supervisor; the old one is paused."""
    from copse import sessions

    db = DB()
    root_id = _session_root(db)
    dest = _run(workspaces.checkout_for_target, db, os.getcwd(), to)
    new = _run(sessions.handover, db, root_id, dest, note)
    typer.secho(f"✓ handed {root_id} over to {new.id} in {dest.id} ({dest.branch})", fg="green")
    _cull_detached(dest.repo_root)
    if attach:
        _attach(dest, new.tmux_window)


@app.callback(invoke_without_command=True)
def default(
    ctx: typer.Context,
    cont: bool = typer.Option(False, "--continue", "-c", help="Pick up the most recent paused session instead of starting fresh."),
    autopilot: Optional[bool] = typer.Option(None, "--autopilot/--no-autopilot", help="Start with autopilot on or off (default: on, or `autopilot` in .copse/config.json)."),
    provider: Optional[str] = typer.Option(None, "--provider", help="Run the supervisor on this CLI instead of Claude Code (codex, antigravity)."),
    show_version: bool = typer.Option(False, "--version", help="Print copse's version and exit."),
) -> None:
    """Bare `copse`: a fresh supervisor chat here (a scratch session outside git)."""
    if show_version:
        from copse import __version__

        typer.echo(f"copse {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        if cont:
            continue_cmd(session_id=None, attach=True)
        else:
            start(agent="supervisor", prompt=None, provider=provider, attach=True, watch=True,
                  autopilot=autopilot, branch=None, worktree=None)


def _session_root(db: DB) -> str:
    """This repo's current session: the one running here, else the newest
    paused one."""
    from copse import sessions

    cwd = os.getcwd()
    try:
        ws = workspaces.adopt_root(db, cwd)
    except git.GitError:
        from copse import scratch

        ws = scratch.for_origin(db, cwd) or workspaces.current(db)
        if ws is None:
            _fail("no copse session here")
    live = agents.find_running(db, ws, "supervisor")
    if live:
        return live.id
    found = sessions.paused(db, ws.repo_root)
    if found:
        return found[0].root.id
    _fail("no copse session here. Run `copse` to start one.")
    raise AssertionError


@app.command("delegation")
def delegation_cmd(
    level: Optional[str] = typer.Argument(None, help="conservative, balanced or fast. Omit to show the current one."),
    repo: bool = typer.Option(False, "--repo", help="Only for this repo (.copse/config.local.json), not every repo."),
) -> None:
    """How readily the supervisor hands work to workers: conservative, balanced (default) or fast.

    conservative does most work in its own chat (fewest tokens); fast splits
    work across parallel workers straight away (quickest, most tokens). It's
    saved in ~/.copse/config.json for every repo and session (with --repo,
    for this repo only), and a running supervisor is told at once."""
    from copse import autopilot as pilot
    from copse.config import RepoConfig, load_repo_config, set_local, set_user, user_settings

    try:
        root = git.out(["rev-parse", "--show-toplevel"], os.getcwd())
    except git.GitError:
        root = None
    if level is None:
        current = load_repo_config(root).delegation if root else \
            user_settings().get("delegation", RepoConfig().delegation)
        typer.echo(f"delegation: {current}  (conservative, balanced, fast)")
        return
    if level not in pilot.DELEGATIONS:
        _fail("delegation must be conservative, balanced or fast")
    if repo:
        if not root:
            _fail("--repo needs to run inside a git repo")
        set_local(root, "delegation", level)
        typer.echo(f"✓ delegation {level} for this repo (.copse/config.local.json)")
    else:
        path = set_user("delegation", level)
        typer.echo(f"✓ delegation {level} for every repo ({path})")
    _tell_supervisors_about_delegation(DB())


def _tell_supervisors_about_delegation(db: DB) -> None:
    """Send each running supervisor its repo's delegation rule as it now stands."""
    from copse import autopilot as pilot
    from copse.config import load_repo_config

    for ws in db.find_workspaces():
        running = _running_session(db, ws)
        if not running:
            continue
        try:
            rule = pilot.delegation_rule(load_repo_config(ws.repo_root))
            agents.send_message(db, running.id, "[copse] The person changed how readily you delegate. "
                                "From now on this replaces your earlier delegation rule:\n\n" + rule)
            typer.echo(f"  told the running supervisor ({running.id})")
        except (agents.AgentError, ValueError):
            pass


@app.command("autopilot")
def autopilot_cmd(
    action: Optional[str] = typer.Argument(None, help="on, off, or check (run the milestone checks now). Omit to show progress."),
) -> None:
    """Show the goal's progress, or turn autopilot on or off.

    With autopilot on, the supervisor keeps working until every milestone's
    check passes or it needs you."""
    from copse import autopilot as pilot

    db = DB()
    root_id = _session_root(db)
    root = db.get_agent(root_id)
    if action in ("on", "off"):
        on = action == "on"
        pilot.set_enabled(db, root_id, on)
        if root and agents.is_alive(root):
            note = ("Autopilot is on again: keep driving toward the goal (get_progress shows where it stands)."
                    if on else "Autopilot is now off: stop driving and wait for the user's instructions.")
            try:
                agents.send_message(db, root_id, f"[copse autopilot] {note}")
            except agents.AgentError:
                pass
        typer.echo(f"autopilot {action} for session {root_id}")
        if on and not (root and agents.is_alive(root)):
            typer.echo("It takes effect when the session runs: `copse continue`.")
        return
    if action == "check":
        ws = db.get_workspace(root.workspace_id) if root else None
        if ws is None or db.get_autopilot(root_id) is None:
            _fail("autopilot isn't set up for this session")
        typer.echo(_run(pilot.check_milestones, db, root_id, ws))
        return
    if action:
        _fail(f"unknown action {action!r}: use on, off or check")
    typer.echo(pilot.progress(db, root_id))
    u = pilot.usage()
    if u:
        typer.echo(pilot.usage_note(u))


@app.command()
def doctor() -> None:
    """Check that copse has what it needs, and say what to do about anything missing.

    tmux, the agent CLIs, a writable home, leftover processes; in a repo, its
    config, checks and code map."""
    from copse import doctor as doctor_mod

    root = None
    try:
        root = git.main_repo_root(os.getcwd())
    except git.GitError:
        pass
    results = doctor_mod.checks(root)
    typer.echo(doctor_mod.render(results))
    if any(c.level == doctor_mod.FAIL for c in results):
        raise typer.Exit(1)


@app.command("ls")
def list_cmd(
    all_repos: bool = typer.Option(False, "--all", help="Every repo, not just this one."),
    as_json: bool = typer.Option(False, "--json", help="Print a JSON array instead of a table."),
) -> None:
    """List workspaces and their agents."""
    db = DB()
    repo_root = None
    if not all_repos:
        try:
            repo_root = git.main_repo_root(os.getcwd())
        except git.GitError:
            pass
    rows = db.find_workspaces(repo_root)
    panes = tmux.list_panes()
    if as_json:
        typer.echo(json.dumps([view.workspace_entry(db, ws, panes=panes) for ws in rows], indent=2))
        return
    if not rows:
        typer.echo("no workspaces")
        return
    for ws in rows:
        if not os.path.isdir(ws.path):
            typer.secho(f"{ws.id}  (missing: {ws.path})", fg="red")
            continue
        e = view.workspace_entry(db, ws, panes=panes)
        info = ""
        if e["ahead"] is not None:
            dirty = f" *{e['dirty']}" if e["dirty"] else ""
            info = f"  ↑{e['ahead']} ↓{e['behind']}{dirty} vs {ws.base_branch}"
        typer.secho(f"{ws.id}", bold=True, nl=False)
        typer.echo(f"  [{ws.branch}]{info}")
        for a in e["agents"]:
            tokens = f"  {a['tokens']}" if a.get("tokens") else ""
            typer.echo(f"    {a['id']}  {a['profile']:<12} {a['provider']:<7} {a['status']:<11} {a['mode']}{tokens}")


@app.command()
def history(
    limit: int = typer.Option(50, "--limit", help="Most recent rows to show."),
    kind: Optional[str] = typer.Option(
        None, "--kind", help=f"Only this kind: one of {', '.join(history_mod.KINDS)}."
    ),
    all_repos: bool = typer.Option(False, "--all", help="Every repo, not just this one."),
    share: bool = typer.Option(False, "--share", help="Summarize this repo's session in a few lines to paste into Slack or a post."),
    session: Optional[str] = typer.Option(None, "--session", help="With --share: this session (an id from `copse sessions`) instead of the current one."),
) -> None:
    """Durable history of worker results, reviews, merges and milestone checks."""
    db = DB()
    if share:
        root_id = session or _session_root(db)
        typer.echo(_run(history_mod.share_card, db, root_id))
        return
    repo_root = None
    if not all_repos:
        try:
            repo_root = git.main_repo_root(os.getcwd())
        except git.GitError:
            typer.echo("not in a git repo: showing all repos")
    rows = db.list_history(repo_root, kind, limit)
    if not rows:
        typer.echo("no history")
        return
    total = 0
    for r in rows:
        when = time.strftime("%m-%d %H:%M", time.localtime(r.ts))
        tokens = history_mod.tokens_summary(r.tokens)
        total += history_mod.tokens_total(r.tokens)
        branch = r.branch or "-"
        typer.echo(
            f"{when}  {r.kind:<13} {branch:<28} {tokens:<16} {history_mod.row_summary(r)}"
        )
    typer.echo(f"\ntotal tokens: {format_tokens(total)}")


# -- copse permissions (the permission policy; see copse.permissions) -------------------------

permissions_app = typer.Typer(no_args_is_help=True,
                              help="The rules copse answers workers' permission requests with "
                                   "(when permission_policy is \"on\").")
app.add_typer(permissions_app, name="permissions")


def _here_repo() -> str | None:
    try:
        return git.main_repo_root(os.getcwd())
    except git.GitError:
        return None


@permissions_app.command("list")
def permissions_list() -> None:
    """Every rule in force (built-in, this repo's denies, yours and learned), with its source."""
    from copse import permissions as perms

    repo = _here_repo()
    if repo:
        from copse.config import load_repo_config

        state = load_repo_config(repo).permission_policy
        typer.echo(f"permission_policy: {state if state == 'on' else 'off'}\n")
    for r in perms.all_rules(repo):
        typer.echo(f"{r.id:<12} {r.decision:<5} {r.kind:<5} {r.source:<7} {r.describe()}")


@permissions_app.command("suggestions")
def permissions_suggestions() -> None:
    """Requests you approved at least twice that no rule covers yet (accept one with `accept ID`)."""
    from copse import permissions as perms

    rows = perms.suggestions()
    if not rows:
        typer.echo("no suggestions")
        return
    for s in rows:
        typer.echo(f"{s.id:<12} allow {s.kind:<5} exact {s.match!r}  (approved {s.count}x)")


@permissions_app.command("accept")
def permissions_accept(rule_id: str = typer.Argument(..., metavar="ID")) -> None:
    """Turn a suggestion into an allow rule."""
    from copse import permissions as perms

    rule = perms.accept(rule_id)
    if rule is None:
        typer.echo(f"no suggestion {rule_id} (see `copse permissions suggestions`)")
        raise typer.Exit(1)
    typer.echo(f"{rule.id}: allow {rule.describe()} (learned)")


def _add_permission_rule(decision: str, kind: str, match: str, prefix: bool, glob: bool) -> None:
    from copse import permissions as perms

    if prefix and glob:
        typer.echo("--prefix and --glob can't be combined")
        raise typer.Exit(2)
    try:
        rule = perms.add_rule(kind, match, decision, "prefix" if prefix else "glob" if glob else "exact")
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(2)
    typer.echo(f"{rule.id}: {decision} {rule.describe()}")


_KIND_HELP = "read, write, edit, bash, fetch, mcp or other."


@permissions_app.command("allow")
def permissions_allow(
    kind: str = typer.Argument(..., help=_KIND_HELP),
    match: str = typer.Argument(..., help="The command, path, URL or tool name (exact unless --prefix/--glob)."),
    prefix: bool = typer.Option(False, "--prefix", help="Match anything starting with MATCH."),
    glob: bool = typer.Option(False, "--glob", help="MATCH is a shell-style glob (* also crosses /)."),
) -> None:
    """Allow requests that match. A bash allow never covers a command with shell metacharacters."""
    _add_permission_rule("allow", kind, match, prefix, glob)


@permissions_app.command("deny")
def permissions_deny(
    kind: str = typer.Argument(..., help=_KIND_HELP),
    match: str = typer.Argument(..., help="The command, path, URL or tool name (exact unless --prefix/--glob)."),
    prefix: bool = typer.Option(False, "--prefix", help="Match anything starting with MATCH."),
    glob: bool = typer.Option(False, "--glob", help="MATCH is a shell-style glob (* also crosses /)."),
) -> None:
    """Deny requests that match (a deny beats any allow)."""
    _add_permission_rule("deny", kind, match, prefix, glob)


@permissions_app.command("forget")
def permissions_forget(rule_id: str = typer.Argument(..., metavar="ID")) -> None:
    """Remove one of your or learned rules (or a suggestion's approval count). Built-in rules stay."""
    from copse import permissions as perms

    gone = perms.forget(rule_id)
    if gone is None:
        typer.echo(f"no rule or suggestion {rule_id} of yours (built-in and repo rules can't be forgotten)")
        raise typer.Exit(1)
    typer.echo(f"forgot {gone}")


@permissions_app.command("reset")
def permissions_reset(yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask for confirmation.")) -> None:
    """Back to the built-in rules: drop your rules, learned rules and approval counts."""
    from copse import permissions as perms

    if not yes:
        typer.confirm("Remove all your and learned permission rules and approval counts?",
                      default=False, abort=True)
    perms.reset()
    typer.echo("permission rules reset to the defaults")


@app.command()
def learning() -> None:
    """What copse Pro's hosted learner has learned about which profiles fit which tasks (nothing is learned on this machine)."""
    from copse import learning as learning_mod
    from copse.config import load_repo_config

    try:
        repo_root = git.main_repo_root(os.getcwd())
    except git.GitError:
        typer.echo("not in a git repo")
        raise typer.Exit(1)
    cfg = load_repo_config(repo_root)
    from copse import plugins

    name = plugins.learning_name(cfg)
    if name == plugins.OFF:
        configured = (cfg.learning or plugins.OFF).strip()
        if configured == plugins.AUTO:
            typer.echo("learning is off: hosted learning needs copse Pro "
                       "(`copse account` shows your plan; `copse account upgrade` gets it). "
                       "Nothing is learned on this machine.")
        elif configured == plugins.OFF:
            typer.echo('learning is off. Set "learning" to "auto" or "cloud" in .copse/config.json '
                       "to use hosted learning (copse Pro).")
        else:
            typer.echo(f'learning is off: "learning": {configured!r} is not a supported value '
                       '(use "auto", "cloud" or "off"). Learning is hosted only (copse Pro).')
        return
    p = learning_mod.plugin(cfg, repo_root)
    if p is None:
        typer.echo("hosted learning is unavailable")
        raise typer.Exit(1)
    typer.echo(p.report())


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def account(ctx: typer.Context) -> None:
    """copse Pro/Team: paid features, login, upgrade, billing, orgs. Bare `copse account` shows what you have."""
    from copse import account as account_mod
    from copse.config import load_repo_config

    try:
        repo_root = git.main_repo_root(os.getcwd())
    except git.GitError:
        repo_root = os.getcwd()
    try:
        cfg = load_repo_config(repo_root)
    except ValueError:
        from copse.config import RepoConfig

        cfg = RepoConfig()
    raise typer.Exit(account_mod.run(cfg, repo_root, list(ctx.args), echo=typer.echo))


# -- copse audit (copse Enterprise: the local tamper-evident audit chain) ---------------------

audit_app = typer.Typer(no_args_is_help=True,
                        help="copse Enterprise: the local tamper-evident audit log "
                             "(~/.copse/audit; see `src/copse/pro/audit_chain.py`).")
app.add_typer(audit_app, name="audit")


def _audit_repo(repo: Optional[str]) -> str:
    """The main repo root for ``--repo`` (default: here), or the path as given."""
    start = repo or os.getcwd()
    try:
        return git.main_repo_root(start)
    except git.GitError:
        return os.path.abspath(start)


@audit_app.command("verify")
def audit_verify(
    repo: Optional[str] = typer.Option(None, "--repo", help="The repo whose log to verify (default: here)."),
) -> None:
    """Recompute the chain and check every signature; exit 1 at the first broken record."""
    from copse.pro import audit_chain

    try:
        report = audit_chain.verify(_audit_repo(repo))
    except audit_chain.AuditError as e:
        typer.echo(f"audit: {e}")
        raise typer.Exit(2)
    typer.echo(report.describe())
    if not report.ok:
        raise typer.Exit(1)


@audit_app.command("export")
def audit_export(
    repo: Optional[str] = typer.Option(None, "--repo", help="The repo whose log to export (default: here)."),
    since: Optional[str] = typer.Option(None, "--since", help="Only records from this ISO 8601 time on."),
    fmt: str = typer.Option("jsonl", "--format", help="jsonl or csv."),
) -> None:
    """Print the audit records (unverified) as JSONL or CSV."""
    from copse.pro import audit_chain

    start = None
    if since:
        try:
            start = audit_chain.parse_time(since)
        except ValueError:
            typer.echo(f"audit: --since wants an ISO 8601 time, not {since!r}")
            raise typer.Exit(2)
    try:
        out = audit_chain.export(_audit_repo(repo), since=start, fmt=fmt)
    except audit_chain.AuditError as e:
        typer.echo(f"audit: {e}")
        raise typer.Exit(2)
    sys.stdout.write(out)
    sys.stdout.flush()


@audit_app.command("pubkey")
def audit_pubkey() -> None:
    """This install's Ed25519 public key (hex), which every audit record is signed with."""
    from copse.pro import audit_chain

    try:
        typer.echo(audit_chain.public_key_hex())
    except audit_chain.AuditError as e:
        typer.echo(f"audit: {e}")
        raise typer.Exit(2)


@app.command()
def watch(
    all_repos: bool = typer.Option(False, "--all", help="Every repo, not just this one."),
    once: bool = typer.Option(False, "--once", help="Print one snapshot and exit."),
    sidebar: bool = typer.Option(False, "--sidebar", hidden=True),
) -> None:
    """Live dashboard of workspaces and agents (highlights agents waiting on you)."""
    from copse import watch as watch_mod

    repo_root = None
    if not all_repos:
        try:
            repo_root = git.main_repo_root(os.getcwd())
        except git.GitError:
            pass
    if once or not sys.stdout.isatty():
        typer.echo(watch_mod.print_once(DB(), repo_root, color=sys.stdout.isatty()))
        return
    watch_mod.run(repo_root, sidebar=sidebar)
    if sidebar:
        # Quit on purpose (a crash raises instead): keep it gone, so switching
        # windows doesn't bring it back. `copse continue` starts a fresh one.
        agents.dismiss_sidebar(DB(), os.environ.get("TMUX_PANE"))


@app.command()
def close(
    agent_id: Optional[str] = typer.Argument(None, help="Agent to close (an unambiguous prefix works)."),
    exited: bool = typer.Option(False, "--exited", help="Close every agent that has stopped or finished."),
    all_repos: bool = typer.Option(False, "--all", help="With --exited: every repo, not just this one."),
) -> None:
    """Hide agents from the dashboard for good, stopping any still running.

    Their worktrees, branches and records stay; `copse ls` still lists them."""
    db = DB()
    if exited == bool(agent_id):
        _fail("pass an agent id, or --exited")
    panes = tmux.list_panes()
    if agent_id:
        target = _run(agents.get, db, agent_id)
        was_running = agents.is_alive(target, panes) and agents.owns_pane(db, target)
        a = _run(agents.close, db, agent_id, panes)
        typer.echo(f"✓ closed {a.id}" + (" (stopped it first)" if was_running else ""))
        return
    repo_root = None
    if not all_repos:
        try:
            repo_root = git.main_repo_root(os.getcwd())
        except git.GitError:
            pass
    alive = view.live_agents(db, panes)
    closed = [a for ws in db.find_workspaces(repo_root) for a in db.list_agents(ws.id)
              if a.dismissed_at is None and (a.id not in alive or a.status == "done")]
    for a in closed:
        agents.close(db, a.id, panes)
    typer.echo(f"✓ closed {len(closed)} agent(s)" if closed else "nothing to close")


@app.command()
def attach(workspace: Optional[str] = typer.Argument(None)) -> None:
    """Attach to a workspace's tmux session, at the agent that needs you (else its busiest or newest agent)."""
    if not sys.stdin.isatty():
        # From a chat's `!` or a script there's no terminal to attach: tmux
        # would switch whatever client it finds instead, or nothing at all.
        _fail("copse attach needs a terminal: run it in a terminal window, or select the "
              "agent in the copse sidebar and press ⏎.")
    db = DB()
    ws = _ws(db, workspace)
    _attach(ws, agents.attach_target(db, ws))


@app.command()
def cd(workspace: str) -> None:
    """Print a workspace's path (use: cd "$(copse cd NAME)")."""
    typer.echo(_ws(DB(), workspace).path)


@app.command("open")
def open_cmd(
    workspace: Optional[str] = typer.Argument(None),
    editor: str = typer.Option(os.environ.get("COPSE_EDITOR", "code"), help="Editor command."),
) -> None:
    """Open a workspace in your editor."""
    subprocess.run([editor, _ws(DB(), workspace).path])


@app.command()
def setup(workspace: Optional[str] = typer.Argument(None)) -> None:
    """Re-run setup commands in a workspace."""
    from copse.config import load_repo_config

    ws = _ws(DB(), workspace)
    cmds = load_repo_config(ws.repo_root).setup
    res = workspaces.run_commands(cmds, ws.path, workspaces.workspace_env(ws))
    typer.echo(res.log or "(no setup commands)")
    raise typer.Exit(0 if res.ok else 1)


@app.command()
def status(workspace: Optional[str] = typer.Argument(None)) -> None:
    """Branch status against base: ahead/behind, uncommitted files, unpushed."""
    ws = _ws(DB(), workspace)
    st = _run(git.status, ws.path, ws.base_branch)
    typer.echo(f"{ws.id}  [{st.branch}]  base {st.base or '-'}")
    typer.echo(f"  {st.ahead} ahead, {st.behind} behind")
    typer.echo(f"  unpushed: {'no upstream' if st.unpushed is None else st.unpushed}")
    for f in st.dirty_files:
        typer.echo(f"  M {f}")


@app.command()
def diff(
    workspace: Optional[str] = typer.Argument(None),
    stat: bool = typer.Option(False, "--stat"),
) -> None:
    """Everything the branch changes vs. its base (commits + uncommitted)."""
    ws = _ws(DB(), workspace)
    text = _run(git.diff, ws.path, workspaces.require_base(ws), stat)
    if sys.stdout.isatty() and not stat:
        subprocess.run(["less", "-R"], input=text, text=True)
    else:
        typer.echo(text or "(no changes)")


@app.command()
def sync(
    workspace: Optional[str] = typer.Argument(None),
    merge: bool = typer.Option(False, "--merge", help="Merge base in instead of rebasing."),
) -> None:
    """Bring the base branch's latest commits into this workspace."""
    ws = _ws(DB(), workspace)
    if git.dirty_files(ws.path):
        _fail("uncommitted changes; commit or stash first")
    ref = _run(git.sync, ws.path, workspaces.require_base(ws), "merge" if merge else "rebase")
    typer.echo(f"✓ {ws.branch} is up to date with {ref}")


@app.command()
def commit(
    workspace: Optional[str] = typer.Argument(None),
    message: str = typer.Option(..., "--message", "-m"),
) -> None:
    """Stage everything and commit in a workspace."""
    ws = _ws(DB(), workspace)
    sha = _run(git.commit_all, ws.path, message)
    typer.echo(f"✓ {sha}" if sha else "nothing to commit")


@app.command()
def push(workspace: Optional[str] = typer.Argument(None)) -> None:
    """Push the workspace branch and set its upstream."""
    ws = _ws(DB(), workspace)
    _run(git.push, ws.path, ws.branch)
    typer.echo(f"✓ pushed {ws.branch}")


@app.command()
def pr(
    workspace: Optional[str] = typer.Argument(None),
    title: Optional[str] = typer.Option(None, "--title", "-t"),
    draft: bool = typer.Option(False, "--draft"),
) -> None:
    """Push and open a pull request against the base branch."""
    ws = _ws(DB(), workspace)
    url = _run(workspaces.pull_request, ws, title, draft)
    typer.echo(url)
    _offer_delete_on_merge(ws.repo_root)


def _offer_delete_on_merge(repo_root: str) -> None:
    """The first PR in a repo where GitHub keeps merged branches: offer to
    turn on its automatic deletion (with the person's own gh login)."""
    if not workspaces.keeps_merged_branches(repo_root):
        return
    how = "`gh repo edit --delete-branch-on-merge`"
    if sys.stdin.isatty() and typer.confirm(
            "GitHub keeps this repo's branches after their PRs merge. Delete them automatically?",
            default=True, err=True):
        if workspaces.delete_branches_on_merge(repo_root):
            typer.echo("✓ GitHub now deletes a PR's branch when it merges.", err=True)
            return
        typer.secho(f"couldn't change it (it needs admin rights on the repo); an admin can run {how}.",
                    fg="yellow", err=True)
    elif not sys.stdin.isatty():
        typer.secho(f"tip: GitHub keeps this repo's branches after their PRs merge; {how} "
                    "deletes them automatically.", fg="yellow", err=True)


@app.command("merge")
def merge_cmd(
    workspace: Optional[str] = typer.Argument(None),
    squash: bool = typer.Option(False, "--squash"),
) -> None:
    """Merge the workspace branch into its base branch locally."""
    db = DB()
    ws = _ws(db, workspace)
    target = _run(workspaces.merge_back, db, ws, squash)
    typer.echo(f"✓ merged {ws.branch} into {ws.base_branch} ({target})")


@app.command("services")
def services_cmd(
    action: str = typer.Argument("ls", help="ls, up or down."),
    workspace: Optional[str] = typer.Argument(None, help="Workspace (default: the current one)."),
) -> None:
    """Per-worktree Docker services (copse Pro): list, start or stop a workspace's."""
    from copse import services as services_mod
    from copse.config import load_repo_config

    if action not in ("ls", "up", "down"):
        _fail("action must be ls, up or down")
    ws = _ws(DB(), workspace)
    cfg = load_repo_config(ws.repo_root)
    if not cfg.services:
        _fail('no services configured; add a "services" list to .copse/config.json')
    if action == "up":
        done = services_mod.up(ws, cfg)
        typer.echo("started: " + (", ".join(done) or "nothing"))
    elif action == "down":
        done = services_mod.down(ws, cfg)
        typer.echo("stopped: " + (", ".join(done) or "nothing"))
    else:
        lines = services_mod.status(ws)
        typer.echo("\n".join(lines) if lines else f"no services running for {ws.id}")


@app.command()
def rm(
    workspace: str,
    force: bool = typer.Option(False, "--force", "-f", help="Discard uncommitted changes; ignore teardown failure."),
    delete_branch: Optional[bool] = typer.Option(None, "--delete-branch/--keep-branch", "-D/-K", help="Delete the branch (only if merged, unless --force), or keep it. Default: delete it once fully merged into its base."),
) -> None:
    """Stop a workspace's agents and remove its worktree; a fully merged branch goes too.

    An unmerged branch is always kept; -K keeps any branch, and a repo can
    set delete_merged_branches to false."""
    db = DB()
    ws = _ws(db, workspace)
    if ws.base_branch and os.path.isdir(ws.path) and not delete_branch:
        try:
            st = git.status(ws.path, ws.base_branch)
        except git.GitError:
            st = None  # e.g. the base branch is gone; the note is only a courtesy
        if st and st.ahead and st.unpushed != 0:
            typer.secho(
                f"note: {ws.branch} has {st.ahead} commit(s) not in {ws.base_branch} "
                "and not pushed; the branch is kept.", fg="yellow",
            )
    removed = _run(workspaces.remove, db, ws, force=force, delete_branch=delete_branch)
    if removed.teardown and not removed.teardown.ok:
        typer.secho(f"teardown failed (ignored with --force):\n{removed.teardown.log}", fg="yellow")
    typer.echo(f"✓ removed {ws.id}. {removed.branch_note or 'branch deleted'}")


# -- agents -----------------------------------------------------------------


@agent_app.command("spawn")
def agent_spawn(
    profile: str,
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w"),
    prompt: Optional[str] = typer.Option(None, "--prompt", "-p"),
    provider: Optional[str] = typer.Option(None),
) -> None:
    """Start another agent in an existing workspace."""
    db = DB()
    ws = _ws(db, workspace)
    a = _run(agents.spawn, db, ws, profile, prompt=prompt, provider_name=provider)
    typer.echo(f"✓ {a.id} ({a.profile}/{a.provider}) in {ws.id}")


@agent_app.command("profiles")
def agent_profiles() -> None:
    """List available agent profiles."""
    root = None
    try:
        root = git.main_repo_root(os.getcwd())
    except git.GitError:
        pass
    from copse.providers import unusable

    why: dict[str, str | None] = {}
    hidden: dict[str, list[str]] = {}
    for p in list_profiles(root):
        if p.provider not in why:
            why[p.provider] = unusable(p.provider)
        if why[p.provider]:
            hidden.setdefault(why[p.provider], []).append(p.name)
            continue
        typer.echo(f"{p.name:<14} {p.provider:<7} {p.description}")
    for reason, names in hidden.items():
        typer.secho(f"hidden ({reason}): {', '.join(names)}", dim=True)


@agent_app.command("kill")
def agent_kill(agent_id: str) -> None:
    """Stop an agent and close its window."""
    db = DB()
    _run(agents.kill, db, agent_id)
    typer.echo(f"✓ killed {agent_id}")


@agent_app.command("peek")
def agent_peek(agent_id: str, lines: int = typer.Option(40, "--lines", "-n")) -> None:
    """Print the last lines of an agent's terminal."""
    db = DB()
    a = _run(agents.get, db, agent_id)
    if not a.tmux_window:
        typer.secho(f"{a.id} has no terminal (it runs as its supervisor's own subagent)"
                    if not agents.runs_process(a) else f"{a.id} has no terminal", fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(tmux.capture(a.tmux_window, lines=lines).rstrip())


@app.command()
def send(agent_id: str, message: str) -> None:
    """Send a message to an agent (queued until it's idle)."""
    db = DB()
    outcome = _run(agents.send_message, db, agent_id, message, person=True)
    typer.echo(outcome)


@app.command()
def mcp() -> None:
    """Run the copse MCP server on stdio (agents launch this automatically)."""
    from copse.mcp_server import main

    main()


# -- copse ci (copse Team) ---------------------------------------------------

ci_app = typer.Typer(no_args_is_help=True,
                     help="Run copse headless in CI: an issue in, a pull request out (copse Team).")
app.add_typer(ci_app, name="ci")


@ci_app.command("run")
def ci_run(
    goal: Optional[str] = typer.Option(None, "--goal", help="The goal, as text (a goals.md-shaped text brings its milestones)."),
    goal_file: Optional[str] = typer.Option(None, "--goal-file", help="Read the goal from this file (goals.md format or plain text)."),
    issue: Optional[int] = typer.Option(None, "--issue", help="Take the goal from this GitHub issue (title and body, via gh); the PR closes it."),
    timeout: float = typer.Option(60, "--timeout", help="Minutes to wait for every milestone to be verified."),
    max_workers: Optional[int] = typer.Option(None, "--max-workers", help="Cap on workers running at once (sets max_agents in .copse/config.local.json)."),
    base: Optional[str] = typer.Option(None, "--base", help="Branch to cut the work from and open the PR against (default: the repo's base)."),
    no_pr: bool = typer.Option(False, "--no-pr", help="Don't push or open a pull request; just report."),
) -> None:
    """Run a supervisor with autopilot on, unattended, until the goal is verified; then open a PR.

    The work happens on a fresh `copse/ci-<issue or slug>` branch. Exits 0
    with the PR URL when every milestone's check passes; otherwise exits 1
    with what happened (the supervisor's question, a stall, the timeout).
    Needs the `ci` feature (copse Team); in CI, set COPSE_PRO_TOKEN to an org CI
    token from `copse account org ci-token create`."""
    from copse import ci

    raise typer.Exit(ci.run_cli(goal=goal, goal_file=goal_file, issue=issue, timeout_min=timeout,
                                max_workers=max_workers, base=base, pr=not no_pr, echo=typer.echo))


@ci_app.command("init")
def ci_init(
    label: str = typer.Option("copse", "--label", help="Issues given this label start a run."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing workflow file."),
) -> None:
    """Write .github/workflows/copse.yml: `copse ci run` on labelled issues and on demand."""
    from copse import ci

    root = _run(git.main_repo_root, os.getcwd())
    try:
        path = ci.init(root, label=label, force=force)
    except ci.CIError as e:
        _fail(str(e))
    typer.echo(f"wrote {path}")
    typer.echo("Add the COPSE_PRO_TOKEN secret (from `copse account org ci-token create`) and "
               "ANTHROPIC_API_KEY, and allow GitHub Actions to create pull requests in the repo's "
               "Actions settings. Only people you trust with write access should be able to apply "
               "the label.")


# -- internal ----------------------------------------------------------------


@app.command("_hook", hidden=True)
def hook(event: str, agent: Optional[str] = typer.Option(None, "--agent"),
         payload: Optional[str] = typer.Argument(None)) -> None:
    if event.startswith("agy-"):
        from copse import antigravity

        typer.echo(antigravity.hook_main(DB(), event, sys.stdin.read()))
        return
    # --agent is baked into the hook command at launch; the environment is
    # only a fallback for sessions launched by an older copse (and may be stale).
    agent_id = agent or os.environ.get("COPSE_AGENT_ID")
    if not agent_id:
        return
    # Codex's notify passes the JSON as an argument; Claude Code's hooks use stdin.
    text = payload if payload is not None else sys.stdin.read()
    if event == "permission-request":
        try:  # any failure means no output: the person is asked as usual
            out = agents.hook_main(DB(), agent_id, event, text, trusted=agent is not None)
        except Exception:  # noqa: BLE001
            return
    else:
        out = agents.hook_main(DB(), agent_id, event, text, trusted=agent is not None)
    if out:
        typer.echo(out)


def _helper_db() -> DB:
    """The DB for a detached helper (``_after-launch``, ``_cull``, ...). One
    can outlive whatever started it; if its copse home is gone by then (a
    finished test run's temp dir), it stops rather than recreate the home
    and act on an empty DB."""
    from copse.config import db_path

    if not db_path().exists():
        raise typer.Exit(0)
    return DB()


@app.command("_after-launch", hidden=True)
def after_launch_cmd(agent_id: str) -> None:
    from copse.providers import get_provider

    db = _helper_db()
    a = db.get_agent(agent_id)
    if a and a.tmux_window:
        get_provider(a.provider).after_launch(a.tmux_window)
        agents.ready(db, agent_id)


@app.command("_headless", hidden=True)
def headless_cmd(agent_id: str, resume: Optional[str] = typer.Option(None)) -> None:
    """A headless worker's pane: runs its `claude -p` turns (agents.run_headless)."""
    raise typer.Exit(agents.run_headless(DB(), agent_id, resume))


@app.command("_native", hidden=True)
def _native(agent_id: str, resume: Optional[str] = typer.Option(None, "--resume")):
    """A native worker's pane: copse's own agent loop (copse.native.runner)."""
    from copse.native import runner

    raise typer.Exit(runner.run_native(DB(), agent_id, resume))


@app.command("_ended", hidden=True)
def ended_cmd(agent_id: str) -> None:
    import signal

    # Runs inside the window it's about to close; don't die with it.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    agents.ended(DB(), agent_id)


@app.command("_quit", hidden=True)
def quit_cmd(root_id: str) -> None:
    import signal

    # Runs detached from the sidebar it's about to close; don't die with it.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    db = DB()
    root = db.get_agent(root_id)
    if root is not None and root.status not in ("paused", "done"):
        agents.pause(db, root_id)


@app.command("_close", hidden=True)
def close_cmd(agent_id: str, delay: float = typer.Option(0.0)) -> None:
    time.sleep(delay)
    try:
        agents.kill(_helper_db(), agent_id)
    except agents.AgentError:
        pass


@app.command("_statusline", hidden=True)
def statusline_cmd() -> None:
    from copse.providers import status_line

    out = status_line(sys.stdin.read())
    if out:
        typer.echo(out)


@app.command("_sidebar-follow", hidden=True)
def sidebar_follow_cmd(session: str) -> None:
    """Run from the session-window-changed / client-session-changed hooks
    tmux.apply_theme sets on every copse session: relocate the sidebar pane
    here (see agents.sidebar_follow). Never raises: this runs from a tmux
    hook, where an uncaught error would show as a message popup or a
    nonzero exit tmux might complain about."""
    try:
        agents.sidebar_follow(DB(), session)
    except Exception:
        pass


@app.command("_local-models", hidden=True)
def local_models_cmd(repo: Optional[str] = typer.Option(None, "--repo")) -> None:
    """Start Ollama for the native profiles and load their models (detached
    from `copse`; what happened goes to ~/.copse/ollama.log)."""
    from copse.config import RepoConfig, load_repo_config
    from copse.native import serve

    try:
        cfg = load_repo_config(repo) if repo else RepoConfig()
        lines = serve.ensure(repo, cfg)
        with open(serve.log_path(), "a", encoding="utf-8") as f:
            for line in lines:
                f.write(f"== copse: {line}\n")
    except Exception:  # noqa: BLE001 -- detached: nobody to report to
        pass


@app.command("_sync-settings", hidden=True)
def sync_settings_cmd() -> None:
    """Pull synced settings (copse Pro); detached from `copse`."""
    from copse.pro import settings_sync

    settings_sync.pull()


@app.command("_cull", hidden=True)
def cull_cmd(repo: Optional[str] = typer.Option(None, "--repo")) -> None:
    from copse import cull, sessions

    db = _helper_db()
    if repo:
        try:
            sessions.enforce(db, repo)
        except Exception:  # noqa: BLE001 -- detached: nobody to report to
            pass
    cull.sweep_quietly(db)


@app.command("_flush", hidden=True)
def flush_cmd(agent_id: str, delay: float = typer.Option(0.0)) -> None:
    time.sleep(delay)
    agents.flush(_helper_db(), agent_id)


@app.command("_deliver-checks", hidden=True)
def deliver_checks_cmd(reviewer_id: str, workspace_id: str) -> None:
    """Run a repo's checks for a reviewer and deliver the summary to its
    inbox. Started detached from request_review, so the checks still finish
    and get delivered even if the MCP server that started it has exited."""
    from copse.config import load_repo_config

    db = _helper_db()
    ws = db.get_workspace(workspace_id)
    if ws is None:
        return
    agents.deliver_check_summary(db, reviewer_id, ws, load_repo_config(ws.repo_root))


@app.command("_check-milestones", hidden=True)
def check_milestones_cmd(root_id: str, workspace_id: str,
                         position: Optional[int] = typer.Option(None, "--position")) -> None:
    """Run a session's milestone checks and deliver the result to its
    supervisor's inbox. Started detached from check_milestone."""
    from copse import autopilot
    from copse.mcp_server import record_milestone_changes

    db = _helper_db()
    ws = db.get_workspace(workspace_id)
    root = db.get_agent(root_id)
    if ws is None or root is None:
        return
    before = {m.id: m.status for m in db.milestones(root_id)}
    try:
        text = autopilot.check_milestones(db, root_id, ws, position)
        record_milestone_changes(db, root_id, ws, before, root, position, text)
    except Exception as e:  # noqa: BLE001 - the supervisor must hear about it either way
        text = f"The milestone check failed to run: {e}"
    finally:
        db.update_autopilot(root_id, checking_since=None)
    if db.get_agent(root_id) is None:
        return
    body = f"[copse] Milestone check finished.\n\n{text}"
    try:
        agents.send_message(db, root_id, body)
    except (agents.AgentError, tmux.TmuxError):
        # Not running (or unreachable): it stays queued; the next Stop hook hands it over.
        db.enqueue(root_id, body, None)


@app.command("_warm-checks", hidden=True)
def warm_checks_cmd(workspace_id: str) -> None:
    """Run and cache a branch's checks right after its worker reports, so the
    review and the merge gate find the result ready instead of each running
    the suite. Started detached from report_result."""
    from copse import gates
    from copse.config import load_repo_config

    db = _helper_db()
    ws = db.get_workspace(workspace_id)
    if ws is None:
        return
    gates.check_summary(db, ws, load_repo_config(ws.repo_root))


@app.command("_pool-fill", hidden=True)
def pool_fill_cmd(repo_root: str) -> None:
    """Top the worktree pool back up to `pool_size`. Started detached, after a
    claim and at supervisor start (see `workspaces.create`, `start`)."""
    from copse import pool

    pool.fill_locked(_helper_db(), repo_root)
