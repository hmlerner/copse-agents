import json
import time

import pytest

from copse import agents, tmux, workspaces
from copse.db import Agent
from copse.profiles import load_profile
from copse.providers import Codex, LaunchContext


@pytest.fixture
def ws(db, repo):
    return workspaces.create(db, str(repo), "feature").workspace


def codex_agent(db, ws, status="processing"):
    a = Agent("c1", ws.id, "developer", "codex", None, "interactive", status, "@0", None, time.time())
    db.add_agent(a)
    return a


def test_command_passes_notify_hook_with_agent_id(monkeypatch):
    monkeypatch.setenv("COPSE_CODEX_BIN", "/opt/codex")
    argv = Codex().command(LaunchContext("abc", load_profile("developer"), "do it"))
    setting = next(a for a in argv if a.startswith("notify="))
    notify = json.loads(setting[len("notify="):])
    assert notify[-4:] == ["_hook", "codex-notify", "--agent", "abc"]
    assert argv[argv.index(setting) - 1] == "-c"
    assert argv[-1].endswith("do it")


def test_codex_trusts_hook_status():
    assert Codex.uses_hooks is True
    assert Codex.announces_start is False


def test_notify_turn_complete_sets_idle_and_delivers_queued_message(db, ws, monkeypatch):
    codex_agent(db, ws)
    db.enqueue("c1", "next step please", None)
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: "")
    pasted = []
    monkeypatch.setattr(tmux, "paste", lambda target, body, **k: pasted.append(body))
    assert agents.handle_hook(db, "c1", "codex-notify", {"type": "agent-turn-complete"}) is None
    assert pasted == ["next step please"]
    assert db.pending_count("c1") == 0


def test_notify_turn_complete_without_queue_is_idle(db, ws):
    codex_agent(db, ws)
    agents.handle_hook(db, "c1", "codex-notify", {"type": "agent-turn-complete"})
    assert db.get_agent("c1").status == "idle"


def test_notify_other_event_leaves_status(db, ws):
    codex_agent(db, ws)
    agents.handle_hook(db, "c1", "codex-notify", {"type": "something-else"})
    assert db.get_agent("c1").status == "processing"


def test_hook_main_parses_notify_payload(db, ws):
    codex_agent(db, ws)
    agents.hook_main(db, "c1", "codex-notify", json.dumps({"type": "agent-turn-complete"}))
    assert db.get_agent("c1").status == "idle"


def test_autopilot_codex_supervisor_is_told_to_keep_going(db, ws, monkeypatch):
    """No Stop hook on Codex: a finished turn is where autopilot nudges it."""
    from copse import autopilot

    codex_agent(db, ws)
    db.add_autopilot("c1")
    autopilot.set_goal(db, "c1", "Ship it", [("Done", "false", None)])
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: "")
    pasted = []
    monkeypatch.setattr(tmux, "paste", lambda target, body, **k: pasted.append(body))
    agents.handle_hook(db, "c1", "codex-notify", {"type": "agent-turn-complete"})
    assert len(pasted) == 1 and "Keep going" in pasted[0]
    # Capped like any nudge: no endless loop on a supervisor making no progress.
    for _ in range(autopilot.MAX_NUDGES + 2):
        agents.handle_hook(db, "c1", "codex-notify", {"type": "agent-turn-complete"})
    assert len(pasted) == autopilot.MAX_NUDGES


def test_supervisor_refuses_a_provider_that_cant_supervise(repo, monkeypatch):
    from typer.testing import CliRunner

    from copse.cli import app

    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["--provider", "native"])
    assert res.exit_code != 0 and "can't run the supervisor" in res.output
