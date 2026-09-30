"""Autopilot: the supervisor as a project manager that keeps going.

A session with autopilot on has a goal broken into milestones. Each milestone
has a check command that copse runs itself (exit 0 means done), so progress
is verified rather than claimed. The supervisor splits milestones into tasks,
hands them to workers, gets their branches reviewed and merged (through the
merge gates in ``gates``), and re-runs the checks.

When the supervisor stops while milestones are still unverified and no worker
is running, its Stop hook tells it to keep going. It stops pushing when:
- every milestone's check passes (the goal is done),
- the supervisor calls ``need_user`` (blocked on a decision only the user can
  make), until the user next types something,
- it has nudged MAX_NUDGES times in a row with no progress (stalled), or
- Claude usage is near its limit.

Goals come from the chat (the supervisor calls ``set_goal``) or from
``.copse/goals.md``, loaded when the session starts:

    # Settings page

    Users can change their name and email.

    ## Settings API
    check: uv run pytest tests/test_settings_api.py -q

    ## Settings UI
    check: npm test -- settings
    The form saves and shows errors inline.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from copse.config import CONFIG_DIR, RepoConfig, config_root, copse_home, load_repo_config
from copse.db import DB, Agent, Autopilot, Milestone, Workspace
from copse.profiles import load_profile

GOALS_FILE = "goals.md"
MAX_NUDGES = 3
MAX_GOAL_CHARS = 4000        # Claude Code's limit for a /goal condition
OUTPUT_TAIL_LINES = 30
USAGE_FRESH_SECONDS = 15 * 60

GUIDE = """

## Autopilot is on

You are this project's manager. Drive the goal to completion without waiting
to be asked each step.

- Size first. A request you can finish yourself in one sitting (a fix, a
  change within one area) is not a goal: do it directly, run the targeted
  tests, and report. No `set_goal`, no workers. Use the rest of this guide
  only for multi-part work that benefits from milestones and parallel workers.
- Within a goal, do a milestone's tasks yourself when they're small or you
  already have the context; `assign` workers only for independent pieces that
  can run in parallel or run long.
- The goal: if none is set yet, ask the user what we're building. Turn the
  answer into a goal with 2-6 milestones, each with a `check` command copse can
  run from the root of this checkout that exits 0 only when that milestone is
  done (e.g. `uv run pytest tests/test_settings.py -q`). Record it with
  `set_goal`, tell the user the plan in a few lines, then start. If
  `.copse/goals.md` exists, copse has already loaded it: call `get_progress`.
