import shutil
import time
from pathlib import Path

import pytest

from copse import agents, tmux, workspaces
from copse.db import Agent
from copse.providers import ClaudeCode, LaunchContext
from copse.profiles import load_profile


def fake_agent(db, ws, status="processing", mode="interactive", parent=None, agent_id="a1"):
    a = Agent(agent_id, ws.id, "developer", "claude", parent, mode, status, "@0", None, time.time())
    db.add_agent(a)
    return a


@pytest.fixture
def ws(db, repo):
    return workspaces.create(db, str(repo), "feature").workspace


def test_stop_hook_delivers_queued_message(db, ws):
    fake_agent(db, ws)
    db.enqueue("a1", "please also add tests", None)
    out = agents.handle_hook(db, "a1", "stop", {})
    assert out == {"decision": "block", "reason": "please also add tests"}
    assert db.get_agent("a1").status == "processing"
    assert agents.handle_hook(db, "a1", "stop", {}) is None
    assert db.get_agent("a1").status == "idle"


def test_stop_hook_nudges_worker_to_report_once(db, ws):
    fake_agent(db, ws, mode="handoff")
    out = agents.handle_hook(db, "a1", "stop", {})
    assert out and "report_result" in out["reason"]
    # Claude Code sets stop_hook_active on the follow-up stop; don't loop.
    assert agents.handle_hook(db, "a1", "stop", {"stop_hook_active": True}) is None


def test_prompt_and_notification_hooks(db, ws):
    fake_agent(db, ws, status="idle")
    agents.handle_hook(db, "a1", "prompt-submit", {})
    assert db.get_agent("a1").status == "processing"
    agents.handle_hook(db, "a1", "notification", {"message": "Claude needs your permission to use Bash"})
    assert db.get_agent("a1").status == "waiting"


def test_claim_idle_is_exclusive(db, ws):
    fake_agent(db, ws, status="idle")
    assert db.claim_idle("a1") is True
    assert db.claim_idle("a1") is False


def test_report_result_forwards_to_parent_on_assign(db, ws, monkeypatch):
    # The manual flow: with the pipeline off, the report goes to the parent.
    from pathlib import Path

    (Path(ws.repo_root) / ".copse").mkdir(exist_ok=True)
    (Path(ws.repo_root) / ".copse" / "config.json").write_text('{"pipeline": false}')
    fake_agent(db, ws, status="processing", agent_id="boss")
    fake_agent(db, ws, mode="assign", parent="boss", agent_id="w1")
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    assert "sent" in agents.report_result(db, "w1", "done: added login")
    assert db.get_agent("w1").result == "done: added login"
    # Boss is busy, so the result waits in its inbox for the next Stop hook.
    msg = db.pop_pending("boss")
    assert msg and "done: added login" in msg.body and "w1" in msg.body


def test_claude_command_wires_hooks_mcp_and_profile():
    ctx = LaunchContext("abc", load_profile("developer"), "do the thing")
    argv = ClaudeCode().command(ctx)
    assert argv[0] == "claude" and argv[-1] == "do the thing"
    settings = argv[argv.index("--settings") + 1]
    assert "_hook" in settings and "Stop" in settings
    assert '"COPSE_AGENT_ID": "abc"' in argv[argv.index("--mcp-config") + 1]
    assert argv[argv.index("--permission-mode") + 1] == "auto"
    # Agent view (background sessions) is where a pasted message can land in
    # the wrong conversation or start a brand-new one; disable it outright.
    assert '"disableAgentView": true' in settings


def test_disable_agent_view_follows_mode_not_profile_name():
    """disableAgentView is about how copse drives the pane (a human's own
    interactive chat vs. one copse pastes messages into), not the profile's
    name -- a custom-named profile run interactively must still get the
    agent view, and a non-interactive one must still lose it."""
    from dataclasses import replace

    custom = replace(load_profile("developer"), name="my-custom-profile")

    def settings_for(mode):
        argv = ClaudeCode().command(LaunchContext("abc", custom, "hi", mode=mode))
        return argv[argv.index("--settings") + 1]

    assert '"disableAgentView": false' in settings_for("interactive")
    assert '"disableAgentView": true' in settings_for("assign")
    assert '"disableAgentView": true' in settings_for("handoff")


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_shell_agent_in_tmux_end_to_end(db, ws):
    a = agents.spawn(db, ws, "developer", provider_name="shell")
    try:
        assert agents.is_alive(a)
        assert agents.send_message(db, a.id, "echo copse-says-hi-$COPSE_AGENT_ID") == "delivered"
        deadline = time.time() + 5
        while time.time() < deadline and f"copse-says-hi-{a.id}" not in tmux.capture(a.tmux_window):
            time.sleep(0.2)
        assert f"copse-says-hi-{a.id}" in tmux.capture(a.tmux_window)
    finally:
        tmux.kill_session(ws.tmux_session)


