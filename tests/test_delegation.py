"""The repo's "delegation" setting: how readily a supervisor hands work to
workers (conservative, balanced by default, or fast)."""

import json
import time

from typer.testing import CliRunner

from copse import agents, workspaces
from copse.cli import app
from copse.config import set_user, user_config_path
from copse.db import Agent


def _prompt(db, ws, aid="s1", profile="supervisor", parent=None, mode="interactive"):
    db.add_agent(Agent(aid, ws.id, profile, "claude", parent, mode, "idle", "@0", None, time.time()))
    return agents._profile_for(db, db.get_agent(aid), ws).prompt


def test_supervisor_defaults_to_balanced(db, repo):
    ws = workspaces.adopt_root(db, str(repo))
    prompt = _prompt(db, ws)
    assert "Delegation rule: balanced" in prompt


def test_repo_can_choose_fast_or_conservative(db, repo):
    (repo / ".copse").mkdir()
    (repo / ".copse" / "config.json").write_text(json.dumps({"delegation": "fast"}))
    ws = workspaces.adopt_root(db, str(repo))
    assert "Delegation rule: fast" in _prompt(db, ws)
    (repo / ".copse" / "config.local.json").write_text(json.dumps({"delegation": "conservative"}))
    assert "Delegation rule: conservative" in _prompt(db, ws, aid="s2")  # local wins


def test_unknown_level_falls_back_to_balanced(db, repo):
    (repo / ".copse").mkdir()
    (repo / ".copse" / "config.json").write_text(json.dumps({"delegation": "turbo"}))
    ws = workspaces.adopt_root(db, str(repo))
    assert "Delegation rule: balanced" in _prompt(db, ws)


def test_workers_get_no_delegation_rule(db, repo):
    ws = workspaces.adopt_root(db, str(repo))
    assert "Delegation rule" not in _prompt(db, ws, aid="w1", profile="developer", parent="s1", mode="assign")


def test_cli_saves_it_for_every_repo_and_tells_a_running_supervisor(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("s1", ws.id, "supervisor", "claude", None, "interactive", "idle", "@0", None, time.time()))
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "s1")
    told = []
    monkeypatch.setattr(agents, "send_message", lambda db_, to, body, **k: told.append((to, body)))
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(app, ["delegation", "fast"])
    assert result.exit_code == 0, result.output
    assert json.loads(user_config_path().read_text())["delegation"] == "fast"
    assert not (repo / ".copse" / "config.local.json").exists()
    assert told and told[0][0] == "s1" and "Delegation rule: fast" in told[0][1]
    assert "delegation: fast" in CliRunner().invoke(app, ["delegation"]).output
    assert CliRunner().invoke(app, ["delegation", "turbo"]).exit_code != 0


def test_user_setting_applies_everywhere_but_a_repo_can_override(db, repo, monkeypatch):
    set_user("delegation", "fast")
    ws = workspaces.adopt_root(db, str(repo))
    assert "Delegation rule: fast" in _prompt(db, ws)
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(app, ["delegation", "conservative", "--repo"])
    assert result.exit_code == 0, result.output
    assert "Delegation rule: conservative" in _prompt(db, ws, aid="s2")
    assert json.loads(user_config_path().read_text())["delegation"] == "fast"  # untouched
