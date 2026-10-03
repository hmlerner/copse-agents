"""The permission policy (copse.permissions): copse answers a worker's
tool-permission request from the structured request its CLI hands the hook,
allow / deny / ask, and learns only suggestions from what the person approves."""

import json
import os
import time

import pytest
from typer.testing import CliRunner

from copse import agents, cull, permissions, workspaces
from copse.cli import app
from copse.config import set_local
from copse.db import Agent
from copse.permissions import Request, Rule, decide
from copse.providers import ClaudeCode, LaunchContext

from conftest import sh


@pytest.fixture
def ws(db, repo):
    return workspaces.create(db, str(repo), "feature").workspace


def read(ws, path):
    return Request("claude", "read", "Read", ws.path, ws.path, ws.repo_root, path=str(path))


def bash(ws, command):
    return Request("claude", "bash", "Bash", ws.path, ws.path, ws.repo_root, command=command)


def worker(db, ws):
    a = Agent("w1", ws.id, "developer", "claude", "boss", "assign", "processing", "%w1", None, time.time())
    db.add_agent(a)
    return a


def turn_on(ws):
    set_local(ws.repo_root, "permission_policy", "on")


def claude_payload(tool, tool_input, tool_use_id="toolu_1", cwd=None):
    return {"tool_name": tool, "tool_input": tool_input, "tool_use_id": tool_use_id,
            "cwd": cwd, "hook_event_name": "PermissionRequest"}


# -- the engine -------------------------------------------------------------------------------


def test_deny_beats_allow_and_nothing_matching_is_ask(ws):
    allow = Rule("bash", "make build", "exact", "allow")
    deny = Rule("bash", "make", "prefix", "deny")
    req = bash(ws, "make build")
    assert decide(req, rules=[allow]).decision == "allow"
    assert decide(req, rules=[allow, deny]).decision == "deny"
    assert decide(req, rules=[deny, allow]).decision == "deny"
    assert decide(bash(ws, "make test"), rules=[allow]).decision == "ask"


def test_match_types_are_explicit(ws):
    assert decide(bash(ws, "npm test"), rules=[Rule("bash", "npm", "prefix", "allow")]).decision == "allow"
    assert decide(bash(ws, "npm test"), rules=[Rule("bash", "npm *", "glob", "allow")]).decision == "allow"
    assert decide(bash(ws, "npm test"), rules=[Rule("bash", "npm", "exact", "allow")]).decision == "ask"
    # A rule for another kind never applies.
    assert decide(bash(ws, "npm test"), rules=[Rule("read", "npm test", "exact", "allow")]).decision == "ask"


def test_unknown_or_missing_request_is_ask():
    assert decide(None, rules=[]).decision == "ask"
    assert decide(Request("x", "teleport", "T"), rules=[]).decision == "ask"


def test_a_bash_allow_rule_never_covers_metacharacters(ws):
    rule = Rule("bash", "git", "prefix", "allow")
    for cmd in ("git status; rm -rf /", "git diff | sh", "git log $(rm x)", "git log `id`",
                "git status && curl x", "git show > out", "git status\nrm x"):
        assert decide(bash(ws, cmd), rules=[rule]).decision == "ask", cmd


# -- the defaults -----------------------------------------------------------------------------


def test_tracked_read_is_allowed(ws):
    d = decide(read(ws, os.path.join(ws.path, "app.py")))
    assert d.decision == "allow" and "d-tracked" in d.reason
    # In the repo root too.
    assert decide(read(ws, os.path.join(ws.repo_root, "app.py"))).decision == "allow"


