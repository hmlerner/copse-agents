"""Supervisor chats whose terminal is gone must not stay in the sidebar
forever. They used to: a supervisor only gets paused by the wrapper around
its own CLI (agents._pause_when_done -> agents.ended), which never runs when
tmux itself goes away (a reboot, `tmux kill-server`), so the row stayed
"idle"; paused sessions were never hidden; and after a tmux restart the new
server hands out the same pane ids (%1 again), so an old supervisor's pane
could even look alive."""

import time

import pytest

from copse import agents, sessions, tmux, view, workspaces
from copse.db import Agent

LONG_AGO = 3600.0


@pytest.fixture
def root(db, repo):
    ws = workspaces.adopt_root(db, str(repo))
    yield ws
    tmux.kill_session(ws.tmux_session)


def add(db, ws, agent_id, *, mode="interactive", status="idle", window="", parent=None,
        age=0.0, profile="supervisor"):
    t = time.time() - age
    a = Agent(agent_id, ws.id, profile, "claude", parent, mode, status, window, None, t,
              status_since=t)
    db.add_agent(a)
    return a


def running_window(ws):
    tmux.ensure_session(ws.tmux_session, ws.path, {})
    return tmux.new_window(ws.tmux_session, "agent", ws.path, ["sleep", "300"], {})


def shown(db, repo):
    return {a["id"]: a for ws in view.snapshot(db, str(repo)) for a in ws["agents"]}


def test_a_supervisor_that_died_without_pausing_is_hidden_and_paused(db, repo, root):
    add(db, root, "old", window="%99999", age=LONG_AGO)
    assert "old" not in shown(db, repo)
    # Recorded as agents.pause would have, so it can still be continued.
    assert db.get_agent("old").status == "paused"
    assert [s.root.id for s in sessions.paused(db, str(repo))] == ["old"]
    assert db.get_agent("old").dismissed_at is None


def test_a_paused_supervisor_with_nothing_running_is_hidden_but_resumable(db, repo, root):
    add(db, root, "p1", status="paused", window="%99999", age=LONG_AGO)
    assert "p1" not in shown(db, repo)
    assert agents.latest_paused(db, root).id == "p1"


def test_a_supervisor_that_just_stopped_lingers_briefly(db, repo, root):
    add(db, root, "fresh", window="%99999", age=1)
    assert shown(db, repo)["fresh"]["status"] == "exited"
    assert db.get_agent("fresh").status == "idle"  # not touched yet


def test_a_new_launch_hides_the_session_it_just_paused(db, repo, root):
    # `copse` pauses the chat running here and starts a new one: the old one
    # mustn't linger beside it, paused or stopped without being paused.
    add(db, root, "older", window="%99997", age=5)
    add(db, root, "old", status="paused", window="%99999", age=2)
    pane = running_window(root)
    add(db, root, "new", window=pane)
    got = shown(db, repo)
    assert "new" in got and "old" not in got and "older" not in got
    assert {s.root.id for s in sessions.paused(db, str(repo))} == {"old", "older"}


def test_a_hidden_session_takes_its_stopped_workers_with_it(db, repo, root):
    add(db, root, "boss", status="paused", window="%99999", age=LONG_AGO)
    add(db, root, "w1", mode="assign", status="paused", parent="boss", window="%99998",
        profile="developer", age=LONG_AGO)
    add(db, root, "sub", mode="assign", status="paused", parent="w1", window="%99996",
        profile="developer", age=LONG_AGO)
    got = shown(db, repo)
    assert not {"boss", "w1", "sub"} & set(got)
    assert [m.id for m in sessions.paused(db, str(repo))[0].members] == ["boss", "w1", "sub"]


def test_a_live_supervisor_and_its_workers_show(db, repo, root):
    pane = running_window(root)
    add(db, root, "boss", window=pane, age=LONG_AGO)
    add(db, root, "w1", mode="assign", parent="boss", window="%99998", profile="developer",
        age=LONG_AGO)
    assert {"boss", "w1"} <= set(shown(db, repo))


def test_a_dead_supervisor_with_a_live_worker_still_shows(db, repo, root):
    pane = running_window(root)
    add(db, root, "boss", window="%99999", age=LONG_AGO)
    add(db, root, "w1", mode="assign", parent="boss", window=pane, profile="developer")
    got = shown(db, repo)
    assert got["boss"]["status"] == "exited" and "w1" in got
    assert db.get_agent("boss").status == "idle"


def test_dead_workers_of_a_dead_supervisor_are_paused_with_it(db, repo, root):
    add(db, root, "boss", window="%99999", age=LONG_AGO)
    add(db, root, "w1", mode="assign", parent="boss", window="%99998", profile="developer",
        age=LONG_AGO)
    view.snapshot(db, str(repo))
    assert db.get_agent("boss").status == "paused"
    assert db.get_agent("w1").status == "paused"
    assert [m.id for m in sessions.paused(db, str(repo))[0].members] == ["boss", "w1"]


def test_a_reused_pane_id_only_counts_for_the_newest_agent(db, repo, root):
    pane = running_window(root)
    add(db, root, "stale", window=pane, age=LONG_AGO)
    add(db, root, "current", window=pane)
    alive = view.live_agents(db, tmux.list_panes())
    assert "current" in alive and "stale" not in alive
    got = shown(db, repo)
    assert "current" in got and "stale" not in got


