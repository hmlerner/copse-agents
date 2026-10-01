"""``copse ci``: goal sources, the headless run (with fakes for tmux, the
supervisor, git push and gh), the entitlement gate, and ``copse ci init``."""

import functools
import json
import time

import pytest
from typer.testing import CliRunner

from copse import autopilot as pilot
from copse import ci, git, workspaces
from copse.cli import app
from copse.config import load_repo_config
from copse.db import Agent
from copse.pro import auth, credentials
from pro_fixtures import (  # noqa: F401 - fixtures
    BASE, backend, claims, pro_env, sign, signing_key,
)


class FakeSession:
    """Stands in for tmux, the supervisor, git push and gh: records what the
    run asks for, and ``script`` plays the supervisor on every poll."""

    def __init__(self, db, monkeypatch, script=None):
        self.db = db
        self.prompts, self.stopped, self.pushed, self.prs = [], [], [], []
        self.alive = True
        self.clock, self.ticks = 1_000.0, 0
        self.script = script or (lambda session, root_id: None)
        self.root_id = None
        monkeypatch.setattr(ci, "_spawn", self.spawn)
        monkeypatch.setattr(ci, "_alive", lambda db, root_id: self.alive)
        monkeypatch.setattr(ci, "_stop", self.stop)
        monkeypatch.setattr(ci, "_push", lambda ws: self.pushed.append(ws.branch))
        monkeypatch.setattr(ci, "_create_pr", self.create_pr)

    def spawn(self, db, ws, prompt):
        self.prompts.append(prompt)
        a = Agent("sup1", ws.id, "supervisor", "claude", None, "interactive", "processing", "%9",
                  None, time.time())
        db.add_agent(a)
        db.add_autopilot(a.id)
        self.root_id = a.id
        return a

    def stop(self, db, root_id):
        self.stopped.append(root_id)

    def create_pr(self, ws, base, title, body):
        self.prs.append((ws.branch, base, title, body))
        return "https://github.com/o/r/pull/7"

    def now(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += seconds
        self.ticks += 1
        self.script(self, self.root_id)


def finish_goal(session, root_id, title="Add health", n=1):
    """The supervisor sets a goal and gets every milestone verified."""
    if session.ticks < n:
        return
    db = session.db
    if not db.milestones(root_id):
        pilot.set_goal(db, root_id, title, [("Endpoint", "uv run pytest -q", None),
                                            ("Docs", None, "README")])
    for m in db.milestones(root_id):
        db.record_check(m.id, True, "ok", "abc1234")


def run(db, repo, goal, session, **kw):
    kw.setdefault("timeout_min", 30)
    return ci.run(db, str(repo), goal, clock=session.now, sleep=session.sleep, **kw)


# -- goal sources ------------------------------------------------------------


def test_goal_from_issue_uses_gh(monkeypatch):
    seen = []

    def gh(args, cwd):
        seen.append(args)
        return {"number": 42, "title": "Add a /health endpoint", "body": "Return 200 with uptime."}

    monkeypatch.setattr(ci, "_gh_json", gh)
    g = ci.goal_from_issue(42, "/repo")
    assert seen == [["issue", "view", "42", "--json", "number,title,body"]]
    assert (g.title, g.detail, g.issue, g.source) == (
        "Add a /health endpoint", "Return 200 with uptime.", 42, "issue")
    assert g.branch == "copse/ci-42"
    assert g.plan() is None


def test_goal_from_issue_failures(monkeypatch):
    monkeypatch.setattr(ci, "_gh_json", lambda args, cwd: {"number": 3, "title": "", "body": ""})
    with pytest.raises(ci.CIError, match="no title"):
        ci.goal_from_issue(3, "/repo")

    def broken(args, cwd):
        raise ci.CIError("gh issue view failed: not found")

    monkeypatch.setattr(ci, "_gh_json", broken)
    with pytest.raises(ci.CIError, match="not found"):
        ci.goal_from_issue(3, "/repo")


def test_goal_from_text_slug_and_plan():
    g = ci.goal_from_text("Add a /health endpoint\nIt returns uptime.")
    assert (g.title, g.detail, g.issue) == ("Add a /health endpoint", "It returns uptime.", None)
    assert g.branch == "copse/ci-add-a-health-endpoint"
    assert g.plan() is None

    shaped = ci.goal_from_text("# Settings page\n\n## API\ncheck: uv run pytest tests/test_api.py -q\n")
    assert shaped.title == "Settings page"
    plan = shaped.plan()
    assert plan is not None and plan.milestones == [("API", "uv run pytest tests/test_api.py -q", None)]

    long = ci.goal_from_text("x" * 100)
    assert len(long.branch) <= len(ci.BRANCH_PREFIX) + ci.MAX_SLUG
    with pytest.raises(ci.CIError, match="empty"):
        ci.goal_from_text("  \n")


def test_goal_from_file(tmp_path):
    f = tmp_path / "goals.md"
    f.write_text("# Billing\n\n## Invoices\ncheck: make test-invoices\n", encoding="utf-8")
    g = ci.goal_from_file(f)
    assert (g.title, g.source) == ("Billing", "file")
    assert g.plan().milestones == [("Invoices", "make test-invoices", None)]
    with pytest.raises(ci.CIError, match="cannot read"):
        ci.goal_from_file(tmp_path / "missing.md")


def test_resolve_goal_wants_exactly_one_source(tmp_path):
    with pytest.raises(ci.CIError, match="exactly one"):
        ci.resolve_goal(None, None, None, str(tmp_path))
    with pytest.raises(ci.CIError, match="exactly one"):
        ci.resolve_goal("a", "b", None, str(tmp_path))
    assert ci.resolve_goal("Fix it", None, None, str(tmp_path)).title == "Fix it"


# -- the run --------------------------------------------------------------------


def test_completion_pushes_and_opens_the_pr(db, repo, monkeypatch):
    s = FakeSession(db, monkeypatch, script=finish_goal)
    goal = ci.Goal("Add a /health endpoint", "Return 200.", issue=42, source="issue")
    out = run(db, repo, goal, s)

    assert out.status == "done" and out.ok
    assert out.pr_url == "https://github.com/o/r/pull/7"
    assert s.pushed == ["copse/ci-42"]
    branch, base, title, body = s.prs[0]
    assert (branch, base, title) == ("copse/ci-42", "main", "Add a /health endpoint")
    assert "Return 200." in body
    assert "- [x] Endpoint (`uv run pytest -q`)" in body and "- [x] Docs" in body
    assert "Closes #42" in body
    assert s.stopped == ["sup1"]
    assert [m["status"] for m in out.milestones] == ["passed", "passed"]
    # The work happened in a worktree on the fresh branch, cut from the base.
    assert git.worktree_for_branch(str(repo), "copse/ci-42")
    # The supervisor was told it runs unattended, and to derive the milestones.
    assert "unattended" in s.prompts[0] and "issue #42" in s.prompts[0]
    assert "set_goal" in s.prompts[0] and "Return 200." in s.prompts[0]


def test_a_rerun_reuses_the_branch_worktree(db, repo, monkeypatch):
    s = FakeSession(db, monkeypatch, script=finish_goal)
    goal = ci.Goal("Add health", issue=42)
    run(db, repo, goal, s)
    first = git.worktree_for_branch(str(repo), "copse/ci-42")
    s2 = FakeSession(db, monkeypatch, script=finish_goal)
    db.delete_agent("sup1")
    run(db, repo, goal, s2)
    assert git.worktree_for_branch(str(repo), "copse/ci-42") == first


def test_shaped_goal_records_the_milestones_up_front(db, repo, monkeypatch):
    def verify(session, root_id):
        for m in session.db.milestones(root_id):
            session.db.record_check(m.id, True, "ok")

    s = FakeSession(db, monkeypatch, script=verify)
    goal = ci.goal_from_text("# Settings\n\nUsers edit their name.\n\n## API\ncheck: make test-api\n"
                             "## UI\ncheck: make test-ui\n")
    out = run(db, repo, goal, s, pr=False)
    assert out.status == "done" and out.ok and out.pr_url is None
    assert [m["title"] for m in out.milestones] == ["API", "UI"]
    assert db.get_autopilot("sup1").goal == "Settings"
    assert "already recorded" in s.prompts[0] and "make test-api" not in s.prompts[0]
    assert s.pushed == [] and s.prs == []
    body = ci.pr_body(out)
    assert "- [x] API (`make test-api`)" in body and "## API" not in body


def test_need_user_fails_with_the_question(db, repo, monkeypatch):
    def ask(session, root_id):
        if session.ticks == 1:
            pilot.set_goal(session.db, root_id, "Add health", [("Endpoint", "make test", None)])
        if session.ticks == 2:
            pilot.need_user(session.db, root_id, "Postgres or SQLite for the store?")

    s = FakeSession(db, monkeypatch, script=ask)
    out = run(db, repo, ci.Goal("Add health"), s)
    assert out.status == "need_user" and not out.ok
    assert "Postgres or SQLite" in out.note
    assert s.pushed == [] and s.prs == []
    assert s.stopped == ["sup1"]
    assert out.milestones[0]["status"] == "pending"
    assert "Postgres or SQLite" in out.describe()


def test_stalled_and_usage_paused_fail(db, repo, monkeypatch):
    def stall(session, root_id):
        session.db.update_autopilot(root_id, state="stalled", note="no progress after 3 reminders")

    s = FakeSession(db, monkeypatch, script=stall)
    out = run(db, repo, ci.Goal("Add health"), s)
    assert (out.status, out.note) == ("stalled", "no progress after 3 reminders")

    def paused(session, root_id):
        session.db.update_autopilot(root_id, state="usage_paused")

    db.delete_agent("sup1")
    s = FakeSession(db, monkeypatch, script=paused)
    out = run(db, repo, ci.Goal("Add health"), s)
    assert out.status == "stalled" and "usage" in out.note
    assert s.stopped == ["sup1"]


def test_timeout_is_non_zero_and_stops_the_session(db, repo, monkeypatch):
    s = FakeSession(db, monkeypatch)
    out = run(db, repo, ci.Goal("Add health"), s, timeout_min=1)
    assert out.status == "timeout" and not out.ok
    assert "1 minutes" in out.note
    assert out.elapsed == pytest.approx(60)
    assert s.pushed == [] and s.prs == [] and s.stopped == ["sup1"]
    assert s.ticks == 6   # polled every 10 s, never past the deadline


def test_supervisor_exit_fails(db, repo, monkeypatch):
    def die(session, root_id):
        session.alive = False

    s = FakeSession(db, monkeypatch, script=die)
    out = run(db, repo, ci.Goal("Add health"), s)
    assert out.status == "exited" and "exited" in out.note
    assert s.stopped == ["sup1"]


def test_cleanup_runs_when_the_run_itself_breaks(db, repo, monkeypatch):
    def boom(session, root_id):
        raise RuntimeError("db went away")

    s = FakeSession(db, monkeypatch, script=boom)
    with pytest.raises(RuntimeError, match="db went away"):
        run(db, repo, ci.Goal("Add health"), s)
    assert s.stopped == ["sup1"]


def test_pr_failure_is_reported_as_a_failure(db, repo, monkeypatch):
    s = FakeSession(db, monkeypatch, script=finish_goal)

    def refuse(ws, base, title, body):
        raise ci.CIError("gh pr create failed: no commits between main and copse/ci-add-health")

    monkeypatch.setattr(ci, "_create_pr", refuse)
    out = run(db, repo, ci.Goal("Add health"), s)
    assert out.status == "done" and not out.ok and out.pr_url is None
    assert "no commits" in out.note
    assert s.pushed == ["copse/ci-add-health"]


def test_base_and_max_workers(db, repo, monkeypatch):
    from conftest import sh

    sh("git branch develop && git push -q origin develop", repo)
    s = FakeSession(db, monkeypatch, script=finish_goal)
    out = run(db, repo, ci.Goal("Add health"), s, base="develop", max_workers=2)
    assert out.ok and s.prs[0][1] == "develop"
    assert load_repo_config(str(repo)).max_agents == 2
    assert (repo / ".copse" / ".gitignore").read_text() == "config.local.json\n"
    # Other local keys are kept, and the cap can change.
    local = repo / ".copse" / "config.local.json"
    local.write_text(json.dumps({"max_agents": 2, "sidebar": "bottom"}))
    ci.set_max_workers(str(repo), 3)
    assert json.loads(local.read_text()) == {"max_agents": 3, "sidebar": "bottom"}


def test_step_summary_is_json(db, repo, monkeypatch, tmp_path):
    summary = tmp_path / "summary.md"
    summary.write_text("earlier step\n")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    s = FakeSession(db, monkeypatch, script=finish_goal)
    out = run(db, repo, ci.Goal("Add health", issue=5), s)
    ci.write_step_summary(out)
    text = summary.read_text()
    assert text.startswith("earlier step\n## copse ci: done")
    data = json.loads(text.split("```json\n", 1)[1].split("```")[0])
    assert data["ok"] is True and data["status"] == "done"
    assert data["issue"] == 5 and data["branch"] == "copse/ci-5"
    assert data["pr_url"] == "https://github.com/o/r/pull/7"
    assert [m["status"] for m in data["milestones"]] == ["passed", "passed"]
    monkeypatch.delenv("GITHUB_STEP_SUMMARY")
    assert ci.write_step_summary(out) is None


# -- the entitlement gate ----------------------------------------------------------


def ci_backend(backend, features):
    backend.routes["GET /entitlement"] = lambda f, h: (
        200, {"entitlement": sign(backend.key, claims(plan="team", features=features))})
    return auth.Client(BASE, transport=backend)


def test_token_from_env_is_exchanged_in_memory(backend, monkeypatch, copse_home):
    client = ci_backend(backend, ["learning", "team", "ci"])
    monkeypatch.setenv("COPSE_PRO_TOKEN", backend.issue()["refresh_token"])
    ent = ci.require_ci(client)
    assert "ci" in ent.features and ent.plan == "team"
    assert "POST /token/refresh" in backend.paths() and "GET /entitlement" in backend.paths()
    # Nothing was written: no credentials, no file under the home.
    assert credentials.default_store().load() is None
    assert not (copse_home / "pro").exists()


def test_token_without_ci_feature_is_refused(backend, monkeypatch):
    client = ci_backend(backend, ["learning", "autopilot"])
    monkeypatch.setenv("COPSE_PRO_TOKEN", backend.issue()["refresh_token"])
    with pytest.raises(ci.CIError, match="copse Team"):
        ci.require_ci(client)


def test_bad_token_is_refused_without_leaking_it(backend, monkeypatch):
    client = ci_backend(backend, ["ci"])
    monkeypatch.setenv("COPSE_PRO_TOKEN", "cpr_stolen")
    with pytest.raises(ci.CIError) as e:
        ci.require_ci(client)
    assert "invalid_grant" in str(e.value) and "cpr_stolen" not in str(e.value)


def test_not_logged_in_says_how(monkeypatch):
    monkeypatch.delenv("COPSE_PRO_TOKEN", raising=False)
    with pytest.raises(ci.CIError, match="COPSE_PRO_TOKEN"):
        ci.require_ci()


# -- the CLI -------------------------------------------------------------------------


def test_cli_run_is_refused_when_not_entitled(repo, monkeypatch):
    monkeypatch.delenv("COPSE_PRO_TOKEN", raising=False)
    monkeypatch.chdir(repo)
    spawned = []
    monkeypatch.setattr(ci, "_spawn", lambda db, ws, prompt: spawned.append(prompt))
    res = CliRunner().invoke(app, ["ci", "run", "--goal", "Add health"])
    assert res.exit_code == 1, res.output
    assert "copse ci" in res.output and "COPSE_PRO_TOKEN" in res.output
    assert spawned == []


def test_cli_run_wants_one_goal_source(repo, monkeypatch):
    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["ci", "run"])
    assert res.exit_code == 1 and "exactly one" in res.output
    res = CliRunner().invoke(app, ["ci", "run", "--goal", "a", "--issue", "1"])
    assert res.exit_code == 1 and "exactly one" in res.output


