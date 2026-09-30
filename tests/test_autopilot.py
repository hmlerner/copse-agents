import json
import time
from types import SimpleNamespace

import pytest

from conftest import sh
from copse import agents, autopilot, gates, watch, workspaces
from copse.config import RepoConfig
from copse.db import Agent
from copse.providers import ClaudeCode, LaunchContext, status_line
from copse.profiles import load_profile

GOALS = """# Settings page

Users can change their name.

## Settings API
check: `test -f api.txt`
The endpoint saves the name.

## Settings UI
check: test -f ui.txt
"""


def add_agent(db, ws, agent_id, mode="interactive", parent=None, status="processing"):
    a = Agent(agent_id, ws.id, "supervisor", "claude", parent, mode, status, "@0", None, time.time())
    db.add_agent(a)
    return a


@pytest.fixture
def root(db, repo):
    ws = workspaces.adopt_root(db, str(repo))
    agent = add_agent(db, ws, "boss")
    db.add_autopilot("boss")
    return agent, ws


def with_goal(db, checks=("test -f api.txt", "test -f ui.txt")):
    autopilot.set_goal(db, "boss", "Settings page",
                       [(f"Milestone {i}", c, None) for i, c in enumerate(checks, start=1)])


def test_parse_goals_file():
    plan = autopilot.parse_goals(GOALS)
    assert plan.goal == "Settings page"
    assert plan.detail == "Users can change their name."
    assert plan.milestones == [
        ("Settings API", "test -f api.txt", "The endpoint saves the name."),
        ("Settings UI", "test -f ui.txt", None),
    ]
    assert autopilot.parse_goals("no heading here") is None


def test_enable_loads_goals_md(db, repo):
    (repo / ".copse").mkdir()
    (repo / ".copse" / "goals.md").write_text(GOALS)
    ws = workspaces.adopt_root(db, str(repo))
    add_agent(db, ws, "boss")
    plan = autopilot.enable(db, "boss", ws)
    assert plan and db.get_autopilot("boss").goal == "Settings page"
    assert [m.title for m in db.milestones("boss")] == ["Settings API", "Settings UI"]
    assert "2 milestones" in autopilot.kickoff(plan)


def test_no_goal_means_no_nudge(db, root):
    agent, _ = root
    assert autopilot.on_stop(db, agent, {}) is None


def test_nudges_until_stalled_without_progress(db, root, monkeypatch):
    agent, _ = root
    with_goal(db)
    monkeypatch.setattr(autopilot, "active_workers", lambda db, rid: [])
    for _ in range(autopilot.MAX_NUDGES):
        out = autopilot.on_stop(db, agent, {"stop_hook_active": True})
        assert out and out["decision"] == "block" and "0 of 2 milestones verified" in out["reason"]
    assert autopilot.on_stop(db, agent, {"stop_hook_active": True}) is None
    assert db.get_autopilot("boss").state == "stalled"
    # The user replies: autopilot drives again.
    autopilot.user_spoke(db, db.get_agent("boss"))
    assert db.get_autopilot("boss").state == "running"
    assert autopilot.on_stop(db, agent, {}) is not None


def test_progress_resets_the_nudge_count(db, root, monkeypatch):
    agent, _ = root
    with_goal(db)
    monkeypatch.setattr(autopilot, "active_workers", lambda db, rid: [])
    for _ in range(autopilot.MAX_NUDGES + 2):
        assert autopilot.on_stop(db, agent, {}) is not None
        db.bump_progress("boss")


def test_running_workers_let_the_supervisor_idle(db, root, monkeypatch):
    agent, _ = root
    with_goal(db)
    monkeypatch.setattr(autopilot, "active_workers",
                        lambda db, rid: [SimpleNamespace(id="w", provider="claude",
                                                         result=None, status="processing",
                                                         status_since=time.time())])
    assert autopilot.on_stop(db, agent, {}) is None


def test_open_subagent_workers_dont_let_the_supervisor_idle(db, root, monkeypatch):
    # A subagent worker sends no message when done, so waiting would stall.
    agent, _ = root
    with_goal(db)
    monkeypatch.setattr(autopilot, "active_workers",
                        lambda db, rid: [SimpleNamespace(id="sub1", provider="subagent",
                                                         result=None, status="processing",
                                                         status_since=time.time())])
    out = autopilot.on_stop(db, agent, {})
    assert out and out["decision"] == "block"
    assert "complete_subagent for sub1" in out["reason"]


def test_need_user_blocks_until_they_reply(db, root, monkeypatch):
    agent, _ = root
    with_goal(db)
    monkeypatch.setattr(autopilot, "active_workers", lambda db, rid: [])
    autopilot.need_user(db, "boss", "Postgres or SQLite?")
    assert autopilot.on_stop(db, agent, {}) is None
    assert "Postgres or SQLite?" in autopilot.progress(db, "boss")