def test_developer_may_run_tests_and_builds_but_not_everything():
    argv = ClaudeCode().command(LaunchContext("abc", load_profile("developer"), None))
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert "mcp__copse" in allowed
    assert "Bash(pytest:*)" in allowed and "Bash(npm run:*)" in allowed
    assert "Bash(git push:*)" not in allowed
    assert not any(t in ("Bash", "Bash(*)") for t in allowed)


CLAUDE_IDLE = "⏺ Done.\n\n────\n❯ \n────\n  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents\n"
# Real Claude Code 2.1.283 layouts: a blank line sits between the box's top
# border and the status line above it (and another below the bottom
# border). The status line shows a spinner while a turn runs, in shapes
# ranging from a bare verb to one with token/timing detail, and a "done
# HH:MM" marker once it ends. Older versions instead said "esc to interrupt"
# in the footer below the box.
BOX = "\n────\n❯ \n────\n\n  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents\n"
CLAUDE_BUSY = "✻ Tomfoolering… (7m 22s · ↓ 35.0k tokens · thinking)" + BOX
CLAUDE_BUSY_SHORT = "✻ Tomfoolering… (3s)" + BOX
CLAUDE_BUSY_ESC = "✻ Tomfoolering… (3s · esc to interrupt)" + BOX
CLAUDE_BUSY_BARE = "✻ Tomfoolering…" + BOX
CLAUDE_BUSY_BACKGROUND = ("· Gallivanting… (7m 22s · ↓ 31.7k tokens · thinking)\n"
                          "  (ctrl+b ctrl+b (twice) to run in background)" + BOX)
# The todo list sits below the spinner, closer to the box, indented under it.
CLAUDE_BUSY_TODO = ("✻ Tomfoolering… (7m 22s · ↓ 35.0k tokens · thinking)\n"
                    "  ⎿  ☐ Write the fix\n"
                    "     ☐ Add tests" + BOX)
CLAUDE_DONE = "✻ Sautéed for 7m 49s · done 7:57 PM" + BOX
# An exact capture of a real busy pane (2.1.283), truncated transcript line
# and all: it must not be mistaken for the spinner above the box. Shared
# with test_reliability.py.
CLAUDE_BUSY_REAL_CAPTURE = (
    '     os.environ.setdefault("GIT_COMMITTER_EMAIL", "t@example.c…\n'
    "\n"
    "✽ Hashing… (2m 53s · ↓ 7.6k tokens · thinking)\n"
    "\n"
    "────────────────────────────────────────\n"
    "❯ \n"
    "────────────────────────────────────────\n"
    "\n"
    "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents\n"
)
# The quote sits with no blank-line padding at all right above the box;
# only the anchored regex (not distance from the box) keeps this idle.
CLAUDE_QUOTED_BUSY_NO_PADDING = (
    "⏺ It shows \"Tomfoolering… (7m 22s · ↓ 35.0k tokens)\" while busy.\n"
    "────\n❯ \n────\n  ⏵⏵ accept edits on (shift+tab to cycle) · ? for shortcuts\n"
)
# "esc to interrupt" only counts in the footer below the box, never quoted in
# prose above it.
CLAUDE_IDLE_ESC_MENTION = "⏺ Older builds printed esc to interrupt under the box." + BOX
# Neither of these is the busy block busy_in_footer requires (its first line
# must itself be the spinner): here it's prose describing the spinner, with
# the real-looking spinner line only as a second, indented line — which is
# neither the required first line nor a todo line, so the block is invalid
# either way. screen_state's broader tail search still calls both of these
# busy, unrelated to busy_in_footer's stricter shape check.
CLAUDE_IDLE_SPINNER_DESCRIBED = ("⏺ The spinner looks like:\n"
                                 "  ✻ Tomfoolering… (3s)" + BOX)
