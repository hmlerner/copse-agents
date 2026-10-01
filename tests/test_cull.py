import json
import os
import subprocess
import sys
import time

import pytest

from copse import agents, cull, procs, workspaces
from copse.db import Agent

SLEEPER = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)"


def launch_as(agent_id, *, argv_config=True, env_only=False):
    """A stand-in for an agent's CLI: named in its command line the way copse
    launches Claude Code (unless env_only), with a child process of its own."""
    args = [sys.executable, "-c", SLEEPER]
    if argv_config:
        env_block = {"COPSE_AGENT_ID": agent_id}
        if os.environ.get("COPSE_HOME"):
            env_block["COPSE_HOME"] = os.environ["COPSE_HOME"]
        args.append(json.dumps({"mcpServers": {"copse": {"env": env_block}}}))
    env = {**os.environ}
    if env_only:
        env["COPSE_AGENT_ID"] = agent_id
    proc = subprocess.Popen(args, env=env, start_new_session=True)
    time.sleep(0.5)
    return proc


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); check it's really running.
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    return bool(out.strip()) and not out.strip().startswith("Z")


@pytest.fixture
def proc_cleanup():
    started = []
    yield started
    import signal

    for p in started:
        try:
            os.killpg(p.pid, signal.SIGKILL)  # the stand-in and the child it started
        except (ProcessLookupError, PermissionError):
            # Already gone, or (macOS) a group whose exited leader isn't reaped yet.
            p.kill()
        p.wait(timeout=5)


def test_stop_finds_the_agent_and_what_it_started(proc_cleanup):
    p = launch_as("abc12345")
    proc_cleanup.append(p)
    found = procs.agent_pids(["abc12345"])["abc12345"]
    assert p.pid in found and len(found) >= 2  # the CLI and its child
    assert procs.stop(["abc12345"], grace=2) >= 2
    p.wait(timeout=5)
    assert not any(alive(pid) for pid in found)


def test_an_inherited_environment_alone_is_not_enough(proc_cleanup):
    # Claude Code's daemon inherits the environment of the agent that started
    # it; that must never make the daemon (or its other sessions) that agent's.
    decoy = launch_as("abc12345", argv_config=False, env_only=True)
    proc_cleanup.append(decoy)
    assert decoy.pid not in procs.agent_pids(["abc12345"]).get("abc12345", set())


def test_the_daemon_is_never_included():
    config = json.dumps({"COPSE_AGENT_ID": "abc12345", "COPSE_HOME": os.environ["COPSE_HOME"]})
    t = {1: procs.Proc(1, 0, "init"),
         10: procs.Proc(10, 1, f"/usr/bin/claude daemon run --spawned-by {config}"),
         11: procs.Proc(11, 10, f"claude --mcp-config {config}")}
    assert procs.agent_pids(["abc12345"], procs=t)["abc12345"] == {11}


@pytest.fixture
def ws(db, repo):
    return workspaces.adopt_root(db, str(repo))


def add(db, ws, agent_id, **kw):
    fields = dict(profile="developer", provider="claude", parent_id=None, mode="assign",
                  status="idle", tmux_window="@999", result=None, created_at=time.time() - 7200)
    fields.update(kw)
    db.add_agent(Agent(agent_id, ws.id, **fields))


def test_sweep_stops_processes_of_paused_agents(db, ws, proc_cleanup):
    add(db, ws, "abc12345", status="paused")
    p = launch_as("abc12345")
    proc_cleanup.append(p)
    notes = cull.sweep(db)
    p.wait(timeout=5)
    assert any("leftover" in n for n in notes)


def test_sweep_pauses_agents_whose_window_is_gone(db, ws, proc_cleanup):
    add(db, ws, "abc12345", mode="interactive", status="processing", profile="supervisor")
    p = launch_as("abc12345")
    proc_cleanup.append(p)
    cull.sweep(db)
    p.wait(timeout=5)
    a = db.get_agent("abc12345")
    assert a.status == "paused" and a.dismissed_at is None  # still resumable, still shown


def test_sweep_closes_idle_reported_workers(db, ws, monkeypatch):
    add(db, ws, "w1", result="done: added the flag", tmux_window="@991")
    add(db, ws, "w2", tmux_window="@992")              # still working on it
    add(db, ws, "w3", result="done", tmux_window="@993", created_at=time.time())  # reported just now
    with db.tx() as c:
        c.execute("UPDATE agents SET status_since = created_at")
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    notes = cull.sweep(db)
    assert db.get_agent("w1").dismissed_at is not None and db.get_agent("w1").status == "done"
    assert db.get_agent("w2").dismissed_at is None and db.get_agent("w3").dismissed_at is None
    assert notes == ["closed idle worker w1 after 120 min"]


def test_stale_after_zero_keeps_workers(db, ws, repo, monkeypatch):
    (repo / ".copse").mkdir()
    (repo / ".copse" / "config.json").write_text('{"stale_after": 0}')
    add(db, ws, "w1", result="done")
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    cull.sweep(db)
    assert db.get_agent("w1").dismissed_at is None


def test_stopped_workers_of_a_paused_session_stay_resumable(db, ws):
    add(db, ws, "boss", mode="interactive", status="paused", profile="supervisor")
    add(db, ws, "w1", parent_id="boss", status="paused")
    add(db, ws, "w2", parent_id=None, status="processing")   # its session went on without it
    cull.sweep(db)
    assert db.get_agent("w1").dismissed_at is None
    assert db.get_agent("w2").dismissed_at is not None


def test_another_copse_homes_agents_are_left_alone(proc_cleanup, monkeypatch):
    # The person's own copse (no COPSE_HOME) while a test run uses its own.
    other = json.dumps({"mcpServers": {"copse": {"env": {"COPSE_AGENT_ID": "fedcba98"}}}})
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", other],
                         start_new_session=True)
    proc_cleanup.append(p)
    time.sleep(0.5)
    assert "fedcba98" not in procs.all_agent_ids()
    assert procs.agent_pids(["fedcba98"]) == {}

