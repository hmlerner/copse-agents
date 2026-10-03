"""Launching never leaves an agent hung at startup, and one that is stuck on
a prompt is visible.

- A profile's permission_mode (and allowed_tools) reaches the CLI on every
  launch path: a fresh spawn, a handoff/assign worker, a resume via `copse
  continue` (with and without a saved conversation), and a reviewer.
- The worktree a Claude worker starts in is pre-trusted, so Claude Code's
  first-run folder-trust dialog never waits on nobody.
- The foreground launch paths (`copse start`, handoff/assign) never wait on
  slow work: retention, culling, pool refills and startup dialogs happen in
  detached helpers.
- A pane showing a permission or trust prompt reads as 'waiting', and a
  worker stuck like that is reported to its supervisor, once.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
import time

import pytest

from copse import agents, cli, cull, pool, procs, sessions, tmux, workspaces
from copse.db import Agent
from copse.providers import ClaudeCode, trust_folder

from test_agents import CLAUDE_PROMPT
from test_reliability import CLAUDE_BUSY, CLAUDE_IDLE

TRUST_SCREEN = (
    " Do you trust the files in this folder?\n\n"
    " /Users/me/.copse/worktrees/proj/feature\n\n"
    " ❯ 1. Yes, proceed\n   2. No, exit\n\n Enter to confirm · Esc to exit\n"
)


def flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


@pytest.fixture
def launches(monkeypatch):
    """Record each CLI command line copse would run, without running it."""
    seen: list[list[str]] = []

    def fake_open(db, agent, ws, name, argv, watch_pane):
        seen.append(argv)
        target = f"%fake-{agent.id}"
        db.update_agent(agent.id, tmux_window=target)
        agent.tmux_window = target
        return target

    monkeypatch.setattr(agents, "_open_window", fake_open)
    monkeypatch.setattr(ClaudeCode, "after_launch", lambda self, target: None)
    return seen


@pytest.fixture
def detached(monkeypatch):
    """Record copse's detached helpers (`copse _after-launch`, `_cull`,
    `_pool-fill`) instead of starting them; every other process runs."""
    started: list[list[str]] = []
    real = subprocess.Popen

    def popen(args, *a, **k):
        if isinstance(args, list) and args[1:3] == ["-m", "copse"] and str(args[3]).startswith("_"):
            started.append([str(x) for x in args])
            return real(["true"])
        return real(args, *a, **k)

    monkeypatch.setattr(subprocess, "Popen", popen)
    return started


@pytest.fixture
def root(db, repo):
    ws = workspaces.adopt_root(db, str(repo))
    boss = Agent("boss", ws.id, "supervisor", "claude", None, "interactive",
                 "processing", "%boss", None, time.time())
    db.add_agent(boss)
    return boss, ws


@pytest.fixture
def claude_config(tmp_path, monkeypatch):
    """A Claude Code config dir with an existing global state file."""
    config = tmp_path / "claude-config"
    config.mkdir(exist_ok=True)
    (config / ".claude.json").write_text(json.dumps(
        {"numStartups": 3, "projects": {"/elsewhere": {"hasTrustDialogAccepted": True}}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    return config


# -- permission mode survives every launch path ----------------------------------


def test_fresh_worker_spawn_gets_its_permission_mode(db, root, launches, detached):
    boss, ws = root
    worker, _ = agents.delegate(db, boss, ws, "developer", "add a flag", "assign")
    argv = launches[-1]
    assert flag(argv, "--permission-mode") == "auto"
    assert "Bash(git commit:*)" in flag(argv, "--allowedTools")
    assert worker.profile == "developer"


def test_resume_keeps_permission_mode_with_and_without_a_saved_conversation(
        db, root, launches, detached, claude_config):
    boss, ws = root
    worker, _ = agents.delegate(db, boss, ws, "developer", "add a flag", "assign")
    for a in (boss, worker):
        db.set_status(a.id, "paused")
    # The worker has a saved conversation (resumed with --resume); the
    # supervisor never said anything (starts fresh on its task).
    db.update_agent(worker.id, session_ref="sess-1")
    saved = claude_config / "projects" / "p"
    saved.mkdir(parents=True)
    (saved / "sess-1.jsonl").write_text("{}\n")
    launches.clear()

    agents.resume(db, boss.id, watch_pane=False)

    by_resume = {flag(argv, "--resume"): argv for argv in launches}
    assert flag(by_resume["sess-1"], "--permission-mode") == "auto"
    assert "Bash(git commit:*)" in flag(by_resume["sess-1"], "--allowedTools")
    assert len(launches) == 2


def test_profile_named_differently_inside_still_resumes_with_its_own_permissions(
        db, root, launches, detached):
    """A copied profile whose frontmatter `name:` wasn't changed: the agent
    must record the file it was started from, or a resume loads another
    profile (here the built-in developer) with other permissions."""
    boss, ws = root
    agents_dir = os.path.join(ws.repo_root, ".copse", "agents")
    os.makedirs(agents_dir)
    with open(os.path.join(agents_dir, "careful.md"), "w") as f:
        f.write("---\nname: developer\nprovider: claude\npermission_mode: auto\n---\nBe careful.\n")
    worker, _ = agents.delegate(db, boss, ws, "careful", "add a flag", "assign")
    assert db.get_agent(worker.id).profile == "careful"
    db.set_status(boss.id, "paused")
    db.set_status(worker.id, "paused")
    launches.clear()

    agents.resume(db, boss.id, watch_pane=False)

    assert [flag(argv, "--permission-mode") for argv in launches].count("auto") == 1


def test_reviewer_never_waits_on_a_permission_prompt(db, root, launches, detached):
    boss, ws = root
    agents.spawn(db, ws, "reviewer", prompt="review this", parent_id=boss.id, mode="review")
    argv = launches[-1]
    # dontAsk: whatever allowed_tools doesn't cover is refused, not asked about.
    assert flag(argv, "--permission-mode") == "dontAsk"
    assert "Bash(git diff:*)" in flag(argv, "--allowedTools")


# -- worktree pre-trust ------------------------------------------------------------


def test_worker_worktree_is_pre_trusted(db, root, launches, detached, claude_config):
    boss, ws = root
    _, wws = agents.delegate(db, boss, ws, "developer", "add a flag", "assign")
    data = json.loads((claude_config / ".claude.json").read_text())
    assert data["projects"][os.path.realpath(wws.path)]["hasTrustDialogAccepted"] is True
    # Everything else in Claude Code's state is left as it was.
    assert data["numStartups"] == 3
    assert data["projects"]["/elsewhere"] == {"hasTrustDialogAccepted": True}


def test_trust_leaves_a_missing_or_broken_config_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert trust_folder(str(tmp_path)) is False
    assert not (tmp_path / ".claude.json").exists()
    (tmp_path / ".claude.json").write_text("{not json")
    assert trust_folder(str(tmp_path)) is False
    assert (tmp_path / ".claude.json").read_text() == "{not json"


def test_trust_keeps_the_config_private(tmp_path, claude_config):
    config = claude_config / ".claude.json"
    config.chmod(0o600)
    assert trust_folder(str(tmp_path)) is True
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_trust_cleans_up_its_temp_file_when_the_write_fails(tmp_path, claude_config, monkeypatch):
    config = claude_config / ".claude.json"
    before = config.read_text()

    def fail(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    assert trust_folder(str(tmp_path)) is False
    assert config.read_text() == before
    assert sorted(p.name for p in claude_config.iterdir()) == [".claude.json"]


def test_concurrent_trusts_all_land(tmp_path, claude_config):
    folders = [tmp_path / f"wt{i}" for i in range(8)]
    for f in folders:
        f.mkdir()
    threads = [threading.Thread(target=trust_folder, args=(str(f),)) for f in folders]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    projects = json.loads((claude_config / ".claude.json").read_text())["projects"]
    assert all(projects[os.path.realpath(f)]["hasTrustDialogAccepted"] is True for f in folders)
    assert sorted(p.name for p in claude_config.iterdir()) == [".claude.json"]


def test_trust_is_idempotent(tmp_path, claude_config):
    assert trust_folder(str(tmp_path)) is True
    before = (claude_config / ".claude.json").stat().st_mtime_ns
    assert trust_folder(str(tmp_path)) is True
    assert (claude_config / ".claude.json").stat().st_mtime_ns == before


# -- the foreground launch path never waits on slow work -------------------------

BUDGET = 3.0  # seconds; each stubbed slow step below takes longer than this alone


@pytest.fixture
def slow_background(monkeypatch):
    def slow(*a, **k):
        time.sleep(BUDGET + 1)

    monkeypatch.setattr(sessions, "enforce", slow)
    monkeypatch.setattr(cull, "sweep", slow)
    monkeypatch.setattr(pool, "fill_in_background", slow)
    monkeypatch.setattr(procs, "stop", slow)
    monkeypatch.setattr(ClaudeCode, "after_launch", lambda self, target: slow())


def test_copse_start_returns_within_budget(db, repo, monkeypatch, detached, slow_background):
    monkeypatch.chdir(repo)
    ws = workspaces.adopt_root(db, str(repo))
    # A session still running here, which start must pause first.
    old = agents.spawn(db, ws, "supervisor", provider_name="shell")
    try:
        t0 = time.monotonic()
        cli.start(agent="supervisor", prompt=None, provider="shell", attach=False,
                  watch=False, autopilot=False, branch=None, worktree=None)
        elapsed = time.monotonic() - t0
    finally:
        tmux.kill_session(ws.tmux_session)
    assert elapsed < BUDGET
    assert db.get_agent(old.id).status == "paused"
    # Retention and culling went to the detached helper instead.
    assert ["_cull", "--repo", ws.repo_root] in [argv[3:] for argv in detached]


def test_assign_returns_within_budget(db, root, launches, detached, slow_background):
    boss, ws = root
    t0 = time.monotonic()
    worker, _ = agents.delegate(db, boss, ws, "developer", "add a flag", "assign")
    assert time.monotonic() - t0 < BUDGET
    # The startup dialog is handled by the detached helper, not waited on here.
    assert ["_after-launch", worker.id] in [argv[3:] for argv in detached]


# -- a prompt is visible: 'waiting', and its supervisor is told once -----------------


def test_trust_dialog_reads_as_waiting():
    assert ClaudeCode().screen_state(TRUST_SCREEN) == "waiting"
    assert ClaudeCode().screen_state(CLAUDE_PROMPT) == "waiting"


def worker_on(db, ws, status, since, parent="boss"):
    a = Agent("w1", ws.id, "developer", "claude", parent, "assign", status, "%w1", None,
              since, status_since=since)
    db.add_agent(a)
    return a


@pytest.fixture
def screens(monkeypatch):
    """What each pane shows; the supervisor's is busy, so messages queue."""
    shown = {"%boss": CLAUDE_BUSY}
    monkeypatch.setattr(tmux, "capture", lambda target, **k: shown.get(target, CLAUDE_IDLE))
    monkeypatch.setattr(tmux, "paste", lambda *a, **k: None)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return shown


