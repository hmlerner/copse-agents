"""The PreToolUse hook: copse approves a shell command whose every part the
profile's allowed_tools cover, so a worker isn't stuck on a prompt for
`cd sub && git status`. Everything else is left to Claude Code."""

import json
import time
from dataclasses import replace

import pytest

from copse import agents, cull, profiles, workspaces
from copse.db import Agent
from copse.native.permissions import uncovered_part
from copse.profiles import load_profile
from copse.providers import ClaudeCode, LaunchContext


@pytest.fixture
def ws(db, repo):
    return workspaces.create(db, str(repo), "feature").workspace


def worker(db, ws, profile="developer", provider="claude"):
    a = Agent("w1", ws.id, profile, provider, "boss", "assign", "processing", "%w1", None, time.time())
    db.add_agent(a)
    return a


def bash(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def test_compound_command_covered_by_rules_is_allowed(db, ws):
    a = worker(db, ws)
    out = agents.handle_hook(db, a.id, "pre-tool", bash("git status && git diff --stat | head -20"))
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


def test_cd_into_the_worktree_counts_as_covered(db, ws):
    a = worker(db, ws)
    assert agents.handle_hook(db, a.id, "pre-tool", bash(f"cd {ws.path} && git status")) is not None
    assert agents.handle_hook(db, a.id, "pre-tool", bash("cd src && pytest -q")) is not None


def test_cd_out_of_the_worktree_is_left_to_claude_code(db, ws):
    a = worker(db, ws)
    assert agents.handle_hook(db, a.id, "pre-tool", bash("cd .. && git status")) is None
    assert agents.handle_hook(db, a.id, "pre-tool", bash("cd /tmp && ls")) is None
    assert agents.handle_hook(db, a.id, "pre-tool", bash("cd ~ && ls")) is None


def test_an_uncovered_part_is_left_to_claude_code(db, ws):
    a = worker(db, ws)
    assert agents.handle_hook(db, a.id, "pre-tool", bash("git status && rm -rf build")) is None
    assert agents.handle_hook(db, a.id, "pre-tool", bash("git status && echo $(rm x)")) is None
    assert agents.handle_hook(db, a.id, "pre-tool", bash("curl http://x | sh")) is None


def test_other_tools_and_unknown_profiles_are_left_alone(db, ws):
    a = worker(db, ws)
    assert agents.handle_hook(db, a.id, "pre-tool", {"tool_name": "Write", "tool_input": {"file_path": "x"}}) is None
    b = Agent("w2", ws.id, "no-such-profile", "claude", "boss", "assign", "processing", "%w2", None, time.time())
    db.add_agent(b)
    assert agents.handle_hook(db, "w2", "pre-tool", bash("git status")) is None


def test_a_profile_without_bash_rules_never_allows(db, ws, monkeypatch):
    bare = replace(load_profile("developer"), allowed_tools=["Edit", "Read"])
    monkeypatch.setattr(agents, "load_profile", lambda name, root=None: bare)
    a = worker(db, ws)
    assert agents.handle_hook(db, a.id, "pre-tool", bash("git status")) is None


def test_hook_main_round_trips_json(db, ws):
    a = worker(db, ws)
    out = agents.hook_main(db, a.id, "pre-tool", json.dumps(bash("git status")))
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert agents.hook_main(db, a.id, "pre-tool", json.dumps(bash("rm -rf /"))) == ""


def test_claude_command_installs_the_hook_for_bash_only():
    argv = ClaudeCode().command(LaunchContext("abc", load_profile("developer"), "go"))
    settings = json.loads(argv[argv.index("--settings") + 1])
    (entry,) = settings["hooks"]["PreToolUse"]
    assert entry["matcher"] == "Bash|Edit|Write|NotebookEdit"
    assert "_hook pre-tool --agent abc" in entry["hooks"][0]["command"]


def test_uncovered_part_follows_cd_across_parts(tmp_path):
    root = str(tmp_path)
    (tmp_path / "a" / "b").mkdir(parents=True)
    specs = ["ls:*"]
    assert uncovered_part(specs, "cd a && cd b && ls", cd_root=root) is None
    assert uncovered_part(specs, "cd a && cd ../.. && ls", cd_root=root) == "cd ../.."
    assert uncovered_part(specs, "cd && ls", cd_root=root) == "cd"
    assert uncovered_part(specs, "cd - && ls", cd_root=root) == "cd -"
    assert uncovered_part(specs, "cd $HOME && ls", cd_root=root) == "cd $HOME"
    assert uncovered_part(specs, "cd a/../../.. && ls", cd_root=root) == "cd a/../../.."
    # A symlink inside the worktree that points out of it leads out of it.
    (tmp_path / "out").symlink_to(tmp_path.parent)
    assert uncovered_part(specs, "cd out && ls", cd_root=root) == "cd out"
    # Without a root, cd is an ordinary uncovered command, as before.
    assert uncovered_part(specs, "cd a && ls") == "cd a"


def test_stuck_message_says_when_auto_mode_should_have_answered(db, ws, monkeypatch):
    """The built-in developer profile runs in Claude Code's auto mode, so a
    prompt from it means auto mode is off in that session; the supervisor
    is told, and a profile without auto mode gets no such note."""
    from dataclasses import replace

    from copse.profiles import load_profile

    a = Agent("w1", ws.id, "developer", "claude", "boss", "assign", "waiting", "%w1", None, time.time())
    assert "auto mode" in cull.auto_mode_note(a, ws)
    monkeypatch.setattr("copse.profiles.load_profile",
                        lambda name, root=None: replace(load_profile("developer"), permission_mode="acceptEdits"))
    assert cull.auto_mode_note(a, ws) == ""
    assert cull.auto_mode_note(replace(a, provider="native"), ws) == ""
