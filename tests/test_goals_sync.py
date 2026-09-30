"""Milestone status is written back to .copse/goals.md, and nothing else."""

import os
import re
import time

import pytest

from conftest import sh
from copse import agents, autopilot, sessions, workspaces
from copse.db import Agent

GOALS = """# Settings page

Users can change their name.

## Settings API
check: `test -f api.txt`
profile: reviewer
The endpoint saves the name.

## Settings UI
check: test -f ui.txt

Some *user prose* to keep.
"""
SHA = "abc1234def5678abc1234def5678abc1234def56"


def add_agent(db, ws, agent_id, status="processing"):
    a = Agent(agent_id, ws.id, "supervisor", "claude", None, "interactive", status, "@0", None, time.time())
    db.add_agent(a)
    return a


def load(db, repo, agent_id="boss", text=GOALS, status="processing"):
    """A session whose goal was loaded from ``repo``'s goals.md."""
    (repo / ".copse").mkdir(exist_ok=True)
    (repo / ".copse" / "goals.md").write_text(text)
    ws = workspaces.adopt_root(db, str(repo))
    add_agent(db, ws, agent_id, status)
    autopilot.enable(db, agent_id, ws)
    return ws


def goals(repo):
    return (repo / ".copse" / "goals.md").read_text()


def pass_first(db, agent_id="boss", sha=SHA):
    db.record_check(db.milestones(agent_id)[0].id, True, "SECRET check output", sha)


def test_status_lines_are_ignored_when_parsing():
    text = GOALS.replace("check: test -f ui.txt\n", "check: test -f ui.txt\nstatus: passed at abc1234 (2026-09-29)\n")
    assert autopilot.parse_goals(text) == autopilot.parse_goals(GOALS)


def test_status_is_written_after_a_check_and_the_rest_is_preserved(db, repo):
    load(db, repo)
    assert goals(repo) == GOALS   # loading alone writes nothing
    pass_first(db)
    autopilot.sync_goals_file(db, "boss")
    day = time.strftime("%Y-%m-%d")
    new = goals(repo)
    assert f"status: passed at abc1234 ({day})" in new
    assert "status: pending" in new
    assert [l for l in new.splitlines() if not l.startswith("status:")] == GOALS.splitlines()
    assert autopilot.parse_goals(new) == autopilot.parse_goals(GOALS)
    # The status goes after the check/profile lines, and rewriting is idempotent.
    assert new.index("profile: reviewer") < new.index("status: passed")
    before = os.stat(repo / ".copse" / "goals.md").st_mtime_ns
    autopilot.sync_goals_file(db, "boss")
    assert goals(repo) == new and os.stat(repo / ".copse" / "goals.md").st_mtime_ns == before


def test_status_updates_in_place(db, repo):
    load(db, repo)
    pass_first(db)
    autopilot.sync_goals_file(db, "boss")
    db.record_check(db.milestones("boss")[0].id, False, "boom", "fff0000")
    autopilot.sync_goals_file(db, "boss")
    new = goals(repo)
    assert new.count("status:") == 2 and "status: failed at fff0000" in new


def test_crlf_and_missing_final_newline_survive(db, repo):
    text = GOALS.replace("\n", "\r\n").rstrip()
    (repo / ".copse").mkdir()
    (repo / ".copse" / "goals.md").write_bytes(text.encode())
    ws = workspaces.adopt_root(db, str(repo))
    add_agent(db, ws, "boss")
    autopilot.enable(db, "boss", ws)
    pass_first(db)
    autopilot.sync_goals_file(db, "boss")
    raw = (repo / ".copse" / "goals.md").read_bytes().decode()
    assert "\r\nstatus: passed" in raw and not re.search(r"(?<!\r)\n", raw)
    assert raw.replace("\r\nstatus: passed at abc1234 (" + time.strftime("%Y-%m-%d") + ")", "").count("status") == 1


def test_only_state_sha_and_date_are_written(db, repo):
    load(db, repo)
    pass_first(db)
    db.update_autopilot("boss", note="which database, Postgres?", state="blocked")
    autopilot.sync_goals_file(db, "boss")
    new = goals(repo)
    for leak in ("boss", "SECRET", "Postgres", "blocked", "processing", "claude"):
        assert leak not in new
    for line in new.splitlines():
        if line.startswith("status:"):
            assert autopilot.STATUS_RE.match(line)


def test_a_new_session_starts_pending_and_never_trusts_a_status_line(db, repo):
    load(db, repo)
    pass_first(db)
    autopilot.sync_goals_file(db, "boss")
    assert "status: passed" in goals(repo)
    ws = workspaces.adopt_root(db, str(repo))
    add_agent(db, ws, "second")
    autopilot.enable(db, "second", ws)
    assert [m.status for m in db.milestones("second")] == ["pending", "pending"]
    assert all(m.passed_sha is None and m.checked_sha is None for m in db.milestones("second"))
    assert "status: passed" in goals(repo)   # loading didn't rewrite it either