# A plain "-" bullet isn't a spinner glyph, even though the rest of the line
# is shaped like one.
CLAUDE_IDLE_PROSE_ELLIPSIS = "  - Loading…" + BOX
CLAUDE_PROMPT = " Bash command\n   pytest\n This command requires approval\n\n Do you want to proceed?\n ❯ 1. Yes\n   4. No\n\n Esc to cancel\n"


@pytest.mark.parametrize("screen,want", [
    (CLAUDE_IDLE, "idle"), (CLAUDE_DONE, "idle"), (CLAUDE_IDLE_ESC_MENTION, "idle"),
    (CLAUDE_BUSY, "busy"), (CLAUDE_BUSY_SHORT, "busy"), (CLAUDE_BUSY_ESC, "busy"),
    (CLAUDE_BUSY_BARE, "busy"), (CLAUDE_BUSY_BACKGROUND, "busy"), (CLAUDE_BUSY_TODO, "busy"),
    (CLAUDE_BUSY_REAL_CAPTURE, "busy"), (CLAUDE_QUOTED_BUSY_NO_PADDING, "idle"),
    (CLAUDE_PROMPT, "waiting"), ("", None),
])
def test_claude_screen_state(screen, want):
    assert ClaudeCode().screen_state(screen) == want


@pytest.mark.parametrize("screen,want", [
    (CLAUDE_BUSY, True), (CLAUDE_BUSY_SHORT, True), (CLAUDE_BUSY_ESC, True),
    (CLAUDE_BUSY_BARE, True), (CLAUDE_BUSY_TODO, True),
    (CLAUDE_BUSY_REAL_CAPTURE, True),
    # The background-run note doesn't fit the spinner-then-todos shape, so
    # busy_in_footer misses it (screen_state's broad tail search still
    # catches it above); a conservative false negative, never a false
    # positive.
    (CLAUDE_BUSY_BACKGROUND, False),
    (CLAUDE_IDLE_ESC_MENTION, False),
    (CLAUDE_IDLE_SPINNER_DESCRIBED, False), (CLAUDE_IDLE_PROSE_ELLIPSIS, False),
    (CLAUDE_IDLE, False), (CLAUDE_DONE, False), (CLAUDE_QUOTED_BUSY_NO_PADDING, False),
])
def test_claude_busy_in_footer(screen, want):
    assert ClaudeCode().busy_in_footer(screen) is want


