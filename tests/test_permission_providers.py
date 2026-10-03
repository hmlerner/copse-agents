"""The permission policy for Codex and Antigravity (agy) workers: their hook
payloads as copse Requests, their answers, the one-time Codex hook trust, and
the copy of copse's rules kept in agy's own settings."""

import json
import os
import time

import pytest
from typer.testing import CliRunner

from copse import agents, antigravity, codex_hook, permissions, workspaces
from copse.cli import app
from copse.config import set_local
from copse.db import Agent
from copse.permissions import Decision, Rule, decide_all, from_agy, from_codex


@pytest.fixture
def ws(db, repo):
    return workspaces.create(db, str(repo), "feature").workspace


def worker(db, ws, provider="codex", id_="c1"):
    a = Agent(id_, ws.id, "developer", provider, "boss", "assign", "processing", f"%{id_}", None, time.time())
    db.add_agent(a)
    return a


def turn_on(ws):
    set_local(ws.repo_root, "permission_policy", "on")


def codex_payload(tool, tool_input, cwd):
    return {"session_id": "s", "turn_id": "t", "cwd": cwd, "hook_event_name": "PermissionRequest",
            "model": "m", "permission_mode": "default", "transcript_path": None,
            "tool_name": tool, "tool_input": tool_input}


PATCH = """*** Begin Patch
*** Add File: new.txt
+hello
*** Update File: src/app.py
@@
-a
+b
*** Delete File: old.txt
*** Update File: a.txt
*** Move to: b.txt
*** End Patch
"""


# -- Codex: payload -> requests ----------------------------------------------------------------


def test_codex_bash_mcp_and_other(ws):
    [r] = from_codex(codex_payload("Bash", {"command": "git status", "description": None}, ws.path),
                     ws.path, ws.repo_root)
    assert (r.provider, r.kind, r.tool, r.command, r.cwd) == ("codex", "bash", "Bash", "git status", ws.path)
    [r] = from_codex(codex_payload("mcp__github__create_issue", {"title": "x"}, ws.path))
    assert (r.kind, r.tool) == ("mcp", "mcp__github__create_issue")
    [r] = from_codex(codex_payload("web_search", {}, ws.path))
    assert r.kind == "other"
    assert from_codex({"tool_input": {}}) is None
    [r] = from_codex(codex_payload("Bash", {"command": ["git", "log", "-1"]}, ws.path))
    assert r.command == "git log -1"


def test_codex_patch_is_a_request_per_file(ws):
    reqs = from_codex(codex_payload("apply_patch", {"command": PATCH}, ws.path), ws.path, ws.repo_root)
    got = [(r.kind, os.path.relpath(r.path, ws.path)) for r in reqs]
    assert got == [("write", "new.txt"), ("edit", "src/app.py"), ("write", "old.txt"),
                   ("edit", "a.txt"), ("write", "b.txt")]
    assert all(r.tool == "apply_patch" for r in reqs)
    # No file headers: one request without a path, which can only be ask.
    [r] = from_codex(codex_payload("apply_patch", {"command": "garbage"}, ws.path))
    assert r.kind == "edit" and r.path is None


def test_a_patch_is_allowed_only_if_every_file_is(ws):
    two = "*** Begin Patch\n*** Update File: a.py\n*** Update File: b.py\n*** End Patch\n"
    reqs = from_codex(codex_payload("apply_patch", {"command": two}, ws.path), ws.path, ws.repo_root)
    allow_a = Rule("edit", "a.py", "exact", "allow")
    allow_b = Rule("edit", "b.py", "exact", "allow")
    deny_b = Rule("edit", "b.py", "exact", "deny")
    assert decide_all(reqs, rules=[allow_a, allow_b]).decision == "allow"
    assert decide_all(reqs, rules=[allow_a]).decision == "ask"
    assert decide_all(reqs, rules=[allow_a, deny_b]).decision == "deny"
    assert decide_all([], rules=[]).decision == "ask"


