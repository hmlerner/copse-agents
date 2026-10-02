"""copse leaves nothing behind: test tmux servers, merged worktrees, stale
locks, orphan sessions and servers, empty worktree folders."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import sh
from copse import cull, procs, tmux, view, workspaces
from copse.cli import app
from copse.config import copse_home, worktrees_dir
from copse.db import Agent

OLD = time.time() - 3600


def add_agent(db, ws, agent_id, *, status="done", result="did it", window="%999",
              mode="handoff", created_at=OLD):
    db.add_agent(Agent(agent_id, ws.id, "developer", "shell", None, mode, status, window,
                       result, created_at))
    return db.get_agent(agent_id)


def merged_worker(db, repo, name="feat-done"):
    """A worker's workspace whose one commit is merged into main."""
    ws = workspaces.create(db, str(repo), name).workspace
    (Path(ws.path) / f"{name}.py").write_text("x = 1\n")
    sh("git add -A && git commit -qm work", Path(ws.path))
    workspaces.merge_back(db, ws)
    return ws


def unmerged_worker(db, repo, name="feat-open"):
    ws = workspaces.create(db, str(repo), name).workspace
    (Path(ws.path) / f"{name}.py").write_text("x = 1\n")
    sh("git add -A && git commit -qm work", Path(ws.path))
    return ws


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def start_server(name: str, home: str) -> None:
    subprocess.run(["tmux", "-L", name, "new-session", "-d", "-s", "copse_x_y",
                    "-e", f"COPSE_HOME={home}"], check=True)


def server_up(name: str) -> bool:
    return subprocess.run(["tmux", "-L", name, "has-session"], capture_output=True).returncode == 0


# -- test servers and detached helpers ------------------------------------------------


def test_killing_a_private_server_removes_its_socket():
    tmux.ensure_session("copse_sock_test", "/tmp", {})
    sock = tmux.socket_dir() / os.environ["COPSE_TMUX_SOCKET"]
    assert sock.exists()
    tmux.kill_server()
    assert not sock.exists()


def test_a_dead_test_runs_server_is_reaped(tmp_path):
    name = f"copse-test-{dead_pid()}"
    start_server(name, str(tmp_path))  # its home still exists: the pid decides
    try:
        assert any(name in line for line in cull.orphan_servers())
        assert not server_up(name)
        assert not (tmux.socket_dir() / name).exists()
    finally:
        tmux.reap_server(name)


def test_conftest_reaps_dead_runs_left_over_from_earlier(tmp_path):
    from conftest import _reap_dead_test_servers

    name = f"copse-test-{dead_pid()}"
    start_server(name, str(tmp_path))
    try:
        _reap_dead_test_servers()
        assert not server_up(name)
        assert not (tmux.socket_dir() / name).exists()
    finally:
        tmux.reap_server(name)


def test_a_live_test_runs_server_is_left_alone(tmp_path):
    name = f"copse-test-{os.getpid()}-other"  # not a pid suffix, home exists
    start_server(name, str(tmp_path))
    try:
        cull.orphan_servers()
        assert server_up(name)
    finally:
        tmux.reap_server(name)


def test_a_server_whose_copse_home_is_gone_is_reaped(tmp_path):
    name = f"copse-e2e-remnant-{os.getpid()}"
    start_server(name, str(tmp_path / "deleted-home"))
    try:
        assert any(name in line for line in cull.orphan_servers())
        assert not server_up(name)
    finally:
        tmux.reap_server(name)


def test_a_dead_servers_socket_file_is_removed():
    name = f"copse-e2e-stale-{os.getpid()}"
    sock = tmux.socket_dir() / name
    tmux.socket_dir().mkdir(parents=True, exist_ok=True)
    sock.touch()
    cull.orphan_servers()
    assert not sock.exists()


