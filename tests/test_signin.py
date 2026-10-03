"""A CLI that isn't signed in stops a launch with how to sign in, instead of
opening on its login screen with copse's prompt typed into it."""

import json

import pytest

from copse import agents, autopilot, doctor, providers, workspaces
from copse.config import load_repo_config


def fake_probe(monkeypatch, replies):
    calls = []

    def probe(argv):
        calls.append(argv)
        return replies.get(argv[1])
    monkeypatch.setattr(providers, "_auth_probe", probe)
    for k in providers._ENV_AUTH["claude"] + providers._ENV_AUTH["codex"]:
        monkeypatch.delenv(k, raising=False)
    return calls


CLAUDE_OUT = (1, json.dumps({"loggedIn": False, "authMethod": "none"}))
CLAUDE_IN = (0, json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
CODEX_OUT = (1, "Not logged in\n")


def test_signed_out_claude_and_codex(monkeypatch):
    fake_probe(monkeypatch, {"auth": CLAUDE_OUT, "login": CODEX_OUT})
    assert "claude auth login" in providers.signed_out("claude")
    assert "codex login" in providers.signed_out("codex")


def test_unknown_answers_never_block(monkeypatch):
    # An older CLI without the status command, a timeout, or an odd reply.
    fake_probe(monkeypatch, {"auth": (1, "error: unknown command 'auth'"), "login": (2, "boom")})
    assert providers.signed_out("claude") is None
    assert providers.signed_out("codex") is None
    assert providers.signed_out("antigravity") is None
    assert providers.signed_out("native") is None


def test_env_credentials_skip_the_check(monkeypatch):
    calls = fake_probe(monkeypatch, {"auth": CLAUDE_OUT})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert providers.signed_out("claude") is None
    assert calls == []


def test_signed_in_is_remembered_signed_out_is_not(monkeypatch):
    calls = fake_probe(monkeypatch, {"auth": CLAUDE_IN})
    assert providers.signed_out("claude") is None
    assert providers.signed_out("claude") is None
    assert len(calls) == 1
    providers._SIGNED_IN.clear()
    calls = fake_probe(monkeypatch, {"auth": CLAUDE_OUT})
    providers.signed_out("claude")
    providers.signed_out("claude")
    assert len(calls) == 2


def test_spawn_refuses_and_leaves_no_agent(db, repo, monkeypatch):
    fake_probe(monkeypatch, {"login": CODEX_OUT})
    ws = workspaces.adopt_root(db, str(repo))
    with pytest.raises(agents.AgentError, match="codex login"):
        agents.spawn(db, ws, "reviewer-codex", prompt="review", mode="review")
    assert db.list_agents() == []


def test_routing_skips_a_signed_out_cli(repo, monkeypatch):
    fake_probe(monkeypatch, {"login": CODEX_OUT})
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    why = autopilot._unavailable("reviewer-codex", load_repo_config(str(repo)), str(repo))
    assert why == "codex isn't signed in, skipped reviewer-codex"


def test_chat_preflight_and_doctor_say_how_to_sign_in(monkeypatch):
    fake_probe(monkeypatch, {"auth": CLAUDE_OUT})
    monkeypatch.setattr(providers, "claude_binary", lambda: "/bin/sh")
    assert any("claude auth login" in p for p in doctor.preflight("claude"))
    check = next(c for c in doctor.signin_checks() if c.name == "Claude Code sign-in")
    assert check.level == doctor.FAIL and "claude auth login" in check.detail


def test_signed_out_profiles_are_not_offered(db, repo, monkeypatch):
    from copse import mcp_server

    fake_probe(monkeypatch, {"login": CODEX_OUT, "auth": CLAUDE_IN})
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.chdir(repo)
    workspaces.adopt_root(db, str(repo))
    listed = mcp_server.list_agent_profiles()
    assert "(claude)" in listed and "(codex)" not in listed