def test_cli_run_end_to_end(db, repo, monkeypatch, tmp_path):
    monkeypatch.chdir(repo)
    monkeypatch.setattr(ci, "require_ci", lambda client=None: None)
    monkeypatch.setattr(ci, "_gh_json", lambda args, cwd: {"number": 9, "title": "Add health", "body": ""})
    s = FakeSession(db, monkeypatch, script=finish_goal)
    monkeypatch.setattr(ci, "run", functools.partial(ci.run, clock=s.now, sleep=s.sleep))
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    res = CliRunner().invoke(app, ["ci", "run", "--issue", "9", "--timeout", "5"])
    assert res.exit_code == 0, res.output
    assert res.output.rstrip().endswith("https://github.com/o/r/pull/7")
    assert "2 of 2 milestones verified" in res.output
    assert s.prs[0][0] == "copse/ci-9" and "Closes #9" in s.prs[0][3]
    assert '"status": "done"' in summary.read_text()

    # The failing path exits 1 and says why.
    db.delete_agent("sup1")
    s2 = FakeSession(db, monkeypatch)
    monkeypatch.setattr(ci, "run", functools.partial(ci.run, clock=s2.now, sleep=s2.sleep))
    res = CliRunner().invoke(app, ["ci", "run", "--goal", "Other thing", "--timeout", "1", "--no-pr"])
    assert res.exit_code == 1, res.output
    assert "timeout" in res.output and s2.stopped == ["sup1"]