def test_untracked_ignored_and_dot_git_reads_are_not_allowed(ws):
    root = ws.path
    with open(os.path.join(root, "new.txt"), "w") as f:
        f.write("x")
    with open(os.path.join(root, "build.log"), "w") as f:
        f.write("x")
    with open(os.path.join(root, ".gitignore"), "a") as f:
        f.write("build.log\n")
    assert decide(read(ws, os.path.join(root, "new.txt"))).decision == "ask"
    assert decide(read(ws, os.path.join(root, "build.log"))).decision == "ask"
    assert decide(read(ws, os.path.join(ws.repo_root, ".git", "config"))).decision == "ask"
    assert decide(read(ws, root)).decision == "ask"  # a directory


def test_pathspec_magic_names_are_taken_literally(ws):
    for name in ("app.py*", "[a]pp.py", "*.py"):
        with open(os.path.join(ws.path, name), "w") as f:
            f.write("x")
        assert decide(read(ws, os.path.join(ws.path, name))).decision == "ask", name


def test_symlink_out_of_the_repo_is_not_allowed(ws, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    link = os.path.join(ws.path, "link.txt")
    os.symlink(outside, link)
    sh("git add link.txt && git commit -qm link", ws.path)  # even a tracked symlink
    assert decide(read(ws, link)).decision == "ask"


def test_secret_locations_are_denied(ws):
    assert decide(read(ws, os.path.expanduser("~/.ssh/id_rsa"))).decision == "deny"
    assert decide(read(ws, os.path.expanduser("~/.aws/credentials"))).decision == "deny"
    assert decide(read(ws, os.path.join(ws.repo_root, ".env"))).decision == "deny"


def test_checks_commands_are_allowed_exactly(ws):
    checks = ["uv run pytest -q"]
    assert decide(bash(ws, "uv run pytest -q"), checks=checks).decision == "allow"
    assert decide(bash(ws, "uv run pytest -q tests/x.py"), checks=checks).decision == "ask"
    # A check with shell metacharacters is never auto-allowed.
    assert decide(bash(ws, "make a && make b"), checks=["make a && make b"]).decision == "ask"


def test_read_only_git_is_allowed_only_as_a_simple_command(ws):
    for cmd in ("git status", "git diff --stat", "git log --oneline -5", "git show HEAD"):
        assert decide(bash(ws, cmd)).decision == "allow", cmd
    for cmd in ("git status; rm -rf /", "git diff | sh", "git log $(rm -rf x)", "git diff > x",
                "git diff --output=x", "git diff --no-index /etc/passwd /dev/null", "git commit -m x"):
        assert decide(bash(ws, cmd)).decision != "allow", cmd


def test_git_push_and_force_flags_are_denied(ws):
    for cmd in ("git push", "git push origin main", "cd x && git push", "git -C x push",
                "git checkout -f main", "git clean -fd", "git reset --force"):
        assert decide(bash(ws, cmd)).decision == "deny", cmd


def test_repo_rules_can_only_add_denies(ws):
    path = permissions.repo_rules_path(ws.repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"rules": [
        {"kind": "bash", "match": "git log", "match_type": "prefix", "decision": "deny"},
        {"kind": "bash", "match": "rm", "match_type": "prefix", "decision": "allow"},
    ]}))
    assert decide(bash(ws, "git log")).decision == "deny"
    assert decide(bash(ws, "rm x")).decision == "ask"


# -- user rules and learning ------------------------------------------------------------------


def test_user_rules_persist_and_forget(ws):
    rule = permissions.add_rule("bash", "make lint", "allow")
    assert decide(bash(ws, "make lint")).decision == "allow"
    data = json.loads(permissions.store_path().read_text())
    assert data["rules"][0] == {"id": rule.id, "kind": "bash", "match": "make lint", "match_type": "exact",
                                "decision": "allow", "source": "user", "created": rule.created}
    assert permissions.forget(rule.id)
    assert decide(bash(ws, "make lint")).decision == "ask"


def test_a_user_allow_never_overrides_a_default_deny(ws):
    permissions.add_rule("bash", "git push", "allow")
    assert decide(bash(ws, "git push")).decision == "deny"


