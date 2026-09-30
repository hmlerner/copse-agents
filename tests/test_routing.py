"""Routing by weight: assign/handoff's `weight` picks an available profile for
the tier, after an explicit profile and a milestone's, before the default."""

import asyncio
import json
import time

import pytest

from copse import agents, autopilot, learning, mcp_server, quota, workspaces
from copse.config import DEFAULT_ROUTING, load_repo_config
from copse.db import Agent


class Picker(learning.LearningPlugin):
    def __init__(self, prefer=None):
        self.prefer, self.asked = prefer, []

    def record(self, task, outcome):
        pass

    def suggest(self, task, candidates):
        self.asked.append((task, list(candidates)))
        return self.prefer if self.prefer in candidates else None


@pytest.fixture(autouse=True)
def clear_cache():
    learning._loaded.clear()
    yield
    learning._loaded.clear()


@pytest.fixture
def boss(db, repo, monkeypatch):
    (repo / ".copse").mkdir(exist_ok=True)
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    monkeypatch.setenv("COPSE_AGENT_ID", "boss")
    return ws


@pytest.fixture(autouse=True)
def everything_available(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cli: f"/usr/bin/{cli}")
    monkeypatch.setattr(quota, "headroom", lambda provider, cfg=None, repo_root=None: 100.0)


def config(repo, **kw):
    (repo / ".copse").mkdir(exist_ok=True)
    (repo / ".copse" / "config.json").write_text(json.dumps(kw))


def choose(db, repo, requested=None, weight=None, why=None):
    return autopilot.choose_profile(db, "boss", str(repo), requested, "task", None, weight, why)


def install(monkeypatch, plugin):
    class EP:
        name = "test"

        def load(self):
            return lambda repo_root: plugin

    monkeypatch.setattr(learning, "entry_points", lambda group: [EP()] if group == learning.GROUP else [])


def test_defaults(repo):
    assert load_repo_config(repo).routing == DEFAULT_ROUTING


def test_config_overrides_one_tier_and_keeps_the_rest(repo):
    config(repo, routing={"heavy": ["developer"], "bogus": ["x"]})
    r = load_repo_config(repo).routing
    assert r["heavy"] == ["developer"]
    assert r["light"] == DEFAULT_ROUTING["light"] and "bogus" not in r


def test_weight_picks_the_first_candidate(db, repo, boss):
    why = []
    assert choose(db, repo, weight="heavy", why=why) == ("developer-heavy", False)
    assert why == ["weight heavy -> developer-heavy"]
    assert choose(db, repo, weight="medium")[0] == "developer-codex"
    assert choose(db, repo, weight="light")[0] == "developer-local"


def test_no_weight_keeps_the_default_agent(db, repo, boss):
    config(repo, default_agent="developer-codex")
    assert choose(db, repo) == ("developer-codex", False)


def test_invalid_weight_is_refused(db, repo, boss):
    with pytest.raises(autopilot.AutopilotError):
        choose(db, repo, weight="enormous")


def test_precedence_explicit_then_milestone_then_weight(db, repo, boss):
    assert choose(db, repo, "reviewer", "heavy")[0] == "reviewer"
    db.add_autopilot("boss", enabled=True)
    autopilot.set_goal(db, "boss", "g", [("m1", "true", "", "developer-local")])
    assert choose(db, repo, None, "heavy")[0] == "developer-local"
    milestone = db.milestones("boss")[0]
    db.record_check(milestone.id, True, "", "sha")
    assert choose(db, repo, None, "heavy")[0] == "developer-heavy"


def test_weight_routing_beats_learning_candidates(db, repo, boss, monkeypatch):
    install(monkeypatch, Picker(prefer="developer-local"))
    config(repo, learning="test", learning_candidates=["developer-local"])
    assert choose(db, repo, weight="heavy")[0] == "developer-heavy"


def test_missing_cli_is_skipped(db, repo, boss, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cli: None if cli == "codex" else f"/usr/bin/{cli}")
    why = []
    assert choose(db, repo, weight="medium", why=why)[0] == "developer"
    assert "codex isn't installed, skipped developer-codex" in why[0]