def test_codex_output_shape():
    allow = permissions.codex_output(Decision("allow", "ok"))
    assert allow == {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                            "decision": {"behavior": "allow"}}}
    deny = permissions.codex_output(Decision("deny", "no"))
    assert deny["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
    assert deny["hookSpecificOutput"]["decision"] == {"behavior": "deny", "message": "copse: no"}
    assert permissions.codex_output(Decision("ask", "?")) is None


# -- Codex: the hook ---------------------------------------------------------------------------


def test_codex_hook_decides_and_records(db, ws):
    a = worker(db, ws)
    turn_on(ws)
    out = agents.hook_main(db, a.id, "codex-permission-request",
                           json.dumps(codex_payload("Bash", {"command": "git push"}, ws.path)))
    assert json.loads(out)["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    out = agents.hook_main(db, a.id, "codex-permission-request",
                           json.dumps(codex_payload("Bash", {"command": "git status"}, ws.path)))
    assert json.loads(out)["hookSpecificOutput"]["decision"] == {"behavior": "allow"}
    # Ask: no output, and nothing kept to learn from (Codex has no tool_use_id).
    assert agents.hook_main(db, a.id, "codex-permission-request",
                            json.dumps(codex_payload("Bash", {"command": "make deploy"}, ws.path))) == ""
    assert db.latest_permission_request(a.id) is None
    rows = db.list_history(ws.repo_root, "permission")
    assert {r.result.split(":")[0] for r in rows} == {"deny", "allow", "ask"}


def test_codex_hook_off_or_broken_is_no_output(db, ws, monkeypatch):
    a = worker(db, ws)
    payload = json.dumps(codex_payload("Bash", {"command": "git push"}, ws.path))
    assert agents.hook_main(db, a.id, "codex-permission-request", payload) == ""  # policy off
    turn_on(ws)
    assert agents.hook_main(db, a.id, "codex-permission-request", "{not json") == ""
    assert agents.hook_main(db, a.id, "codex-permission-request", "[1, 2]") == ""

    def boom(*a, **k):
        raise RuntimeError("broken")

    monkeypatch.setattr(permissions, "decide_all", boom)
    assert agents.hook_main(db, a.id, "codex-permission-request", payload) == ""


def test_codex_hook_cli_finds_its_agent_in_the_environment(db, ws, monkeypatch):
    a = worker(db, ws)
    turn_on(ws)
    monkeypatch.setenv("COPSE_AGENT_ID", a.id)
    res = CliRunner().invoke(app, ["_hook", "codex-permission-request"],
                             input=json.dumps(codex_payload("Bash", {"command": "git push"}, ws.path)))
    assert res.exit_code == 0
    assert json.loads(res.output)["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    res = CliRunner().invoke(app, ["_hook", "codex-permission-request"], input="garbage")
    assert res.exit_code == 0 and res.output == ""


# -- Codex: trusting the hook once -----------------------------------------------------------


class FakeAppServer:
    """Stands in for `codex app-server`: lists copse's hook, and stores trust
    the way Codex does (config.toml hooks.state)."""
    calls: list = []

    def __init__(self, binary, flags, timeout=30.0):
        self.flags = flags

    def call(self, method, params):
        FakeAppServer.calls.append((method, params))
        config = codex_hook.codex_home() / "config.toml"
        trusted = config.exists() and "sha256:abc" in config.read_text()
        if method == "hooks/list":
            command = json.loads(self.flags[1].split("command=", 1)[1].split(",timeout=")[0])
            return {"data": [{"cwd": "/", "errors": [], "warnings": [], "hooks": [{
                "key": "/<session-flags>/config.toml:permission_request:0:0", "source": "sessionFlags",
                "command": command, "currentHash": "sha256:abc",
                "trustStatus": "trusted" if trusted else "untrusted"}]}]}
        if method == "config/batchWrite":
            [edit] = params["edits"]
            [(key, value)] = edit["value"].items()
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(f'[hooks.state."{key}"]\ntrusted_hash = "{value["trusted_hash"]}"\n')
            return {"status": "ok"}
        raise AssertionError(method)

    def close(self):
        pass


def test_install_codex_hook_dry_run_then_yes(monkeypatch):
    FakeAppServer.calls = []
    monkeypatch.setattr(codex_hook, "_AppServer", FakeAppServer)
    r = CliRunner()
    res = r.invoke(app, ["permissions", "install-codex-hook"])
    assert res.exit_code == 0, res.output
    assert 'hooks.state."/<session-flags>/config.toml:permission_request:0:0"' in res.output
    assert "run again with --yes" in res.output
    assert not (codex_hook.codex_home() / "config.toml").exists()
    assert permissions.load_store().codex_hook == {}
    assert [m for m, _ in FakeAppServer.calls] == ["hooks/list"]

    res = r.invoke(app, ["permissions", "install-codex-hook", "--yes"])
    assert res.exit_code == 0, res.output
    assert "trusted" in res.output
    rec = permissions.load_store().codex_hook
    assert rec == {"command": codex_hook.hook_command(),
                   "key": "/<session-flags>/config.toml:permission_request:0:0", "hash": "sha256:abc"}
    assert codex_hook.trusted()
    assert "already trusted" in r.invoke(app, ["permissions", "install-codex-hook"]).output


def test_codex_launch_gets_the_hook_only_when_on_and_trusted(ws, monkeypatch):
    from copse.profiles import load_profile
    from copse.providers import Codex, LaunchContext

    def argv():
        return Codex().command(LaunchContext("c1", load_profile("developer"), None, cwd=ws.path))

    flag = codex_hook.config_flags()[1]
    assert flag not in argv()  # off
    turn_on(ws)
    assert flag not in argv()  # on, but not trusted
    monkeypatch.setattr(codex_hook, "_AppServer", FakeAppServer)
    codex_hook.trust("codex", codex_hook.inspect("codex"))
    a = argv()
    assert a[a.index(flag) - 1] == "-c"
    assert "--agent" not in flag  # one command for every agent: one trust
    # Codex's config no longer has the trust (say the person removed it): no hook.
    (codex_hook.codex_home() / "config.toml").write_text("")
    assert flag not in argv()


# -- agy: payload -> request -------------------------------------------------------------------


def agy_payload(name, args, cwd="/w"):
    return {"toolCall": {"name": name, "args": args}, "stepIdx": 3, "conversationId": "c",
            "workspacePaths": [cwd]}


def test_agy_mapping(ws):
    r = from_agy(agy_payload("run_command", {"CommandLine": '"git status"', "Cwd": json.dumps(ws.path)}),
                 ws.path, ws.repo_root)
    assert (r.provider, r.kind, r.command, r.cwd) == ("antigravity", "bash", "git status", ws.path)
    r = from_agy(agy_payload("view_file", {"AbsolutePath": "/etc/hosts"}))
    assert (r.kind, r.path) == ("read", "/etc/hosts")
    r = from_agy(agy_payload("write_to_file", {"TargetFile": "notes.md"}, ws.path))
    assert (r.kind, r.path) == ("write", os.path.join(ws.path, "notes.md"))
    assert from_agy(agy_payload("replace_file_content", {"TargetFile": "/x"})).kind == "edit"
    r = from_agy(agy_payload("read_url_content", {"Url": "https://example.com/a"}))
    assert (r.kind, r.url) == ("fetch", "https://example.com/a")
    r = from_agy(agy_payload("call_mcp_tool", {"ServerName": '"github"', "ToolName": '"create_issue"'}))
    assert (r.kind, r.tool) == ("mcp", "mcp__github__create_issue")
    assert from_agy(agy_payload("mcp_copse_get_progress", {})).kind == "mcp"
    assert from_agy(agy_payload("browser_click", {})).kind == "other"
    assert from_agy({"stepIdx": 1}) is None
    assert from_agy({"toolCall": "x"}) is None


def test_agy_output_is_deny_or_ask():
    assert permissions.agy_output(Decision("deny", "no")) == {"decision": "deny", "reason": "copse: no"}
    for d in (Decision("allow", "ok"), Decision("ask", "?"), None):
        out = permissions.agy_output(d)
        assert out["decision"] == "ask" and out["reason"]


# -- agy: the hook never answers nothing -------------------------------------------------------


def test_agy_pre_tool_denies_and_otherwise_asks(db, ws, monkeypatch):
    a = worker(db, ws, provider="antigravity", id_="g1")
    turn_on(ws)
    monkeypatch.setenv("COPSE_AGENT_ID", a.id)
    push = json.dumps(agy_payload("run_command", {"CommandLine": "git push", "Cwd": ws.path}, ws.path))
    out = json.loads(antigravity.pre_tool_main(push, db_factory=lambda: db))
    assert out["decision"] == "deny" and "git push" in out["reason"]
    status = json.dumps(agy_payload("run_command", {"CommandLine": "git status"}, ws.path))
    assert json.loads(antigravity.pre_tool_main(status, db_factory=lambda: db))["decision"] == "ask"
    [row] = db.list_history(ws.repo_root, "permission")
    assert row.result.startswith("deny:")


@pytest.mark.parametrize("stdin", ["", "{broken", "[]", "null", '{"toolCall": 5}', "\x00\xff"])
def test_agy_pre_tool_never_answers_nothing(db, ws, monkeypatch, stdin):
    a = worker(db, ws, provider="antigravity", id_="g1")
    turn_on(ws)
    monkeypatch.setenv("COPSE_AGENT_ID", a.id)
    assert json.loads(antigravity.pre_tool_main(stdin, db_factory=lambda: db))["decision"] == "ask"


def test_agy_pre_tool_failures_are_ask(db, monkeypatch):
    def broken_db():
        raise RuntimeError("no db")

    out = antigravity.pre_tool_main(json.dumps(agy_payload("run_command", {"CommandLine": "x"})),
                                    db_factory=broken_db)
    assert json.loads(out)["decision"] == "ask"
    # No agent (agy outside copse, in a checkout copse set up): ask, never empty.
    monkeypatch.setattr(antigravity, "agent_from_parent", lambda: None)
    assert json.loads(antigravity.pre_tool_main("{}", db_factory=lambda: db))["decision"] == "ask"
    # Even copse's own answer-maker failing still answers ask.
    monkeypatch.setattr(permissions, "agy_output", lambda d: (_ for _ in ()).throw(RuntimeError()))
    assert json.loads(antigravity.pre_tool_main("{}", db_factory=lambda: db))["decision"] == "ask"


def test_agy_pre_tool_cli_always_prints_json(monkeypatch):
    monkeypatch.setattr(antigravity, "agent_from_parent", lambda: None)
    for stdin in ("", "garbage", json.dumps(agy_payload("run_command", {"CommandLine": "git push"}))):
        res = CliRunner().invoke(app, ["_hook", "agy-pre-tool"], input=stdin)
        assert res.exit_code == 0
        assert json.loads(res.stdout)["decision"] == "ask"


def test_agy_install_adds_the_pre_tool_hook_only_with_the_policy(ws, monkeypatch):
    monkeypatch.setattr(antigravity, "tool_names", lambda: ["report_result"])
    antigravity.install(ws.path)
    hooks = json.loads(open(os.path.join(ws.path, ".agents", "hooks.json")).read())["copse"]
    assert "PreToolUse" not in hooks
    antigravity.install(ws.path, permission_policy=True)
    hooks = json.loads(open(os.path.join(ws.path, ".agents", "hooks.json")).read())["copse"]
    [entry] = hooks["PreToolUse"]
    assert "run_command" in entry["matcher"] and "agy-pre-tool" in entry["hooks"][0]["command"]


# -- agy: mirroring copse's rules into its settings --------------------------------------------


ORIGINAL = """{
    "colorScheme": "tokyo night",
    "trustedWorkspaces": ["/a"],
    "permissions": {
        "allow": [
            "command(regex:^git status$)",
            "command(npm)"
        ],
        "ask": ["command(*)"]
    }
}
"""


def test_agy_sync_adds_and_removes_only_its_own_entries(agy_settings, monkeypatch, ws):
    agy_settings.parent.mkdir(parents=True)
    agy_settings.write_text(ORIGINAL)
    monkeypatch.chdir(ws.repo_root)
    set_local(ws.repo_root, "checks", ["uv run pytest -q"])
    r = CliRunner()

    # Policy off and nothing of copse's there: nothing changes.
    res = r.invoke(app, ["permissions", "sync-agy"])
    assert res.exit_code == 0 and "already in sync" in res.output
    assert agy_settings.read_text() == ORIGINAL

    turn_on(ws)
    res = r.invoke(app, ["permissions", "sync-agy"])
    assert res.exit_code == 0, res.output
    data = json.loads(agy_settings.read_text())
    allow, deny = data["permissions"]["allow"], data["permissions"]["deny"]
    assert allow[:2] == ["command(regex:^git status$)", "command(npm)"]  # the person's, first, untouched
    assert "command(regex:^uv run pytest -q$)" in allow and "command(regex:^git diff$)" in allow
    assert allow.count("command(regex:^git status$)") == 1  # theirs already; not copse's
    assert "command(git push)" in deny and f"read_file({os.path.expanduser('~/.ssh')})" in deny
    assert data["colorScheme"] == "tokyo night" and data["permissions"]["ask"] == ["command(*)"]
    assert agy_settings.read_text().startswith('{\n    "colorScheme"')  # same indentation
    backup = agy_settings.with_name("settings.json.copse-backup")
    assert backup.read_text() == ORIGINAL
    managed = permissions.load_store().agy_managed
    assert "command(regex:^git status$)" not in managed["allow"]

    # Idempotent.
    before = agy_settings.read_text()
    assert "already in sync" in r.invoke(app, ["permissions", "sync-agy"]).output
    assert agy_settings.read_text() == before

    # Changing copse's rules re-syncs; the backup is made only once.
    res = r.invoke(app, ["permissions", "allow", "bash", "make lint"])
    rule_id = res.output.split(":")[0]
    assert "command(regex:^make lint$)" in json.loads(agy_settings.read_text())["permissions"]["allow"]
    r.invoke(app, ["permissions", "deny", "bash", "rm -rf", "--prefix"])
    assert "command(regex:^rm -rf)" in json.loads(agy_settings.read_text())["permissions"]["deny"]
    r.invoke(app, ["permissions", "forget", rule_id])
    assert "command(regex:^make lint$)" not in json.loads(agy_settings.read_text())["permissions"]["allow"]
    assert backup.read_text() == ORIGINAL

    # Something the person adds meanwhile stays.
    data = json.loads(agy_settings.read_text())
    data["permissions"]["allow"].append("command(ls)")
    agy_settings.write_text(json.dumps(data, indent=4) + "\n")

    # Off: copse's entries go; everything else is as it was.
    set_local(ws.repo_root, "permission_policy", "off")
    res = r.invoke(app, ["permissions", "sync-agy"])
    assert res.exit_code == 0 and "removed" in res.output
    data = json.loads(agy_settings.read_text())
    expected = json.loads(ORIGINAL)
    expected["permissions"]["allow"].append("command(ls)")
    assert data == expected
    assert permissions.load_store().agy_managed == {}


def test_agy_sync_creates_and_removes_its_own_file(agy_settings, ws):
    antigravity.sync_permissions(ws.repo_root, on=True, checks=[])
    assert json.loads(agy_settings.read_text())["permissions"]["allow"]
    assert not agy_settings.with_name("settings.json.copse-backup").exists()
    antigravity.sync_permissions(ws.repo_root, on=False)
    assert not agy_settings.exists()


def test_agy_sync_leaves_a_file_it_cannot_read(agy_settings, ws):
    agy_settings.parent.mkdir(parents=True)
    agy_settings.write_text("{ not json")
    with pytest.raises(antigravity.SettingsError):
        antigravity.sync_permissions(ws.repo_root, on=True, checks=[])
    assert agy_settings.read_text() == "{ not json"


def test_agy_launch_syncs_only_with_the_policy(agy_settings, ws, monkeypatch):
    from copse.profiles import load_profile
    from copse.providers import Antigravity, LaunchContext

    monkeypatch.setattr(antigravity, "tool_names", lambda: ["report_result"])
    ctx = LaunchContext("g1", load_profile("developer"), "hi", cwd=ws.path)
    Antigravity().command(ctx)
    assert not agy_settings.exists()  # off: never touched
    turn_on(ws)
    Antigravity().command(ctx)
    assert "command(regex:^git log$)" in json.loads(agy_settings.read_text())["permissions"]["allow"]
    set_local(ws.repo_root, "permission_policy", "off")
    Antigravity().command(ctx)
    assert not agy_settings.exists()


def test_agy_mirror_never_widens_an_allow():
    rules = [Rule("bash", "echo $(id)", "exact", "allow"),     # copse never allows it
             Rule("bash", "npm *", "glob", "allow"),           # agy can't say it as narrowly
             Rule("bash", "a;b", "prefix", "allow"),
             Rule("read", "src/*.py", "glob", "allow"),
             Rule("read", "src", "prefix", "allow"),           # not a folder: could be src2/...
             Rule("fetch", "https://x.com/a", "exact", "allow"),
             Rule("read", "docs/", "prefix", "allow"),
             Rule("mcp", "mcp__gh__", "prefix", "allow"),
             Rule("bash", "npm test", "exact", "allow")]
    out = permissions.mirror_agy(rules, ["make check", "a && b"])
    assert out["allow"] == ["read_file(docs/)", "mcp(gh/*)", "command(regex:^npm test$)"]
    out = permissions.mirror_agy([*permissions.DEFAULT_RULES], ["make check", "a && b"])
    assert "command(regex:^make check$)" in out["allow"]
    assert not any("a && b" in e for e in out["allow"])
    assert not any("tracked" in e for e in out["allow"])
