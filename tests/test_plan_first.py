"""plan_first workers: propose a plan with submit_plan, wait for the
supervisor's approve_plan, and can't edit files (Claude's PreToolUse hook)
until it's approved."""

import asyncio
import re
import time

import pytest

from copse import agents, autopilot, mcp_server, workspaces
from copse.config import RepoConfig
from copse.db import Agent, Autopilot
from copse.providers import get_provider


def fake_spawn(db, ws, profile, *, prompt=None, parent_id=None, mode="handoff", done_when=None,
               plan_first=False, **kw):
    """Stands in for agents.spawn: records a worker without launching a CLI."""
    a = Agent(agents.new_id(), ws.id, profile, "claude", parent_id, mode, "processing", "",
              None, time.time(), task=prompt, done_when=done_when)
    db.add_agent(a)
    if plan_first:
        db.update_agent(a.id, plan_first=1)
    return a


@pytest.fixture(autouse=True)
def no_real_spawn(monkeypatch):
    monkeypatch.setattr(agents, "spawn", fake_spawn)


@pytest.fixture
def boss(db, repo, monkeypatch):
    (repo / ".copse").mkdir(exist_ok=True)
    (repo / ".copse" / "config.json").write_text('{"pipeline": false}')
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                        "@0", None, time.time()))
    monkeypatch.setenv("COPSE_AGENT_ID", "boss")
    return ws


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(agents, "send_message",
                        lambda db, to, body, sender_id=None: out.append((to, body, sender_id)) or "ok")
    return out


def worker_id(reply: str) -> str:
    m = re.search(r"Started worker (\S+)", reply)
    assert m, reply
    return m.group(1)


def planner(db, ws, parent="boss", **kw):
    a = Agent("w1", ws.id, "developer", "claude", parent, "assign", "processing", "", None,
              time.time())
    db.add_agent(a)
    db.update_agent("w1", plan_first=1, **kw)  # add_agent doesn't write these columns
    return db.get_agent("w1")


# -- assign / config ----------------------------------------------------------


def test_assign_plan_first_is_stored(db, repo, boss):
    wid = worker_id(asyncio.run(mcp_server.assign("developer", "big job", plan_first=True)))
    assert db.get_agent(wid).plan_first == 1


def test_assign_defaults_to_the_repo_config(db, repo, boss):
    (repo / ".copse" / "config.json").write_text('{"pipeline": false, "plan_first": true}')
    on = worker_id(asyncio.run(mcp_server.assign("developer", "job one")))
    off = worker_id(asyncio.run(mcp_server.assign("developer", "job two", plan_first=False)))
    assert db.get_agent(on).plan_first == 1
    assert not db.get_agent(off).plan_first


def test_plan_first_config_defaults_off():
    assert RepoConfig().plan_first is False


def test_queued_task_keeps_plan_first(db, repo, boss):
    out = asyncio.run(mcp_server.assign("developer", "later", depends_on=["nothing-yet"],
                                         plan_first=True))
    assert "Queued" in out
    [t] = db.list_tasks(str(repo), state="pending")
    assert t.plan_first == 1


# -- the prompt ---------------------------------------------------------------


def test_prompt_mentions_submit_plan_only_when_plan_first(db, repo, boss):
    ws = boss
    p = get_provider("claude")
    plain = agents.decorate_worker_prompt("do it", "abc", ws, None, p, True)
    plan = agents.decorate_worker_prompt("do it", "abc", ws, None, p, True, plan_first=True)
    assert "submit_plan" not in plain
    assert "submit_plan" in plan and "before you edit" in plan


# -- submit_plan / approve_plan ------------------------------------------------


def test_submit_plan_messages_the_parent_and_records_proposed(db, repo, boss, sent):
    planner(db, boss)
    agents.submit_plan(db, "w1", "1. edit a.py")
    assert db.get_agent("w1").plan_state == "proposed"
    [(to, body, sender)] = sent
    assert to == "boss" and sender == "w1" and "1. edit a.py" in body


def test_submit_plan_needs_a_plan_first_worker(db, repo, boss, sent):
    db.add_agent(Agent("w2", boss.id, "developer", "claude", "boss", "assign", "processing", "",
                       None, time.time()))
    assert "isn't plan-first" in agents.submit_plan(db, "w2", "x")
    assert not sent


