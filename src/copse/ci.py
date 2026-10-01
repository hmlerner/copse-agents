"""Copse-CI: copse with nobody at a terminal (copse Team).

``copse ci run`` turns a goal (typed, read from a file, or a GitHub issue)
into a pull request: it cuts a fresh branch, starts a supervisor with
autopilot on in a detached tmux session, gives it the goal, and polls the
autopilot state in the DB until every milestone is verified, the supervisor
needs a person (``need_user``: the run fails with the question), it stalls,
or the time runs out. On success the branch is pushed and ``gh pr create``
opens the pull request. Whatever happens, the session and its workers are
stopped at the end, and a JSON summary is appended to ``$GITHUB_STEP_SUMMARY``
when that is set (GitHub Actions).

``copse ci init`` writes the workflow that runs it when an issue gets a label.

Entitlement: ``ci`` must be in the copse Pro entitlement. In CI there is no
keychain and no browser, so ``COPSE_PRO_TOKEN`` holds an org CI token
(``cpc_...``, from ``copse account org ci-token create``). Each run presents
it to ``POST /ci/entitlement`` and verifies the entitlement it gets back, in
memory; nothing is written to disk. CI tokens don't rotate, so the same
secret works on every run until an admin revokes it or the org's plan lapses.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from copse import agents, git, workspaces
from copse.config import CONFIG_DIR, LOCAL_CONFIG_FILE, config_root
from copse.db import DB, Agent, Workspace

CI_FEATURE = "ci"
TOKEN_ENV = "COPSE_PRO_TOKEN"
CI_TOKEN_PREFIX = "cpc_"
BRANCH_PREFIX = "copse/ci-"
DEFAULT_TIMEOUT_MIN = 60
POLL_SECONDS = 10.0
WORKFLOW_PATH = Path(".github") / "workflows" / "copse.yml"
MAX_SLUG = 40


class CIError(RuntimeError):
    """The run couldn't start, or a step the run depends on failed."""


# -- the goal ---------------------------------------------------------------------


@dataclass
class Goal:
    title: str
    detail: str | None = None
    issue: int | None = None
    source: str = "goal"           # goal | file | issue

    @property
    def slug(self) -> str:
        return git.slug(self.title)[:MAX_SLUG].strip("-") or "goal"

    @property
    def branch(self) -> str:
        return f"{BRANCH_PREFIX}{self.issue if self.issue is not None else self.slug}"

    def plan(self):
        """The milestones when ``detail`` is goals.md-shaped, else None."""
        from copse import autopilot as pilot

        plan = pilot.parse_goals(self.detail) if self.detail else None
        return plan if plan and plan.milestones else None