CLAUDE_TYPING = "⏺ Done.\n\n────\n❯ half a message I'm still writ\n────\n  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents\n"
# The real agent-view screen: its footer ("ctrl+x to delete") is the reliable
# anchor, not the loose "moved to the background" phrasing, which can appear
# quoted in an ordinary transcript (see CLAUDE_TRANSCRIPT_QUOTES_THE_PHRASES).
CLAUDE_BACKGROUND = (
    "Your conversation moved to the background — enter opens it · esc returns to it\n"
    "────\n❯ describe a task for a new session\n────\n"
    "⏵⏵ auto mode · enter to open · space to reply · ctrl+x to delete · ? for shortcuts\n"
)
CLAUDE_BACKGROUND_RETURN_FOOTER = (
    "Some other session's last message\n"
    "────\n❯ describe a task for a new session\n────\n"
    "⏵⏵ auto mode · enter to return · space to reply · ctrl+x to delete · ? for shortcuts\n"
)
# A transcript that happens to quote both telltale phrases, above the last
# border: must NOT be mistaken for the real agent-view footer.
CLAUDE_TRANSCRIPT_QUOTES_THE_PHRASES = (
    '⏺ I told them: "Your conversation moved to the background" and to press\n'
    '  "ctrl+x to delete" if they wanted out.\n'
    "────\n❯ \n────\n  ⏵⏵ accept edits on (shift+tab to cycle) · esc to interrupt\n"
)
# Claude Code's dim placeholder/suggestion in an otherwise-empty input box:
# looks like typed text unless the styling (SGR 2, faint) is taken into account.
CLAUDE_PLACEHOLDER = (
    "⏺ Done.\n\n────\n"
    '❯ \x1b[2mTry "create a util logging.py that..."\x1b[0m\n'
    "────\n  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents\n"
)
CLAUDE_PLACEHOLDER_GREY_256 = (
    "⏺ Done.\n\n────\n"
    '❯ \x1b[38;5;244mTry "fix the flaky test in test_agents.py"\x1b[0m\n'
    "────\n  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents\n"
)
# A worker's own pane, its box border woven with its own session title (not
# some OTHER session): must not be mistaken for a wrong-session pane and
# block delivery. See test_claude_paste_blocked's own-title case below.
CLAUDE_OWN_SESSION_TITLE = (
    "⏺ working on it\n"
    "──── feat/sidebar-everywhere-3 ────\n"
    "❯ \n────\n  ⏵⏵ accept edits on (shift+tab to cycle) · esc to interrupt\n"
)
# A dim placeholder styled with a truecolor (38;2;r;g;b) grey instead of the
# 256-color palette.
CLAUDE_PLACEHOLDER_TRUECOLOR_GREY = (
    "⏺ Done.\n\n────\n"
    '❯ \x1b[38;2;128;128;128mTry "fix the flaky test in test_agents.py"\x1b[0m\n'
    "────\n  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents\n"
)
# Real typed text in a non-grey truecolor (not dim) must still count as typing.
CLAUDE_TYPING_TRUECOLOR_COLOR = (
    "⏺ Done.\n\n────\n"
    '❯ \x1b[38;2;200;60;60mnot a placeholder\x1b[0m\n'
    "────\n  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents\n"
)
# A bright color (91-97) is a real color, not dimming -- must still count as typing.
CLAUDE_TYPING_BRIGHT_COLOR = (
    "⏺ Done.\n\n────\n"
    '❯ \x1b[91mnot a placeholder\x1b[0m\n'
    "────\n  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents\n"
)
# The terminal cursor sits on the first cell in inverse video (SGR 7); it must
# be ignored so real typed text right after it is still detected.
CLAUDE_TYPING_WITH_INVERSE_CURSOR = (
    "⏺ Done.\n\n────\n"
    '❯ \x1b[7mh\x1b[27mello\n'
    "────\n  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents\n"
)


@pytest.mark.parametrize("screen,interactive,want", [
    (CLAUDE_IDLE, True, None),
    (CLAUDE_IDLE, False, None),
    (CLAUDE_TYPING, True, "typing"),
    (CLAUDE_TYPING, False, None),  # a worker's input box is never hand-typed
    (CLAUDE_BACKGROUND, True, "background"),
    (CLAUDE_BACKGROUND, False, "background"),  # workers can end up here too
    (CLAUDE_BACKGROUND_RETURN_FOOTER, True, "background"),
    (CLAUDE_TRANSCRIPT_QUOTES_THE_PHRASES, True, None),  # not the real footer
    (CLAUDE_TRANSCRIPT_QUOTES_THE_PHRASES, False, None),
    (CLAUDE_PLACEHOLDER, True, None),  # dim placeholder, not real input
    (CLAUDE_PLACEHOLDER_GREY_256, True, None),  # grey 256-colour placeholder
    (CLAUDE_PLACEHOLDER_TRUECOLOR_GREY, True, None),  # grey truecolor placeholder
    (CLAUDE_TYPING_TRUECOLOR_COLOR, True, "typing"),  # non-grey truecolor is real input
    (CLAUDE_TYPING_BRIGHT_COLOR, True, "typing"),  # bright (91-97) is a color, not dimming
    (CLAUDE_TYPING_WITH_INVERSE_CURSOR, True, "typing"),  # ignore the inverse-video cursor cell
    # A worker's own session title in its border isn't a "wrong session"
    # any more: nothing blocks it (there's no queued/typed text either).
    (CLAUDE_OWN_SESSION_TITLE, True, None),
    (CLAUDE_OWN_SESSION_TITLE, False, None),
])
def test_claude_paste_blocked(screen, interactive, want):
    assert ClaudeCode().paste_blocked(screen, interactive) == want


