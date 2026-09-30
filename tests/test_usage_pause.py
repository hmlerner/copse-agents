import time

import pytest

from copse import agents, autopilot, cull, quota, watch, workspaces
from copse.db import Agent

NOW = time.time()  # quota drops windows already past their reset, so RESET must be ahead of the real clock
RESET = NOW + 3600


@pytest.fixture
def session(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive",
                       "processing", "@0", None, time.time()))
    db.add_agent(Agent("w1", ws.id, "developer", "claude", "boss", "assign",
                       "processing", "@1", None, time.time()))
    db.add_autopilot("boss")
    autopilot.set_goal(db, "boss", "Goal", [("M1", "true", None)])
    stopped, resumed, sent = [], [], []
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.status not in ("paused", "done"))
    monkeypatch.setattr(agents, "_stop", lambda db, a: stopped.append(a.id))

    def fake_resume(db, root_id, only=None, **kw):
        out = [a for a in agents.tree(db, root_id)
               if a.status == "paused" and (only is None or a.id in only)]
        for a in out:
            db.set_status(a.id, "processing")
        resumed.extend(a.id for a in out)
        return out

    monkeypatch.setattr(agents, "resume", fake_resume)
    monkeypatch.setattr(agents, "send_message", lambda db, to, body, sender_id=None: sent.append((to, body)))
    return SimpleSession(stopped, resumed, sent)


class SimpleSession:
    def __init__(self, stopped, resumed, sent):
        self.stopped, self.resumed, self.sent = stopped, resumed, sent


def use(percent, resets_at=RESET):
    autopilot.record_usage({"rate_limits": {"five_hour": {"used_percentage": percent, "resets_at": resets_at}}})


def test_sweep_pauses_workers_at_the_limit(db, session):
    use(95)
    notes = autopilot.usage_sweep(db)
    assert session.stopped == ["w1"]
    assert db.get_agent("w1").status == "paused"
    assert db.get_agent("boss").status == "processing"  # the supervisor isn't touched
    ap = db.get_autopilot("boss")
    assert ap.state == "usage_paused" and ap.usage_resets_at == RESET
    assert notes


def test_on_stop_pauses_even_with_workers_running(db, session):
    use(95)
    assert autopilot.on_stop(db, db.get_agent("boss"), {}) is None
    assert db.get_autopilot("boss").state == "usage_paused"
    assert db.get_agent("w1").status == "paused"


def test_no_reset_time_just_blocks(db, session):
    use(95, resets_at=None)
    autopilot.usage_sweep(db)
    assert db.get_autopilot("boss").state == "blocked"
    assert session.stopped == []


def test_below_limit_does_nothing(db, session):
    use(20)
    assert autopilot.usage_sweep(db) == []
    assert db.get_autopilot("boss").state == "running"


def test_resumes_after_reset(db, session):
    use(95)
    autopilot.usage_sweep(db)
    assert autopilot.usage_sweep(db, now=RESET - 10) == []  # window not over yet
    assert db.get_autopilot("boss").state == "usage_paused"
    use(10)
    notes = autopilot.usage_sweep(db, now=RESET + 1)
    assert session.resumed == ["w1"] and notes
    ap = db.get_autopilot("boss")
    assert ap.state == "running" and ap.usage_resets_at is None
    assert len(session.sent) == 1 and session.sent[0][0] == "boss" and "w1" in session.sent[0][1]


def test_still_over_limit_after_reset_time_keeps_waiting(db, session):
    use(95)
    autopilot.usage_sweep(db)
    assert autopilot.usage_sweep(db, now=RESET + 1) == []  # fresh usage still at the limit
    assert db.get_autopilot("boss").state == "usage_paused"


def test_no_fresh_usage_resumes_after_reset(db, session):
    use(95)
    autopilot.usage_sweep(db)
    quota.path().unlink()
    autopilot.usage_sweep(db, now=RESET + 1)
    assert db.get_autopilot("boss").state == "running"


def test_user_speaking_does_not_clear_it(db, session):
    use(95)
    autopilot.usage_sweep(db)
    autopilot.user_spoke(db, db.get_agent("boss"))
    assert db.get_autopilot("boss").state == "usage_paused"


def test_cull_sweep_drives_it_and_keeps_paused_workers(db, session, monkeypatch):
    monkeypatch.setattr(cull.tmux, "list_panes", lambda: {})
    monkeypatch.setattr(cull.procs, "table", lambda: {})
    monkeypatch.setattr(cull.procs, "all_agent_ids", lambda t: [])
    monkeypatch.setattr(cull, "clean_locks", lambda db, now: 0)
    monkeypatch.setattr(cull, "note_stuck", lambda db, now, panes: [])
    monkeypatch.setattr(cull.agents, "pane_owners", lambda db, panes=None: {})
    use(95)
    cull.sweep(db, now=time.time())
    assert db.get_autopilot("boss").state == "usage_paused"
    cull.sweep(db, now=time.time() + 10 * 3600)  # far past any idle limit
    assert not db.get_agent("w1").dismissed_at
    use(5)
    cull.sweep(db, now=RESET + 1)
    assert db.get_autopilot("boss").state == "running"


def test_limit_error_names_a_non_claude_provider_and_leaves_claude_workers(db, session):
    db.update_agent("w1", provider="antigravity")
    autopilot.limit_reached(db, db.get_agent("w1"))
    ap = db.get_autopilot("boss")
    assert ap.state == "blocked" and "antigravity" in ap.note and "Claude" not in ap.note
    assert session.stopped == []


def test_claude_limit_error_pauses_only_claude_workers(db, session):
    ws = db.get_workspace(db.get_agent("boss").workspace_id)
    db.add_agent(Agent("w2", ws.id, "developer", "antigravity", "boss", "assign",
                       "processing", "@2", None, time.time()))
    use(50)  # under the limit, but a turn just failed on it
    autopilot.limit_reached(db, db.get_agent("w1"))
    assert session.stopped == ["w1"]
    assert db.get_agent("w2").status == "processing"
    assert db.get_autopilot("boss").state == "usage_paused"


def test_paused_session_is_not_restarted_by_the_sweep(db, session):
    use(95)
    autopilot.usage_sweep(db)
    db.set_status("boss", "paused")  # the person paused the whole session
    use(5)
    assert autopilot.usage_sweep(db, now=RESET + 1) == []
    assert autopilot.usage_resume(db, "boss", now=RESET + 1) is None
    assert session.resumed == [] and session.sent == []
    ap = db.get_autopilot("boss")
    assert ap.state == "usage_paused" and ap.usage_paused_ids
    # Once the session is continued, the next sweep finishes the job.
    db.set_status("boss", "processing")
    autopilot.usage_sweep(db, now=RESET + 1)
    assert db.get_autopilot("boss").state == "running"


def test_worker_paused_for_another_reason_is_left_alone(db, session):
    ws = db.get_workspace(db.get_agent("boss").workspace_id)
    db.add_agent(Agent("w2", ws.id, "developer", "claude", "boss", "assign",
                       "paused", "@2", None, time.time()))
    use(95)
    autopilot.usage_sweep(db)
    use(5)
    autopilot.usage_sweep(db, now=RESET + 1)
    assert session.resumed == ["w1"]
    assert db.get_agent("w2").status == "paused"


def test_sidebar_line():
    pilot = {"enabled": True, "goal": "Goal", "milestones": [], "state": "usage_paused",
             "usage_resets_at": RESET, "workers": 0, "note": None}
    text = " ".join(line.text for line in watch.render_autopilot(pilot, 60))
    assert "paused for usage until" in text