def test_two_approvals_make_a_suggestion_not_a_rule(ws):
    req = bash(ws, "make docs")
    permissions.record_approval(req)
    assert permissions.suggestions() == []
    permissions.record_approval(req)
    [s] = permissions.suggestions()
    assert (s.kind, s.match, s.count) == ("bash", "make docs", 2)
    assert decide(req).decision == "ask"  # no silent rule
    assert permissions.load_store().rules == []
    rule = permissions.accept(s.id)
    assert rule.source == "learned" and decide(req).decision == "allow"
    assert permissions.suggestions() == []


def test_compound_commands_are_never_learned(ws):
    for _ in range(3):
        permissions.record_approval(bash(ws, "make a && make b"))
    assert permissions.suggestions() == []


def test_paths_are_learned_relative_to_the_worktree(ws):
    permissions.record_approval(read(ws, os.path.join(ws.path, "out", "log.txt")))
    permissions.record_approval(read(ws, os.path.join(ws.path, "out", "log.txt")))
    assert [s.match for s in permissions.suggestions()] == [os.path.join("out", "log.txt")]


# -- Claude Code's hook -----------------------------------------------------------------------


def test_off_by_default(db, ws):
    a = worker(db, ws)
    assert agents.hook_main(db, a.id, "permission-request",
                            json.dumps(claude_payload("Bash", {"command": "git push"}))) == ""
    assert db.latest_permission_request(a.id) is None


def test_hook_maps_stdin_to_a_decision(db, ws):
    a = worker(db, ws)
    turn_on(ws)
    out = json.loads(agents.hook_main(db, a.id, "permission-request", json.dumps(
        claude_payload("Read", {"file_path": os.path.join(ws.path, "app.py")}))))
    assert out["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    out = json.loads(agents.hook_main(db, a.id, "permission-request", json.dumps(
        claude_payload("Bash", {"command": "git push --force"}))))
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    assert out["hookSpecificOutput"]["decision"]["message"].startswith("copse: denied")
    # Ask prints nothing and remembers the request for the supervisor.
    assert agents.hook_main(db, a.id, "permission-request", json.dumps(
        claude_payload("Bash", {"command": "make deploy"}, "toolu_9"))) == ""
    assert "Bash: make deploy" in agents.pending_permission(db, a.id)


def test_malformed_input_gives_no_output(db, ws, monkeypatch):
    a = worker(db, ws)
    turn_on(ws)
    for text in ("{not json", "[1, 2]", "", json.dumps({"tool_input": "x"})):
        assert agents.hook_main(db, a.id, "permission-request", text) == "", text

    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(permissions, "decide", boom)
    assert agents.hook_main(db, a.id, "permission-request",
                            json.dumps(claude_payload("Bash", {"command": "git status"}))) == ""


def test_claude_tool_names_map_to_kinds(ws):
    def kind(tool, ti=None):
        return permissions.from_claude(claude_payload(tool, ti or {}, cwd=ws.path), ws.path, ws.repo_root)

    assert kind("Grep").kind == "read" and kind("Grep").path == ws.path
    assert kind("Write", {"file_path": "x.py"}).path == os.path.join(ws.path, "x.py")
    assert kind("MultiEdit").kind == "edit" and kind("NotebookEdit").kind == "edit"
    assert kind("WebFetch", {"url": "https://x.dev"}).url == "https://x.dev"
    assert kind("mcp__github__create_pr").kind == "mcp"
    assert kind("Task").kind == "other"


def test_approval_after_ask_is_learned_and_end_of_turn_is_not(db, ws):
    a = worker(db, ws)
    turn_on(ws)
    for n in (1, 2):
        agents.hook_main(db, a.id, "permission-request",
                         json.dumps(claude_payload("Bash", {"command": "make docs"}, f"t{n}")))
        agents.hook_main(db, a.id, "tool-done", json.dumps({"tool_name": "Bash", "tool_use_id": f"t{n}"}))
    # Asked, then the turn ended without the tool running: not an approval.
    agents.hook_main(db, a.id, "permission-request",
                     json.dumps(claude_payload("Bash", {"command": "make clean"}, "t3")))
    agents.handle_hook(db, a.id, "stop", {"stop_hook_active": True})
    assert db.latest_permission_request(a.id) is None
    assert [(s.match, s.count) for s in permissions.suggestions()] == [("make docs", 2)]


