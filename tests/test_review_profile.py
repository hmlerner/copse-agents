"""default_review_profile: which reviewer request_review picks by default."""

import shutil
import time

import pytest

from copse import agents
from copse.config import RepoConfig
from copse.db import Agent
from copse.native import runner


def worker(provider="claude"):
    return Agent("w1", "ws1", "developer", provider, None, "assign", "idle", "@0", None, time.time())


@pytest.fixture
def which(monkeypatch):
    """Control whether codex is on PATH."""
    state = {"codex": False}
    monkeypatch.setattr(shutil, "which", lambda name: name if state["codex"] and name.endswith("codex") else None)
    return state


@pytest.fixture
def probe(monkeypatch):
    """Control the endpoint probe's answer; records the calls."""
    state = {"result": (True, "model qwen3-coder:30b is available"), "calls": 0}

    def fake_probe(endpoint, timeout=3.0):
        state["calls"] += 1
        result = state["result"]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(runner, "probe", fake_probe)
    return state


def test_explicit_review_profile_wins(which, probe):
    which["codex"] = True
    cfg = RepoConfig(review_profile="mine")
    assert agents.default_review_profile(cfg, worker()) == "mine"
    assert probe["calls"] == 0


def test_claude_worker_with_codex_gets_reviewer_codex(which, probe):
    which["codex"] = True
    assert agents.default_review_profile(RepoConfig(), worker()) == "reviewer-codex"
    assert probe["calls"] == 0


def test_claude_worker_without_codex_gets_local_reviewer_when_available(which, probe):
    assert agents.default_review_profile(RepoConfig(), worker()) == "reviewer-local"
    assert probe["calls"] == 1


def test_claude_worker_without_codex_and_unreachable_endpoint_gets_cfg_reviewer(which, probe):
    probe["result"] = (False, "connection refused")
    assert agents.default_review_profile(RepoConfig(reviewer="rev"), worker()) == "rev"


def test_reachable_endpoint_without_the_model_is_not_available(which, probe):
    probe["result"] = (True, "reachable, but model qwen3-coder:30b is not among: llama3")
    assert agents.default_review_profile(RepoConfig(reviewer="rev"), worker()) == "rev"


def test_probe_exception_means_not_available(which, probe):
    probe["result"] = OSError("boom")
    assert agents.default_review_profile(RepoConfig(reviewer="rev"), worker()) == "rev"


def test_endpoint_exception_means_not_available(which, probe, monkeypatch):
    def boom(profile):
        raise ValueError("no endpoint")

    monkeypatch.setattr(runner, "endpoint_for", boom)
    assert agents.default_review_profile(RepoConfig(reviewer="rev"), worker()) == "rev"
    assert probe["calls"] == 0


def test_non_claude_worker_gets_cfg_reviewer(which, probe):
    which["codex"] = True
    assert agents.default_review_profile(RepoConfig(reviewer="rev"), worker("native")) == "rev"
    assert agents.default_review_profile(RepoConfig(reviewer="rev"), None) == "rev"
    assert probe["calls"] == 0