def test_starting_worker_on_the_trust_dialog_shows_waiting(db, root, screens):
    _, ws = root
    screens["%w1"] = TRUST_SCREEN
    a = worker_on(db, ws, "starting", time.time())
    agents.reconcile(db, a, samples=1)  # what the sidebar does every redraw
    assert db.get_agent("w1").status == "waiting"
    # Once the dialog is answered, Claude Code starts and says so.
    agents.handle_hook(db, "w1", "session-start", {})
    assert db.get_agent("w1").status == "idle"


def test_starting_worker_is_not_marked_ready_by_the_screen(db, root, screens):
    _, ws = root
    a = worker_on(db, ws, "starting", time.time())
    agents.reconcile(db, a, samples=1)
    assert db.get_agent("w1").status == "starting"  # only its hooks say it's ready


def test_stuck_worker_is_reported_to_its_supervisor_once(db, root, screens):
    _, ws = root
    screens["%w1"] = CLAUDE_PROMPT
    since = time.time() - cull.STUCK_AFTER - 5
    worker_on(db, ws, "waiting", since)
    now = time.time()

    assert cull.note_stuck(db, now, {}) != []
    assert cull.note_stuck(db, now + 60, {}) == []  # still the same spell: not again
    assert db.pending_count("boss") == 1
    body = db.pop_pending("boss").body
    assert "w1" in body and "waiting on a prompt" in body and "Do you want to proceed?" in body

    # Answered, then stuck again later: that's a new spell, reported again.
    db.set_status("w1", "processing")
    db.set_status("w1", "waiting")
    db.update_agent("w1", status_since=now - cull.STUCK_AFTER - 1)
    assert cull.note_stuck(db, now, {}) != []
    assert db.pending_count("boss") == 1


