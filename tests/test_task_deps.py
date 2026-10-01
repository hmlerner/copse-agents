"""assign/handoff's `files` (declared scope, overlap warnings) and
`depends_on` (queue until an earlier task's branch merges, cancel if its
workspace is removed unmerged instead)."""

import asyncio
import re
import time
from pathlib import Path

import pytest

from conftest import sh
from copse import agents, mcp_server, tasks, workspaces
from copse.db import Agent


def fake_spawn(db, ws, profile, *, prompt=None, parent_id=None, mode="handoff", done_when=None, **kw):
    """Stands in for agents.spawn: records a worker without launching a real
    CLI process, so delegate()'s real workspace creation (the part these
    tests care about) still runs for real."""
    a = Agent(agents.new_id(), ws.id, profile, "claude", parent_id, mode, "processing", "",
              None, time.time(), task=prompt, done_when=done_when)
    db.add_agent(a)
    return a


@pytest.fixture(autouse=True)
def no_real_spawn(monkeypatch):
    monkeypatch.setattr(agents, "spawn", fake_spawn)


@pytest.fixture
def boss(db, repo, monkeypatch):
    """A supervisor caller adopted on the main checkout, so mcp_server tools
    that need `_caller` resolve without touching the real process cwd."""
    # These tests exercise overlap *warnings*; the default now refuses overlaps.
    (repo / ".copse").mkdir(exist_ok=True)
    (repo / ".copse" / "config.json").write_text('{"overlap": "warn", "pipeline": false}')
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                        "@0", None, time.time()))
    monkeypatch.setenv("COPSE_AGENT_ID", "boss")
    return ws


def started_worker_id(reply: str) -> str:
    m = re.search(r"Started worker (\S+)", reply)
    assert m, reply
    return m.group(1)


def commit_file(path: Path, name: str, content: str = "x\n") -> None:
    (path / name).write_text(content)
    sh(f"git add -A && git commit -qm {name}", path)


# -- storing files/depends_on on a started task -------------------------------


def test_assign_stores_files_and_depends_on_on_the_started_task(db, repo, boss):
    out = asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a", files=["src/a.py"]))
    worker_id = started_worker_id(out)

    [t] = db.list_tasks(str(repo), state="started")
    assert t.agent_id == worker_id
    assert t.branch == "feat-a"
    assert tasks._loads(t.files) == ["src/a.py"]
    assert tasks._loads(t.depends_on) == []


def test_handoff_stores_files_and_depends_on_too(db, repo, boss, monkeypatch):
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    out = asyncio.run(mcp_server.handoff("developer", "do A", branch="feat-a", files=["src/a.py"],
                                         wait_seconds=0))
    assert "still running" in out
    [t] = db.list_tasks(str(repo))
    assert t.state == "started" and tasks._loads(t.files) == ["src/a.py"]


# -- overlap warnings -----------------------------------------------------------


def test_overlap_warning_against_another_workers_declared_files(db, repo, boss):
    out_a = asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a", files=["src/a.py"]))
    worker_a = started_worker_id(out_a)

    out_b = asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b", files=["src/a.py"]))
    assert "Warning" in out_b
    assert f"overlaps with {worker_a}" in out_b
    assert "feat-a" in out_b and "src/a.py" in out_b


def test_overlap_warning_against_another_workers_actual_changed_files(db, repo, boss):
    out_a = asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a"))
    worker_a = started_worker_id(out_a)
    ws_a = next(w for w in db.find_workspaces(str(repo)) if w.branch == "feat-a")
    commit_file(Path(ws_a.path), "src_touched.py")

    out_b = asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b",
                                          files=["src_touched.py"]))
    assert f"overlaps with {worker_a}" in out_b


def test_no_overlap_warning_when_files_are_disjoint(db, repo, boss):
    asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a", files=["src/a.py"]))
    out_b = asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b", files=["src/b.py"]))
    assert "Warning" not in out_b


def test_no_overlap_warning_against_a_worker_that_already_reported(db, repo, boss):
    out_a = asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a", files=["src/a.py"]))
    worker_a = started_worker_id(out_a)
    db.set_result(worker_a, "done")  # no longer active

    out_b = asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b", files=["src/a.py"]))
    assert "Warning" not in out_b


def test_overlap_warning_matches_recursive_globs(db, repo, boss):
    asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a", files=["src/**/a.py"]))
    out_b = asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b",
                                          files=["src/nested/dir/a.py"]))
    assert "Warning" in out_b


def test_overlap_warning_matches_despite_a_leading_dot_slash(db, repo, boss):
    asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a", files=["./src/a.py"]))
    out_b = asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b", files=["src/a.py"]))
    assert "Warning" in out_b


# -- dependencies: queue until merge, then auto-start --------------------------


def test_depends_on_queues_the_task_until_the_dependency_is_unmet(db, repo, boss):
    out_a = asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a"))
    worker_a = started_worker_id(out_a)

    out_b = asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b",
                                          depends_on=[worker_a]))
    assert "Queued task" in out_b and worker_a in out_b

    [pending] = db.list_tasks(str(repo), state="pending")
    assert pending.branch == "feat-b"
    assert tasks._loads(pending.depends_on) == [worker_a]
    assert pending.agent_id is None
    # No workspace/branch was created yet for the queued task.
    assert not any(w.branch == "feat-b" for w in db.find_workspaces(str(repo)))


