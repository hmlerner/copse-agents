"""request_review's reviewer-profile selection: an explicit `profile` param,
else the repo's `review_profile` config, else the built-in `reviewer-codex`
profile when Codex is installed and the worker being reviewed ran on Claude,
else `reviewer`."""

import asyncio
import time
from pathlib import Path

import pytest

from conftest import sh
from copse import agents, mcp_server, workspaces
from copse.config import RepoConfig, load_repo_config
from copse.db import Agent
from copse.profiles import Profile, load_profile


@pytest.fixture
def worker_ws(db, repo):
    ws = workspaces.create(db, str(repo), "feat").workspace
    (Path(ws.path) / "new.py").write_text("x = 1\n")
    sh("git add -A && git commit -qm work", Path(ws.path))
    return ws


def add_worker(db, ws, agent_id, provider="claude", mode="handoff", status="done", result="did stuff"):
    a = Agent(agent_id, ws.id, "developer", provider, None, mode, status, "@0", result, time.time())
    db.add_agent(a)
    return a


def capture_profile(monkeypatch):
    """Replace agents.spawn to record the profile request_review chose,
    without launching a real reviewer process. Also stubs load_profile to
    resolve any name (these tests use fake profile names like
    "team-reviewer" purely to check which one was picked, not that it's a
    real, loadable profile) to a harmless claude profile, so request_review's
    own profile-existence check doesn't get in the way of that."""
    captured = {}

    def fake_spawn(db_, ws_, profile, *, prompt=None, parent_id=None, mode="review", **kw):
        captured["profile"] = profile
        return Agent("rev1", ws_.id, profile, "claude", parent_id, mode, "starting", "@0",
                     None, time.time())

    monkeypatch.setattr(agents, "spawn", fake_spawn)
    monkeypatch.setattr(agents, "load_profile",
                        lambda name, repo_root=None: Profile(name, "", "claude", ""))
    return captured


def codex_present(monkeypatch, present: bool):
    # Like a PATH with claude, and codex when ``present``: a name or the
    # path it resolved to both count.
    def which(name):
        base = name.rsplit("/", 1)[-1]
        return f"/usr/bin/{base}" if base == "claude" or (present and base == "codex") else None
    monkeypatch.setattr(agents.shutil, "which", which)
    monkeypatch.setattr(agents, "_local_reviewer_available", lambda: False)  # never probe a live endpoint


# -- default_review_profile: pure selection logic -----------------------------


def test_default_profile_picks_reviewer_codex_when_codex_installed_and_worker_is_claude(monkeypatch):
    codex_present(monkeypatch, True)
    worker = Agent("w1", "ws1", "developer", "claude", None, "handoff", "done", "@0", "ok", time.time())
    assert agents.default_review_profile(RepoConfig(), worker) == "reviewer-codex"


def test_default_profile_falls_back_to_reviewer_without_codex(monkeypatch):
    codex_present(monkeypatch, False)
    worker = Agent("w1", "ws1", "developer", "claude", None, "handoff", "done", "@0", "ok", time.time())
    assert agents.default_review_profile(RepoConfig(), worker) == "reviewer"


def test_default_profile_falls_back_when_worker_itself_ran_on_codex(monkeypatch):
    # Codex is installed, but the worker already used it: no cross-model gain.
    codex_present(monkeypatch, True)
    worker = Agent("w1", "ws1", "developer", "codex", None, "handoff", "done", "@0", "ok", time.time())
    assert agents.default_review_profile(RepoConfig(), worker) == "reviewer"


def test_default_profile_with_no_worker_falls_back_to_reviewer(monkeypatch):
    codex_present(monkeypatch, True)
    assert agents.default_review_profile(RepoConfig(), None) == "reviewer"


def test_default_profile_config_override_beats_codex_autopick(monkeypatch):
    codex_present(monkeypatch, True)
    worker = Agent("w1", "ws1", "developer", "claude", None, "handoff", "done", "@0", "ok", time.time())
    cfg = RepoConfig(review_profile="team-reviewer")
    assert agents.default_review_profile(cfg, worker) == "team-reviewer"


def test_default_profile_respects_a_custom_reviewer_default_without_codex(monkeypatch):
    codex_present(monkeypatch, False)
    worker = Agent("w1", "ws1", "developer", "claude", None, "handoff", "done", "@0", "ok", time.time())
    cfg = RepoConfig(reviewer="my-house-reviewer")
    assert agents.default_review_profile(cfg, worker) == "my-house-reviewer"