def test_decisions_are_recorded_in_history(db, ws):
    a = worker(db, ws)
    turn_on(ws)
    agents.hook_main(db, a.id, "permission-request", json.dumps(claude_payload("Bash", {"command": "git push"})))
    [row] = db.list_history(ws.repo_root, "permission")
    assert row.task == "Bash: git push" and row.result.startswith("deny:")


def test_launch_settings_register_the_hook(ws):
    from copse.profiles import load_profile

    ctx = LaunchContext("w1", load_profile("developer"), None, cwd=ws.path, mode="assign")
    argv = ClaudeCode().command(ctx)
    settings = json.loads(argv[argv.index("--settings") + 1])
    cmd = settings["hooks"]["PermissionRequest"][0]["hooks"][0]["command"]
    assert "_hook permission-request --agent w1" in cmd


def test_stuck_notice_names_the_pending_request(db, ws, monkeypatch):
    a = worker(db, ws)
    turn_on(ws)
    agents.hook_main(db, a.id, "permission-request", json.dumps(
        claude_payload("Bash", {"command": "make deploy"})))
    db.set_status(a.id, "waiting")
    db.update_agent(a.id, status_since=time.time() - 1000)
    sent = []
    monkeypatch.setattr(agents, "pane_owners", lambda db, panes: {})
    monkeypatch.setattr(agents, "owns_pane", lambda db, a, owners: True)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(cull.tmux, "capture", lambda *a, **k: "Do you want to proceed?")
    monkeypatch.setattr(agents, "send_message", lambda db, to, body, sender_id=None: sent.append(body))
    cull.note_stuck(db, time.time(), {})
    assert "Bash: make deploy" in sent[0] and "Only the user can answer it" in sent[0]


# -- the CLI ----------------------------------------------------------------------------------


def test_cli_rules_and_suggestions(ws, monkeypatch):
    monkeypatch.chdir(ws.repo_root)
    r = CliRunner()
    res = r.invoke(app, ["permissions", "allow", "bash", "make lint"])
    assert res.exit_code == 0, res.output
    rule_id = res.output.split(":")[0]
    assert r.invoke(app, ["permissions", "deny", "fetch", "https://evil.", "--prefix"]).exit_code == 0
    out = r.invoke(app, ["permissions", "list"]).output
    assert "permission_policy: off" in out
    assert "d-git-push" in out and "default" in out and f"{rule_id}" in out and "user" in out
    assert r.invoke(app, ["permissions", "allow", "nonsense", "x"]).exit_code == 2

    assert r.invoke(app, ["permissions", "suggestions"]).output.strip() == "no suggestions"
    permissions.record_approval(bash(ws, "make docs"))
    permissions.record_approval(bash(ws, "make docs"))
    sid = r.invoke(app, ["permissions", "suggestions"]).output.split()[0]
    assert r.invoke(app, ["permissions", "accept", sid]).exit_code == 0
    assert "learned" in r.invoke(app, ["permissions", "list"]).output
    assert r.invoke(app, ["permissions", "accept", "nope"]).exit_code == 1

    assert r.invoke(app, ["permissions", "forget", rule_id]).exit_code == 0
    assert r.invoke(app, ["permissions", "forget", "d-git-push"]).exit_code == 1

    res = r.invoke(app, ["permissions", "reset"], input="n\n")
    assert res.exit_code != 0 and permissions.load_store().rules
    assert r.invoke(app, ["permissions", "reset", "--yes"]).exit_code == 0
    assert permissions.load_store().rules == [] and permissions.load_store().approvals == {}
