"""The pipeline: copse reviews and merges a reported branch itself."""
import asyncio
import json
import time
from pathlib import Path

import pytest

from conftest import sh
from copse import agents, autopilot, gates, mcp_server, pipeline, workspaces
from copse.db import Agent


def add(db, ws, agent_id, mode, profile="developer", parent=None, status="idle", **kw):
    db.add_agent(Agent(agent_id, ws.id, profile, "claude", parent, mode, status, "@0", None,
                       time.time(), **kw))


@pytest.fixture
def piped(db, repo, monkeypatch):
    """A supervisor in the checkout, a worker on a branch with a commit, and a
    reviewer that copse can start without a real process."""
    (repo / ".copse").mkdir()
    (repo / ".copse" / "config.json").write_text(json.dumps(
        {"review": True, "auto_merge_default_branch": True}))
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")  # busy: messages queue
    ws = workspaces.create(db, str(repo), "feat").workspace
    (Path(ws.path) / "new.py").write_text("x = 1\n")
    sh("git add new.py && git commit -qm work", Path(ws.path))
    add(db, ws, "w1", "assign", parent="boss", status="processing")
    started = []

    def fake_review(db_, caller, ws_, profile=None, focus=None, cfg=None):
        add(db_, ws_, f"rev{len(started)}", "review", "reviewer", parent=caller.id)
        started.append(ws_.id)
        return db_.get_agent(f"rev{len(started) - 1}")

    monkeypatch.setattr(agents, "request_review", fake_review)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "reconcile", lambda db_, a, **kw: a)
    monkeypatch.setattr(agents, "warm_checks", lambda ws_: None)
    monkeypatch.setattr(agents, "close_later", lambda agent_id, delay=5.0: None)
    monkeypatch.setattr(pipeline, "_detach", lambda argv: None)
    monkeypatch.setattr(agents, "_stop", lambda db_, a: None)
    return root, ws, started


def test_a_report_starts_the_review_instead_of_going_to_the_supervisor(db, piped):
    root, ws, started = piped
    out = agents.report_result(db, "w1", "added new.py")
    assert "copse is having your branch reviewed" in out
    assert started == [ws.id] and db.get_agent("w1").pipeline == "reviewing"
    assert db.pending_count("boss") == 0


def test_approval_merges_and_tells_the_supervisor_once(db, piped, repo):
    root, ws, started = piped
    agents.report_result(db, "w1", "added new.py")
    reviewer = db.get_agent("rev0")
    db.add_review(ws.id, gates.head(ws), reviewer.id, True, "lgtm")
    assert pipeline.on_review(db, reviewer, ws, True, "lgtm") is True
    assert "new.py" in sh("git ls-tree --name-only HEAD", repo)      # merged into main
    msg = db.pop_pending("boss")
    assert msg and "Merged feat into main" in msg.body and "added new.py" in msg.body and "lgtm" in msg.body
    assert db.get_workspace(ws.id) is None                           # worktree removed
    assert db.get_agent("w1") is None                                # and its worker record with it


def test_a_reviewer_submitting_an_approval_survives_the_removal(db, piped, repo):
    root, ws, started = piped
    agents.report_result(db, "w1", "added new.py")
    out = agents.submit_review(db, "rev0", True, "lgtm")
    assert "copse takes it from here" in out
    assert "new.py" in sh("git ls-tree --name-only HEAD", repo)
    assert db.get_workspace(ws.id) is None and db.get_agent("rev0") is None


def test_changes_requested_go_back_to_the_worker_then_to_the_supervisor(db, piped):
    root, ws, started = piped
    agents.report_result(db, "w1", "added new.py")
    reviewer = db.get_agent("rev0")
    assert pipeline.on_review(db, reviewer, ws, False, "missing a test") is True
    w = db.get_agent("w1")
    assert w.pipeline == "fixing" and w.pipeline_rounds == 1
    fix_request = db.pop_pending("w1")
    assert fix_request and "missing a test" in fix_request.body and "report_result again" in fix_request.body
    assert db.pending_count("boss") == 0
    # It reports again: reviewed again.
    agents.report_result(db, "w1", "added the test")
    assert started == [ws.id, ws.id] and db.get_agent("w1").pipeline == "reviewing"
    # Second rejection: round 2 of 2 goes to the worker; a third goes to the supervisor.
    pipeline.on_review(db, db.get_agent("rev1"), ws, False, "still missing")
    assert db.get_agent("w1").pipeline == "fixing"
    agents.report_result(db, "w1", "tried again")
    pipeline.on_review(db, db.get_agent("rev2"), ws, False, "no")
    msg = db.pop_pending("boss")
    assert msg and "needs you" in msg.body and "no" in msg.body
    assert db.get_agent("w1").pipeline is None


def test_verdicts_on_piped_branches_are_not_forwarded(db, piped, monkeypatch):
    root, ws, started = piped
    agents.report_result(db, "w1", "added new.py")
    monkeypatch.setenv("COPSE_AGENT_ID", "rev0")
    monkeypatch.setattr(mcp_server, "_caller", lambda db_: (db_.get_agent("rev0"), ws))
    out = mcp_server.submit_review(False, "missing a test")
    assert "copse takes it from here" in out
    assert db.pending_count("boss") == 0 and db.pending_count("w1") == 1


def test_pipeline_off_keeps_the_old_flow(db, piped, repo):
    (repo / ".copse" / "config.json").write_text(json.dumps({"review": True, "pipeline": False}))
    root, ws, started = piped
    out = agents.report_result(db, "w1", "added new.py")
    assert "sent to your supervisor" in out and started == [] and db.pending_count("boss") == 1


def test_a_waiting_handoff_is_not_piped(db, piped):
    root, ws, started = piped
    db.update_agent("w1", mode="handoff")
    agents.report_result(db, "w1", "added new.py")
    assert started == [] and db.get_agent("w1").pipeline is None


def test_a_branch_in_the_pipeline_counts_as_work_in_progress(db, piped):
    root, ws, started = piped
    db.add_autopilot("boss")
    agents.report_result(db, "w1", "added new.py")
    assert [a.id for a in autopilot.active_workers(db, "boss", reviewers=False)] == ["w1"]


def test_overlapping_tasks_are_blocked(db, repo, monkeypatch):
    from copse import tasks

    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor")
    monkeypatch.setenv("COPSE_AGENT_ID", "boss")
    monkeypatch.setattr(tasks, "overlap_warning", lambda db_, ws_, files: "overlaps with w0 (feat/a) on ledger.py")
    delegated = []
    monkeypatch.setattr(agents, "delegate", lambda *a, **k: delegated.append(a))
    out = asyncio.run(mcp_server.assign("developer", "do it", files=["ledger.py"]))
    assert out.startswith("Not started") and "depends_on" in out and delegated == []