@pytest.mark.parametrize("how", ["handover", "paused", "off"])
def test_a_stopped_session_stops_writing(db, repo, how):
    load(db, repo)
    pass_first(db)
    if how == "handover":
        db.update_autopilot("boss", enabled=0)   # what sessions.handover does to the old session
    elif how == "paused":
        db.set_status("boss", "paused")
    else:
        autopilot.set_enabled(db, "boss", False)
    autopilot.sync_goals_file(db, "boss")
    assert goals(repo) == GOALS


def test_real_handover_stops_the_old_session(db, repo, monkeypatch):
    load(db, repo)
    dest = workspaces.checkout_for(db, str(repo), branch="integration")

    def fake_spawn(db_, ws, profile, **kw):
        a = add_agent(db_, ws, "new")
        db_.add_autopilot("new")
        return a

    monkeypatch.setattr(agents, "spawn", fake_spawn)
    monkeypatch.setattr(agents, "pause", lambda *a, **kw: None)
    sessions.handover(db, "boss", dest, "note")
    pass_first(db, "boss")
    autopilot.sync_goals_file(db, "boss")
    assert goals(repo) == GOALS


def test_sessions_in_different_checkouts_write_their_own_file(db, repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    sh("git init -q && git -c user.name=t -c user.email=t@t commit -q --allow-empty -m init", other)
    (other / ".copse").mkdir()
    (other / ".copse" / "goals.md").write_text(GOALS)
    load(db, repo, "a")
    ws = workspaces.adopt_root(db, str(other))
    add_agent(db, ws, "b")
    autopilot.enable(db, "b", ws)
    pass_first(db, "a")
    autopilot.sync_goals_file(db, "a")
    assert "status: passed" in goals(repo)
    assert (other / ".copse" / "goals.md").read_text() == GOALS
    pass_first(db, "b", "1111111")
    autopilot.sync_goals_file(db, "b")
    assert "status: passed at 1111111" in (other / ".copse" / "goals.md").read_text()
    assert "1111111" not in goals(repo)


def test_a_linked_worktree_writes_the_main_checkouts_file(db, repo, tmp_path):
    (repo / ".copse").mkdir()
    (repo / ".copse" / "goals.md").write_text(GOALS)
    linked = tmp_path / "linked"
    sh(f"git worktree add -q -b other {linked}", repo)
    ws = workspaces.adopt_root(db, str(linked))
    add_agent(db, ws, "boss")
    autopilot.enable(db, "boss", ws)
    assert not (linked / ".copse").exists()
    pass_first(db)
    autopilot.sync_goals_file(db, "boss")
    assert "status: passed" in goals(repo)
    assert not (linked / ".copse").exists()


def test_a_goal_set_from_the_chat_never_touches_a_file(db, repo):
    ws = workspaces.adopt_root(db, str(repo))
    add_agent(db, ws, "boss")
    db.add_autopilot("boss")
    (repo / ".copse").mkdir()
    (repo / ".copse" / "goals.md").write_text(GOALS)
    autopilot.set_goal(db, "boss", "Settings page", [("Settings API", "true", None), ("Settings UI", "true", None)])
    pass_first(db)
    autopilot.sync_goals_file(db, "boss")
    assert goals(repo) == GOALS


def test_a_replaced_goal_no_longer_matches_the_file(db, repo):
    load(db, repo)
    autopilot.set_goal(db, "boss", "Something else", [("Other", "true", None)])
    pass_first(db)
    autopilot.sync_goals_file(db, "boss")
    assert goals(repo) == GOALS


def test_a_write_failure_never_fails_the_check(db, repo, monkeypatch):
    ws = load(db, repo)
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("read-only")))
    (repo / "api.txt").write_text("x")
    out = autopilot.check_milestones(db, "boss", ws, cfg=autopilot.RepoConfig())
    assert db.milestones("boss")[0].status == "passed" and out
    assert goals(repo) == GOALS
    assert not [p for p in (repo / ".copse").iterdir() if p.name != "goals.md"]   # no tmp left behind


def test_check_milestones_syncs(db, repo):
    ws = load(db, repo)
    (repo / "api.txt").write_text("x")
    autopilot.check_milestones(db, "boss", ws, cfg=autopilot.RepoConfig())
    new = goals(repo)
    assert re.search(r"status: passed( at [0-9a-f]{7})? \(\d{4}-\d{2}-\d{2}\)", new)
    assert re.search(r"status: failed", new)


def test_a_sync_failure_is_logged_not_raised(db, repo, monkeypatch, caplog):
    ws = load(db, repo)
    monkeypatch.setattr(autopilot, "rewrite_goals", lambda *a: 1 / 0)
    (repo / "api.txt").write_text("x")
    autopilot.check_milestones(db, "boss", ws, cfg=autopilot.RepoConfig())
    assert "couldn't sync milestone status" in caplog.text
