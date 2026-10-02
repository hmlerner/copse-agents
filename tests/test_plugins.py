"""copse's extension points: the guarded plugin loader, events at every
call site, a policy plugin's deny blocking assign and merge, and the
``copse account`` passthrough."""

import asyncio
import json
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import sh
from copse import account, agents, events, mcp_server, pipeline, plugins, policy, quota, workspaces
from copse.cli import app
from copse.config import RepoConfig, load_repo_config
from copse.db import Agent


# -- fakes ------------------------------------------------------------------------------


class Recorder(events.EventsPlugin):
    def __init__(self, fail=False):
        self.events, self.fail = [], fail

    def emit(self, event):
        if self.fail:
            raise RuntimeError("boom")
        self.events.append(event)

    def kinds(self):
        return [e.kind for e in self.events]


class Gate(policy.PolicyPlugin):
    def __init__(self, assign="", merge="", fail=False):
        self.assign_reason, self.merge_reason, self.fail = assign, merge, fail
        self.seen = []

    def check_assign(self, info):
        if self.fail:
            raise RuntimeError("boom")
        self.seen.append(info)
        return policy.deny(self.assign_reason) if self.assign_reason else policy.allow()

    def check_merge(self, info):
        if self.fail:
            raise RuntimeError("boom")
        self.seen.append(info)
        return policy.deny(self.merge_reason) if self.merge_reason else policy.allow()


class Account(account.AccountPlugin):
    def __init__(self, code=0):
        self.calls, self.code = [], code

    def run(self, args):
        self.calls.append(list(args))
        print("account plugin ran:", " ".join(args))
        return self.code


class EP:
    def __init__(self, name, factory):
        self.name, self._factory = name, factory

    def load(self):
        return self._factory


def install(monkeypatch, table):
    """``table``: group -> list of (name, factory)."""
    monkeypatch.setattr(
        plugins, "entry_points",
        lambda group: [EP(n, f) for n, f in table.get(group, [])])


def install_one(monkeypatch, group, obj, name="pro"):
    install(monkeypatch, {group: [(name, lambda repo_root: obj)]})
    return obj


@pytest.fixture(autouse=True)
def clear_cache():
    plugins.reset()
    yield
    plugins.reset()


@pytest.fixture(autouse=True)
def everything_available(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cli: f"/usr/bin/{cli}")
    monkeypatch.setattr(quota, "headroom", lambda provider, cfg=None, repo_root=None: 100.0)


def config(repo, **kw):
    (repo / ".copse").mkdir(exist_ok=True)
    (repo / ".copse" / "config.json").write_text(json.dumps(kw))


@pytest.fixture
def boss(db, repo, monkeypatch):
    config(repo, pipeline=False)
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    monkeypatch.setenv("COPSE_AGENT_ID", "boss")
    return ws


def fake_spawn(db, ws, profile, *, prompt=None, parent_id=None, mode="handoff", done_when=None, **kw):
    a = Agent(agents.new_id(), ws.id, profile, "claude", parent_id, mode, "processing", "",
              None, time.time(), task=prompt, done_when=done_when)
    db.add_agent(a)
    return a


# -- the loader -------------------------------------------------------------------------


def test_nothing_installed_selects_nothing(repo, monkeypatch):
    install(monkeypatch, {})
    cfg = RepoConfig()
    for group in plugins.GROUPS:
        assert plugins.installed(group) == []
        assert plugins.select(group, cfg, str(repo)) is None
    assert policy.check_assign(cfg, str(repo), "developer", "t", "assign").allowed
    assert account.plugin(cfg, str(repo)) is None


def test_exactly_one_installed_is_the_default(repo, monkeypatch):
    rec = Recorder()
    install(monkeypatch, {plugins.EVENTS: [("pro", lambda r: rec)]})
    assert plugins.select(plugins.EVENTS, RepoConfig(), str(repo)) is rec


def test_several_installed_need_the_config_to_choose(repo, monkeypatch):
    a, b = Recorder(), Recorder()
    install(monkeypatch, {plugins.EVENTS: [("a", lambda r: a), ("b", lambda r: b)]})
    assert plugins.installed(plugins.EVENTS) == ["a", "b"]
    assert plugins.select(plugins.EVENTS, RepoConfig(), str(repo)) is None
    assert plugins.select(plugins.EVENTS, RepoConfig(plugins={"events": "b"}), str(repo)) is b
    assert plugins.select(plugins.EVENTS, RepoConfig(plugins={"events": "off"}), str(repo)) is None
    assert plugins.select(plugins.EVENTS, RepoConfig(plugins={"events": "nope"}), str(repo)) is None