- Work on the first unverified milestone. Split it into independent tasks and
  `assign` them to workers in parallel (at most {max_agents} at once). Give
  every task a `done_when` finish line the worker can verify itself. Pass
  `files` (the paths/globs each task will touch) so copse can warn about
  overlaps, and `depends_on` (an earlier task's agent id or branch) when one
  task's work must merge before another starts; copse queues it until then.
- Keep task briefs short: the goal in a sentence or two, the files, and the
  test that proves it done. Workers read the tests and code themselves;
  never paste them. Writing is the slowest thing you do.
- When a worker reports, copse has its branch reviewed and, once approved and
  the checks pass, merges it and removes the worktree; you get one message
  per branch: merged, or "needs you" with the details. Don't request_review
  or merge_workspace a reported branch yourself unless copse says so.
- After merging, call `check_milestone`. Only copse's check marks a milestone
  done: never claim one is done yourself.
- Keep going until every milestone passes. If you stop early, copse will ask
  you to continue. Stop only when you are blocked on something only the user
  can decide: call `need_user` with the question, then ask it.
- When a milestone passes, tell the user in one line.
- If a message says autopilot is off, stop driving: wait for the user's
  instructions.
"""

KICKOFF = (
    "[copse autopilot] The goal in .copse/goals.md is loaded: {goal} ({n} milestones). "
    "Call get_progress, tell me your plan in a few lines, then start working toward it."
)


class AutopilotError(RuntimeError):
    pass


# -- goals.md ---------------------------------------------------------------


@dataclass
class Plan:
    goal: str
    detail: str | None
    milestones: list[tuple]   # (title, check, detail[, profile])


def parse_goals(text: str) -> Plan | None:
    """``# Goal`` then ``## Milestone`` sections, each with an optional
    ``check: <command>`` line and ``profile: <name>`` line. Returns None when
    there's no goal heading."""
    goal, detail_lines = None, []
    milestones: list[list] = []
    for line in text.splitlines():
        if m := re.match(r"^#\s+(.+?)\s*$", line):
            if goal is None:
                goal = m.group(1)
            continue
        if m := re.match(r"^##\s+(.+?)\s*$", line):
            milestones.append([m.group(1), None, [], None])
            continue
        if milestones and STATUS_RE.match(line):
            continue   # written by sync_goals_file: information only, never read back
        if milestones and milestones[-1][3] is None and (
            m := re.match(r"^\s*profile:\s*`?([\w.-]+)`?\s*$", line, re.I)
        ):
            milestones[-1][3] = m.group(1)
            continue
        if milestones and milestones[-1][1] is None and (
            m := re.match(r"^\s*check:\s*`?(.+?)`?\s*$", line, re.I)
        ):
            milestones[-1][1] = m.group(1)
            continue
        (milestones[-1][2] if milestones else detail_lines).append(line)
    if not goal:
        return None
    clean = lambda lines: "\n".join(lines).strip() or None  # noqa: E731
    return Plan(goal, clean(detail_lines),
                [(t, c, clean(d), *([p] if p else [])) for t, c, d, p in milestones])


def goals_path(root: str) -> Path:
    # config_root: in a linked worktree the git-ignored .copse lives in the
    # main checkout, and that is the file the session loads (and syncs to).
    return config_root(root) / CONFIG_DIR / GOALS_FILE


def load_goals_file(root: str) -> Plan | None:
    path = goals_path(root)
    if not path.is_file():
        return None
    return parse_goals(path.read_text(encoding="utf-8"))


# A status line is what sync_goals_file writes under each milestone. The
# format is strict so user prose is never mistaken for one.
STATUS_RE = re.compile(r"^status: (passed|failed|pending)( at [0-9a-f]{7,40})?( \(\d{4}-\d{2}-\d{2}\))?\s*$")


def status_line(m: Milestone) -> str:
    """``status: passed at abc1234 (2026-09-29)``: only the state, a short sha
    and a date, nothing else about the session."""
    if m.status not in ("passed", "failed"):
        return "status: pending"
    sha = (m.passed_sha if m.status == "passed" else None) or m.checked_sha
    out = f"status: {m.status}"
    if sha and re.fullmatch(r"[0-9a-f]{7,40}", sha):
        out += f" at {sha[:7]}"
    if m.checked_at:
        out += time.strftime(" (%Y-%m-%d)", time.localtime(m.checked_at))
    return out


def rewrite_goals(text: str, lines_for: list[str]) -> str | None:
    """``text`` with one status line per milestone (``lines_for``, in order),
    the rest byte for byte. None when the file's milestones don't number
    ``len(lines_for)``."""
    lines = text.splitlines(keepends=True)
    sections: list[list[int]] = []   # line indexes per milestone, heading first
    for i, line in enumerate(lines):
        if re.match(r"^##\s+(.+?)\s*$", line.rstrip("\r\n")):
            sections.append([i])
        elif sections:
            sections[-1].append(i)
    if len(sections) != len(lines_for):
        return None
    out = lines[:sections[0][0]] if sections else lines
    for idx, new in zip(sections, lines_for):
        body = [lines[i] for i in idx]
        eol = "\r\n" if body[0].endswith("\r\n") else "\n"
        placed, anchor, have_profile, have_check = False, 0, False, False
        kept: list[str] = []
        for j, line in enumerate(body):
            bare = line.rstrip("\r\n")
            if j and STATUS_RE.match(bare):
                if not placed:
                    kept.append(new + (line[len(bare):] or eol))
                    placed = True
                continue
            kept.append(line)
            if j and not have_profile and re.match(r"^\s*profile:\s*`?([\w.-]+)`?\s*$", bare, re.I):
                have_profile, anchor = True, len(kept)
            elif j and not have_check and re.match(r"^\s*check:\s*`?(.+?)`?\s*$", bare, re.I):
                have_check, anchor = True, len(kept)
        if not placed:
            anchor = anchor or 1
            if not kept[anchor - 1].endswith("\n"):
                kept[anchor - 1] += eol
            kept.insert(anchor, new + eol)
        out += kept
    return "".join(out)


def sync_goals_file(db: DB, root_id: str) -> None:
    """Write each milestone's status back to the goals.md this session's goal
    was loaded from. goals.md may be committed, so what goes in it is only
    passed/failed/pending, a short sha and a date; loading it never trusts a
    status line. Does nothing for a session that didn't load its goal from a
    file, has autopilot off (a handover switches the old session's off),
    is paused, or whose milestones no longer match the file. Never raises: a
    file we can't write must not fail a check."""
    try:
        ap = db.get_autopilot(root_id)
        root = db.get_agent(root_id)
        if not ap or not ap.enabled or not ap.goals_file or not ap.goal or root is None \
                or root.status in ("paused", "done"):
            return
        path = Path(ap.goals_file)
        text = path.read_bytes().decode("utf-8")
        plan = parse_goals(text)
        ms = db.milestones(root_id)
        if plan is None or plan.goal != ap.goal or [m[0] for m in plan.milestones] != [m.title for m in ms]:
            return   # the goal was replaced from the chat: the file is no longer ours
        new = rewrite_goals(text, [status_line(m) for m in ms])
        if new is None or new == text:
            return
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_bytes(new.encode("utf-8"))
            os.chmod(tmp, path.stat().st_mode & 0o7777)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


# -- sessions ----------------------------------------------------------------


def root_of(db: DB, agent_id: str) -> str:
    """The session an agent belongs to: its topmost ancestor."""
    seen = set()
    agent = db.get_agent(agent_id)
    while agent and agent.parent_id and agent.parent_id not in seen:
        seen.add(agent.id)
        parent = db.get_agent(agent.parent_id)
        if parent is None:
            break
        agent = parent
    return agent.id if agent else agent_id


def for_agent(db: DB, agent_id: str) -> Autopilot | None:
    """The autopilot of the session ``agent_id`` belongs to, if it has one."""
    return db.get_autopilot(root_of(db, agent_id))


def enable(db: DB, root_id: str, ws: Workspace) -> Plan | None:
    """Turn autopilot on for a new session. Loads ``.copse/goals.md`` if the
    checkout has one, and returns it."""
    db.add_autopilot(root_id)
    plan = load_goals_file(ws.path)
    if plan:
        set_goal(db, root_id, plan.goal, plan.milestones, plan.detail)
        # Recorded after set_goal, so loading a file never rewrites it: the
        # first write is after a check. Every milestone starts pending.
        db.update_autopilot(root_id, goals_file=str(goals_path(ws.path)))
    return plan


def set_enabled(db: DB, root_id: str, on: bool) -> None:
    db.add_autopilot(root_id, enabled=on)
    fields: dict[str, object] = {"enabled": int(on), "nudges": 0}
    ap = db.get_autopilot(root_id)
    if on and ap and ap.state in ("blocked", "stalled"):
        fields.update(state="running", note=None)
    db.update_autopilot(root_id, **fields)


def set_goal(db: DB, root_id: str, goal: str,
             milestones: list[tuple], detail: str | None = None) -> None:
    """Record the goal and its milestones, ``(title, check, detail[, profile])``.
    A milestone that keeps its title and check keeps its last result."""
    if not goal.strip():
        raise AutopilotError("the goal needs a title")
    if not milestones:
        raise AutopilotError("give at least one milestone")
    old = {(m.title, m.check_cmd): m for m in db.milestones(root_id)}
    db.update_autopilot(root_id, goal=goal.strip(), detail=detail, state="running", note=None)
    db.set_milestones(root_id, milestones)
    for m in db.milestones(root_id):
        prev = old.get((m.title, m.check_cmd))
        if prev and prev.status != "pending":
            db.record_check(m.id, prev.status == "passed", prev.output or "", prev.checked_sha,
                            passed_sha=prev.passed_sha)
    db.bump_progress(root_id)
    sync_goals_file(db, root_id)


def choose_profile(db: DB, caller_id: str, repo_root: str, requested: str | None = None,
                   task: str | None = None, files: list[str] | None = None) -> tuple[str, bool]:
    """The worker profile for a delegation, and whether learning chose it:
    ``requested`` if given, else the first unverified milestone's profile in
    the caller's session, else (with a learning plugin selected) the plugin's
    pick for this task, else the repo's ``default_agent``. Raises
    AutopilotError if it doesn't exist."""
    name = (requested or "").strip()
    learned = False
    if not name:
        pending = next((m for m in db.milestones(root_of(db, caller_id)) if m.status != "passed"), None)
        name = (pending.profile if pending else None) or ""
    if not name:
        from copse import learning

        cfg = load_repo_config(repo_root)
        name = learning.choose(db, cfg, repo_root, task, files) or ""
        learned = bool(name)
        name = name or cfg.default_agent
    try:
        load_profile(name, repo_root)
    except KeyError:
        raise AutopilotError(
            f"no agent profile named {name!r}; see list_agent_profiles, "
            "or pass agent_profile explicitly"
        ) from None
    return name, learned


def resolve_profile(db: DB, caller_id: str, repo_root: str, requested: str | None = None,
                    task: str | None = None, files: list[str] | None = None) -> str:
    """``choose_profile`` without the learned flag."""
    return choose_profile(db, caller_id, repo_root, requested, task, files)[0]


def need_user(db: DB, root_id: str, question: str) -> None:
    db.update_autopilot(root_id, state="blocked", note=question.strip()[:500], nudges=0)


def user_spoke(db: DB, agent: Agent) -> None:
    """The user typed into the supervisor's chat: whatever blocked autopilot
    is theirs to have answered, so it may drive again."""
    ap = db.get_autopilot(agent.id)
    if ap and ap.state in ("blocked", "stalled"):
        db.update_autopilot(agent.id, state="running", note=None, nudges=0)
    elif ap:
        db.update_autopilot(agent.id, nudges=0)


# -- checks ------------------------------------------------------------------


def tail(text: str, lines: int = OUTPUT_TAIL_LINES) -> str:
    out = text.rstrip().splitlines()
    return "\n".join(out[-lines:])


def run_check(cmd: str, cwd: str, env: dict[str, str], timeout: int) -> tuple[bool, str]:
    """Run one check command. Returns (passed, the tail of its output)."""
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=cwd, env={**os.environ, **env}, capture_output=True,
            text=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return False, f"$ {cmd}\n(timed out after {timeout}s)"
    output = tail((proc.stdout or "") + (proc.stderr or ""))
    status = "" if proc.returncode == 0 else f"(exit {proc.returncode})"
    return proc.returncode == 0, "\n".join(p for p in (f"$ {cmd}", output, status) if p)


def checking(ap: Autopilot, timeout: float = 900.0) -> bool:
    """Whether a background milestone check is still running."""
    return bool(ap.checking_since) and time.time() - ap.checking_since < timeout


def _detach(args: list[str]) -> None:
    """Start a copse helper that outlives this process."""
    subprocess.Popen(args, start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def check_in_background(db: DB, root_id: str, ws: Workspace, position: int | None) -> str:
    """Start the milestone checks detached and return at once. The result is
    delivered to the session root's inbox (see cli._check_milestones). A full
    suite can outrun an MCP call's time limit, and nothing should wait on it."""
    from copse.providers import copse_invocation

    ap = db.get_autopilot(root_id)
    if ap and checking(ap):
        return ("A milestone check is already running; its result arrives as a message. "
                "Carry on with other work meanwhile.")
    db.update_autopilot(root_id, checking_since=time.time())
    args = [*copse_invocation(), "_check-milestones", root_id, ws.id]
    if position is not None:
        args += ["--position", str(position)]
    _detach(args)
    which = f"milestone {position}" if position else "every milestone"
    return (f"Checking {which} in the background (the checks run in your checkout; results "
            "already recorded for this commit are reused). The result arrives as a message; "
            "carry on with other work meanwhile, and don't claim a milestone done until it does.")


def check_milestones(db: DB, root_id: str, ws: Workspace, position: int | None = None,
                     cfg: RepoConfig | None = None) -> str:
    """Run milestone checks in the supervisor's checkout (where merges land)
    and record the results. With ``position``, just that one milestone, then
    the other milestones currently marked passed are re-run too, to catch a
    merge that broke one of them while another is still in progress; those
    that passed at the checkout's current HEAD are skipped. Only a milestone
    newly passing, and not already passed at this commit, counts as progress,
    so a flaky check can't keep resetting the nudge limit."""
    from copse import git, workspaces

    cfg = cfg or load_repo_config(ws.repo_root)
    ms = db.milestones(root_id)
    if not ms:
        raise AutopilotError("no milestones yet: record the goal with set_goal first")
    chosen = [m for m in ms if position is None or m.position == position]
    if not chosen:
        raise AutopilotError(f"no milestone {position}; they're numbered 1-{len(ms)}")
    env = workspaces.workspace_env(ws)
    # The commit the checks ran against. Uncommitted changes could break a
    # check without moving HEAD, so a dirty checkout records no sha.
    try:
        head: str | None = None if git.dirty_files(ws.path) else git.out(["rev-parse", "HEAD"], ws.path)
    except git.GitError:
        head = None
    newly_passed = False

    def run(batch: list[Milestone]) -> None:
        nonlocal newly_passed
        from copse import gates

        for m in batch:
            if not m.check_cmd:
                continue
            # Cached by commit when the checkout is clean, so a check that
            # already passed at this HEAD (as a merge gate, say) isn't re-run.
            ok, out = gates.run_checked(db, ws, m.check_cmd, env, cfg.check_timeout)
            # Not if it already passed at this very commit: a flaky check
            # flipping back isn't progress.
            newly_passed |= ok and m.status != "passed" and (head is None or m.passed_sha != head)
            db.record_check(m.id, ok, out, head)

    run(chosen)
    ms = db.milestones(root_id)
    regressed: list[Milestone] = []
    if position is not None and len(ms) > 1:
        recheck = [m for m in ms if m.position != position and m.status == "passed"
                   and (head is None or m.checked_sha != head)]
        if recheck:
            was_passed = {m.id for m in recheck}
            run(recheck)
            ms = db.milestones(root_id)
            regressed = [m for m in ms if m.id in was_passed and m.status != "passed"]
    if newly_passed:
        db.bump_progress(root_id)
    done = all(m.status == "passed" for m in ms)
    if done:
        db.update_autopilot(root_id, state="done", note=None)
    elif (ap := db.get_autopilot(root_id)) and ap.state == "done":
        db.update_autopilot(root_id, state="running")
    sync_goals_file(db, root_id)
    regressed_ids = {m.id for m in regressed}
    shown = [m for m in ms if position is None or m.position == position or done or m.id in regressed_ids]
    lines = []
    if regressed:
        positions = ", ".join(str(m.position) for m in regressed)
        verb = "was passing but now fails" if len(regressed) == 1 else "were passing but now fail"
        lines.append(f"REGRESSED: milestone {positions} {verb}.")
    lines.append(progress(db, root_id))
    for m in shown:
        if m.output and m.status == "failed":
            lines.append(f"\nMilestone {m.position} check output:\n{m.output}")
    if done:
        lines.append("\nEvery milestone's check passes: the goal is reached. Tell the user.")
    return "\n".join(lines)


# -- progress -----------------------------------------------------------------

MARK = {"passed": "✓", "failed": "✗", "pending": "○"}


def counts(db: DB, root_id: str) -> tuple[int, int]:
    ms = db.milestones(root_id)
    return sum(m.status == "passed" for m in ms), len(ms)


def progress(db: DB, root_id: str) -> str:
    ap = db.get_autopilot(root_id)
    if ap is None:
        return "Autopilot is not set up for this session."
    head = f"Autopilot {'on' if ap.enabled else 'off'}"
    if not ap.goal:
        return f"{head}. No goal yet: ask the user what we're building, then call set_goal."
    done, total = counts(db, root_id)
    lines = [f"{head}. Goal: {ap.goal}", f"{done} of {total} milestones verified."]
    for m in db.milestones(root_id):
        how = f"check: `{m.check_cmd}`" if m.check_cmd else "NO CHECK: propose one with set_goal"
        when = f", last checked {time.strftime('%H:%M', time.localtime(m.checked_at))}" if m.checked_at else ""
        who = f", profile: {m.profile}" if m.profile else ""
        lines.append(f"  {MARK.get(m.status, '·')} {m.position}. {m.title} ({how}{who}{when})")
    if ap.state in ("blocked", "stalled") and ap.note:
        lines.append(f"{ap.state.capitalize()}: {ap.note}")
    return "\n".join(lines)


# -- workers and the cap ----------------------------------------------------------


BUSY = ("starting", "processing", "waiting")
IDLE_GRACE_SECONDS = 60  # how long a worker may sit idle and unreported before it's called out


def active_workers(db: DB, root_id: str, *, reviewers: bool = True) -> list[Agent]:
    """Agents in the session still working: not yet reported, or back at work
    after reporting (e.g. on review feedback). Includes workers stalled idle
    and unreported past the grace period; see ``stalled_workers`` and
    ``working_workers`` to tell those apart."""
    from copse import agents

    return [a for a in agents.tree(db, root_id)[1:]
            if a.mode in agents.REPORTING_MODES and (reviewers or a.mode != "review")
            and (a.result is None or a.status in BUSY or a.pipeline)
            and a.status not in ("paused", "done") and agents.is_alive(a)]


def split_workers(db: DB, root_id: str, *, screen: bool = False) -> tuple[list[Agent], list[Agent]]:
    """``active_workers`` split into (working, stalled). Stalled: idle without
    reporting a result for more than ``IDLE_GRACE_SECONDS``. Claude Code
    already reminded them once to call report_result (see agents.handle_hook's
    "stop" case); past the grace period they won't be reminded again on their
    own, so the supervisor has to check on them itself. Working: the rest,
    still expected to report on their own.

    Only workers whose provider reports idle through hooks can stall this way
    (Codex and shell workers never leave 'unknown'). A stale 'idle' can also
    outlive the turn after it, so with ``screen`` a worker with a screen to
    read is only stalled when the screen shows it idle too (its status is
    reconciled otherwise). That sleeps between samples, so only the Stop hook
    asks for it, and only for idle, unreported workers past the grace period.
    Without it (the dashboard), the status is taken as it stands."""
    from copse import agents
    from copse.providers import get_provider

    now = time.time()
    working, stalled = [], []
    for a in active_workers(db, root_id):
        maybe = (a.result is None and a.status == "idle" and a.plan_state != "proposed"
                 and get_provider(a.provider).uses_hooks
                 and now - (a.status_since or a.created_at) >= IDLE_GRACE_SECONDS)
        if maybe and (not screen or a.headless or agents.screen_status(db, a, samples=2) == "idle"):
            stalled.append(a)
        else:
            working.append(a)
    return working, stalled


def stalled_workers(db: DB, root_id: str) -> list[Agent]:
    """Active workers the supervisor has to check on; see ``split_workers``."""
    return split_workers(db, root_id)[1]


def working_workers(db: DB, root_id: str) -> list[Agent]:
    """Active workers still expected to report on their own; see ``split_workers``."""
    return split_workers(db, root_id)[0]


def check_capacity(db: DB, caller_id: str | None, cfg: RepoConfig) -> None:
    """Refuse a new worker beyond ``max_agents``. Reviewers don't count: they
    are short, and holding up a review would hold up every merge."""
    if not caller_id or cfg.max_agents <= 0:
        return
    busy = active_workers(db, root_of(db, caller_id), reviewers=False)
    if len(busy) >= cfg.max_agents:
        raise AutopilotError(
            f"{len(busy)} workers are already running, the most allowed at once "
            f"(max_agents in .copse/config.json). Wait for one to report, then try again."
        )


# -- Claude usage -------------------------------------------------------------------


def usage_path() -> Path:
    return copse_home() / "usage.json"


def record_usage(status: dict) -> None:
    """Keep the plan usage Claude Code gives its status line (Claude.ai
    subscriptions only)."""
    limits = status.get("rate_limits")
    if not isinstance(limits, dict):
        return
    data = {"updated_at": time.time()}
    for window in ("five_hour", "seven_day"):
        w = limits.get(window)
        if isinstance(w, dict) and isinstance(w.get("used_percentage"), (int, float)):
            data[window] = {"used": float(w["used_percentage"]), "resets_at": w.get("resets_at")}
    if len(data) > 1:
        path = usage_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)


def usage() -> dict | None:
    """The fullest recent usage window: {"window", "used", "resets_at"}, or None."""
    try:
        data = json.loads(usage_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if time.time() - data.get("updated_at", 0) > USAGE_FRESH_SECONDS:
        return None
    windows = [(k, v) for k, v in data.items() if isinstance(v, dict)]
    if not windows:
        return None
    name, w = max(windows, key=lambda kv: kv[1].get("used", 0))
    return {"window": name, "used": w.get("used", 0), "resets_at": w.get("resets_at")}


def usage_note(u: dict) -> str:
    window = "5-hour" if u["window"] == "five_hour" else "weekly"
    resets = ""
    if isinstance(u.get("resets_at"), (int, float)):
        resets = f", resets {time.strftime('%-I:%M%p', time.localtime(u['resets_at'])).lower()}"
    return f"Claude usage at {u['used']:.0f}% of the {window} limit{resets}"


def limit_reached(db: DB, agent: Agent) -> None:
    """A turn failed on the usage limit: stop pushing until the user is back."""
    ap = db.get_autopilot(root_of(db, agent.id))
    if ap and ap.enabled:
        db.update_autopilot(ap.root_id, state="blocked", nudges=0,
                            note="Claude's usage limit was reached. Continue when it resets.")


# -- the Stop hook -------------------------------------------------------------------


def on_stop(db: DB, agent: Agent, payload: dict) -> dict | None:
    """The supervisor is about to stop. Returns a "block" decision telling it
    to keep going, or None to let it stop."""
    ap = db.get_autopilot(agent.id)
    if ap is None or not ap.enabled or not ap.goal or ap.state != "running":
        return None
    ms = db.milestones(agent.id)
    if ms and all(m.status == "passed" for m in ms):
        db.update_autopilot(agent.id, state="done")
        return None
    from copse import agents

    if checking(ap):
        return None  # the check's result arrives as a message and wakes it up
    working, stalled = split_workers(db, agent.id, screen=True)
    # A worker waiting on plan approval waits on the supervisor, so the supervisor
    # can't stop for it (see nudge).
    if any(agents.runs_process(a) for a in working
            if getattr(a, "plan_state", None) != "proposed"):
        return None  # their results arrive as messages and wake it up
    ws = db.get_workspace(agent.workspace_id)
    cfg = load_repo_config(ws.repo_root) if ws else RepoConfig()
    u = usage()
    if u and u["used"] >= cfg.usage_limit:
        db.update_autopilot(agent.id, state="blocked", note=usage_note(u) + ". Autopilot paused so work doesn't stall halfway.")
        return None
    nudges = ap.nudges if ap.nudged_at == ap.progress else 0
    if nudges >= MAX_NUDGES:
        db.update_autopilot(agent.id, state="stalled", nudges=0,
                            note=f"no progress after {MAX_NUDGES} reminders to keep going")
        return None
    db.update_autopilot(agent.id, nudges=nudges + 1, nudged_at=ap.progress)
    return {"decision": "block", "reason": nudge(db, ap, cfg, working, stalled)}


def nudge(db: DB, ap: Autopilot, cfg: RepoConfig,
          working: list[Agent], stalled: list[Agent]) -> str:
    from copse import agents

    # Subagent workers send no message when done; the supervisor records them.
    open_subagents = [a.id for a in working if not agents.runs_process(a)]
    pending = ""
    if open_subagents:
        pending = ("Subagent work not yet recorded: when each of your subagents finishes, call "
                   f"complete_subagent for {', '.join(open_subagents)}.\n\n")
    planning = [a.id for a in working if getattr(a, "plan_state", None) == "proposed"]
    if planning:
        pending += (f"Plans awaiting your approval: {', '.join(planning)}. Read each plan "
                    "(it arrived as a message) and call approve_plan, with feedback and "
                    "approved=false to ask for changes; the worker is waiting on you.\n\n")
    stuck = ""
    if stalled:
        names = ", ".join(a.id for a in stalled)
        stuck = (
            f"{names} went idle without ever calling report_result and won't be reminded "
            "again on their own. Check on them: workspace_diff to see what they did, "
            "send_message asking them to report or finish, or remove_workspace if the work "
            "is abandoned.\n\n"
        )
    return (
        "[copse autopilot] The goal isn't reached yet, and no workers are running.\n"
        f"{progress(db, ap.root_id)}\n\n{pending}{stuck}"
        "Keep going without waiting for the user: plan the next tasks for the first "
        f"unverified milestone and assign workers (at most {cfg.max_agents or 'any number'} "
        "at once), get finished branches reviewed and merged, and call check_milestone. "
        "If you need a decision only the user can make, call need_user with the question, "
        "then ask it."
    )


def guide(cfg: RepoConfig) -> str:
    return GUIDE.format(max_agents=cfg.max_agents or "any number of")


def kickoff(plan: Plan) -> str:
    return KICKOFF.format(goal=plan.goal, n=len(plan.milestones))


def worker_goal(prompt: str, done_when: str, branch: str) -> str | None:
    """A worker's first message as a Claude Code ``/goal``: Claude keeps working
    until a small model judges the finish line met. None if too long for one."""
    text = (f"/goal Finish line: {done_when.strip()} The work is committed on branch "
            f"`{branch}` and reported with the copse report_result tool.\n\nTask:\n{prompt}")
    return text if len(text) - len("/goal ") <= MAX_GOAL_CHARS else None