def test_titled_border_detection_is_removed():
    """A worker whose pane border shows its own session title must never be
    blocked as 'wrong-session' -- that check is gone entirely."""
    assert not hasattr(ClaudeCode, "TITLED_BORDER")


def test_flush_keeps_message_queued_when_user_is_mid_typing(db, ws, monkeypatch):
    fake_agent(db, ws, status="idle")
    db.enqueue("a1", "queued message", None)
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: CLAUDE_TYPING)
    pasted = []
    monkeypatch.setattr(tmux, "paste", lambda target, body, **k: pasted.append(body))
    assert agents.flush(db, "a1") is False
    assert not pasted
    assert db.get_agent("a1").status == "idle"
    assert db.pending_count("a1") == 1


def test_flush_keeps_message_queued_over_background_session_launcher(db, ws, monkeypatch):
    # A worker too: a blind paste there would start a NEW background session
    # instead of reaching this one.
    fake_agent(db, ws, status="idle", mode="assign")
    db.enqueue("a1", "queued message", None)
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: CLAUDE_BACKGROUND)
    sent, pasted = [], []
    monkeypatch.setattr(tmux, "send_keys", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(tmux, "paste", lambda target, body, **k: pasted.append(body))
    assert agents.flush(db, "a1") is False
    assert sent and sent[0][1] == "Escape"  # tried backing out before giving up
    assert not pasted
    assert db.pending_count("a1") == 1


def test_flush_delivers_once_background_view_clears_after_escape(db, ws, monkeypatch):
    fake_agent(db, ws, status="idle")
    db.enqueue("a1", "queued message", None)
    screens = iter([CLAUDE_BACKGROUND, CLAUDE_IDLE])
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: next(screens))
    monkeypatch.setattr(tmux, "send_keys", lambda *a, **k: None)
    pasted = []
    monkeypatch.setattr(tmux, "paste", lambda target, body, **k: pasted.append(body))
    assert agents.flush(db, "a1") is True
    assert pasted == ["queued message"]


def test_flush_delivers_when_input_box_is_clear(db, ws, monkeypatch):
    fake_agent(db, ws, status="idle")
    db.enqueue("a1", "queued message", None)
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: CLAUDE_IDLE)
    pasted = []
    monkeypatch.setattr(tmux, "paste", lambda target, body, **k: pasted.append(body))
    assert agents.flush(db, "a1") is True
    assert pasted == ["queued message"]


def test_reconcile_recovers_from_interrupted_turn(db, ws, monkeypatch):
    # Esc-interrupted turns run no Stop hook: status says waiting, screen says idle.
    fake_agent(db, ws, status="waiting")
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: CLAUDE_IDLE)
    a = agents.reconcile(db, db.get_agent("a1"), gap=0)
    assert a.status == "idle" == db.get_agent("a1").status


def test_reconcile_keeps_hook_status_when_screen_is_unclear(db, ws, monkeypatch):
    fake_agent(db, ws, status="processing")
    screens = iter([CLAUDE_IDLE, CLAUDE_BUSY])
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: next(screens))
    assert agents.reconcile(db, db.get_agent("a1"), gap=0).status == "processing"


def test_handoff_wait_is_bounded_and_detaches(db, ws, monkeypatch):
    from pathlib import Path

    (Path(ws.repo_root) / ".copse").mkdir(exist_ok=True)
    (Path(ws.repo_root) / ".copse" / "config.json").write_text('{"pipeline": false}')
    from copse import mcp_server

    fake_agent(db, ws, status="processing", agent_id="boss")
    fake_agent(db, ws, mode="handoff", parent="boss", agent_id="w1")
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    monkeypatch.setattr(agents, "kill", lambda db, aid: db.delete_agent(aid))

    out = mcp_server._await_worker(db, "w1", wait_seconds=0)
    assert "still running" in out and "wait_for_worker" in out
    assert db.get_agent("w1").mode == "handoff_detached"

    # Finishing later forwards the result to the supervisor's inbox...
    agents.report_result(db, "w1", "done later")
    assert db.pending_count("boss") == 1
    # ...and if the supervisor collects it directly, the duplicate is dropped.
    out = mcp_server._await_worker(db, "w1", wait_seconds=0)
    assert "done later" in out
    assert db.pending_count("boss") == 0
    assert db.get_agent("w1") is None  # handoff worker closed after collection


