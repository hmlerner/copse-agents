"""The pipeline: review and merge a reported branch without the supervisor.

Every stage between a worker's report and its merge used to wait for a
supervisor turn: report, turn, review, verdict, turn, merge, turn, remove.
With a supervisor carrying millions of tokens of context, each turn is
seconds to minutes, and it sits on the critical path of every branch.

With ``pipeline`` on (the default), copse runs those stages itself:

1. A worker reports (``report_result``). copse starts the review at once
   and runs the repo's checks in the background.
2. The reviewer approves: copse merges (through the same gates
   ``merge_workspace`` uses), removes the worktree, and sends the supervisor
   one message: merged, with the worker's report and the review.
   The reviewer requests changes: copse sends the findings straight to the
   worker, which fixes them and reports again, back to step 1; after
   ``review_rounds`` rounds it hands the findings to the supervisor instead.
3. Anything the pipeline can't settle (a conflict, a failing check, no
   reviewer available) goes to the supervisor as "needs you", with the
   details.

The supervisor still plans, assigns, and answers the user; it no longer
relays between the worker, the reviewer and the merge. ``handoff`` workers
whose caller is waiting for the result are not piped: the caller gets the
result directly, as before.
"""

from __future__ import annotations

import os
import subprocess

from copse import agents, autopilot, codemap, gates, git, history, learning, tasks, workspaces
from copse.config import RepoConfig, load_repo_config
from copse.db import DB, Agent, Workspace

PIPED_MODES = ("assign", "handoff_detached")


def enabled(cfg: RepoConfig, worker: Agent, ws: Workspace) -> bool:
    return bool(cfg.pipeline) and worker.mode in PIPED_MODES and ws.kind == "worktree" \
        and bool(worker.parent_id)