# -- hooks must reach the agent that launched them ------------------------------
#
# Claude Code can run a session in a process its background daemon started for
# an earlier launch, so the environment its hooks inherit (COPSE_AGENT_ID) can
# name an older supervisor: that one was then marked busy and nudged with its
# old autopilot goal, while the real session looked idle.


def _claude_settings(agent_id):
    import json

    from copse.profiles import load_profile
    from copse.providers import ClaudeCode, LaunchContext

    argv = ClaudeCode().command(LaunchContext(agent_id, load_profile("supervisor"), None,
                                              mode="interactive"))
    return json.loads(argv[argv.index("--settings") + 1])


def test_claude_hook_commands_name_their_agent(monkeypatch):
    monkeypatch.setenv("COPSE_TMUX_SOCKET", "sock")
    settings = _claude_settings("new12345")
    for event, entries in settings["hooks"].items():
        cmd = entries[0]["hooks"][0]["command"]
        assert "--agent new12345" in cmd, event
        assert "COPSE_TMUX_SOCKET=sock" in cmd


def test_the_hook_command_wins_over_a_stale_environment(db, repo, root, monkeypatch):
    from typer.testing import CliRunner

    from copse.cli import app

    add(db, root, "old", status="idle", age=LONG_AGO)
    add(db, root, "new", status="idle")
    monkeypatch.setenv("COPSE_AGENT_ID", "old")  # what the daemon's process carries
    res = CliRunner().invoke(app, ["_hook", "prompt-submit", "--agent", "new"],
                             input='{"session_id": "s-new", "prompt": "hi"}')
    assert res.exit_code == 0, res.output
    assert db.get_agent("new").status == "processing"
    assert db.get_agent("new").session_ref == "s-new"
    assert db.get_agent("old").status == "idle" and db.get_agent("old").session_ref is None


def test_an_env_only_hook_prefers_the_agent_that_owns_the_session(db, repo, root, monkeypatch):
    from typer.testing import CliRunner

    from copse.cli import app

    add(db, root, "old", status="idle", age=LONG_AGO)
    add(db, root, "new", status="idle")
    db.update_agent("new", session_ref="s-new")
    monkeypatch.setenv("COPSE_AGENT_ID", "old")  # a session launched by an older copse
    res = CliRunner().invoke(app, ["_hook", "prompt-submit"],
                             input='{"session_id": "s-new", "prompt": "hi"}')
    assert res.exit_code == 0, res.output
    assert db.get_agent("new").status == "processing"
    assert db.get_agent("old").status == "idle"


# -- a pane says whose it is ------------------------------------------------------
#
# The DB alone can't tell an agent's own pane from a newer pane with the same
# id (a restarted server counts from %0 again) until the newer agent's row
# records it, and the sidebar's pane is never recorded by any agent. Panes
# carry a tag from the moment they exist (agents.AGENT_TAG, SIDEBAR_TAG).


def test_a_tagged_pane_belongs_only_to_the_agent_it_names(db, repo, root):
    pane = running_window(root)
    add(db, root, "stale", window=pane, age=LONG_AGO)
    # The new chat's pane exists and is tagged, but its row doesn't name it yet.
    tmux.set_pane_tag(pane, agents.AGENT_TAG, "launching")
    assert not agents.owns_pane(db, db.get_agent("stale"))
    assert "stale" not in view.live_agents(db, tmux.list_panes())
    tmux.set_pane_tag(pane, agents.AGENT_TAG, "stale")
    assert agents.owns_pane(db, db.get_agent("stale"))


def test_the_sidebar_pane_is_no_agents(db, repo, root):
    pane = running_window(root)
    add(db, root, "stale", window=pane, age=LONG_AGO)
    tmux.set_pane_tag(pane, agents.SIDEBAR_TAG, "someroot")
    assert not agents.owns_pane(db, db.get_agent("stale"))


def test_an_untagged_pane_still_goes_by_the_db(db, repo, root):
    """Panes from before tagging (an older copse's sessions) keep working."""
    pane = running_window(root)
    add(db, root, "stale", window=pane, age=LONG_AGO)
    assert agents.owns_pane(db, db.get_agent("stale"))
    add(db, root, "current", window=pane)
    assert not agents.owns_pane(db, db.get_agent("stale"))


def test_spawn_tags_the_pane_with_its_agent(db, repo):
    ws = workspaces.create(db, str(repo), "feat-tag").workspace
    a = agents.spawn(db, ws, "developer", prompt="hi", provider_name="shell", mode="handoff")
    import time as _t; _t.sleep(1.5)
    listing = tmux._tmux("list-panes", "-a", "-F", "#{session_name} #{pane_id} dead=#{pane_dead} cmd=#{pane_current_command} deadstatus=#{pane_dead_status} deadsig=#{pane_dead_signal} tag=#{@copse_agent}", check=False)
    assert tmux.get_pane_tag(a.tmux_window, agents.AGENT_TAG) == a.id, listing.stdout + listing.stderr
    assert agents.owns_pane(db, a)