def test_limited_provider_is_skipped(db, repo, boss, monkeypatch):
    monkeypatch.setattr(quota, "headroom",
                        lambda provider, cfg=None, repo_root=None: 7.0 if provider == "codex" else 100.0)
    why = []
    assert choose(db, repo, weight="medium", why=why)[0] == "developer"
    assert why == ["weight medium -> developer (Codex at 93%, skipped developer-codex)"]


def test_provider_below_the_usage_limit_is_kept(db, repo, boss, monkeypatch):
    monkeypatch.setattr(quota, "headroom", lambda provider, cfg=None, repo_root=None: 30.0)
    assert choose(db, repo, weight="medium")[0] == "developer-codex"


def test_native_down_is_skipped(db, repo, boss, monkeypatch):
    monkeypatch.setattr(quota, "headroom",
                        lambda provider, cfg=None, repo_root=None: 0.0 if provider == "native" else 100.0)
    why = []
    assert choose(db, repo, weight="light", why=why)[0] == "developer"
    assert "local model server not answering" in why[0]


def test_learning_plugin_reorders_the_remaining_candidates(db, repo, boss, monkeypatch):
    plugin = Picker(prefer="developer")
    install(monkeypatch, plugin)
    config(repo, learning="test")
    why = []
    assert choose(db, repo, weight="medium", why=why) == ("developer", True)
    task, candidates = plugin.asked[0]
    assert candidates == ["developer-codex", "developer"] and task.weight == "medium"
    assert "learning picked it" in why[0]


def test_learning_only_sees_available_candidates(db, repo, boss, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cli: None if cli == "codex" else f"/usr/bin/{cli}")
    plugin = Picker(prefer="developer-codex")
    install(monkeypatch, plugin)
    config(repo, learning="test")
    assert choose(db, repo, weight="medium") == ("developer", False)
    assert plugin.asked[0][1] == ["developer"]


def test_all_out_falls_back_to_the_default_agent_and_says_why(db, repo, boss, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cli: None)
    config(repo, default_agent="reviewer")
    why = []
    assert choose(db, repo, weight="heavy", why=why) == ("reviewer", False)
    assert "every heavy candidate was out" in why[0] and "using reviewer" in why[0]


# -- the assign/handoff reply, and the weight stored for learning -----------------------


def fake_spawn(db, ws, profile, *, prompt=None, parent_id=None, mode="handoff", done_when=None, **kw):
    a = Agent(agents.new_id(), ws.id, profile, "claude", parent_id, mode, "processing", "",
              None, time.time(), task=prompt, done_when=done_when)
    db.add_agent(a)
    return a


def test_assign_reply_names_the_pick_and_stores_the_weight(db, repo, boss, monkeypatch):
    monkeypatch.setattr(agents, "spawn", fake_spawn)
    monkeypatch.setattr(quota, "headroom",
                        lambda provider, cfg=None, repo_root=None: 7.0 if provider == "codex" else 100.0)
    config(repo, pipeline=False)
    out = asyncio.run(mcp_server.assign(task="do A", branch="feat-a", weight="medium"))
    assert "Started worker" in out
    assert "weight medium -> developer (Codex at 93%, skipped developer-codex)" in out
    [t] = db.list_tasks(str(repo), state="started")
    assert t.weight == "medium" and t.profile == "developer"


def test_learning_outcomes_carry_the_weight(db, repo, boss, monkeypatch):
    monkeypatch.setattr(agents, "spawn", fake_spawn)
    config(repo, pipeline=False, learning="test")

    class Recorder(Picker):
        def __init__(self):
            super().__init__()
            self.events = []

        def record(self, task, outcome):
            self.events.append(task)

    plugin = Recorder()
    install(monkeypatch, plugin)
    asyncio.run(mcp_server.assign(task="do A", branch="feat-a", weight="heavy"))
    [t] = db.list_tasks(str(repo), state="started")
    worker = db.get_agent(t.agent_id)
    learning.note(db, load_repo_config(repo), worker, boss, merged=True)
    assert plugin.events[0].weight == "heavy"