# -- copse ci init -------------------------------------------------------------------


def test_init_writes_a_workflow_and_respects_force(repo):
    path = ci.init(repo)
    assert path == repo / ".github" / "workflows" / "copse.yml"
    text = path.read_text()
    assert "name: copse" in text
    assert "  issues:\n    types: [labeled]" in text
    assert "  workflow_dispatch:" in text
    assert "github.event.label.name == 'copse'" in text
    assert "apt-get install -yq tmux" in text
    assert "uv tool install copse-agents" in text
    assert "npm install -g @anthropic-ai/claude-code" in text
    assert "COPSE_PRO_TOKEN: ${{ secrets.COPSE_PRO_TOKEN }}" in text
    assert "copse ci run --issue ${{ github.event.issue.number || inputs.issue }}" in text
    assert "{{{{" not in text and "}}}}" not in text
    # Every line is a YAML-shaped line: a comment, blank, or `key:` / `- item` text.
    for line in text.splitlines():
        assert not line.strip() or line.lstrip().startswith(("#", "-")) or ":" in line or \
            line.startswith("          "), line

    with pytest.raises(ci.CIError, match="--force"):
        ci.init(repo)
    assert "'copse'" in path.read_text()
    ci.init(repo, label="agent", force=True)
    assert "github.event.label.name == 'agent'" in path.read_text()
    with pytest.raises(ci.CIError, match="label"):
        ci.init(repo, label="bad\nlabel", force=True)


def test_cli_init(repo, monkeypatch):
    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["ci", "init"])
    assert res.exit_code == 0, res.output
    assert "wrote" in res.output and (repo / ".github" / "workflows" / "copse.yml").exists()
    res = CliRunner().invoke(app, ["ci", "init"])
    assert res.exit_code == 1 and "--force" in res.output
    res = CliRunner().invoke(app, ["ci", "init", "--force", "--label", "copse-please"])
    assert res.exit_code == 0, res.output
    assert "copse-please" in (repo / ".github" / "workflows" / "copse.yml").read_text()


def test_real_seams_exist():
    """The fakes replace real functions with these names and shapes."""
    for name in ("_spawn", "_alive", "_stop", "_push", "_create_pr", "_gh_json", "_checkout"):
        assert callable(getattr(ci, name)), name
    assert workspaces.create and pilot.set_goal