def test_plugins_config_is_read_per_group(repo):
    config(repo, plugins={"events": "x", "policy": "off"})
    (repo / ".copse" / "config.local.json").write_text(json.dumps({"plugins": {"policy": "y"}}))
    assert load_repo_config(repo).plugins == {"events": "x", "policy": "y"}


def test_a_broken_factory_or_listing_never_raises(repo, monkeypatch):
    def broken(repo_root):
        raise RuntimeError("import failed")

    install(monkeypatch, {plugins.POLICY: [("pro", broken)]})
    cfg = RepoConfig()
    assert plugins.select(plugins.POLICY, cfg, str(repo)) is None
    assert policy.check_assign(cfg, str(repo), "developer", "t", "assign").allowed

    def explode(group):
        raise RuntimeError("metadata unreadable")

    monkeypatch.setattr(plugins, "entry_points", explode)
    plugins.reset()
    assert plugins.installed(plugins.EVENTS) == []
    assert plugins.select(plugins.EVENTS, cfg, str(repo)) is None


def test_loading_is_cached_per_repo(repo, tmp_path, monkeypatch):
    made = []

    def factory(repo_root):
        made.append(repo_root)
        return Recorder()

    install(monkeypatch, {plugins.EVENTS: [("pro", factory)]})
    cfg = RepoConfig()
    first = plugins.select(plugins.EVENTS, cfg, str(repo))
    assert plugins.select(plugins.EVENTS, cfg, str(repo)) is first
    other = plugins.select(plugins.EVENTS, cfg, str(tmp_path))
    assert other is not first and made == [str(repo), str(tmp_path)]


# -- events at each call site -----------------------------------------------------------


def test_a_failing_events_plugin_never_breaks_the_operation(db, repo, boss, monkeypatch):
    install_one(monkeypatch, plugins.EVENTS, Recorder(fail=True))
    monkeypatch.setattr(agents, "spawn", fake_spawn)
    out = asyncio.run(mcp_server.assign(task="do A", branch="feat-a"))
    assert "Started worker" in out


def test_assign_and_handoff_emit_without_task_text(db, repo, boss, monkeypatch):
    rec = install_one(monkeypatch, plugins.EVENTS, Recorder())
    monkeypatch.setattr(agents, "spawn", fake_spawn)
    monkeypatch.setattr(mcp_server, "_await_worker", lambda db_, agent_id, wait: "done")
    asyncio.run(mcp_server.assign(task="do A secret", branch="feat-a"))
    asyncio.run(mcp_server.handoff(task="do B secret", branch="feat-b"))
    assert rec.kinds() == ["assign", "handoff"]
    e = rec.events[0]
    assert e.repo_root == str(repo) and e.branch == "feat-a" and e.actor == "boss"
    assert e.profile == "developer" and e.provider == "claude" and e.agent_id
    assert e.at and abs(time.time() - e.at) < 60
    for ev in rec.events:
        assert "secret" not in json.dumps(ev.__dict__)


def test_a_queued_task_emits_when_it_starts(db, repo, boss, monkeypatch):
    rec = install_one(monkeypatch, plugins.EVENTS, Recorder())
    monkeypatch.setattr(agents, "spawn", fake_spawn)
    asyncio.run(mcp_server.assign(task="first", branch="feat-a"))
    out = asyncio.run(mcp_server.assign(task="second", branch="feat-b", depends_on=["feat-a"]))
    assert out.startswith("Queued")
    assert rec.kinds() == ["assign"]
    from copse import tasks

    [queued] = db.list_tasks(str(repo), state="pending")
    tasks.start_queued(db, queued)
    assert rec.kinds() == ["assign", "assign"] and rec.events[-1].branch == "feat-b"


