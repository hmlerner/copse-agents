import os
import time

import pytest

from copse import agents, git, sessions, tmux, workspaces
from copse.db import Agent


def add(db, ws, aid, mode="interactive", parent=None, status="paused", since=None, result=None):
    a = Agent(aid, ws.id, "supervisor" if mode == "interactive" else "developer", "claude", parent,
              mode, status, "", result, time.time(), since or time.time(), "task", f"sess-{aid}")
    db.add_agent(a)
    return a


@pytest.fixture
def root(db, repo):
    return workspaces.adopt_root(db, str(repo))


def test_keeps_only_the_newest_three(db, root):
    for i in range(5):
        add(db, root, f"s{i}", since=1000 + i)
    assert sessions.enforce(db, root.repo_root, now=1100) == 2
    assert [s.root.id for s in sessions.paused(db, root.repo_root)] == ["s4", "s3", "s2"]


def test_drops_sessions_older_than_a_week(db, root):
    now = time.time()
    add(db, root, "fresh", since=now - 3600)
    add(db, root, "stale", since=now - 8 * 86400)
    sessions.enforce(db, root.repo_root, now=now)
    assert [s.root.id for s in sessions.paused(db, root.repo_root)] == ["fresh"]


def test_forget_removes_clean_worktrees_keeps_dirty_ones_and_all_branches(db, root, repo):
    clean = workspaces.create(db, str(repo), "clean-work").workspace
    dirty = workspaces.create(db, str(repo), "dirty-work").workspace
    open(os.path.join(clean.path, "done.txt"), "w").write("x")
    git.commit_all(clean.path, "finished")
    open(os.path.join(dirty.path, "wip.txt"), "w").write("x")  # uncommitted
    add(db, root, "boss", since=1)
    add(db, clean, "w1", mode="assign", parent="boss", result="ok")
    add(db, dirty, "w2", mode="assign", parent="boss")
    for i in range(3):
        add(db, root, f"new{i}", since=100 + i)  # push "boss" past KEEP

    sessions.enforce(db, root.repo_root, now=200)
    assert db.get_agent("boss") is None and db.get_agent("w1") is None
    assert not os.path.exists(clean.path) and os.path.exists(dirty.path)
    assert git.branch_exists(str(repo), "clean-work") and git.branch_exists(str(repo), "dirty-work")
    assert git.out(["log", "-1", "--format=%s", "main"], str(repo)) == "init"  # nothing merged


