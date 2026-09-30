import json
import time

from typer.testing import CliRunner

from copse import workspaces
from copse.cli import app
from copse.db import Agent

from conftest import sh


def test_ls_json(db, repo, monkeypatch):
    ws = workspaces.create(db, str(repo), "feat/x").workspace
    sh("echo change >> app.py && touch new.txt", ws.path)
    db.add_agent(Agent("a1", ws.id, "developer", "claude", None, "interactive", "idle", "", None, time.time()))

    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["ls", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert len(data) == 1
    entry = data[0]
    assert set(entry) == {"id", "name", "branch", "base_branch", "path", "ahead", "behind", "dirty", "agents"}
    assert entry["id"] == ws.id and entry["name"] == "feat-x"
    assert entry["branch"] == "feat/x" and entry["base_branch"] == "main"
    assert entry["path"] == ws.path
    assert (entry["ahead"], entry["behind"], entry["dirty"]) == (0, 0, 2)
    # No tmux window, so the agent reads as exited.
    assert entry["agents"] == [
        {"id": "a1", "profile": "developer", "provider": "claude", "status": "exited", "mode": "interactive"}
    ]


def test_ls_json_respects_all(db, repo, tmp_path, monkeypatch):
    workspaces.create(db, str(repo), "feat/x")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    sh("git init -q -b main", elsewhere)
    assert json.loads(CliRunner().invoke(app, ["ls", "--json"]).stdout) == []
    assert len(json.loads(CliRunner().invoke(app, ["ls", "--json", "--all"]).stdout)) == 1



def test_rm_survives_a_missing_base_branch(db, repo, monkeypatch):
    sh("git branch tmp-base", repo)
    ws = workspaces.create(db, str(repo), "feat/x", "tmp-base", fetch=False).workspace
    sh("git branch -D tmp-base", repo)  # the base is gone, so git.status raises

    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["rm", ws.id])
    assert res.exit_code == 0, res.output
    assert "removed" in res.output
    assert db.get_workspace(ws.id) is None


def test_version_flag_prints_the_installed_version():
    from typer.testing import CliRunner

    from copse import __version__
    from copse.cli import app

    out = CliRunner().invoke(app, ["--version"])
    assert out.exit_code == 0 and out.output.strip() == f"copse {__version__}"
    assert __version__ != "unknown"