def test_detached_helpers_stop_when_their_copse_home_is_gone(tmp_path, monkeypatch):
    gone = tmp_path / "gone-home"
    monkeypatch.setenv("COPSE_HOME", str(gone))
    runner = CliRunner()
    for args in (["_after-launch", "abc"], ["_cull"], ["_deliver-checks", "abc", "p/w"],
                 ["_pool-fill", str(tmp_path)], ["_flush", "abc"], ["_close", "abc"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, (args, result.output)
    assert not gone.exists()  # nothing recreated it (and nothing opened a session for it)


def test_real_spawn_sessions_end_with_the_test(db, repo):
    from copse import agents

    ws = workspaces.create(db, str(repo), "feat-leak").workspace
    # No prompt: a shell worker's prompt is pasted into a real shell and run
    # (its worker footer's `copse` would start a supervisor in this session).
    agents.spawn(db, ws, "developer", provider_name="shell", mode="handoff")
    assert tmux.has_session(ws.tmux_session)
    # conftest's pytest_runtest_teardown kills it after this test; the next test
    # checks nothing carried over.


def test_no_session_carries_over_from_the_previous_test():
    assert tmux.list_sessions() == []


# -- merged, finished worktrees ------------------------------------------------------


def test_merged_worktree_with_finished_agents_is_hidden(db, repo):
    done = merged_worker(db, repo)
    add_agent(db, done, "a1")
    open_ = unmerged_worker(db, repo)
    add_agent(db, open_, "a2")
    shown = {e["id"] for e in view.snapshot(db, str(repo), panes={})}
    assert done.id not in shown
    assert open_.id in shown


def test_merged_worktree_with_uncommitted_changes_is_hidden_but_kept(db, repo):
    ws = merged_worker(db, repo)
    add_agent(db, ws, "a1")
    (Path(ws.path) / "wip.py").write_text("y = 2\n")
    assert ws.id not in {e["id"] for e in view.snapshot(db, str(repo), panes={})}
    lines = cull.prune_retired(db)
    assert any("uncommitted" in line for line in lines)
    assert os.path.isdir(ws.path) and db.get_workspace(ws.id)


def test_merged_worktree_with_a_paused_unreported_agent_stays(db, repo):
    ws = merged_worker(db, repo)
    add_agent(db, ws, "a1", status="paused", result=None)
    assert ws.id in {e["id"] for e in view.snapshot(db, str(repo), panes={})}
    assert cull.prune_retired(db) == []


def test_merged_worktree_with_no_agents_is_hidden_once_old(db, repo):
    ws = merged_worker(db, repo)
    assert ws.id in {e["id"] for e in view.snapshot(db, str(repo), panes={})}  # just made
    db.conn.execute("UPDATE workspaces SET created_at=? WHERE id=?", (OLD, ws.id))
    assert ws.id not in {e["id"] for e in view.snapshot(db, str(repo), panes={})}


def test_prune_removes_merged_worktrees_and_their_branches(db, repo):
    done = merged_worker(db, repo)
    add_agent(db, done, "a1")
    open_ = unmerged_worker(db, repo)
    add_agent(db, open_, "a2")
    lines = cull.prune_retired(db)
    assert any(done.path in line and "branch deleted" in line for line in lines)
    assert not os.path.exists(done.path)
    assert db.get_workspace(done.id) is None
    assert not sh(f"git branch --list {done.branch}", repo)  # merged: branch gone too
    assert os.path.isdir(open_.path) and db.get_workspace(open_.id)  # unmerged untouched


def test_prune_keeps_merged_branches_when_the_repo_says_so(db, repo):
    (repo / ".copse").mkdir(exist_ok=True)
    (repo / ".copse" / "config.json").write_text('{"delete_merged_branches": false}')
    done = merged_worker(db, repo)
    add_agent(db, done, "a1")
    lines = cull.prune_retired(db)
    assert any(f"branch {done.branch} kept" in line for line in lines)
    assert sh(f"git branch --list {done.branch}", repo)


def test_removing_a_worktree_keeps_an_unmerged_branch(db, repo):
    open_ = unmerged_worker(db, repo)
    removed = workspaces.remove(db, open_)
    assert not removed.branch_deleted and "kept" in removed.branch_note
    assert sh(f"git branch --list {open_.branch}", repo)


def test_prune_command_cleans_up(db, repo, monkeypatch):
    done = merged_worker(db, repo)
    add_agent(db, done, "a1")
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(app, ["prune"])
    assert result.exit_code == 0, result.output
    assert "removed merged worktree" in result.output
    assert not os.path.exists(done.path)
    # its now-empty parent folders went too
    assert not (worktrees_dir() / "proj" / "feat-done").exists()


# -- stale sidebar locks -------------------------------------------------------------


def test_stale_sidebar_locks_are_removed(db, repo):
    ws = workspaces.adopt_root(db, str(repo))
    add_agent(db, ws, "live", status="processing", result=None, mode="interactive")
    add_agent(db, ws, "over", status="done", mode="interactive")
    locks = copse_home() / "locks"
    locks.mkdir(parents=True)
    for aid in ("live", "over", "gone", "fresh"):
        (locks / f"sidebar-{aid}.lock").write_text("")
        if aid != "fresh":
            os.utime(locks / f"sidebar-{aid}.lock", (OLD, OLD))
    assert cull.clean_locks(db) == 2
    assert sorted(p.name for p in locks.iterdir()) == ["sidebar-fresh.lock", "sidebar-live.lock"]


def test_the_periodic_sweep_cleans_locks(db):
    locks = copse_home() / "locks"
    locks.mkdir(parents=True)
    (locks / "sidebar-gone.lock").write_text("")
    os.utime(locks / "sidebar-gone.lock", (OLD, OLD))
    assert any("lock" in line for line in cull.sweep(db))
    assert not (locks / "sidebar-gone.lock").exists()


# -- orphan tmux sessions, empty folders -----------------------------------------------


def test_orphan_copse_sessions_are_killed(db, repo):
    ws = workspaces.create(db, str(repo), "feat-live").workspace
    tmux.ensure_session(ws.tmux_session, ws.path, {})
    pane = tmux.new_window(ws.tmux_session, "agent", ws.path, ["sleep", "60"], {})
    add_agent(db, ws, "live", status="processing", result=None, window=pane)
    tmux.ensure_session("copse_proj_feat-orphan", str(repo), {})
    tmux.ensure_session("not-copse", str(repo), {})
    tmux.ensure_session("copse_proj_devserver", str(repo), {})
    tmux.new_window("copse_proj_devserver", "server", str(repo), ["sleep", "60"], {})
    time.sleep(0.3)  # let the shells start

    lines = cull.orphan_sessions(db)
    assert any("copse_proj_feat-orphan" in line for line in lines)
    assert not tmux.has_session("copse_proj_feat-orphan")
    assert tmux.has_session(ws.tmux_session)
    assert tmux.has_session("not-copse")
    assert tmux.has_session("copse_proj_devserver")  # something the person runs


def test_empty_worktree_folders_are_removed(db, repo):
    ws = workspaces.create(db, str(repo), "feat/real").workspace
    (worktrees_dir() / "proj" / "fix" / "gone").mkdir(parents=True)
    assert cull.empty_worktree_dirs() == 2
    assert not (worktrees_dir() / "proj" / "fix").exists()
    assert os.path.isdir(ws.path)
    assert worktrees_dir().is_dir()


def test_procs_alive():
    assert procs.alive(os.getpid())
    assert not procs.alive(dead_pid())


def _fake_gh(tmp_path, monkeypatch, answer):
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    log = tmp_path / "gh.log"
    gh.write_text(f'#!/bin/sh\necho "$@" >> {log}\necho {answer}\n')
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{gh.parent}{os.pathsep}{os.environ['PATH']}")
    return log


def test_first_pr_offers_to_delete_merged_branches_on_github(repo, tmp_path, monkeypatch):
    from copse import cli

    log = _fake_gh(tmp_path, monkeypatch, "false")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.typer, "confirm", lambda *a, **k: True)
    cli._offer_delete_on_merge(str(repo))
    assert "repo edit --delete-branch-on-merge" in log.read_text()
    log.write_text("")
    cli._offer_delete_on_merge(str(repo))                   # asked once per repo
    assert log.read_text() == ""


def test_no_offer_when_github_already_deletes_them(repo, tmp_path, monkeypatch):
    from copse import cli

    log = _fake_gh(tmp_path, monkeypatch, "true")
    monkeypatch.setattr(cli.typer, "confirm", lambda *a, **k: pytest.fail("nothing to ask"))
    cli._offer_delete_on_merge(str(repo))
    assert "repo edit" not in log.read_text()