def test_off_means_no_nudge(db, root, monkeypatch):
    agent, _ = root
    with_goal(db)
    monkeypatch.setattr(autopilot, "active_workers", lambda db, rid: [])
    autopilot.set_enabled(db, "boss", False)
    assert autopilot.on_stop(db, agent, {}) is None


def test_checks_verify_milestones_and_finish_the_goal(db, root, repo):
    agent, ws = root
    with_goal(db)
    out = autopilot.check_milestones(db, "boss", ws)
    assert "0 of 2 milestones verified" in out and "(exit 1)" in out
    (repo / "api.txt").write_text("")
    out = autopilot.check_milestones(db, "boss", ws, position=1)
    assert "1 of 2" in out
    (repo / "ui.txt").write_text("")
    out = autopilot.check_milestones(db, "boss", ws, position=2)
    assert "goal is reached" in out
    assert db.get_autopilot("boss").state == "done"
    assert autopilot.on_stop(db, agent, {}) is None


def test_single_check_rechecks_all_before_declaring_done(db, root, repo):
    _, ws = root
    with_goal(db)
    (repo / "api.txt").write_text("")
    autopilot.check_milestones(db, "boss", ws)
    (repo / "api.txt").unlink()           # milestone 1 regressed
    (repo / "ui.txt").write_text("")
    out = autopilot.check_milestones(db, "boss", ws, position=2)
    assert "goal is reached" not in out
    assert [m.status for m in db.milestones("boss")] == ["failed", "passed"]


def test_set_goal_keeps_results_of_unchanged_milestones(db, root, repo):
    _, ws = root
    with_goal(db)
    (repo / "api.txt").write_text("")
    autopilot.check_milestones(db, "boss", ws)
    autopilot.set_goal(db, "boss", "Settings page", [
        ("Milestone 1", "test -f api.txt", None), ("Milestone 3", "true", None)])
    assert [m.status for m in db.milestones("boss")] == ["passed", "pending"]


def test_usage_limit_pauses_autopilot(db, root, monkeypatch):
    agent, _ = root
    with_goal(db)
    monkeypatch.setattr(autopilot, "active_workers", lambda db, rid: [])
    autopilot.record_usage({"rate_limits": {"five_hour": {"used_percentage": 93, "resets_at": None}}})
    assert autopilot.usage()["used"] == 93
    assert autopilot.on_stop(db, agent, {}) is None
    ap = db.get_autopilot("boss")
    assert ap.state == "blocked" and "93%" in ap.note


def test_rate_limit_failure_blocks_autopilot(db, root):
    agent, _ = root
    with_goal(db)
    agents.handle_hook(db, "boss", "stop-failure", {"error_type": "rate_limit"})
    assert db.get_autopilot("boss").state == "blocked"
    assert db.get_agent("boss").status == "idle"


def test_stop_hook_runs_autopilot_for_the_supervisor(db, root, monkeypatch):
    with_goal(db)
    monkeypatch.setattr(autopilot, "active_workers", lambda db, rid: [])
    out = agents.handle_hook(db, "boss", "stop", {})
    assert out and "[copse autopilot]" in out["reason"]
    assert db.get_agent("boss").status == "processing"


def test_agent_cap(db, root, monkeypatch):
    add_agent(db, root[1], "w1", mode="assign", parent="boss")
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    autopilot.check_capacity(db, "boss", RepoConfig(max_agents=2))
    with pytest.raises(autopilot.AutopilotError, match="already running"):
        autopilot.check_capacity(db, "boss", RepoConfig(max_agents=1))
    autopilot.check_capacity(db, "w1", RepoConfig(max_agents=0))


def test_worker_goal():
    text = autopilot.worker_goal("Add a login page.", "tests/test_login.py passes.", "feat/login")
    assert text.startswith("/goal Finish line: tests/test_login.py passes.")
    assert "feat/login" in text and text.endswith("Add a login page.")
    assert autopilot.worker_goal("x" * 5000, "done", "b") is None


# -- merge gates ---------------------------------------------------------------


@pytest.fixture
def worker_ws(db, repo):
    ws = workspaces.create(db, str(repo), "feat").workspace
    from pathlib import Path

    (Path(ws.path) / "new.py").write_text("x = 1\n")
    sh("git add -A && git commit -qm work", Path(ws.path))
    return ws


def test_gate_requires_an_approval_of_this_commit(db, worker_ws):
    from pathlib import Path

    cfg = RepoConfig()
    r = gates.run(db, worker_ws, cfg, review_required=True)
    assert not r.ok and "request_review" in r.problem
    db.add_review(worker_ws.id, gates.head(worker_ws), "rev", False, "missing test")
    r = gates.run(db, worker_ws, cfg, review_required=True)
    assert not r.ok and "missing test" in r.problem
    db.add_review(worker_ws.id, gates.head(worker_ws), "rev", True, "lgtm")
    assert gates.run(db, worker_ws, cfg, review_required=True).ok
    # A new commit needs a new review.
    (Path(worker_ws.path) / "more.py").write_text("y = 2\n")
    sh("git add -A && git commit -qm more", Path(worker_ws.path))
    assert not gates.run(db, worker_ws, cfg, review_required=True).ok


