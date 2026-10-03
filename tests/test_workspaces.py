import json
import os

import pytest

from copse import git, workspaces
from copse.config import load_repo_config

from conftest import sh


def write_config(repo, **cfg):
    (repo / ".copse").mkdir(exist_ok=True)
    (repo / ".copse" / "config.json").write_text(json.dumps(cfg))


def test_create_branches_from_fetched_base(db, repo, tmp_path):
    # Someone else pushes to origin/main after our clone.
    other = tmp_path / "other"
    sh(f"git clone -q {tmp_path / 'origin.git'} {other}", tmp_path)
    (other / "new.txt").write_text("x")
    sh("git add -A && git commit -qm upstream && git push -q", other)

    created = workspaces.create(db, str(repo), "feat/login")
    ws = created.workspace
    assert created.how == "new"
    assert created.start_point == "origin/main"
    assert ws.branch == "feat/login" and ws.base_branch == "main"
    assert (tmp_path / "copse-home" / "worktrees" / "proj" / "feat" / "login" / "new.txt").exists()
    assert git.get_base(str(repo), "feat/login") == "main"
    assert ws.name == "feat-login"


def test_copy_setup_env_and_ports(db, repo):
    write_config(repo, copy=[".env"], setup=['echo "$COPSE_BRANCH $COPSE_PORT_BASE" > setup.out'])
    a = workspaces.create(db, str(repo), "one").workspace
    b = workspaces.create(db, str(repo), "two").workspace
    assert (os.path.join(a.path, ".env"))
    assert open(os.path.join(a.path, ".env")).read() == "SECRET=1\n"
    assert open(os.path.join(a.path, "setup.out")).read().strip() == f"one {a.port_base}"
    assert b.port_base == a.port_base + 10


def test_failed_setup_keeps_workspace(db, repo):
    write_config(repo, setup=["echo before", "false", "echo never"])
    created = workspaces.create(db, str(repo), "broken")
    assert created.setup and not created.setup.ok
    assert "never" not in created.setup.log
    assert db.get_workspace(created.workspace.id)


def test_local_config_wraps_shared(repo):
    write_config(repo, setup=["b"])
    (repo / ".copse" / "config.local.json").write_text(json.dumps({"setup": {"before": ["a"], "after": ["c"]}, "branch_prefix": "me/"}))
    cfg = load_repo_config(repo)
    assert cfg.setup == ["a", "b", "c"]
    assert cfg.branch_prefix == "me/"


def test_diff_status_and_merge_back(db, repo):
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "app.py"), "a").write("print('more')\n")
    open(os.path.join(ws.path, "new.py"), "w").write("x = 1\n")
    d = git.diff(ws.path, "main")
    assert "+print('more')" in d and "new.py" in d

    git.commit_all(ws.path, "work")
    st = git.status(ws.path, "main")
    assert (st.ahead, st.behind, st.dirty_files) == (1, 0, [])

    target = workspaces.merge_back(db, ws)
    assert target == str(repo)
    assert "print('more')" in (repo / "app.py").read_text()


def test_sync_rebases_onto_base(db, repo):
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "f.txt"), "w").write("f")
    git.commit_all(ws.path, "feature work")
    (repo / "base.txt").write_text("b")
    sh("git add -A && git commit -qm base && git push -q", repo)

    git.sync(ws.path, "main")
    st = git.status(ws.path, "main")
    assert (st.ahead, st.behind) == (1, 0)
    assert os.path.exists(os.path.join(ws.path, "base.txt"))


def test_sync_conflict_reports_files(db, repo):
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "app.py"), "w").write("mine\n")
    git.commit_all(ws.path, "mine")
    (repo / "app.py").write_text("theirs\n")
    sh("git add -A && git commit -qm theirs && git push -q", repo)
    with pytest.raises(git.GitError, match="app.py"):
        git.sync(ws.path, "main")


def test_dirty_files_reports_a_modified_tracked_file_whole(db, repo):
    # An edit to a tracked file is " M app.py" in porcelain, with a blank first
    # status column. Every other dirty-file test here uses a new file, which is
    # "?? path" and hides a mis-parse of that column.
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "app.py"), "a").write("print('more')\n")
    assert git.dirty_files(ws.path) == ["app.py"]


def test_remove_refuses_dirty_and_keeps_branch(db, repo):
    ws = workspaces.create(db, str(repo), "feature").workspace
    open(os.path.join(ws.path, "wip.txt"), "w").write("wip")
    with pytest.raises(workspaces.WorkspaceError, match="uncommitted"):
        workspaces.remove(db, ws)
    git.commit_all(ws.path, "wip")

    removed = workspaces.remove(db, ws, delete_branch=True)
    # Unmerged: safe delete refuses, branch survives.
    assert not removed.branch_deleted
    assert git.branch_exists(str(repo), "feature")
    assert not os.path.exists(ws.path)
    assert db.get_workspace(ws.id) is None


def test_existing_branch_is_reused_and_names_dedupe(db, repo):
    sh("git branch existing", repo)
    created = workspaces.create(db, str(repo), "existing")
    assert created.how == "existing"
    workspaces.remove(db, created.workspace)
    with pytest.raises(workspaces.WorkspaceError, match="base branch"):
        workspaces.create(db, str(repo), "main")


def test_resolve_from_inside_worktree(db, repo):
    ws = workspaces.create(db, str(repo), "feature").workspace
    assert workspaces.resolve(db, "feature", cwd=ws.path).id == ws.id
    assert workspaces.current(db, cwd=ws.path).id == ws.id
    assert git.main_repo_root(ws.path) == str(repo)


@pytest.mark.parametrize("raw,want", [
    ("Fix the Login bug!", "Fix-the-Login-bug"),
    ("feat//x..y", "feat/x.y"),
    ("-lead/trail.lock", "lead/trail"),
])
def test_sanitize_branch(raw, want):
    assert git.sanitize_branch(raw) == want


def test_unpushed_base_commits_are_not_counted_as_branch_work(db, repo):
    # Local main gains a commit that isn't pushed yet; a workspace branched
    # from local main must not show that commit as its own.
    (repo / "local.txt").write_text("l")
    sh("git add -A && git commit -qm local-only", repo)
    ws = workspaces.create(db, str(repo), "feature", start="main").workspace
    st = git.status(ws.path, "main")
    assert (st.ahead, st.behind) == (0, 0)
    assert "local.txt" not in git.diff(ws.path, "main", stat=True)


def test_stale_local_base_compares_against_origin(db, repo, tmp_path):
    other = tmp_path / "other"
    sh(f"git clone -q {tmp_path / 'origin.git'} {other}", tmp_path)
    (other / "up.txt").write_text("u")
    sh("git add -A && git commit -qm upstream && git push -q", other)
    ws = workspaces.create(db, str(repo), "feature").workspace  # from origin/main
    assert git.base_ref(ws.path, "main") == "origin/main"
    assert git.status(ws.path, "main").ahead == 0


def test_adopt_root_picks_up_a_branch_switch(db, repo):
    sh("git switch -qc feat/old", repo)
    assert workspaces.adopt_root(db, str(repo)).branch == "feat/old"
    sh("git switch -q main", repo)
    assert workspaces.adopt_root(db, str(repo)).branch == "main"
    assert db.workspace_by_path(str(repo)).branch == "main"