def test_queued_task_auto_starts_after_dependency_merges_cut_from_updated_base(db, repo, boss, monkeypatch):
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    out_a = asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a"))
    worker_a = started_worker_id(out_a)
    ws_a = next(w for w in db.find_workspaces(str(repo)) if w.branch == "feat-a")
    commit_file(Path(ws_a.path), "a_output.txt", "from A\n")

    out_b = asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b",
                                          depends_on=[worker_a]))
    assert "Queued task" in out_b

    merge_out = asyncio.run(mcp_server.merge_workspace(ws_a.id))
    assert "Merged" in merge_out

    # Merging retires 'feat-a's own task, so only the dependent stays 'started'.
    [b_task] = db.list_tasks(str(repo), state="started")
    assert b_task.branch == "feat-b"
    assert b_task.state == "started"
    assert b_task.agent_id

    ws_b = db.get_workspace(db.get_agent(b_task.agent_id).workspace_id)
    # Cut from main *after* A merged: A's file is already there.
    assert (Path(ws_b.path) / "a_output.txt").exists()

    msg = db.pop_pending("boss")
    assert msg is not None
    assert "Started" in msg.body and b_task.agent_id in msg.body and worker_a in msg.body


def test_dependency_by_branch_name_also_queues_and_starts(db, repo, boss):
    asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a"))
    ws_a = next(w for w in db.find_workspaces(str(repo)) if w.branch == "feat-a")
    commit_file(Path(ws_a.path), "a_output.txt")

    out_b = asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b",
                                          depends_on=["feat-a"]))
    assert "Queued task" in out_b

    asyncio.run(mcp_server.merge_workspace(ws_a.id))

    [b_task] = [t for t in db.list_tasks(str(repo)) if t.branch == "feat-b"]
    assert b_task.state == "started" and b_task.agent_id


def test_queued_task_stays_queued_if_only_some_dependencies_merged(db, repo, boss):
    out_a = asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a"))
    worker_a = started_worker_id(out_a)
    out_c = asyncio.run(mcp_server.assign("developer", "do C", branch="feat-c"))
    worker_c = started_worker_id(out_c)
    ws_a = next(w for w in db.find_workspaces(str(repo)) if w.branch == "feat-a")

    asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b",
                                  depends_on=[worker_a, worker_c]))

    asyncio.run(mcp_server.merge_workspace(ws_a.id))  # only one of two deps merges

    [b_task] = [t for t in db.list_tasks(str(repo)) if t.branch == "feat-b"]
    assert b_task.state == "pending"


# -- cancellation on unmerged removal --------------------------------------------


def test_queued_task_is_cancelled_when_dependency_workspace_removed_unmerged(db, repo, boss, monkeypatch):
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    out_a = asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a"))
    worker_a = started_worker_id(out_a)
    ws_a = next(w for w in db.find_workspaces(str(repo)) if w.branch == "feat-a")
    commit_file(Path(ws_a.path), "a_output.txt")  # ahead of main: "unmerged"

    asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b", depends_on=[worker_a]))

    remove_out = mcp_server.remove_workspace(ws_a.id, force=True)
    assert "Removed" in remove_out

    [b_task] = [t for t in db.list_tasks(str(repo)) if t.branch == "feat-b"]
    assert b_task.state == "cancelled"

    msg = db.pop_pending("boss")
    assert msg is not None
    assert "Cancelled" in msg.body and worker_a in msg.body


def test_removing_a_fully_merged_workspace_does_not_cancel_dependents(db, repo, boss):
    out_a = asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a"))
    worker_a = started_worker_id(out_a)
    ws_a = next(w for w in db.find_workspaces(str(repo)) if w.branch == "feat-a")

    asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b", depends_on=[worker_a]))
    asyncio.run(mcp_server.merge_workspace(ws_a.id))  # merges and auto-starts B first

    [b_task] = [t for t in db.list_tasks(str(repo)) if t.branch == "feat-b"]
    assert b_task.state == "started"  # already started; removing A now must not touch it

    ws_a = db.get_workspace(ws_a.id)
    mcp_server.remove_workspace(ws_a.id, force=True)

    b_task = db.get_task(b_task.id)
    assert b_task.state == "started"