def test_pause_then_resume_uses_the_saved_claude_session(db, root, monkeypatch, tmp_path):
    saved = tmp_path / "claude" / "projects" / "p"
    saved.mkdir(parents=True)
    (saved / "sess-boss.jsonl").write_text("{}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    launched = []
    monkeypatch.setattr(agents, "_launch", lambda db, a, ws, **kw: launched.append((a.id, kw)))
    add(db, root, "boss")
    add(db, root, "w1", mode="assign", parent="boss")
    add(db, root, "w2", mode="assign", parent="boss", status="done", result="ok")
    agents.resume(db, "boss")
    assert [a for a, _ in launched] == ["boss", "w1"]  # finished workers stay done
    assert launched[0][1]["resume"] == "sess-boss" and launched[0][1]["prompt"] is None
    assert launched[0][1]["watch_pane"] and not launched[1][1]["watch_pane"]


def test_hook_records_the_cli_session_id(db, root):
    add(db, root, "a1", status="idle")
    agents.handle_hook(db, "a1", "prompt-submit", {"session_id": "abc-123"})
    assert db.get_agent("a1").session_ref == "abc-123"


def test_resume_starts_fresh_when_claude_never_saved_the_chat(db, root, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    launched = []
    monkeypatch.setattr(agents, "_launch", lambda db, a, ws, **kw: launched.append(kw))
    add(db, root, "boss")
    agents.resume(db, "boss")
    assert launched[0]["resume"] is None
    (tmp_path / "claude" / "projects" / "p").mkdir(parents=True)
    (tmp_path / "claude" / "projects" / "p" / "sess-boss.jsonl").write_text("{}")
    db.set_status("boss", "paused")
    agents.resume(db, "boss")
    assert launched[1]["resume"] == "sess-boss"


def test_resume_rebuilds_worker_decoration_when_the_session_cant_be_resumed(db, root, monkeypatch, tmp_path):
    """a.task is stored raw (see agents.decorate_worker_prompt); a fresh
    restart needs the finish line, WORKER_FOOTER and /goal wrapper rebuilt,
    same as at the first launch."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))  # no saved session
    w1 = Agent("w1", root.id, "developer", "claude", "boss", "assign", "paused", "", None,
              time.time(), time.time(), "Add input validation.", "sess-w1",
              done_when="tests/test_x.py passes")
    db.add_agent(w1)
    launched = []
    monkeypatch.setattr(agents, "_launch", lambda db, a, ws, **kw: launched.append(kw))
    agents.resume(db, "w1")
    prompt = launched[0]["prompt"]
    assert prompt.startswith("/goal Finish line: tests/test_x.py passes")
    assert "report_result" in prompt  # WORKER_FOOTER
    assert prompt.endswith(agents.RESUME_NOTE)


# -- dropping a session must never touch a newer session's windows ----------------
#
# Regression: the first `copse` after a tmux server restart (a reboot, or the
# previous session's end taking the server with it) got the chat's pane as %1
# and the sidebar as %2, the same ids every earlier session's chat had. The
# detached cull's retention then dropped the oldest paused session and closed
# its windows by those stored ids: the new chat and its sidebar vanished,
# leaving the person on the session's bare shell window.


def _live_pane(root, tag=None):
    tmux.ensure_session(root.tmux_session, root.path, {})
    return tmux.new_window(root.tmux_session, "chat", root.path, ["sleep", "300"], {}, tag=tag)


def test_dropping_a_session_spares_a_pane_that_now_belongs_to_a_launching_chat(db, root):
    pane = _live_pane(root, tag=(agents.AGENT_TAG, "launching"))  # its row isn't recorded yet
    for i in range(3):
        add(db, root, f"s{i}", since=2000 + i)
    add(db, root, "old", since=1000)
    db.update_agent("old", tmux_window=pane)  # the same id, on the server that's gone
    assert sessions.enforce(db, root.repo_root, now=2100) == 1
    assert db.get_agent("old") is None
    assert tmux.window_alive(pane)


def test_dropping_a_session_spares_the_sidebar(db, root):
    pane = _live_pane(root, tag=(agents.SIDEBAR_TAG, "someroot"))
    for i in range(3):
        add(db, root, f"s{i}", since=2000 + i)
    add(db, root, "old", since=1000)
    db.update_agent("old", tmux_window=pane)
    sessions.enforce(db, root.repo_root, now=2100)
    assert tmux.window_alive(pane)


def test_dropping_a_session_still_closes_its_own_leftover_window(db, root):
    pane = _live_pane(root, tag=(agents.AGENT_TAG, "old"))
    for i in range(3):
        add(db, root, f"s{i}", since=2000 + i)
    add(db, root, "old", since=1000)
    db.update_agent("old", tmux_window=pane)
    sessions.enforce(db, root.repo_root, now=2100)
    assert not tmux.window_alive(pane)


def test_sessions_cmd_lists_a_session_with_workers(db, root, repo, monkeypatch):
    # Issue #34: a paused session with workers crashed `copse sessions`
    # (workspaces went into a set, and Workspace isn't hashable).
    from typer.testing import CliRunner

    from copse.cli import app

    sup = add(db, root, "sup")
    ws = workspaces.create(db, str(repo), "feat/x").workspace
    add(db, ws, "w1", mode="assign", parent=sup.id)
    add(db, ws, "r1", mode="review", parent=sup.id)     # shares the worker's workspace
    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["sessions"])
    assert res.exit_code == 0, res.output
    assert "MB in worktrees" in res.output
