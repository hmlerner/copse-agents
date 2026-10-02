"""The seven overhead fixes: tools loaded up front, auto mode, shared
checks, reviews carried over a sync, supervisor sizing, inbox delivery,
and `copse doctor`."""
import asyncio
import json
import socket
import threading
import time

import pytest

from conftest import sh
from copse import agents, autopilot, doctor, gates, inbox, mcp_server, workspaces
from copse.db import Agent
from copse.profiles import load_profile


def add(db, ws, agent_id, mode="interactive", profile="supervisor", **kw):
    fields = dict(provider="claude", parent_id=None, status="idle", tmux_window="@0",
                  result=None, created_at=time.time())
    fields.update(kw)
    db.add_agent(Agent(agent_id, ws.id, profile, fields.pop("provider"), fields.pop("parent_id"),
                       mode, fields.pop("status"), fields.pop("tmux_window"), fields.pop("result"),
                       fields.pop("created_at"), **fields))


# -- 1. tools loaded up front --------------------------------------------------

def test_workers_get_copse_tools_up_front_and_chats_keep_tool_search(db, repo):
    ws = workspaces.adopt_root(db, str(repo))
    add(db, ws, "w1", mode="assign", profile="developer")
    add(db, ws, "boss")
    assert agents.agent_env(ws, "w1", db.get_agent("w1"))["ENABLE_TOOL_SEARCH"] == "false"
    assert "ENABLE_TOOL_SEARCH" not in agents.agent_env(ws, "boss", db.get_agent("boss"))


def test_profile_can_force_tool_search(db, repo):
    (repo / ".copse" / "agents").mkdir(parents=True)
    (repo / ".copse" / "agents" / "developer.md").write_text(
        "---\nname: developer\nprovider: claude\ntool_search: true\n---\nYou develop.\n")
    ws = workspaces.adopt_root(db, str(repo))
    add(db, ws, "w1", mode="assign", profile="developer")
    assert "ENABLE_TOOL_SEARCH" not in agents.agent_env(ws, "w1", db.get_agent("w1"))


# -- 2. auto mode and permissions guidance --------------------------------------

def test_workers_run_in_auto_mode_and_are_told_to_stay_home(db, repo):
    assert load_profile("developer").permission_mode == "auto"
    ws = workspaces.create(db, str(repo), "feat").workspace
    text = agents.worker_guidance(ws)
    assert "Never `cd` into or read from the main checkout" in text and str(repo) in text
    assert "don't write to /tmp" in text


# -- 3. checks run once and are shared -------------------------------------------

@pytest.fixture
def branch(db, repo, tmp_path):
    (repo / ".copse").mkdir()
    log = tmp_path / "check.log"   # outside the worktree, so the check doesn't dirty it
    (repo / ".copse" / "config.json").write_text(json.dumps({"checks": [f"echo checked >> {log}"]}))
    ws = workspaces.create(db, str(repo), "feat").workspace
    (workspaces.Path(ws.path) / "new.py").write_text("x = 1\n")
    sh("git add new.py && git commit -qm work", workspaces.Path(ws.path))
    return ws


def test_check_result_is_cached_by_commit_and_reused(db, branch, repo, tmp_path):
    from copse.config import load_repo_config

    cfg = load_repo_config(str(repo))
    gates.check_summary(db, branch, cfg)          # warmed when the worker reported
    gates.check_summary(db, branch, cfg)          # the reviewer's summary
    assert gates.run(db, branch, cfg, review_required=False).ok   # the merge gate
    assert (tmp_path / "check.log").read_text().count("checked") == 1


def test_report_warms_the_checks(db, branch, monkeypatch):
    started = []
    monkeypatch.setattr(agents, "_detach", started.append)
    add(db, branch, "w1", mode="assign", profile="developer", status="processing")
    agents.report_result(db, "w1", "done")
    assert any("_warm-checks" in argv for argv in started)