# -- agents.request_review: wiring the selection in -----------------------------


def test_request_review_explicit_profile_param_wins(db, worker_ws, monkeypatch):
    add_worker(db, worker_ws, "w1")
    codex_present(monkeypatch, True)
    captured = capture_profile(monkeypatch)
    agents.request_review(db, None, worker_ws, "explicit-profile")
    assert captured["profile"] == "explicit-profile"


def test_request_review_uses_config_review_profile_override(db, worker_ws, monkeypatch):
    add_worker(db, worker_ws, "w1")
    codex_present(monkeypatch, False)
    captured = capture_profile(monkeypatch)
    agents.request_review(db, None, worker_ws, cfg=RepoConfig(review_profile="team-reviewer"))
    assert captured["profile"] == "team-reviewer"


def test_request_review_auto_picks_reviewer_codex_for_a_claude_worker(db, worker_ws, monkeypatch):
    add_worker(db, worker_ws, "w1", provider="claude")
    codex_present(monkeypatch, True)
    captured = capture_profile(monkeypatch)
    agents.request_review(db, None, worker_ws)
    assert captured["profile"] == "reviewer-codex"


def test_request_review_defaults_to_reviewer_when_codex_is_absent(db, worker_ws, monkeypatch):
    add_worker(db, worker_ws, "w1", provider="claude")
    codex_present(monkeypatch, False)
    captured = capture_profile(monkeypatch)
    agents.request_review(db, None, worker_ws)
    assert captured["profile"] == "reviewer"


def test_request_review_skips_reviewer_codex_when_worker_already_used_codex(db, worker_ws, monkeypatch):
    add_worker(db, worker_ws, "w1", provider="codex")
    codex_present(monkeypatch, True)
    captured = capture_profile(monkeypatch)
    agents.request_review(db, None, worker_ws)
    assert captured["profile"] == "reviewer"


# -- agents.request_review: clear errors instead of a dead reviewer -------------


def test_request_review_raises_clearly_for_an_unknown_profile(db, worker_ws):
    add_worker(db, worker_ws, "w1")
    with pytest.raises(agents.AgentError, match="bogus-profile"):
        agents.request_review(db, None, worker_ws, "bogus-profile")
    assert db.list_agents(worker_ws.id) == [db.get_agent("w1")]  # no reviewer was spawned


def test_request_review_raises_clearly_for_an_unknown_configured_review_profile(db, worker_ws):
    add_worker(db, worker_ws, "w1")
    with pytest.raises(agents.AgentError, match="bogus-profile"):
        agents.request_review(db, None, worker_ws, cfg=RepoConfig(review_profile="bogus-profile"))


def test_request_review_raises_clearly_when_codex_profile_but_codex_missing(db, worker_ws, monkeypatch):
    add_worker(db, worker_ws, "w1")
    codex_present(monkeypatch, False)
    with pytest.raises(agents.AgentError, match="codex"):
        agents.request_review(db, None, worker_ws, "reviewer-codex")
    assert db.list_agents(worker_ws.id) == [db.get_agent("w1")]  # no dead reviewer pane


def test_mcp_request_review_returns_a_message_instead_of_raising_for_bad_profile(db, worker_ws):
    add_worker(db, worker_ws, "w1")
    out = asyncio.run(mcp_server.request_review(worker_ws.id, profile="bogus-profile"))
    assert "bogus-profile" in out


def test_mcp_request_review_returns_a_message_instead_of_raising_when_codex_missing(
    db, worker_ws, monkeypatch,
):
    add_worker(db, worker_ws, "w1")
    codex_present(monkeypatch, False)
    out = asyncio.run(mcp_server.request_review(worker_ws.id, profile="reviewer-codex"))
    assert "codex" in out.lower()


# -- config.py: review_profile -----------------------------------------------------


def test_repo_config_reads_review_profile(tmp_path):
    cfg_dir = tmp_path / ".copse"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text('{"review_profile": "custom-reviewer"}')
    cfg = load_repo_config(tmp_path)
    assert cfg.review_profile == "custom-reviewer"


def test_repo_config_review_profile_defaults_to_none(tmp_path):
    assert load_repo_config(tmp_path).review_profile is None


# -- the reviewer-codex builtin profile ------------------------------------------


def test_reviewer_codex_profile_loads_with_codex_provider():
    p = load_profile("reviewer-codex")
    assert p.provider == "codex"
    assert p.name == "reviewer-codex"
    assert "Don't edit files" in p.prompt
    assert "workspace_diff" in p.prompt