@pytest.fixture
def piped(db, repo, monkeypatch):
    """A supervisor, a worker on a branch with a commit, and a reviewer copse
    can start without a process (as in tests/test_pipeline.py)."""
    config(repo, review=True, auto_merge_default_branch=True)
    root = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", root.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    ws = workspaces.create(db, str(repo), "feat").workspace
    (Path(ws.path) / "new.py").write_text("x = 1\n")
    sh("git add new.py && git commit -qm work", Path(ws.path))
    db.add_agent(Agent("w1", ws.id, "developer", "claude", "boss", "assign", "processing", "@0",
                       None, time.time(), task="the task text"))

    def fake_review(db_, caller, ws_, profile=None, focus=None, cfg=None):
        db_.add_agent(Agent("rev0", ws_.id, "reviewer", "claude", caller.id, "review", "idle",
                            "@0", None, time.time()))
        return db_.get_agent("rev0")

    monkeypatch.setattr(agents, "request_review", fake_review)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "reconcile", lambda db_, a, **kw: a)
    monkeypatch.setattr(agents, "warm_checks", lambda ws_: None)
    monkeypatch.setattr(agents, "close_later", lambda agent_id, delay=5.0: None)
    monkeypatch.setattr(pipeline, "_detach", lambda argv: None)
    monkeypatch.setattr(agents, "_stop", lambda db_, a: None)
    return root, ws


def verdict(db, ws, approved, summary="fine"):
    """A reviewer's verdict, the way agents.submit_review records and hands
    it to the pipeline (without the reviewer's own report and close)."""
    from copse import gates

    reviewer = db.get_agent("rev0")
    db.add_review(ws.id, gates.head(ws), reviewer.id, approved, summary)
    pipeline.note_review(db, ws, approved, reviewer=reviewer)
    return pipeline.on_review(db, reviewer, ws, approved, summary)


def test_review_merge_and_remove_are_emitted_from_the_pipeline(db, piped, repo, monkeypatch):
    rec = install_one(monkeypatch, plugins.EVENTS, Recorder())
    root, ws = piped
    agents.report_result(db, "w1", "added new.py")
    assert verdict(db, ws, True) is True
    assert db.get_workspace(ws.id) is None
    assert rec.kinds() == ["review", "merge", "remove"]
    review, merge, remove = rec.events
    assert review.approved is True and review.actor == "rev0" and review.agent_id == "w1"
    assert merge.branch == "feat" and merge.agent_id == "w1"
    assert merge.actor == "boss"  # the pipeline merges on the supervisor's behalf
    assert remove.merged is True and remove.agent_id == "w1" and remove.actor == "rev0"
    for e in rec.events:
        assert e.repo_root == str(repo) and e.profile == "developer"
        assert "task text" not in json.dumps(e.__dict__)


def test_changes_requested_is_a_review_event_too(db, piped, monkeypatch):
    rec = install_one(monkeypatch, plugins.EVENTS, Recorder())
    root, ws = piped
    agents.report_result(db, "w1", "added new.py")
    assert verdict(db, ws, False, "nope") is True
    assert rec.kinds() == ["review"] and rec.events[0].approved is False


def test_removing_an_unmerged_worktree_emits_remove(db, piped, monkeypatch):
    rec = install_one(monkeypatch, plugins.EVENTS, Recorder())
    root, ws = piped
    monkeypatch.setenv("COPSE_AGENT_ID", "boss")
    out = mcp_server.remove_workspace(ws.id)
    assert out.startswith("Removed")
    [e] = rec.events
    assert e.kind == "remove" and e.merged is False and e.actor == "boss" and e.agent_id == "w1"


def test_merge_workspace_emits_merge(db, piped, monkeypatch):
    rec = install_one(monkeypatch, plugins.EVENTS, Recorder())
    root, ws = piped
    config(Path(root.repo_root), review=False, pipeline=False)
    monkeypatch.setenv("COPSE_AGENT_ID", "boss")
    out = asyncio.run(mcp_server.merge_workspace(ws.id))
    assert out.startswith("Merged"), out
    assert rec.kinds() == ["merge"] and rec.events[0].actor == "boss"


# -- policy -----------------------------------------------------------------------------


def test_policy_deny_blocks_assign_and_handoff_with_the_reason(db, repo, boss, monkeypatch):
    gate = install_one(monkeypatch, plugins.POLICY, Gate(assign="no heavy tasks after hours"))
    spawned = []
    monkeypatch.setattr(agents, "spawn", lambda *a, **kw: spawned.append(1) or fake_spawn(*a, **kw))
    out = asyncio.run(mcp_server.assign(task="do A", branch="feat-a", weight="heavy",
                                        files=["a.py"]))
    assert out == "Not started: the repo's policy refused it: no heavy tasks after hours"
    out = asyncio.run(mcp_server.handoff(task="do B", branch="feat-b"))
    assert out.startswith("Not started: the repo's policy refused it")
    assert spawned == [] and db.list_tasks(str(repo)) == []
    info = gate.seen[0]
    assert info.mode == "assign" and info.weight == "heavy" and info.files == ("a.py",)
    assert info.profile == "developer-heavy" and info.provider == "claude" and info.actor == "boss"
    assert info.branch == "feat-a" and info.repo_root == str(repo)
    assert gate.seen[1].mode == "handoff"


