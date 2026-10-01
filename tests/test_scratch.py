import os

import pytest

from copse import git, scratch



@pytest.fixture
def elsewhere(tmp_path):
    d = tmp_path / "Downloads"
    d.mkdir()
    return d


def test_scratch_session_is_created_away_from_the_folder(db, elsewhere):
    ws = scratch.create(db, str(elsewhere))
    assert not (elsewhere / ".git").exists()
    assert scratch.is_scratch(ws.path) and ws.kind == "main" and ws.branch == "main"
    assert scratch.origin_of(ws.path) == str(elsewhere.resolve())
    assert scratch.commit_count(ws) == 0
    assert scratch.for_origin(db, str(elsewhere)).id == ws.id


def test_transfer_replays_commits_and_snapshots_uncommitted_work(db, elsewhere, repo):
    s = scratch.create(db, str(elsewhere))
    open(os.path.join(s.path, "notes.md"), "w").write("plan\n")
    git.commit_all(s.path, "Add notes")
    open(os.path.join(s.path, "tool.py"), "w").write("print(1)\n")  # left uncommitted

    t = scratch.transfer(db, s, str(repo))
    ws = t.workspace
    assert (t.commits, t.snapshot) == (2, True)
    assert ws.branch.startswith("copse/from-") and ws.base_branch == "main"
    assert open(os.path.join(ws.path, "notes.md")).read() == "plan\n"
    assert os.path.exists(os.path.join(ws.path, "tool.py"))
    log = git.out(["log", "--format=%s", "main..HEAD"], ws.path).splitlines()
    assert log == ["Work in progress from copse scratch session", "Add notes"]
    assert "Start copse scratch session" not in git.out(["log", "--format=%s"], ws.path)
    assert scratch.transferred_to(s.path) and s.id not in [p.id for p in scratch.pending(db)]
    assert scratch.for_origin(db, str(elsewhere)) is None  # next copse run starts fresh


def test_transfer_refuses_non_repos_and_empty_sessions(db, elsewhere, tmp_path, repo):
    s = scratch.create(db, str(elsewhere))
    with pytest.raises(scratch.ScratchError, match="no work"):
        scratch.transfer(db, s, str(repo))
    open(os.path.join(s.path, "a.txt"), "w").write("a")
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    with pytest.raises(scratch.ScratchError, match="isn't inside a git repository"):
        scratch.transfer(db, s, str(plain))


def test_transfer_conflict_names_the_file(db, elsewhere, repo):
    s = scratch.create(db, str(elsewhere))
    open(os.path.join(s.path, "app.py"), "w").write("different\n")  # repo has app.py too
    git.commit_all(s.path, "My app.py")
    with pytest.raises(scratch.ScratchError, match="app.py"):
        scratch.transfer(db, s, str(repo))


def test_bare_copse_outside_git_uses_a_scratch_session(db, elsewhere, monkeypatch):
    from copse import cli

    monkeypatch.chdir(elsewhere)
    ws = cli._here_or_scratch(db, reuse_scratch=False)
    assert scratch.is_scratch(ws.path)
    assert cli._here_or_scratch(db, reuse_scratch=True).id == ws.id  # continue: same session
    assert cli._here_or_scratch(db, reuse_scratch=False).id != ws.id  # plain copse: fresh
