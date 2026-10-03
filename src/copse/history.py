"""Durable, append-only history of what agents did: a worker's report, a
reviewer's verdict, a successful merge, and a milestone check. Written at
``report_result``, ``submit_review``, ``merge_workspace`` success, and
``check_milestone``.

Session pruning (sessions.py) deletes agent rows (and cascades reviews and
milestones with them) to keep disk use bounded; history has no foreign keys
so it survives that. It's capped per repo instead (see CAP_PER_REPO).

A row's tokens are what its agent used since that agent's previous row (see
``_delta``), so summing any set of rows never double counts. Milestone and
check rows carry no tokens: the supervisor that ran them is already counted
by its merge rows and so on. The mark that tracks "since its previous row" is
keyed to the transcript it was taken from (``db.add_history``'s ``mark``): if
the agent's current transcript is a different one (e.g. /clear started a new
one), there's nothing to subtract and the row starts a fresh baseline.

Callers use ``record_safely``: history is a side record and must never fail
the report, merge or check it describes.
"""

from __future__ import annotations

import json
import logging
import os
import time

from copse.db import DB, HistoryEntry
from copse.usage import Usage, agent_usage, format_tokens

log = logging.getLogger(__name__)

TASK_CHARS = 300
RESULT_CHARS = 2000
CAP_PER_REPO = 5000

KINDS = ("worker_result", "review", "merge", "check", "milestone", "permission")