def test_trust_dialog_nobody_answered_is_reported_by_the_sweep(db, root, screens):
    """No hook runs before the trust dialog is answered, so the sweep reads
    the screen itself, then reports it once it's been waiting long enough."""
    _, ws = root
    screens["%w1"] = TRUST_SCREEN
    worker_on(db, ws, "starting", time.time() - 600)
    now = time.time()
    assert cull.note_stuck(db, now, {}) == []  # just noticed: waiting from now on
    assert db.get_agent("w1").status == "waiting"
    assert cull.note_stuck(db, now + cull.STUCK_AFTER + 1, {}) != []
    assert db.pending_count("boss") == 1


def test_briefly_waiting_worker_is_not_reported(db, root, screens):
    _, ws = root
    screens["%w1"] = CLAUDE_PROMPT
    worker_on(db, ws, "waiting", time.time() - 5)
    assert cull.note_stuck(db, time.time(), {}) == []
    assert db.pending_count("boss") == 0


def test_trust_writes_through_a_symlinked_config(tmp_path, claude_config):
    real = tmp_path / "dotfiles" / "claude.json"
    real.parent.mkdir()
    link = claude_config / ".claude.json"
    real.write_text(link.read_text())
    link.unlink()
    link.symlink_to(real)
    folder = tmp_path / "wt"
    folder.mkdir()
    assert trust_folder(str(folder)) is True
    assert link.is_symlink()
    assert json.loads(real.read_text())["projects"][os.path.realpath(folder)]["hasTrustDialogAccepted"] is True


def test_silent_codex_worker_is_reported_once(db, root, screens, monkeypatch):
    """Issue #42: a Codex worker (no hooks) stuck on a startup error."""
    _, ws = root
    screens["%w1"] = "■ unexpected status 403 Forbidden: your plan doesn't include Codex\n"
    now = time.time()
    a = worker_on(db, ws, "unknown", now - 3600)
    db.update_agent(a.id, provider="codex")
    last = now - cull.SILENT_AFTER - 5
    monkeypatch.setattr(tmux, "window_activity", lambda target: last)

    assert cull.note_silent(db, now, {}) != []
    assert cull.note_silent(db, now + 60, {}) == []  # same silence: not again
    body = db.pop_pending("boss").body
    assert "w1" in body and "403" in body and "another profile" in body


def test_recently_active_worker_is_not_silent(db, root, screens, monkeypatch):
    _, ws = root
    worker_on(db, ws, "unknown", time.time() - 3600)
    monkeypatch.setattr(tmux, "window_activity", lambda target: time.time() - 30)
    assert cull.note_silent(db, time.time(), {}) == []
