import pytest

from copse import autopilot
from copse.config import RepoConfig
from test_autopilot import add_agent, root  # noqa: F401  (the `root` fixture)


def set_goal(db, items):
    autopilot.set_goal(db, "boss", "Goal", items)


def test_set_goal_stores_profile(db, root):
    set_goal(db, [("A", "true", None, "developer"), ("B", "true", None)])
    a, b = db.milestones("boss")
    assert (a.profile, b.profile) == ("developer", None)
    assert "profile: developer" in autopilot.progress(db, "boss")


def test_parse_goals_reads_profile_line():
    plan = autopilot.parse_goals(
        "# G\n\n## One\ncheck: true\nprofile: reviewer\nbody\n\n## Two\ncheck: true\n"
    )
    assert plan.milestones == [("One", "true", "body", "reviewer"), ("Two", "true", None)]


def test_resolution_prefers_current_milestone_then_default(db, root):
    agent, ws = root
    set_goal(db, [("A", "true", None, "reviewer"), ("B", "true", None, "supervisor"), ("C", "true", None)])
    assert autopilot.resolve_profile(db, "boss", ws.repo_root) == "reviewer"
    assert autopilot.resolve_profile(db, "boss", ws.repo_root, "developer") == "developer"
    first = db.milestones("boss")[0]
    db.record_check(first.id, True, "", "sha")
    assert autopilot.resolve_profile(db, "boss", ws.repo_root) == "supervisor"
    for m in db.milestones("boss")[1:2]:
        db.record_check(m.id, True, "", "sha")
    # the current milestone has no profile: fall back to the repo default
    assert autopilot.resolve_profile(db, "boss", ws.repo_root) == RepoConfig().default_agent


def test_unknown_profile_errors_clearly(db, root):
    agent, ws = root
    set_goal(db, [("A", "true", None, "no-such-profile")])
    with pytest.raises(autopilot.AutopilotError, match="no agent profile named 'no-such-profile'"):
        autopilot.resolve_profile(db, "boss", ws.repo_root)
    with pytest.raises(autopilot.AutopilotError, match="'nope'"):
        autopilot.resolve_profile(db, "boss", ws.repo_root, "nope")