def test_policy_allow_lets_assign_through(db, repo, boss, monkeypatch):
    install_one(monkeypatch, plugins.POLICY, Gate())
    monkeypatch.setattr(agents, "spawn", fake_spawn)
    assert "Started worker" in asyncio.run(mcp_server.assign(task="do A", branch="feat-a"))


def test_policy_deny_blocks_merge_workspace(db, piped, monkeypatch):
    gate = install_one(monkeypatch, plugins.POLICY, Gate(merge="needs two approvals"))
    root, ws = piped
    config(Path(root.repo_root), review=False, pipeline=False)
    monkeypatch.setenv("COPSE_AGENT_ID", "boss")
    out = asyncio.run(mcp_server.merge_workspace(ws.id))
    assert out == "Not merged: the repo's policy refused it: needs two approvals"
    assert sh("git log --oneline main", Path(root.repo_root)).count("\n") == 0  # still one commit
    [info] = gate.seen
    assert info.branch == "feat" and info.base_branch == "main" and info.agent_id == "w1"
    assert info.workspace_id == ws.id and info.actor == "boss" and info.profile == "developer"


def test_policy_deny_blocks_the_pipelines_own_merge(db, piped, monkeypatch):
    install_one(monkeypatch, plugins.POLICY, Gate(merge="frozen"))
    root, ws = piped
    agents.report_result(db, "w1", "added new.py")
    assert verdict(db, ws, True) is True
    msg = db.pop_pending("boss")
    assert msg and "couldn't be merged" in msg.body and "frozen" in msg.body, msg
    assert Path(ws.path).is_dir()


def test_the_policy_sees_how_many_workers_are_running(db, repo, boss, monkeypatch):
    gate = install_one(monkeypatch, plugins.POLICY, Gate())
    monkeypatch.setattr(agents, "spawn", fake_spawn)
    assert "Started worker" in asyncio.run(mcp_server.assign(task="do A", branch="feat-a"))
    assert "Started worker" in asyncio.run(mcp_server.assign(task="do B", branch="feat-b"))
    assert [i.running_workers for i in gate.seen] == [0, 1]


def test_a_broken_policy_plugin_refuses(db, repo, boss, monkeypatch):
    install_one(monkeypatch, plugins.POLICY, Gate(fail=True))
    monkeypatch.setattr(agents, "spawn", fake_spawn)
    out = asyncio.run(mcp_server.assign(task="do A", branch="feat-a"))
    assert "Not started" in out and "failed" in out, out


def test_a_configured_policy_that_wont_load_refuses(repo, monkeypatch):
    from copse import policy
    from copse.config import RepoConfig

    install(monkeypatch, {})
    cfg = RepoConfig()
    cfg.plugins["policy"] = "pro"
    d = policy.check_assign(cfg, str(repo), "dev", "do A", "assign")
    assert not d.allowed and "'pro'" in d.reason
    cfg.plugins["policy"] = "off"
    assert policy.check_assign(cfg, str(repo), "dev", "do A", "assign").allowed


# -- copse account ----------------------------------------------------------------------


def test_account_without_a_plugin_says_so_and_exits_0(repo, monkeypatch):
    install(monkeypatch, {})
    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["account", "status"])
    assert res.exit_code == 0, res.output
    assert "copse Pro isn't installed" in res.output


def test_account_passes_its_arguments_through(repo, monkeypatch):
    acct = install_one(monkeypatch, plugins.ACCOUNT, Account(code=3))
    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["account", "login", "--token-from", "env", "-v"])
    assert acct.calls == [["login", "--token-from", "env", "-v"]]
    assert res.exit_code == 3, res.output
    assert "account plugin ran: login --token-from env -v" in res.output
    res = CliRunner().invoke(app, ["account"])
    assert acct.calls[-1] == []


def test_account_outside_a_repo_still_works(tmp_path, monkeypatch):
    acct = install_one(monkeypatch, plugins.ACCOUNT, Account())
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    res = CliRunner().invoke(app, ["account", "whoami"])
    assert res.exit_code == 0, res.output
    assert acct.calls == [["whoami"]]