def test_result_arriving_during_detach_is_not_lost(db, ws, monkeypatch):
    fake_agent(db, ws, mode="handoff", agent_id="w1")
    db.set_result("w1", "just in time")
    assert agents.detach(db, "w1") == "just in time"


def test_wait_for_worker_leaves_assign_workers_running(db, ws, monkeypatch):
    from copse import mcp_server

    fake_agent(db, ws, mode="assign", agent_id="w2")
    db.set_result("w2", "ok")
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    killed = []
    monkeypatch.setattr(agents, "kill", lambda db, aid: killed.append(aid))
    assert "ok" in mcp_server._await_worker(db, "w2", wait_seconds=0)
    assert killed == []


def test_codex_command_preapproves_only_copse_tools(monkeypatch):
    from copse.providers import Codex

    monkeypatch.setenv("COPSE_CODEX_BIN", "/opt/codex")
    argv = Codex().command(LaunchContext("abc", load_profile("developer"), "do it"))
    assert argv[0] == "/opt/codex"
    assert 'mcp_servers.copse.default_tools_approval_mode="approve"' in argv
    assert any('COPSE_AGENT_ID = "abc"' in a for a in argv)
    assert argv[-1].endswith("do it")  # profile prompt leads the first message
    assert not any("dangerously" in a or "full-auto" in a for a in argv)


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_watch_pane_shares_the_window_and_messages_reach_the_agent(db, ws):
    a = agents.spawn(db, ws, "developer", provider_name="shell", watch_pane=True)
    try:
        assert a.tmux_window.startswith("%")  # a pane, not a window
        panes = tmux._tmux("list-panes", "-t", a.tmux_window, "-F", "#{pane_id}").stdout.split()
        assert len(panes) == 2 and a.tmux_window in panes
        # Even with the dashboard pane focused, messages go to the agent's pane.
        other = next(p for p in panes if p != a.tmux_window)
        tmux._tmux("select-pane", "-t", other)
        agents.send_message(db, a.id, "echo reached-$COPSE_AGENT_ID")
        deadline = time.time() + 5
        while time.time() < deadline and f"reached-{a.id}" not in tmux.capture(a.tmux_window):
            time.sleep(0.2)
        assert f"reached-{a.id}" in tmux.capture(a.tmux_window)
    finally:
        tmux.kill_session(ws.tmux_session)


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_find_running_reuses_a_live_interactive_agent(db, ws):
    assert agents.find_running(db, ws, "developer") is None
    a = agents.spawn(db, ws, "developer", provider_name="shell")
    try:
        assert agents.find_running(db, ws, "developer").id == a.id
        assert agents.find_running(db, ws, "reviewer") is None
    finally:
        tmux.kill_session(ws.tmux_session)
    assert agents.find_running(db, ws, "developer") is None  # dead agents don't count


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_interactive_agent_exit_pauses_its_session(db, ws):
    a = agents.spawn(db, ws, "developer", provider_name="shell", watch_pane=True)
    assert tmux.has_session(ws.tmux_session)
    tmux.send_keys(a.tmux_window, "exit", "Enter")
    # The pane-died hook starts a fresh Python process; slow CI runners need time.
    deadline = time.time() + 30
    while time.time() < deadline and tmux.has_session(ws.tmux_session):
        time.sleep(0.2)
    diag = ""
    if tmux.has_session(ws.tmux_session):
        hooks = tmux._tmux("show-hooks", "-p", "-t", a.tmux_window, check=False)
        panes = tmux._tmux("list-panes", "-s", "-t", ws.tmux_session, "-F",
                           "#{window_name} #{pane_id} dead=#{pane_dead} cmd=#{pane_current_command}", check=False)
        diag = (f"status={db.get_agent(a.id).status} windows={tmux.windows(ws.tmux_session)}\n"
                f"hooks={hooks.stdout.strip()!r} {hooks.stderr.strip()!r}\npanes={panes.stdout.strip()!r}\n"
                f"screen={tmux.capture(a.tmux_window)[-400:]!r}")
    assert not tmux.has_session(ws.tmux_session), diag
    assert db.get_agent(a.id).status == "paused"


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_worker_panes_stay_after_exit_for_their_output(db, ws):
    a = agents.spawn(db, ws, "developer", provider_name="shell", mode="assign")
    try:
        tmux.send_keys(a.tmux_window, "echo last-words; exit", "Enter")
        time.sleep(1.5)
        assert tmux.has_session(ws.tmux_session)
        assert "last-words" in tmux.capture(a.tmux_window)
    finally:
        tmux.kill_session(ws.tmux_session)


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_chat_exit_closes_its_session_even_with_extra_windows(db, ws):
    a = agents.spawn(db, ws, "developer", provider_name="shell", watch_pane=True)
    tmux._tmux("new-window", "-d", "-t", f"={ws.tmux_session}:", "-n", "extra")  # something the user opened
    tmux.send_keys(a.tmux_window, "exit", "Enter")
    deadline = time.time() + 30
    while time.time() < deadline and tmux.has_session(ws.tmux_session):
        time.sleep(0.2)
    assert not tmux.has_session(ws.tmux_session)
    assert db.get_agent(a.id).status == "paused"