def test_approve_plan_approves_and_tells_the_worker(db, repo, boss, sent):
    planner(db, boss, plan_state="proposed")
    agents.approve_plan(db, "boss", "w1", "looks good, watch the tests")
    assert db.get_agent("w1").plan_state == "approved"
    [(to, body, _)] = sent
    assert to == "w1" and "approved" in body and "watch the tests" in body


def test_approve_plan_can_ask_for_a_revision(db, repo, boss, sent):
    planner(db, boss, plan_state="proposed")
    agents.approve_plan(db, "boss", "w1", "split it up", approved=False)
    assert db.get_agent("w1").plan_state == "revise"
    assert "revise" in sent[0][1] and "split it up" in sent[0][1]


def test_only_the_parent_may_approve(db, repo, boss, sent):
    planner(db, boss, plan_state="proposed")
    with pytest.raises(agents.AgentError, match="supervisor"):
        agents.approve_plan(db, "someone-else", "w1")
    with pytest.raises(agents.AgentError, match="supervisor"):
        agents.approve_plan(db, None, "w1")
    assert db.get_agent("w1").plan_state == "proposed"
    assert not sent


def test_approve_needs_a_proposed_plan(db, repo, boss, sent):
    planner(db, boss)
    with pytest.raises(agents.AgentError, match="no plan awaiting"):
        agents.approve_plan(db, "boss", "w1")


def test_mcp_tools_report_errors_as_text(db, repo, boss, monkeypatch, sent):
    planner(db, boss, plan_state="proposed")
    monkeypatch.setenv("COPSE_AGENT_ID", "w1")
    assert "supervisor" in mcp_server.approve_plan("w1")
    assert "Plan sent" in mcp_server.submit_plan("again")
    monkeypatch.setenv("COPSE_AGENT_ID", "boss")
    assert "Approved" in mcp_server.approve_plan("w1")


# -- enforcement ---------------------------------------------------------------


@pytest.mark.parametrize("tool", ["Edit", "Write", "NotebookEdit"])
@pytest.mark.parametrize("state", [None, "proposed", "revise"])
def test_hook_denies_edits_until_approved(db, repo, boss, tool, state):
    a = planner(db, boss, plan_state=state)
    out = agents.pre_tool_decision(db, a, {"tool_name": tool, "tool_input": {}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "submit_plan" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_allows_edits_once_approved_or_when_not_plan_first(db, repo, boss):
    a = planner(db, boss, plan_state="approved")
    assert agents.pre_tool_decision(db, a, {"tool_name": "Edit", "tool_input": {}}) is None
    db.add_agent(Agent("w2", boss.id, "developer", "claude", "boss", "assign", "processing", "",
                       None, time.time()))
    assert agents.pre_tool_decision(db, db.get_agent("w2"), {"tool_name": "Write", "tool_input": {}}) is None


def test_hook_still_lets_an_unapproved_plan_first_worker_read_and_run(db, repo, boss):
    a = planner(db, boss)
    assert agents.pre_tool_decision(db, a, {"tool_name": "Read", "tool_input": {}}) is None


# -- autopilot -----------------------------------------------------------------


def test_waiting_on_a_plan_is_not_stalled(db, repo, boss, monkeypatch):
    monkeypatch.setattr(agents, "is_alive", lambda *a, **k: True)
    long_ago = time.time() - autopilot.IDLE_GRACE_SECONDS - 60
    a = planner(db, boss, plan_state="proposed")
    db.update_agent("w1", status="idle")
    db.update_agent("w1", status_since=long_ago)
    working, stalled = autopilot.split_workers(db, "boss")
    assert [w.id for w in working] == ["w1"] and not stalled

    db.update_agent("w1", plan_state="approved")  # same idleness, no plan pending: stalled
    working, stalled = autopilot.split_workers(db, "boss")
    assert [w.id for w in stalled] == ["w1"]


def test_nudge_mentions_plans_awaiting_approval(db, repo, boss):
    a = planner(db, boss, plan_state="proposed")
    ap = Autopilot("boss", 1, "goal", None, "running", None, 0, 0, None, time.time())
    text = autopilot.nudge(db, ap, RepoConfig(), [a], [])
    assert "Plans awaiting your approval: w1" in text and "approve_plan" in text