def _detach(args: list[str]) -> None:
    subprocess.Popen(args, start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _tell(db: DB, parent_id: str | None, text: str, sender_id: str | None) -> None:
    if parent_id and db.get_agent(parent_id):
        try:
            agents.send_message(db, parent_id, text, sender_id=sender_id)
        except agents.AgentError:
            pass


def _note(db: DB, ws: Workspace, worker: Agent | None = None, **event) -> None:
    """Tell a learning plugin about ``ws``'s worker (a no-op unless the repo
    has one selected; never raises)."""
    try:
        cfg = load_repo_config(ws.repo_root)
        learning.note(db, cfg, worker or agents.workspace_worker(db, ws), ws, **event)
    except Exception:
        pass


def note_review(db: DB, ws: Workspace, approved: bool) -> None:
    _note(db, ws, approved=approved)


def note_removed_unmerged(db: DB, ws: Workspace) -> None:
    _note(db, ws, merged=False)


def _escalate(db: DB, ws: Workspace, worker: Agent | None) -> None:
    _note(db, ws, worker, escalated=True)


# -- stage 1: a worker reported ---------------------------------------------------


def on_report(db: DB, worker: Agent, ws: Workspace, result: str) -> bool:
    """Start the review of a reported branch. Returns whether the pipeline
    took the report (so the caller doesn't forward it to the supervisor)."""
    from copse.providers import copse_invocation

    try:
        cfg = load_repo_config(ws.repo_root)
    except ValueError:
        return False
    if not enabled(cfg, worker, ws):
        return False
    parent = db.get_agent(worker.parent_id) if worker.parent_id else None
    if parent is None:
        return False
    if git.dirty_files(ws.path):
        db.update_agent(worker.id, pipeline=None)
        _escalate(db, ws, worker)
        _tell(db, parent.id,
              f"[copse pipeline] {worker.id} reported on `{ws.branch}` but left uncommitted "
              f"changes, so it can't be reviewed or merged. Its report:\n\n{result}", worker.id)
        return True
    try:
        reviewer = agents.request_review(db, parent, ws, None, None, cfg)
    except agents.AgentError as e:
        db.update_agent(worker.id, pipeline=None)
        _escalate(db, ws, worker)
        _tell(db, parent.id,
              f"[copse pipeline] {worker.id} reported on `{ws.branch}`, but no reviewer could "
              f"start ({e}). Review it yourself with workspace_diff, then merge_workspace.\n\n"
              f"Its report:\n\n{result}", worker.id)
        return True
    db.update_agent(worker.id, pipeline="reviewing")
    if cfg.checks:
        _detach([*copse_invocation(), "_deliver-checks", reviewer.id, ws.id])
    return True


# -- stage 2: the review came in ---------------------------------------------------


def on_review(db: DB, reviewer: Agent, ws: Workspace, approved: bool, summary: str) -> bool:
    """Act on a verdict for a piped branch. Returns whether the pipeline
    handled it (so the verdict isn't forwarded to the supervisor as a
    message)."""
    worker = agents.workspace_worker(db, ws)
    if worker is None or not worker.pipeline:
        return False
    parent_id = worker.parent_id
    try:
        cfg = load_repo_config(ws.repo_root)
    except ValueError:
        cfg = RepoConfig()
    report = worker.result or ""
    if approved:
        parent = db.get_agent(parent_id) if parent_id else None
        if not cfg.auto_merge_default_branch and ws.base_branch \
                and ws.base_branch == git.default_branch(ws.repo_root):
            db.update_agent(worker.id, pipeline=None)
            _escalate(db, ws, worker)
            _tell(db, parent_id,
                  f"[copse pipeline] `{ws.branch}` was approved, but its base `{ws.base_branch}` "
                  "is the repo's default branch, which copse doesn't merge into on its own. "
                  f"This needs you: merge_workspace(\"{ws.id}\") when you're ready to merge it "
                  "(set \"merge_into\" to another branch, or \"auto_merge_default_branch\": true, "
                  f"in .copse/config.json to change this).\n\nWorker's report:\n{report}\n\n"
                  f"Review ({reviewer.id}): approved.\n{summary}", worker.id)
            return True
        text = merge(db, parent, ws)
        if text.startswith("Merged"):
            # This runs inside the reviewer's own process, and removing its
            # workspace must not take that process down half way: record and
            # announce everything first, then remove without killing the
            # session the reviewer is in.
            db.update_agent(worker.id, pipeline=None)
            db.set_status(reviewer.id, "done")
            _tell(db, parent_id,
                  f"[copse pipeline] {text} Removing the worktree.\n\nWorker's report:\n{report}"
                  f"\n\nReview ({reviewer.id}): approved.\n{summary}", worker.id)
            note = _remove(db, ws, keep=reviewer)
            if note.startswith("("):
                _tell(db, parent_id, f"[copse pipeline] {ws.branch}: {note}", worker.id)
        else:
            db.update_agent(worker.id, pipeline=None)
            _escalate(db, ws, worker)
            _tell(db, parent_id,
                  f"[copse pipeline] `{ws.branch}` was approved but couldn't be merged: {text}\n"
                  "This needs you: fix it (or have the worker fix it with send_message), then "
                  f"merge_workspace(\"{ws.id}\").\n\nWorker's report:\n{report}", worker.id)
        return True
    rounds = (worker.pipeline_rounds or 0) + 1
    if rounds <= cfg.review_rounds and agents.is_alive(worker):
        db.update_agent(worker.id, pipeline="fixing", pipeline_rounds=rounds)
        try:
            agents.send_message(
                db, worker.id,
                f"Review of your branch (round {rounds} of {cfg.review_rounds}) requested "
                f"changes:\n\n{summary}\n\nFix what's valid, commit, and call report_result "
                "again; copse will have it reviewed again.",
                sender_id=reviewer.id,
            )
            return True
        except agents.AgentError:
            pass
    db.update_agent(worker.id, pipeline=None)
    _escalate(db, ws, worker)
    _tell(db, parent_id,
          f"[copse pipeline] `{ws.branch}` still has review findings after {rounds - 1} fix "
          f"round(s). This needs you: decide what to do.\n\nLatest review ({reviewer.id}):\n"
          f"{summary}\n\nWorker's last report:\n{report}", worker.id)
    return True


def _remove(db: DB, ws: Workspace, keep: Agent | None = None) -> str:
    """Remove ``ws`` while ``keep`` (the agent running this code) survives:
    the other agents' windows are stopped, but the session ``keep`` is in is
    left for its own close."""
    try:
        if keep is not None:
            for a in db.list_agents(ws.id):
                if a.id != keep.id and agents.is_alive(a):
                    agents._stop(db, a)
        removed = workspaces.remove(db, ws, force=False, delete_branch=False,
                                    keep_session=keep is not None)
        return f"Worktree removed; {removed.branch_note or 'branch kept'}."
    except workspaces.WorkspaceError as e:
        return f"(worktree kept: {e})"


# -- the merge itself, shared with the merge_workspace tool -------------------------


def busy_worker(db: DB, ws: Workspace, exclude_id: str | None) -> Agent | None:
    """A live worker (not a reviewer) still at work in ``ws``, other than
    ``exclude_id``. A worker whose result is recorded is finished even if
    its Stop hook hasn't fired yet."""
    modes = tuple(m for m in agents.REPORTING_MODES if m != "review")
    for a in db.list_agents(ws.id):
        if a.id == exclude_id or a.mode not in modes or a.result is not None:
            continue
        if not agents.is_alive(a):
            continue
        a = agents.reconcile(db, a, samples=1)
        if a.status in ("processing", "waiting"):
            return a
    return None


def merge(db: DB, caller: Agent | None, ws: Workspace, squash: bool = False) -> str:
    """Sync the branch with its base, run the merge gates, and merge. The
    reply starts with "Merged" on success, else "Not merged: ..."."""
    cfg = load_repo_config(ws.repo_root)
    pilot = autopilot.for_agent(db, caller.id) if caller else None
    review = cfg.review if cfg.review is not None else bool(pilot and pilot.enabled)

    try:
        behind, _ahead = git.ahead_behind(ws.path, workspaces.require_base(ws))
        if behind:
            busy = busy_worker(db, ws, caller.id if caller else None)
            if busy:
                return (f"Not merged: {busy.id} is still working on {ws.branch}; "
                        "retry once it reports.")
        sync_result = workspaces.sync_with_base(ws)
    except git.GitError as e:
        return f"Not merged: {e}"
    if sync_result.status == "conflict":
        files = ", ".join(sync_result.conflicts) or "?"
        return (f"Not merged: {ws.branch} conflicts with {ws.base_branch} in: {files}. "
                f"Ask the worker to merge {ws.base_branch} and resolve.")
    if sync_result.status == "synced" and review:
        # The branch's own commits are unchanged: an approval of them
        # carries over the merge commit, and the checks below still run
        # on the merged result. Only an unreviewed branch needs a review.
        prior = db.latest_review(ws.id, sync_result.old_sha) if sync_result.old_sha else None
        if prior and prior.approved:
            db.add_review(ws.id, sync_result.new_sha, prior.reviewer_id, True,
                          f"Carried over from the approved review of {sync_result.old_sha[:8]}: "
                          f"{ws.base_branch} merged in cleanly (commit {sync_result.new_sha[:8]}), "
                          "and the checks run on the merged result below.")
        else:
            return (f"Not merged: synced {ws.branch} with {ws.base_branch} "
                    f"(new commit {sync_result.new_sha[:8]}); request_review again, then merge.")

    report = gates.run(db, ws, cfg, review_required=review)
    if not report.ok:
        return f"Not merged. {report.problem}"
    if gates.head(ws) != report.sha:
        return (f"Not merged: {ws.branch} got new commits while the gates ran. "
                "Call merge_workspace again to check the new commits.")
    try:
        target = workspaces.merge_back(db, ws, squash=squash)
    except git.GitError as e:
        return f"Not merged: {e}"
    text = f"Merged {ws.branch} into {ws.base_branch} at {target} ({report.summary()})."
    history.record_safely(
        db, ws.repo_root, "merge", agent=caller, with_usage=True, branch=ws.branch,
        task=f"merge {ws.branch} into {ws.base_branch}", result=text,
    )
    _note(db, ws, merged=True, checks_passed=True)
    tasks.on_merged(db, ws)
    codemap.refresh_later(ws.repo_root)
    if pilot:
        db.bump_progress(pilot.root_id)
        if pilot.goal:
            text += " Next: call check_milestone to verify progress."
    return text