def test_claude_command_defaults_are_unchanged():
    argv = ClaudeCode().command(LaunchContext("abc", load_profile("developer"), "do the thing"))
    for flag in ("-p", "--strict-mcp-config", "--setting-sources", "--effort", "--add-dir"):
        assert flag not in argv
    assert argv[:2] == ["claude", "--settings"]


def test_claude_command_passes_one_add_dir_per_directory():
    from dataclasses import replace

    profile = replace(load_profile("developer"), add_dirs=["/srv/cache", "/srv/refs"])
    argv = ClaudeCode().command(LaunchContext("abc", profile, "do the thing"))
    pairs = [(argv[i], argv[i + 1]) for i, a in enumerate(argv) if a == "--add-dir"]
    assert pairs == [("--add-dir", "/srv/cache"), ("--add-dir", "/srv/refs")]


def test_claude_command_emits_lightweight_flags():
    from dataclasses import replace

    profile = replace(load_profile("developer"), strict_mcp=True,
                      setting_sources=["project", "local"], effort="low")
    argv = ClaudeCode().command(LaunchContext("abc", profile, "do the thing"))
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--setting-sources") + 1] == "project,local"
    assert argv[argv.index("--effort") + 1] == "low"
    assert "-p" not in argv and argv[-1] == "do the thing"
    # copse's own hooks and MCP server are still passed explicitly.
    assert "--settings" in argv and "--mcp-config" in argv


def test_headless_claude_command_runs_one_turn_with_print():
    from dataclasses import replace

    profile = replace(load_profile("developer"), headless=True)
    argv = ClaudeCode().command(LaunchContext("abc", profile, "do the thing"))
    assert argv[1] == "-p" and argv[-1] == "do the thing"
    # The next turn continues the same conversation, with its own prompt.
    argv = ClaudeCode().command(LaunchContext("abc", profile, "and the tests", resume="sid-1"))
    assert argv[-3:] == ["--resume", "sid-1", "and the tests"]
    # Interactive resume still takes no prompt.
    argv = ClaudeCode().command(LaunchContext("abc", load_profile("developer"), "x", resume="sid-1"))
    assert argv[-2:] == ["--resume", "sid-1"]


def test_lightweight_fields_are_ignored_by_other_providers(monkeypatch):
    from dataclasses import replace

    from copse.providers import Codex

    monkeypatch.setenv("COPSE_CODEX_BIN", "codex")
    profile = replace(load_profile("developer"), strict_mcp=True, setting_sources=["project"],
                      effort="low", headless=True)
    argv = Codex().command(LaunchContext("abc", profile, "do it"))
    for flag in ("-p", "--strict-mcp-config", "--setting-sources", "--effort"):
        assert flag not in argv


def test_workers_test_their_change_and_leave_the_full_suite_to_checks(db, ws):
    from copse.providers import get_provider

    prompt = agents.decorate_worker_prompt("add a flag", "w1", ws, None, get_provider("claude"), headless=False)
    assert "run only the tests that cover your change" in prompt
    assert "Run the full suite once, just before you commit." in prompt
    (Path(ws.repo_root) / ".copse").mkdir(exist_ok=True)
    (Path(ws.repo_root) / ".copse" / "config.json").write_text('{"checks": ["uv run pytest -q"]}')
    prompt = agents.decorate_worker_prompt("add a flag", "w1", ws, None, get_provider("claude"), headless=False)
    assert "Don't run the full suite yourself" in prompt and "`uv run pytest -q`" in prompt
    sub = agents.subagent_prompt("You are a subagent.", "add a flag", ws, None)
    assert "Don't run the full suite yourself" in sub


