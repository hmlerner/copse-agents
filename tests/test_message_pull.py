"""message_delivery "pull": agents' messages to an interactive supervisor wait
unread behind one notice until read_messages (no real CLIs)."""

import json
import time

import pytest

from copse import agents, workspaces
from copse.db import Agent

REAL_PULLS = agents.pulls_messages  # conftest patches it off for the other tests


@pytest.fixture
def boss(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    db.add_agent(Agent("w1", ws.id, "developer", "claude", "boss", "assign", "processing",
                       "@1", None, time.time()))
    monkeypatch.setenv("COPSE_AGENT_ID", "boss")
    monkeypatch.setattr(agents, "is_alive", lambda a, *_: True)
    monkeypatch.setattr(agents, "pulls_messages", REAL_PULLS)
    return ws


@pytest.fixture
def pushed(monkeypatch):
    """Texts that reached the supervisor's inbox socket."""
    out = []

    def deliver(db, agent, message_id, text, sender_id):
        out.append(text)
        db.mark_delivered(message_id)
        return True

    monkeypatch.setattr(agents, "_deliver_to_inbox", deliver)
    return out


def set_delivery(repo, value):
    (repo / ".copse").mkdir(exist_ok=True)
    (repo / ".copse" / "config.json").write_text(json.dumps({"message_delivery": value}))


def test_notice_instead_of_body(db, boss, pushed):
    agents.send_message(db, "boss", "the secret result", sender_id="w1")
    assert len(pushed) == 1
    assert pushed[0] == "copse: 1 new message (from w1). Call read_messages."
    assert "secret" not in pushed[0]
    assert db.unread_count("boss") == 1


def test_single_notice_for_several_messages(db, boss, pushed):
    agents.send_message(db, "boss", "one", sender_id="w1")
    agents.send_message(db, "boss", "two", sender_id="w1")
    agents.send_message(db, "boss", "three")
    assert len(pushed) == 1
    assert db.unread_count("boss") == 3
    # Once read, the next message gets a fresh notice.
    db.read_held("boss")
    agents.send_message(db, "boss", "four", sender_id="w1")
    assert len(pushed) == 2


def test_read_messages_returns_and_marks_read(db, boss, pushed):
    from copse import mcp_server

    agents.send_message(db, "boss", "the result", sender_id="w1")
    agents.send_message(db, "boss", "queued task started")
    text = mcp_server.read_messages()
    assert "[Message from developer agent w1. Reply with the copse send_message tool, to_agent_id=w1]" in text
    assert "the result" in text
    assert "[Message from copse]" in text and "queued task started" in text
    assert db.unread_count("boss") == 0
    assert mcp_server.read_messages() == "No unread messages."


def test_push_mode_unchanged(db, repo, boss, pushed):
    set_delivery(repo, "push")
    agents.send_message(db, "boss", "the result", sender_id="w1")
    assert len(pushed) == 1 and "the result" in pushed[0]
    assert db.unread_count("boss") == 0


def test_person_message_is_pushed(db, boss, pushed):
    agents.send_message(db, "boss", "hello from me", person=True)
    assert pushed == ["hello from me"]
    assert db.unread_count("boss") == 0


def test_messages_to_workers_are_pushed(db, boss, pushed):
    agents.send_message(db, "w1", "do this", sender_id="boss")
    assert len(pushed) == 1 and "do this" in pushed[0]
    assert db.unread_count("w1") == 0


def test_stop_hook_blocks_autopilot_with_unread(db, boss, pushed):
    db.add_autopilot("boss")
    agents.send_message(db, "boss", "the result", sender_id="w1")
    out = agents.handle_hook(db, "boss", "stop", {})
    assert out["decision"] == "block"
    assert "read_messages" in out["reason"]
    db.read_held("boss")
    out = agents.handle_hook(db, "boss", "stop", {})
    assert not out or "read_messages" not in out.get("reason", "")


def test_stop_hook_hands_over_lost_notice(db, boss, monkeypatch):
    db.enqueue_held("boss", "body", "w1")
    out = agents.handle_hook(db, "boss", "stop", {})
    assert out["decision"] == "block"
    assert out["reason"] == "copse: 1 new message (from w1). Call read_messages."
    # The notice is now outstanding: a second stop doesn't repeat it.
    assert not agents.handle_hook(db, "boss", "stop", {})