def _gh_json(args: list[str], cwd: str) -> dict:
    proc = subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise CIError(f"gh {' '.join(args[:2])} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    try:
        data = json.loads(proc.stdout)
    except ValueError as e:
        raise CIError(f"gh {' '.join(args[:2])} returned no JSON") from e
    if not isinstance(data, dict):
        raise CIError(f"gh {' '.join(args[:2])} returned an unexpected shape")
    return data


def goal_from_issue(number: int, cwd: str) -> Goal:
    """The issue's title and body, through ``gh issue view``."""
    data = _gh_json(["issue", "view", str(number), "--json", "number,title,body"], cwd)
    title = str(data.get("title") or "").strip()
    if not title:
        raise CIError(f"issue #{number} has no title")
    body = str(data.get("body") or "").strip() or None
    return Goal(title, body, issue=number, source="issue")


def goal_from_text(text: str) -> Goal:
    """A typed goal: its first line is the title; a goals.md-shaped text
    (``# Goal`` with ``## Milestone`` sections) keeps its shape in ``detail``
    so the supervisor gets the milestones."""
    from copse import autopilot as pilot

    text = text.strip()
    if not text:
        raise CIError("the goal is empty")
    plan = pilot.parse_goals(text)
    if plan:
        return Goal(plan.goal, text, source="goal")
    title, _, rest = text.partition("\n")
    return Goal(title.strip(), rest.strip() or None, source="goal")


def goal_from_file(path: str | Path) -> Goal:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise CIError(f"cannot read goal file {path}: {e.strerror}") from e
    goal = goal_from_text(text)
    goal.source = "file"
    return goal


# -- entitlement ------------------------------------------------------------------


def entitlement_from_token(token: str, client=None, now: float | None = None):
    """Exchange an org CI token (``cpc_...``) for a verified entitlement,
    without storing anything. Refresh tokens are refused: they rotate, and
    the backend treats a reused one as stolen, so a CI secret would work once."""
    from copse.pro import auth, license

    if not token.startswith(CI_TOKEN_PREFIX):
        raise auth.AuthError(f"{TOKEN_ENV} is not a CI token", code="not_a_ci_token")
    client = client or auth.Client()
    status, body = client.call("POST", "/ci/entitlement", None, token=token)
    if status != 200:
        raise auth._error(status, body)
    tok = body.get("entitlement")
    if not isinstance(tok, str):
        raise auth.AuthError("backend returned no entitlement", code="bad_response")
    return license.verify(tok, issuer=client.base, now=time.time() if now is None else now, grace=0)


def require_ci(client=None):
    """The entitlement, which must include ``ci``: from ``COPSE_PRO_TOKEN``
    when set (memory only), else the stored copse Pro credentials."""
    from copse.pro import auth, license

    token = os.environ.get(TOKEN_ENV, "").strip()
    try:
        if token:
            ent = entitlement_from_token(token, client)
        else:
            ent = license.current(client=client) if client is not None else license.current()
    except auth.AuthError as e:
        raise CIError(f"copse ci: {TOKEN_ENV} was refused ({e.code}); set it to a CI token "
                      "from `copse account org ci-token create`") from e
    except license.LicenseError as e:
        raise CIError(f"copse ci: {e}. In CI, set {TOKEN_ENV} to a CI token from "
                      "`copse account org ci-token create`.") from e
    if CI_FEATURE not in ent.features:
        raise CIError(f"copse ci needs copse Team: your plan ({ent.plan}) does not include "
                      f"{CI_FEATURE!r}. Plans: https://pawdelta.com/copse#pricing")
    return ent


# -- the run ----------------------------------------------------------------------


@dataclass
class Outcome:
    status: str                     # done | need_user | timeout | stalled | exited | error
    goal: Goal
    branch: str
    milestones: list[dict] = field(default_factory=list)
    note: str | None = None         # the question, the stall reason, the error
    pr_url: str | None = None
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "done" and (self.pr_url is not None or self.note is None)

    def summary(self) -> dict:
        return {
            "status": self.status, "ok": self.ok, "goal": self.goal.title,
            "issue": self.goal.issue, "branch": self.branch, "pr_url": self.pr_url,
            "note": self.note, "elapsed_seconds": round(self.elapsed),
            "milestones": self.milestones,
        }

    def describe(self) -> str:
        lines = [f"copse ci: {self.status}: {self.goal.title}"]
        if self.milestones:
            done = sum(m["status"] == "passed" for m in self.milestones)
            lines.append(f"  {done} of {len(self.milestones)} milestones verified")
            for m in self.milestones:
                mark = {"passed": "✓", "failed": "✗"}.get(m["status"], "○")
                check = f" (check: {m['check']})" if m.get("check") else ""
                lines.append(f"  {mark} {m['title']}{check}")
        if self.note:
            lines.append(f"  {self.note}")
        if self.pr_url:
            lines.append(f"  pull request: {self.pr_url}")
        return "\n".join(lines)


UNATTENDED = """[copse ci] This session runs unattended in CI: nobody is watching this chat. \
A question to the user (need_user) ends the run as a failure, so make the \
decisions you can yourself and prefer small, reviewable changes. When every \
milestone is verified, stop: copse opens the pull request from this branch.

Goal{where}: {title}
{detail}
{instruction}"""

DERIVE = ("Call set_goal now with this goal and the milestones you derive from it, each "
          "with a check command that verifies it (tests you add count), then drive it to "
          "completion: delegate, review, merge, check_milestone.")
RECORDED = ("The goal and its milestones are already recorded (get_progress shows them): "
            "drive them to completion: delegate, review, merge, check_milestone.")


def kickoff(goal: Goal, recorded: bool) -> str:
    where = f" (from issue #{goal.issue})" if goal.issue is not None else ""
    detail = f"\n{goal.detail}\n" if goal.detail and not recorded else ""
    return UNATTENDED.format(where=where, title=goal.title, detail=detail,
                             instruction=RECORDED if recorded else DERIVE)


def _checkout(db: DB, repo_path: str, branch: str, base: str | None) -> Workspace:
    """The worktree for ``branch``: a fresh one cut from ``base`` (default:
    the repo's base), or the existing one on a re-run."""
    repo_root = git.main_repo_root(repo_path)
    existing = git.worktree_for_branch(repo_root, branch)
    if existing:
        return workspaces.adopt_root(db, existing)
    return workspaces.create(db, repo_path, branch, base, apply_prefix=False).workspace


def _spawn(db: DB, ws: Workspace, prompt: str) -> Agent:
    return agents.spawn(db, ws, "supervisor", prompt=prompt, watch_pane=False,
                        background_setup=True, autopilot=True)


def _alive(db: DB, root_id: str) -> bool:
    a = db.get_agent(root_id)
    return a is not None and agents.is_alive(a)


def _stop(db: DB, root_id: str) -> None:
    """Stop the supervisor and every worker it started; their work stays."""
    agents.pause(db, root_id)


def _push(ws: Workspace) -> None:
    git.push(ws.path, ws.branch)


def _create_pr(ws: Workspace, base: str, title: str, body: str) -> str:
    proc = subprocess.run(
        ["gh", "pr", "create", "--base", base, "--head", ws.branch, "--title", title, "--body", body],
        cwd=ws.path, capture_output=True, text=True)
    if proc.returncode != 0:
        raise CIError(f"gh pr create failed: {proc.stderr.strip() or proc.stdout.strip()}")
    url = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    if not url:
        raise CIError("gh pr create printed no URL")
    return url


def set_max_workers(repo_root: str, n: int) -> Path:
    """Cap this repo's parallel workers through ``.copse/config.local.json``
    (gitignored; other keys are kept)."""
    base = config_root(repo_root) / CONFIG_DIR
    base.mkdir(parents=True, exist_ok=True)
    path = base / LOCAL_CONFIG_FILE
    data: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            data = loaded if isinstance(loaded, dict) else {}
        except ValueError:
            data = {}
    data["max_agents"] = n
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    ignore = base / ".gitignore"
    if not ignore.exists():
        ignore.write_text(f"{LOCAL_CONFIG_FILE}\n", encoding="utf-8")
    return path


def milestone_rows(db: DB, root_id: str) -> list[dict]:
    return [{"position": m.position, "title": m.title, "check": m.check_cmd, "status": m.status}
            for m in db.milestones(root_id)]


def pr_body(outcome: Outcome) -> str:
    g = outcome.goal
    lines = ["## Goal", "", g.title]
    if g.detail and not g.plan():
        lines += ["", g.detail]
    lines += ["", "## Milestones", ""]
    for m in outcome.milestones:
        box = "x" if m["status"] == "passed" else " "
        check = f" (`{m['check']}`)" if m.get("check") else ""
        lines.append(f"- [{box}] {m['title']}{check}")
    if g.issue is not None:
        lines += ["", f"Closes #{g.issue}"]
    lines += ["", "🤖 Opened by `copse ci`"]
    return "\n".join(lines) + "\n"


def _poll(db: DB, root_id: str) -> tuple[str, str | None] | None:
    """The run's terminal state from the autopilot row, or None to keep waiting."""
    ap = db.get_autopilot(root_id)
    ms = db.milestones(root_id)
    if ap is not None and ap.goal and ms and all(m.status == "passed" for m in ms):
        return "done", None
    if ap is not None and ap.state == "done" and ms:
        return "done", None
    if ap is not None and ap.state == "blocked":
        return "need_user", ap.note or "the supervisor asked for a decision"
    if ap is not None and ap.state == "stalled":
        return "stalled", ap.note or "no progress"
    if ap is not None and ap.state == "usage_paused":
        return "stalled", ap.note or "paused for usage: nothing will happen in this run"
    if not _alive(db, root_id):
        return "exited", "the supervisor exited before the goal was verified"
    return None


def run(db: DB, repo_path: str, goal: Goal, *, timeout_min: float = DEFAULT_TIMEOUT_MIN,
        max_workers: int | None = None, base: str | None = None, pr: bool = True,
        poll_seconds: float = POLL_SECONDS, clock=time.time, sleep=time.sleep) -> Outcome:
    """Run ``goal`` to a verified end (or not) and, with ``pr``, open the pull
    request. The session is always stopped before this returns."""
    from copse import autopilot as pilot

    if timeout_min <= 0:
        raise CIError("--timeout must be a positive number of minutes")
    branch = goal.branch
    ws = _checkout(db, repo_path, branch, base)
    base_branch = base or ws.base_branch or git.default_branch(ws.repo_root)
    if max_workers is not None:
        set_max_workers(ws.repo_root, max_workers)
    plan = goal.plan()
    root = _spawn(db, ws, kickoff(goal, plan is not None))
    db.add_autopilot(root.id)
    if plan is not None:
        pilot.set_goal(db, root.id, plan.goal, plan.milestones, plan.detail)
    started = clock()
    deadline = started + timeout_min * 60
    outcome = Outcome("error", goal, branch)
    try:
        while True:
            found = _poll(db, root.id)
            if found:
                outcome.status, outcome.note = found
                break
            if clock() >= deadline:
                outcome.status = "timeout"
                outcome.note = f"not finished after {timeout_min:g} minutes"
                break
            sleep(min(poll_seconds, max(0.0, deadline - clock())))
    except BaseException as e:  # the session must stop even on Ctrl-C or a bug
        outcome.status, outcome.note = "error", f"{type(e).__name__}: {e}"
        raise
    finally:
        outcome.milestones = milestone_rows(db, root.id)
        outcome.elapsed = clock() - started
        try:
            _stop(db, root.id)
        except Exception as e:  # noqa: BLE001 - the outcome matters more than the stop
            outcome.note = (outcome.note + "; " if outcome.note else "") + f"stopping the session failed: {e}"
    if outcome.status == "done" and pr:
        try:
            _push(ws)
            outcome.pr_url = _create_pr(ws, base_branch, goal.title, pr_body(outcome))
        except (git.GitError, CIError) as e:
            outcome.note = str(e)
    return outcome


def write_step_summary(outcome: Outcome, path: str | None = None) -> Path | None:
    """Append the JSON summary to ``$GITHUB_STEP_SUMMARY`` (or ``path``)."""
    path = path or os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return None
    text = (f"## copse ci: {outcome.status}\n\n```json\n"
            f"{json.dumps(outcome.summary(), indent=2)}\n```\n")
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)
    return Path(path)