def test_flush_types_a_lead_naming_the_sender(db, ws, monkeypatch):
    """Agent CLIs distrust pasted text, so copse vouches for its delivery by
    typing (not pasting) a line naming the sender."""
    fake_agent(db, ws, status="processing", agent_id="sup1")
    fake_agent(db, ws, status="idle", mode="assign", parent="sup1")
    db.enqueue("a1", agents.format_message(db, "fix the test", "sup1"), "sup1")
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: CLAUDE_IDLE)
    calls = []
    monkeypatch.setattr(tmux, "paste", lambda target, body, **k: calls.append((body, k.get("lead"))))
    assert agents.flush(db, "a1") is True
    (body, lead), = calls
    assert "fix the test" in body
    assert lead == "copse delivered this message from developer agent sup1:"


def test_no_lead_is_typed_into_a_plain_shell(db, ws):
    a = fake_agent(db, ws)
    a.provider = "shell"
    assert agents.message_lead(db, a, "sup1") is None


def test_claude_agents_are_told_what_vouches_for_a_message():
    from copse.profiles import load_profile
    from copse.providers import DELIVERY_NOTE, LaunchContext

    argv = ClaudeCode().command(LaunchContext("abc", load_profile("developer"), "hi", mode="assign"))
    prompt = argv[argv.index("--append-system-prompt") + 1]
    assert DELIVERY_NOTE in prompt and load_profile("developer").prompt in prompt


def test_add_dir_is_never_the_last_flag():
    """--add-dir is variadic, so a flag must follow the last one.

    If it were last, Claude Code would read the initial prompt as another
    directory and the worker would start with no task and no error.
    """
    from dataclasses import replace

    profile = replace(load_profile("developer"), add_dirs=["/srv/cache", "/srv/refs"])
    argv = ClaudeCode().command(LaunchContext("abc", profile, "do the thing"))
    last = max(i for i, a in enumerate(argv) if a == "--add-dir")
    assert argv[last + 2].startswith("--"), argv[last:]


def test_spawn_reports_a_missing_add_dir_once(db, ws, monkeypatch, capsys):
    """Claude Code ignores an --add-dir that does not exist, so launch says so,
    once, however many times the profile is loaded on the way."""
    from pathlib import Path

    class Launched(Exception):
        pass

    def stop(*a, **k):
        raise Launched

    config = Path(ws.repo_root) / ".copse" / "config.json"
    config.parent.mkdir(exist_ok=True)
    config.write_text('{"add_dirs": ["/no/such/cache"]}')
    # Stopped at the window, so every profile load on the way (spawn's own and
    # _profile_for's) has happened.
    monkeypatch.setattr(agents, "_open_window", stop)
    monkeypatch.setattr("copse.providers.trust_folder", lambda path: None)

    with pytest.raises(Launched):
        agents.spawn(db, ws, "developer", prompt="hi", mode="assign")
    assert capsys.readouterr().err.count("/no/such/cache") == 1


def test_resume_reports_a_missing_add_dir_too(db, ws, monkeypatch, capsys):
    """A directory can go missing between the first launch and a resume, and
    resume goes straight to _launch, never through spawn."""
    from pathlib import Path

    class Launched(Exception):
        pass

    def stop(*a, **k):
        raise Launched

    config = Path(ws.repo_root) / ".copse" / "config.json"
    config.parent.mkdir(exist_ok=True)
    config.write_text('{"add_dirs": ["/no/such/cache"]}')
    monkeypatch.setattr(agents, "_open_window", stop)
    monkeypatch.setattr("copse.providers.trust_folder", lambda path: None)
    fake_agent(db, ws, status="paused")

    with pytest.raises(Launched):
        agents.resume(db, "a1", watch_pane=False)
    assert capsys.readouterr().err.count("/no/such/cache") == 1
