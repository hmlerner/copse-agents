"""copse's learning plugin interface: outcomes go to an installed plugin,
suggestions come back, and nothing breaks when there is none."""

import time

import pytest

from copse import autopilot, learning, plugins, workspaces
from copse.config import RepoConfig
from copse.db import Agent


class Recorder(learning.LearningPlugin):
    def __init__(self, pick=None, fail=False):
        self.events, self.asked, self.pick, self.fail = [], [], pick, fail

    def record(self, task, outcome):
        if self.fail:
            raise RuntimeError("boom")
        self.events.append((task, outcome))

    def suggest(self, task, candidates):
        if self.fail:
            raise RuntimeError("boom")
        self.asked.append((task, candidates))
        return self.pick


@pytest.fixture
def ws(db, repo):
    return workspaces.adopt_root(db, str(repo))


@pytest.fixture(autouse=True)
def clear_cache():
    plugins.reset()
    yield
    plugins.reset()


def install(monkeypatch, plugin, name="test"):
    class EP:
        def __init__(self):
            self.name = name

        def load(self):
            return lambda repo_root: plugin

    monkeypatch.setattr(plugins, "entry_points", lambda group: [EP()] if group == learning.GROUP else [])


def worker(db, ws, task="fix the crash in parser.py"):
    a = Agent("w1", ws.id, "developer", "claude", None, "assign", "processing", "", None,
              time.time() - 5, task=task)
    db.add_agent(a)
    return a


def test_off_by_default_loads_nothing(db, ws, monkeypatch):
    p = Recorder(pick="developer")
    install(monkeypatch, p)
    cfg = RepoConfig(learning_candidates=["developer"])
    assert learning.plugin(cfg, ws.repo_root) is None
    assert learning.choose(db, cfg, ws.repo_root, "task") is None
    learning.note(db, cfg, worker(db, ws), ws, merged=True)
    assert p.events == [] and p.asked == []


def test_outcomes_reach_the_selected_plugin(db, ws, monkeypatch):
    p = Recorder()
    install(monkeypatch, p)
    cfg = RepoConfig(learning="test")
    w = worker(db, ws)
    learning.note(db, cfg, w, ws, approved=False)
    learning.note(db, cfg, w, ws, escalated=True)
    learning.note(db, cfg, w, ws, merged=True, checks_passed=True)
    assert [o.event for _, o in p.events] == ["review", "escalated", "merged"]
    task, last = p.events[-1]
    assert task.agent_id == "w1" and task.profile == "developer" and task.task.startswith("fix")
    assert last.checks_passed and last.wall_seconds >= 5
    learning.note(db, cfg, w, ws, merged=False)
    assert p.events[-1][1].event == "removed_unmerged"


def test_suggestion_must_be_a_candidate(db, ws, monkeypatch):
    cfg = RepoConfig(learning="test", learning_candidates=["developer", "developer-local"])
    install(monkeypatch, Recorder(pick="developer-local"))
    assert learning.choose(db, cfg, ws.repo_root, "add docs", ["README.md"]) == "developer-local"
    plugins.reset()
    install(monkeypatch, Recorder(pick="something-else"))
    assert learning.choose(db, cfg, ws.repo_root, "add docs") is None


def test_explicit_candidates_and_weight_are_passed(db, ws, monkeypatch):
    p = Recorder(pick="developer-heavy")
    install(monkeypatch, p)
    cfg = RepoConfig(learning="test")
    assert learning.choose(db, cfg, ws.repo_root, "t", candidates=["developer-heavy"],
                           weight="heavy") == "developer-heavy"
    assert p.asked[0][0].weight == "heavy" and p.asked[0][1] == ["developer-heavy"]


def test_a_failing_or_missing_plugin_never_raises(db, ws, monkeypatch):
    install(monkeypatch, Recorder(pick="developer", fail=True))
    cfg = RepoConfig(learning="test", learning_candidates=["developer"])
    learning.note(db, cfg, worker(db, ws), ws, merged=True)
    assert learning.choose(db, cfg, ws.repo_root, "t") is None
    missing = RepoConfig(learning="not-installed", learning_candidates=["developer"])
    assert learning.plugin(missing, ws.repo_root) is None
    assert learning.choose(db, missing, ws.repo_root, "t") is None


def test_resolve_profile_precedence(db, ws, monkeypatch):
    install(monkeypatch, Recorder(pick="developer-local"))
    (__import__("pathlib").Path(ws.repo_root) / ".copse").mkdir(exist_ok=True)
    (__import__("pathlib").Path(ws.repo_root) / ".copse" / "config.json").write_text(
        '{"learning": "test", "learning_candidates": ["developer-local"], "default_agent": "developer"}')
    boss = Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing", "", None,
                 time.time())
    db.add_agent(boss)
    assert autopilot.choose_profile(db, "boss", ws.repo_root, "reviewer", "t") == ("reviewer", False)
    assert autopilot.choose_profile(db, "boss", ws.repo_root, None, "t") == ("developer-local", True)