def resolve_goal(goal: str | None, goal_file: str | None, issue: int | None, cwd: str) -> Goal:
    given = sum(x is not None for x in (goal, goal_file, issue))
    if given != 1:
        raise CIError("give exactly one of --goal TEXT, --goal-file PATH or --issue N")
    if issue is not None:
        return goal_from_issue(issue, cwd)
    if goal_file is not None:
        return goal_from_file(goal_file)
    assert goal is not None
    return goal_from_text(goal)


def run_cli(*, goal: str | None, goal_file: str | None, issue: int | None,
            timeout_min: float, max_workers: int | None, base: str | None, pr: bool,
            echo=print, cwd: str | None = None) -> int:
    """``copse ci run``: 0 on a verified goal (and its PR), 1 otherwise."""
    cwd = cwd or os.getcwd()
    try:
        g = resolve_goal(goal, goal_file, issue, cwd)
        require_ci()
        db = DB()
        outcome = run(db, cwd, g, timeout_min=timeout_min, max_workers=max_workers,
                      base=base, pr=pr)
    except (CIError, git.GitError, workspaces.WorkspaceError, agents.AgentError) as e:
        echo(str(e))
        return 1
    echo(outcome.describe())
    if outcome.pr_url:
        echo(outcome.pr_url)
    write_step_summary(outcome)
    return 0 if outcome.ok else 1