def test_cancellation_cascades_to_a_task_depending_on_the_cancelled_one(db, repo, boss, monkeypatch):
    """A unmerged -> B depends_on=[A] queued -> C depends_on=["feat-b"]
    queued -> removing A unmerged must cancel both B and C, not just B."""
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    out_a = asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a"))
    worker_a = started_worker_id(out_a)
    ws_a = next(w for w in db.find_workspaces(str(repo)) if w.branch == "feat-a")
    commit_file(Path(ws_a.path), "a_output.txt")  # ahead of main: "unmerged"

    out_b = asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b",
                                          depends_on=[worker_a]))
    assert "Queued task" in out_b
    out_c = asyncio.run(mcp_server.assign("developer", "do C", branch="feat-c",
                                          depends_on=["feat-b"]))
    assert "Queued task" in out_c

    mcp_server.remove_workspace(ws_a.id, force=True)

    [b_task] = [t for t in db.list_tasks(str(repo)) if t.branch == "feat-b"]
    [c_task] = [t for t in db.list_tasks(str(repo)) if t.branch == "feat-c"]
    assert b_task.state == "cancelled"
    assert c_task.state == "cancelled"

    first = db.pop_pending("boss")
    second = db.pop_pending("boss")
    assert first is not None and second is not None
    bodies = first.body + second.body
    assert "Cancelled" in first.body and "Cancelled" in second.body
    assert worker_a in bodies and b_task.id in bodies


def test_new_task_depending_on_an_already_cancelled_task_fails_clearly(db, repo, boss, monkeypatch):
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a"))
    ws_a = next(w for w in db.find_workspaces(str(repo)) if w.branch == "feat-a")
    commit_file(Path(ws_a.path), "a_output.txt")  # ahead of main: "unmerged"
    asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b", depends_on=["feat-a"]))
    mcp_server.remove_workspace(ws_a.id, force=True)  # cancels B

    out_d = asyncio.run(mcp_server.assign("developer", "do D", branch="feat-d",
                                          depends_on=["feat-b"]))
    assert "cancelled" in out_d.lower() and "feat-b" in out_d
    # D was neither started nor queued: unmet_dependencies failed before either.
    assert not any(t.branch == "feat-d" for t in db.list_tasks(str(repo)))


# -- a missing add_dirs entry reaches the supervisor ---------------------------


# -- cancel_task ------------------------------------------------------------------


def _queue_chain(db, repo):
    """A started (files declared so it's recorded), B queued on A, C queued on B."""
    worker_a = started_worker_id(asyncio.run(
        mcp_server.assign("developer", "do A", branch="feat-a", files=["a.py"])))
    asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b", depends_on=[worker_a]))
    asyncio.run(mcp_server.assign("developer", "do C", branch="feat-c", depends_on=["feat-b"]))
    by_branch = {t.branch: t for t in db.list_tasks(str(repo))}
    return by_branch["feat-a"], by_branch["feat-b"], by_branch["feat-c"]


def test_cancel_task_by_its_caller_cascades_and_lists_dependents(db, repo, boss):
    _, b, c = _queue_chain(db, repo)

    out = mcp_server.cancel_task(b.id, "re-planning")

    assert f"Cancelled task {b.id}" in out and c.id in out
    assert db.get_task(b.id).state == "cancelled"
    assert db.get_task(c.id).state == "cancelled"


def test_cancel_task_refuses_a_task_that_already_started(db, repo, boss):
    a, _, _ = _queue_chain(db, repo)

    out = mcp_server.cancel_task(a.id)

    assert out.startswith("Error") and "not queued" in out
    assert db.get_task(a.id).state == "started"


def test_cancel_task_refuses_someone_elses_task(db, repo, boss, monkeypatch):
    _, b, _ = _queue_chain(db, repo)
    db.add_agent(Agent("other", boss.id, "supervisor", "claude", None, "interactive",
                       "processing", "@1", None, time.time()))
    monkeypatch.setenv("COPSE_AGENT_ID", "other")

    out = mcp_server.cancel_task(b.id)

    assert out.startswith("Error") and "isn't yours" in out
    assert db.get_task(b.id).state == "pending"


def test_cancel_task_unknown_id(db, repo, boss):
    assert mcp_server.cancel_task("nope").startswith("Error")


def _missing_add_dir(repo):
    (repo / ".copse" / "config.json").write_text(
        '{"overlap": "warn", "pipeline": false, "add_dirs": ["/no/such/cache"]}')


def test_assign_reply_names_a_missing_add_dir(db, repo, boss):
    """The launch's stderr belongs to the MCP server, which nobody reads: the
    supervisor only learns about a missing add_dirs entry from the reply."""
    _missing_add_dir(repo)
    out = asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a"))
    assert "Warning: add_dirs names /no/such/cache" in out


def test_handoff_reply_names_a_missing_add_dir(db, repo, boss, monkeypatch):
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    _missing_add_dir(repo)
    out = asyncio.run(mcp_server.handoff("developer", "do A", branch="feat-a", wait_seconds=0))
    assert "Warning: add_dirs names /no/such/cache" in out


def test_no_add_dirs_warning_when_they_exist(db, repo, boss):
    out = asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a"))
    assert "add_dirs" not in out


def test_a_queued_task_starting_names_a_missing_add_dir(db, repo, boss, monkeypatch):
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    out_a = asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a"))
    worker_a = started_worker_id(out_a)
    ws_a = next(w for w in db.find_workspaces(str(repo)) if w.branch == "feat-a")
    commit_file(Path(ws_a.path), "a_output.txt")
    asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b", depends_on=[worker_a]))
    _missing_add_dir(repo)

    asyncio.run(mcp_server.merge_workspace(ws_a.id))

    msg = db.pop_pending("boss")
    assert "Started" in msg.body and "add_dirs names /no/such/cache" in msg.body