def test_milestone_check_runs_in_the_background_and_holds_the_nudge(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    add(db, ws, "boss", status="processing")
    db.add_autopilot("boss")
    autopilot.set_goal(db, "boss", "Goal", [("M1", "false", None)])
    monkeypatch.setattr(autopilot, "_detach", lambda argv: None)
    autopilot.check_in_background(db, "boss", ws, None)
    assert "already running" in autopilot.check_in_background(db, "boss", ws, None)
    monkeypatch.setattr(autopilot, "split_workers", lambda db, rid, screen=False: ([], []))
    assert autopilot.on_stop(db, db.get_agent("boss"), {}) is None   # result is on its way
    db.update_autopilot("boss", checking_since=None)
    assert autopilot.on_stop(db, db.get_agent("boss"), {}) is not None


# -- 4. a review carries over a clean sync --------------------------------------

def test_approval_carries_over_a_clean_sync(db, repo, monkeypatch):
    (repo / ".copse").mkdir()
    (repo / ".copse" / "config.json").write_text(json.dumps({"review": True}))
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss")
    ws = workspaces.create(db, str(repo), "feat").workspace
    (workspaces.Path(ws.path) / "new.py").write_text("x = 1\n")
    sh("git add new.py && git commit -qm work", workspaces.Path(ws.path))
    db.add_review(ws.id, gates.head(ws), "rev", True, "lgtm")
    # main moves on meanwhile
    (repo / "other.py").write_text("y = 2\n")
    sh("git add other.py && git commit -qm main-moved", repo)
    monkeypatch.setenv("COPSE_AGENT_ID", "boss")
    out = asyncio.run(mcp_server.merge_workspace(ws.id))
    assert out.startswith("Merged feat into main"), out
    carried = db.latest_review(ws.id, sh("git rev-parse HEAD", workspaces.Path(ws.path)))
    assert carried and carried.approved and "Carried over" in carried.summary


def test_unreviewed_sync_still_needs_a_review(db, repo, monkeypatch):
    (repo / ".copse").mkdir()
    (repo / ".copse" / "config.json").write_text(json.dumps({"review": True}))
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss")
    ws = workspaces.create(db, str(repo), "feat").workspace
    (workspaces.Path(ws.path) / "new.py").write_text("x = 1\n")
    sh("git add new.py && git commit -qm work", workspaces.Path(ws.path))
    (repo / "other.py").write_text("y = 2\n")
    sh("git add other.py && git commit -qm main-moved", repo)
    monkeypatch.setenv("COPSE_AGENT_ID", "boss")
    out = asyncio.run(mcp_server.merge_workspace(ws.id))
    assert "request_review again" in out


# -- 5. the supervisor sizes work ------------------------------------------------

def test_supervisor_is_told_to_size_work_first():
    from copse.autopilot import DELEGATION

    text = load_profile("supervisor").prompt
    assert "Size first" in text and "delegation rule" in text   # the rule itself comes from config
    assert "Keep your own context small" in text
    assert "genuinely parallel" in DELEGATION["conservative"]


# -- 6. inbox delivery ------------------------------------------------------------

@pytest.fixture
def fake_inbox():
    """A stand-in for Claude Code's inbox socket that records what arrives.
    Unix socket paths are short by necessity, so not under tmp_path."""
    import shutil
    import tempfile

    folder = tempfile.mkdtemp(prefix="copse-inbox-", dir="/tmp")
    path = f"{folder}/inbox.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen()
    received = []

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            data = b""
            while chunk := conn.recv(65536):
                data += chunk
            received.append([json.loads(l) for l in data.decode().splitlines() if l])
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    yield path, received
    server.close()
    shutil.rmtree(folder, ignore_errors=True)


def test_messages_go_through_the_inbox_without_waiting_for_idle(db, repo, fake_inbox, monkeypatch):
    path, received = fake_inbox
    ws = workspaces.adopt_root(db, str(repo))
    add(db, ws, "w1", mode="assign", profile="developer", status="processing",
        inbox_socket=path, inbox_token="tok")
    add(db, ws, "boss")
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    assert agents.send_message(db, "w1", "please add a test", sender_id="boss") == "delivered"
    time.sleep(0.3)
    assert received[0][0] == {"type": "auth", "token": "tok"}
    frame = received[0][1]
    assert frame["type"] == "user" and "please add a test" in frame["message"]["content"]
    assert frame["from"] == "copse supervisor boss"
    assert db.pending_count("w1") == 0          # nothing left for the pane


def test_without_an_inbox_messages_queue_for_the_pane(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    add(db, ws, "w1", mode="assign", profile="developer", status="processing")
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "reconcile", lambda db, a, **kw: a)
    assert agents.send_message(db, "w1", "hello", sender_id=None) == "queued"
    assert db.pending_count("w1") == 1


def test_session_start_records_the_inbox(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    add(db, ws, "w1", mode="assign", profile="developer", status="starting")
    monkeypatch.setenv(inbox.SOCKET_VAR, "/tmp/x.sock")
    monkeypatch.setenv(inbox.TOKEN_VAR, "t")
    agents.handle_hook(db, "w1", "session-start", {})
    a = db.get_agent("w1")
    assert (a.inbox_socket, a.inbox_token) == ("/tmp/x.sock", "t")


# -- 7. copse doctor ------------------------------------------------------------------

def test_doctor_reports_what_is_missing(repo, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None if name == "tmux" else f"/bin/{name}")
    results = doctor.checks(str(repo))
    by_name = {c.name: c for c in results}
    assert by_name["tmux"].level == doctor.FAIL and "brew install tmux" in by_name["tmux"].detail
    assert by_name["checks"].level == doctor.WARN
    text = doctor.render(results)
    assert "will stop copse from working" in text


def test_doctor_reports_a_missing_add_dir(repo):
    (repo / ".copse").mkdir(exist_ok=True)
    (repo / ".copse" / "config.json").write_text('{"add_dirs": ["/no/such/cache", "."]}')
    by_name = {c.name: c for c in doctor.checks(str(repo))}
    assert by_name["add_dirs"].level == doctor.WARN
    assert "/no/such/cache" in by_name["add_dirs"].detail
    assert str(repo) not in by_name["add_dirs"].detail