def _trim(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tokens_json(usage: Usage | None) -> str | None:
    if usage is None:
        return None
    return json.dumps({
        "input": usage.input_tokens, "output": usage.output_tokens,
        "cache_read": usage.cache_read_tokens, "cache_creation": usage.cache_creation_tokens,
        "model": usage.model,
    })


def _delta(db: DB, agent_id: str | None, transcript_path: str | None,
          usage: Usage | None) -> tuple[Usage | None, tuple | None]:
    """``usage`` (an agent's cumulative total) minus what its previous rows
    already recorded, and the new mark to move up to (for the caller to
    persist alongside the row it's about to write, atomically). If there's
    no previous mark, it's for a different transcript (e.g. a new one after
    /clear), or the total went down in spite of that (some other reset),
    there's nothing to subtract: start a fresh baseline."""
    if usage is None or agent_id is None:
        return usage, None
    new_mark = (transcript_path, usage.input_tokens, usage.output_tokens,
               usage.cache_read_tokens, usage.cache_creation_tokens)
    mark = db.get_usage_mark(agent_id)
    if mark is None or mark["transcript_path"] != transcript_path:
        return usage, new_mark
    prev = Usage(mark["input_tokens"], mark["output_tokens"], mark["cache_read_tokens"],
                 mark["cache_creation_tokens"])
    parts = [(usage.input_tokens, prev.input_tokens), (usage.output_tokens, prev.output_tokens),
             (usage.cache_read_tokens, prev.cache_read_tokens),
             (usage.cache_creation_tokens, prev.cache_creation_tokens)]
    if any(now < before for now, before in parts):
        return usage, new_mark
    return Usage(*(now - before for now, before in parts), model=usage.model), new_mark


def record(db: DB, repo_root: str, kind: str, *, agent_id: str | None = None,
          transcript_path: str | None = None, branch: str | None = None,
          profile: str | None = None, task: str | None = None, result: str | None = None,
          usage: Usage | None = None) -> None:
    """Append a row. ``usage`` is the agent's cumulative usage so far; the
    row stores only the part not already in its earlier rows. The row and
    the updated mark are written together, so a mark never moves without
    the row that earned it actually landing."""
    delta, mark = _delta(db, agent_id, transcript_path, usage)
    db.add_history(
        repo_root, kind, agent_id=agent_id, branch=branch, profile=profile,
        task=_trim(task, TASK_CHARS), result=_trim(result, RESULT_CHARS),
        tokens=_tokens_json(delta), mark=mark,
    )
    db.prune_history(repo_root, CAP_PER_REPO)


def record_safely(db: DB, repo_root: str, kind: str, *, agent=None, usage: Usage | None = None,
                  with_usage: bool = False, branch: str | None = None,
                  task: str | None = None, result: str | None = None) -> None:
    """``record`` a row for ``agent`` (a copse ``Agent``, or None) with its
    cumulative ``usage``, or read that now if ``with_usage``. Logs and
    swallows any failure."""
    try:
        u = agent_usage(db, agent) if with_usage and agent else usage
        record(db, repo_root, kind, agent_id=agent.id if agent else None,
              transcript_path=agent.transcript_path if agent else None, branch=branch,
              profile=agent.profile if agent else None, task=task, result=result, usage=u)
    except Exception:
        log.exception("copse: couldn't record %s history", kind)


def _token_totals(tokens: str | None) -> dict | None:
    if not tokens:
        return None
    try:
        return json.loads(tokens)
    except ValueError:
        return None


def tokens_total(tokens: str | None) -> int:
    d = _token_totals(tokens)
    if not d:
        return 0
    return (d.get("input", 0) + d.get("output", 0) + d.get("cache_read", 0)
            + d.get("cache_creation", 0))


def tokens_summary(tokens: str | None) -> str:
    """The compact form for the `copse history` table, or "-" if there's none."""
    d = _token_totals(tokens)
    if not d:
        return "-"
    total_in = d.get("input", 0) + d.get("cache_read", 0) + d.get("cache_creation", 0)
    return f"{format_tokens(total_in)} in · {format_tokens(d.get('output', 0))} out"


def row_summary(row: HistoryEntry, width: int = 60) -> str:
    """The first line of whatever this row has to say: its task, or its
    result if it has no task (a review/merge/check row)."""
    text = (row.task or row.result or "").strip()
    line = text.splitlines()[0] if text else ""
    return line if len(line) <= width else line[: width - 1] + "…"


def _plural(count: int, word: str, plural: str | None = None) -> str:
    return f"{count} {word if count == 1 else plural or word + 's'}"


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = int(seconds // 60)
    if minutes < 10:
        return f"{minutes}m{int(seconds % 60):02d}s"
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def _provider(profile: str | None, repo_root: str) -> str | None:
    from copse.profiles import load_profile

    try:
        return load_profile(profile, repo_root).provider if profile else None
    except Exception:  # noqa: BLE001 - a profile since removed: unknown
        return None


def share_card(db: DB, root_id: str) -> str:
    """A few plain lines about one session, to paste into Slack or a post:
    the goal, milestones verified, workers, merges, reviews, time and tokens.
    Only counts and the goal; no code, paths or branch names.

    Built from the session's tasks and history rows, which outlive the
    workers' and reviewers' agent rows (removed with their worktrees)."""
    from copse import git

    root = db.get_agent(root_id)
    ws = db.get_workspace(root.workspace_id) if root else None
    if root is None or ws is None:
        raise KeyError(f"no session {root_id}")
    callers, queue = {root_id}, [root_id]
    while queue:
        for child in db.children(queue.pop()):
            callers.add(child.id)
            queue.append(child.id)
    tasks = [t for t in db.list_tasks(ws.repo_root)
             if t.caller_id in callers and t.agent_id and t.state in ("started", "merged")]
    branches = {t.branch for t in tasks if t.branch}
    rows = [r for r in db.list_history(ws.repo_root, None, CAP_PER_REPO)
            if r.ts >= root.created_at and (r.agent_id in callers or r.branch in branches)]

    lines = [f"copse session · {os.path.basename(ws.repo_root)} · "
             f"{time.strftime('%Y-%m-%d', time.localtime(root.created_at))}"]
    ap = db.get_autopilot(root_id)
    if ap and ap.goal:
        lines.append(f"Goal: {ap.goal.strip().splitlines()[0]}")
    ms = db.milestones(root_id)
    if ms:
        passed = sum(m.status == "passed" for m in ms)
        lines.append(f"{'✓' if passed == len(ms) else '◐'} {passed}/{len(ms)} milestones "
                     "verified by their check commands")

    # Merged: the pipeline says so, or the base has the branch's merge
    # commit (a branch can land through another that merged it first, and
    # its ref is usually deleted by then).
    merge_subjects = git.run(["log", "--merges", "--format=%s", f"--since=@{int(root.created_at)}",
                              ws.branch], ws.repo_root, check=False).stdout
    merged = sum(t.state == "merged" or bool(t.branch and f"Merge branch '{t.branch}'" in merge_subjects)
                 for t in tasks)
    reviews = [r for r in rows if r.kind == "review"]
    worker_providers = {_provider(t.profile, ws.repo_root) for t in tasks}
    other = sum(_provider(r.profile, ws.repo_root) not in worker_providers for r in reviews)
    lines.append(f"{_plural(len(tasks), 'worker')} · {_plural(merged, 'branch', 'branches')} merged · "
                 f"{_plural(len(reviews), 'review')}"
                 + (f" ({other} by a different model)" if other else ""))

    spans = []
    for t in tasks:
        ends = [r.ts for r in rows if r.agent_id == t.agent_id and r.kind == "worker_result"]
        if ends:
            spans.append((t.started_at or t.created_at, min(ends)))
    if spans:
        wall = max(e for _, e in spans) - min(s for s, _ in spans)
        busy = sum(e - s for s, e in spans)
        speed = (f" · {busy / wall:.1f}× parallel ({_duration(busy)} of worker time)"
                 if wall > 0 and len(spans) > 1 else "")
        lines.append(f"{_duration(wall)} elapsed{speed}")
    total = sum(tokens_total(r.tokens) for r in rows)
    if total:
        lines.append(f"{format_tokens(total)} tokens")
    lines.append("Built with copse: https://pawdelta.com/copse/")
    return "\n".join(lines)
