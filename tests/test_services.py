"""Per-worktree Docker services, against a fake `docker` that records its argv."""

import json
import os
import stat

import pytest

from copse import services, workspaces
from copse.config import PORT_BLOCK_SIZE, load_repo_config
from copse.pro import license


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "docker.log"
    exe = bindir / "docker"
    exe.write_text(f'#!/bin/sh\necho "$@" >> {log}\n[ "$1" = ps ] && echo c0ffee\nexit 0\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    def calls():
        return log.read_text().splitlines() if log.exists() else []

    return calls


@pytest.fixture
def entitled(monkeypatch):
    monkeypatch.setattr(license, "has", lambda feature: feature == "services")


def configure(repo, services_cfg):
    (repo / ".copse").mkdir(exist_ok=True)
    (repo / ".copse" / "config.json").write_text(json.dumps({"services": services_cfg}))


def test_presets_and_env_rendering(db, repo):
    configure(repo, [
        {"name": "db", "preset": "postgres"},
        {"name": "my-cache", "image": "x/y:1", "port": 7000,
         "env": {"CACHE": "{name}@{workspace}:{port}"}},
    ])
    ws = workspaces.create(db, str(repo), "feat-a", run_setup=False).workspace
    cfg = load_repo_config(str(repo))
    svcs = services.resolve(ws, cfg)
    assert svcs[0].image.startswith("postgres:") and svcs[0].port == 5432
    assert svcs[0].env["DATABASE_URL"] == (
        f"postgres://postgres:copse@127.0.0.1:{ws.port_base + 1}/app")
    assert svcs[0].env["COPSE_SVC_DB_PORT"] == str(ws.port_base + 1)
    assert svcs[1].env["CACHE"] == f"my-cache@{ws.name}:{ws.port_base + 2}"
    assert svcs[1].env["COPSE_SVC_MY_CACHE_PORT"] == str(ws.port_base + 2)


def test_ports_come_from_the_block_after_the_apps_own(db, repo):
    configure(repo, [{"name": f"s{i}", "image": "i", "port": 1000 + i}
                     for i in range(PORT_BLOCK_SIZE + 2)])
    ws = workspaces.create(db, str(repo), "feat-a", run_setup=False).workspace
    svcs = services.resolve(ws, load_repo_config(str(repo)))
    assert [s.host_port for s in svcs] == [ws.port_base + 1 + i for i in range(PORT_BLOCK_SIZE - 1)]


def test_started_on_create_and_in_workspace_env(db, repo, fake_docker, entitled):
    configure(repo, [{"name": "db", "preset": "postgres"}])
    ws = workspaces.create(db, str(repo), "feat-a").workspace
    clear, call = fake_docker()
    assert clear == f"rm -f {services.container_name(ws, 'db')}"  # a leftover never blocks `up`
    assert call.startswith("run -d --rm --name copse-")
    assert f"--label copse.workspace={ws.id}" in call
    assert f"-p 127.0.0.1:{ws.port_base + 1}:5432" in call
    assert "-e POSTGRES_PASSWORD=copse" in call
    env = workspaces.workspace_env(ws)
    assert env["DATABASE_URL"].endswith(f":{ws.port_base + 1}/app")
    assert env["COPSE_SVC_DB_PORT"] == str(ws.port_base + 1)


def test_removed_on_remove(db, repo, fake_docker, entitled):
    configure(repo, [{"name": "db", "preset": "postgres"}])
    ws = workspaces.create(db, str(repo), "feat-a").workspace
    configure(repo, [])  # removal goes by label, even after the config changed
    workspaces.remove(db, ws)
    assert fake_docker()[-2:] == [f"ps -aq --filter label=copse.workspace={ws.id}", "rm -f c0ffee"]


def test_not_entitled_runs_nothing(db, repo, fake_docker, monkeypatch, capsys):
    monkeypatch.setattr(license, "has", lambda feature: False)
    configure(repo, [{"name": "db", "preset": "postgres"}])
    ws = workspaces.create(db, str(repo), "feat-a").workspace
    assert fake_docker() == []
    assert "need copse Pro" in capsys.readouterr().err
    assert "DATABASE_URL" not in workspaces.workspace_env(ws)


def test_docker_missing_only_warns(db, repo, entitled, monkeypatch, capsys):
    monkeypatch.setattr(services.shutil, "which",
                        lambda name, *a, **k: None if name == "docker" else "/usr/bin/" + name)
    configure(repo, [{"name": "db", "preset": "postgres"}])
    created = workspaces.create(db, str(repo), "feat-a", run_setup=True)
    assert created.workspace.id
    assert "docker not found" in capsys.readouterr().err
