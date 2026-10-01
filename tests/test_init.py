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


def test_pr_footer_on_by_default_and_opt_out(repo):
    from copse import workspaces

    assert workspaces.with_footer("Fixes #1", str(repo)).endswith(workspaces.PR_FOOTER)
    assert workspaces.with_footer("", str(repo)) == workspaces.PR_FOOTER
    (repo / ".copse").mkdir()
    (repo / ".copse" / "config.json").write_text('{"pr_footer": false}')
    assert workspaces.with_footer("Fixes #1", str(repo)) == "Fixes #1"


def test_doctor_lists_optional_tools_apart():
    out = doctor.render([doctor.Check(doctor.OK, "tmux", "/bin/tmux"),
                         doctor.Check(doctor.WARN, "codex", "not found: only needed for Codex agents")])
    main, optional = out.split("Optional, not set up")
    assert "tmux" in main and "codex" not in main and "codex" in optional
    assert out.endswith("All good.")


def test_autopilot_guide_asks_for_checks_when_none(tmp_path):
    from copse import autopilot
    from copse.config import RepoConfig

    (tmp_path / "go.mod").write_text("module x\n")
    text = autopilot.guide(RepoConfig(), str(tmp_path))
    assert "no `checks`" in text and "`go test ./...`" in text
    assert "no `checks`" not in autopilot.guide(RepoConfig(checks=["make"]), str(tmp_path))


def test_history_share_card(db, repo):
    import time

    from copse import history, workspaces
    from copse.db import Agent, Task

    ws = workspaces.adopt_root(db, str(repo))
    now = time.time()
    db.add_agent(Agent("sup", ws.id, "supervisor", "claude", None, "interactive", "idle", "", None, now - 900))
    db.add_autopilot("sup")
    db.update_autopilot("sup", goal="Ship the settings page")
    db.set_milestones("sup", [("API", "true", ""), ("UI", "true", "")])
    for m in db.milestones("sup"):
        db.record_check(m.id, True, "")
    # Workers' and reviewers' agent rows are gone by now (removed with their
    # worktrees): the card reads the tasks and history.
    for tid, aid, branch, state, started in (("t1", "w1", "feat/api", "merged", now - 600),
                                              ("t2", "w2", "feat/ui", "started", now - 300)):
        db.add_task(Task(tid, str(repo), aid, "sup", ws.id, "developer", "x", "assign", 1, branch,
                         None, None, None, state, started, started))
    sh("git checkout -q -b feat/ui && git commit -q --allow-empty -m ui && git checkout -q main "
       "&& git merge -q --no-ff --no-edit feat/ui && git branch -q -D feat/ui", repo)
    for aid, kind, branch, profile in (("w1", "worker_result", "feat/api", "developer"),
                                       ("w2", "worker_result", "feat/ui", "developer"),
                                       ("r1", "review", "feat/api", "reviewer-codex"),
                                       ("sup", "merge", "feat/api", "supervisor")):
        db.add_history(str(repo), kind, agent_id=aid, branch=branch, profile=profile)

    card = history.share_card(db, "sup")
    assert "Goal: Ship the settings page" in card
    assert "✓ 2/2 milestones verified" in card
    assert "2 workers · 2 branches merged · 1 review (1 by a different model)" in card
    assert "1.5× parallel" in card
    assert card.endswith("https://pawdelta.com/copse/")


def test_demo_repo_has_failing_goal(copse_home):
    import subprocess

    from copse import autopilot, demo
    from copse.config import load_repo_config

    root = demo.create()
    plan = autopilot.load_goals_file(str(root))
    assert [m[1] for m in plan.milestones] == ["python3 -m unittest tests.test_slug -q",
                                               "python3 -m unittest tests.test_wrap -q"]
    # Each branch can pass the merge check alone; the milestone checks fail until the work is done.
    check = load_repo_config(root).checks[0].split()
    assert subprocess.run(check, cwd=root, capture_output=True).returncode == 0
    assert subprocess.run(["python3", "-m", "unittest", "-q"], cwd=root, capture_output=True).returncode != 0
    assert subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True).stdout == ""
    assert demo.create() != root
    assert load_repo_config(demo.create(local=True)).default_agent == "developer-local"