# -- the workflow -----------------------------------------------------------------


WORKFLOW = """\
# Written by `copse ci init`. copse turns an issue labelled "{label}" into a
# pull request: https://github.com/hmlerner/copse-agents#copse-ci-issues-into-pull-requests
#
# Secrets: COPSE_PRO_TOKEN (an org CI token, cpc_..., from
# `copse account org ci-token create`) and ANTHROPIC_API_KEY (for Claude Code).
# In the repo's Actions settings, allow GitHub Actions to create pull requests.
#
# The issue body steers an unattended agent that can push (contents: write):
# only people you trust with write access should be able to apply the label.
# Pull requests opened with GITHUB_TOKEN don't trigger other workflows; to run
# your CI on them, set GH_TOKEN to a GitHub App or personal access token.
name: copse

on:
  issues:
    types: [labeled]
  workflow_dispatch:
    inputs:
      issue:
        description: Issue number to work on
        required: true
        type: number

permissions:
  contents: write
  issues: read
  pull-requests: write

concurrency:
  group: copse-${{{{ github.event.issue.number || inputs.issue }}}}
  cancel-in-progress: false

jobs:
  copse:
    if: github.event_name == 'workflow_dispatch' || github.event.label.name == '{label}'
    runs-on: ubuntu-latest
    timeout-minutes: 120
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0
      - name: Install tmux
        run: sudo apt-get update -q && sudo apt-get install -yq tmux
      - uses: astral-sh/setup-uv@v10.2.0
      - name: Install copse
        run: uv tool install copse-agents
      - name: Install the agent CLI
        run: npm install -g @anthropic-ai/claude-code
      - name: Git identity for the agents' commits
        run: |
          git config --global user.name "copse[bot]"
          git config --global user.email "copse@users.noreply.github.com"
      - name: Run copse
        env:
          COPSE_PRO_TOKEN: ${{{{ secrets.COPSE_PRO_TOKEN }}}}
          ANTHROPIC_API_KEY: ${{{{ secrets.ANTHROPIC_API_KEY }}}}
          GH_TOKEN: ${{{{ secrets.GITHUB_TOKEN }}}}
        run: copse ci run --issue ${{{{ github.event.issue.number || inputs.issue }}}} --timeout 100
"""


def workflow_text(label: str = "copse") -> str:
    if not re.fullmatch(r"[A-Za-z0-9 _.:/-]{1,50}", label):
        raise CIError("the label may use letters, digits, spaces and _ . : / -")
    return WORKFLOW.format(label=label)


def init(repo_root: str | Path, label: str = "copse", force: bool = False) -> Path:
    """Write ``.github/workflows/copse.yml``; refuses to overwrite without ``force``."""
    path = Path(repo_root) / WORKFLOW_PATH
    if path.exists() and not force:
        raise CIError(f"{path} exists; pass --force to overwrite it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(workflow_text(label), encoding="utf-8")
    return path
