"""Issue #35: `copse attach` opens the agent that needs you, and the errors
around it say what happened."""

import time

from typer.testing import CliRunner

from copse import agents, mcp_server, workspaces
from copse.cli import app
from copse.db import Agent


def add(db, ws, aid, status, window, created):
    db.add_agent(Agent(aid, ws.id, "developer", "claude", None, "assign", status, window, None, created))


def test_attach_target_prefers_the_waiting_agent(db, repo, monkeypatch):
    ws = workspaces.create(db, str(repo), "feat/x").workspace
    now = time.time()
    add(db, ws, "old", "processing", "%1", now - 30)
    add(db, ws, "stuck", "waiting", "%2", now - 20)
    add(db, ws, "new", "idle", "%3", now - 10)
    add(db, ws, "gone", "waiting", "%4", now)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id != "gone")
    assert agents.attach_target(db, ws) == "%2"
    db.update_agent("stuck", status="idle")
    assert agents.attach_target(db, ws) == "%1"        # then the busiest
    db.update_agent("old", status="idle")
    assert agents.attach_target(db, ws) == "%3"        # then the newest


def test_attach_target_none_without_live_agents(db, repo, monkeypatch):
    ws = workspaces.create(db, str(repo), "feat/x").workspace
    add(db, ws, "a", "idle", "%1", time.time())
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: False)
    assert agents.attach_target(db, ws) is None


def test_attach_without_a_terminal_explains(db, repo, monkeypatch):
    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["attach"])
    assert res.exit_code == 1
    assert "needs a terminal" in res.output


def test_send_message_to_a_paused_agent_says_why(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("p1", ws.id, "supervisor", "claude", None, "interactive", "paused", "%9", None, time.time()))
    monkeypatch.delenv("COPSE_AGENT_ID", raising=False)
    out = mcp_server.send_message("p1", "hello")
    assert out.startswith("Not sent: agent p1 is paused") and "copse continue" in out


def test_workers_are_told_not_to_enter_another_worktree(db, repo):
    ws = workspaces.create(db, str(repo), "feat/x").workspace
    assert "EnterWorktree" in agents.worker_guidance(ws)
