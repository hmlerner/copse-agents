"""`copse init`: detect a repo's setup and checks, write the config, check tools."""

import json

from typer.testing import CliRunner

from copse import detect, doctor
from copse.cli import app

from conftest import sh


def test_detect_uv_python(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[dependency-groups]\ndev = ["pytest"]\n')
    (tmp_path / "uv.lock").write_text("")
    d = detect.detect(tmp_path)
    assert d.stacks == ["Python (uv)"]
    assert d.setup == ["uv sync"] and d.checks == ["uv run pytest -q"]


def test_detect_node_picks_package_manager_and_skips_placeholder_test(tmp_path):
    pkg = {"scripts": {"test": 'echo "Error: no test specified" && exit 1', "typecheck": "tsc"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    (tmp_path / "pnpm-lock.yaml").write_text("")
    d = detect.detect(tmp_path)
    assert d.stacks == ["Node (pnpm)"]
    assert d.setup == ["pnpm install --frozen-lockfile"]
    assert d.checks == ["pnpm run typecheck"]


def test_detect_go_and_make_fallback(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    (tmp_path / "Makefile").write_text("test:\n\tgo test ./...\n")
    d = detect.detect(tmp_path)
    assert d.checks == ["go test ./..."]          # make test only when nothing else gave a check

    other = tmp_path / "other"
    other.mkdir()
    (other / "Makefile").write_text("build:\n\ttrue\ntest: build\n\ttrue\n")
    assert detect.detect(other).checks == ["make test"]


def test_detect_copies_only_ignored_env_files(repo):
    (repo / ".gitignore").write_text(".env\n")
    (repo / ".env").write_text("SECRET=1\n")
    (repo / ".env.local").write_text("tracked on purpose\n")
    assert detect.detect(repo).copy == [".env"]


def test_detect_nothing_says_add_checks(tmp_path):
    d = detect.detect(tmp_path)
    assert d.stacks == [] and d.checks == []
    assert any("checks" in n for n in d.notes)


def test_init_writes_detected_config_and_keeps_existing(repo, monkeypatch):
    (repo / "go.mod").write_text("module x\n")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(doctor, "checks", lambda root: [])
    res = CliRunner().invoke(app, ["init"])
    assert res.exit_code == 0, res.output
    cfg = json.loads((repo / ".copse" / "config.json").read_text())
    assert cfg["setup"] == ["go mod download"] and cfg["checks"] == ["go test ./..."]
    assert "Ready" in res.output

    (repo / ".copse" / "config.json").write_text('{"checks": ["mine"]}\n')
    res = CliRunner().invoke(app, ["init"])
    assert res.exit_code == 0, res.output
    assert "kept" in res.output
    assert json.loads((repo / ".copse" / "config.json").read_text()) == {"checks": ["mine"]}


def test_init_fails_on_doctor_failure(repo, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.setattr(doctor, "checks", lambda root: [doctor.Check(doctor.FAIL, "tmux", "not found")])
    res = CliRunner().invoke(app, ["init"])
    assert res.exit_code == 1
    assert "tmux" in res.output


def test_init_outside_git_points_at_scratch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(app, ["init"])
    assert res.exit_code == 1
    assert "scratch session" in res.output


def test_preflight_names_missing_cli(monkeypatch):
    monkeypatch.setenv("COPSE_CLAUDE_BIN", "/nonexistent/claude")
    problems = doctor.preflight("claude")
    assert any("Claude Code" in p and "npm install" in p for p in problems)
    assert not any("Claude Code" in p for p in doctor.preflight("shell"))