def test_gate_runs_checks_and_refuses_dirty_work(db, worker_ws):
    from pathlib import Path

    assert gates.run(db, worker_ws, RepoConfig(checks=["test -f new.py"]), review_required=False).ok
    r = gates.run(db, worker_ws, RepoConfig(checks=["false"]), review_required=False)
    assert not r.ok and "Check failed" in r.problem
    (Path(worker_ws.path) / "new.py").write_text("x = 2\n")
    r = gates.run(db, worker_ws, RepoConfig(), review_required=False)
    assert not r.ok and "uncommitted" in r.problem


def test_reviewer_nudged_to_submit(db, worker_ws):
    add_agent(db, worker_ws, "rev", mode="review")
    out = agents.handle_hook(db, "rev", "stop", {})
    assert out and "submit_review" in out["reason"]


# -- display and launch ------------------------------------------------------------


def pilot(**kw):
    return {"enabled": True, "goal": "Settings page", "state": "running", "note": None,
            "milestones": [{"position": 1, "title": "Settings API", "status": "passed", "check": "x"},
                           {"position": 2, "title": "Settings UI", "status": "failed", "check": "y"}],
            "usage": None, "workers": 2, **kw}


def test_sidebar_shows_milestones():
    lines = watch.render([], now=0, width=40, pilot=pilot())
    text = [ln.text for ln in lines]
    assert "Autopilot · Settings page" in text
    assert "  ✓ Settings API" in text and "  ✗ Settings UI" in text
    assert "  1 of 2 verified · 2 workers on it" in text
    assert all(len(t) <= 40 for t in text)


def test_sidebar_shows_blocked_and_usage():
    lines = watch.render([], now=0, width=60, pilot=pilot(
        state="blocked", note="Postgres or SQLite?",
        usage={"window": "five_hour", "used": 91, "resets_at": None}))
    text = "\n".join(ln.text for ln in lines)
    assert "needs you" in text and "Postgres or SQLite?" in text
    assert "Claude usage at 91% of the 5-hour limit" in text


def test_sidebar_without_goal_asks_for_one():
    lines = watch.render([], now=0, pilot=pilot(goal=None))
    assert any("what we're building" in ln.text for ln in lines)


def test_claude_command_records_usage_and_limit_failures():
    argv = ClaudeCode().command(LaunchContext("abc", load_profile("supervisor"), None))
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert "_statusline" in settings["statusLine"]["command"]
    assert "stop-failure" in json.dumps(settings["hooks"]["StopFailure"])


def test_status_line_passes_through_the_persons_own(tmp_path, monkeypatch):
    config = tmp_path / "claude-config"
    config.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    payload = json.dumps({"workspace": {"project_dir": str(tmp_path)},
                          "rate_limits": {"seven_day": {"used_percentage": 40}}})
    assert status_line(payload) == ""
    assert autopilot.usage()["used"] == 40
    (config / "settings.json").write_text(json.dumps(
        {"statusLine": {"type": "command", "command": "echo mine"}}))
    assert status_line(payload) == "mine"


def test_delivered_worker_result_is_not_the_user_replying(db, root):
    with_goal(db)
    autopilot.need_user(db, "boss", "Postgres or SQLite?")
    db.enqueue("boss", "Assigned task finished.", "w1")
    db.pop_pending("boss")  # delivered by typing it into the idle chat
    agents.handle_hook(db, "boss", "prompt-submit", {"prompt": "Assigned task finished.\n"})
    assert db.get_autopilot("boss").state == "blocked"
    agents.handle_hook(db, "boss", "prompt-submit", {"prompt": "SQLite"})
    assert db.get_autopilot("boss").state == "running"


def test_reviewers_dont_count_against_the_cap(db, root, monkeypatch):
    add_agent(db, root[1], "rev", mode="review", parent="boss")
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    autopilot.check_capacity(db, "boss", RepoConfig(max_agents=1))
    assert len(autopilot.active_workers(db, "boss")) == 1   # but the supervisor still waits for it


def test_reported_worker_back_at_work_is_active(db, root, monkeypatch):
    add_agent(db, root[1], "w1", mode="assign", parent="boss", status="idle")
    db.set_result("w1", "done")
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    assert autopilot.active_workers(db, "boss") == []
    db.set_status("w1", "processing")   # the supervisor sent review feedback
    assert [a.id for a in autopilot.active_workers(db, "boss")] == ["w1"]
