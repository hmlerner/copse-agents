"""The one sidebar pane following the user between copse windows and
sessions (see agents.sidebar_follow, agents._ensure_sidebar,
tmux.set_follow_hooks, tmux.move_pane).

Uses real tmux on the private socket the test suite already runs on
(COPSE_TMUX_SOCKET, see conftest.py's private_tmux_server fixture), since
the behaviour under test is genuine tmux pane/hook plumbing.
"""

import subprocess
import sys
import time

import pytest

from copse import agents, tmux
from copse.db import DB, Agent, Workspace


@pytest.fixture
def db(copse_home):
    return DB()


def make_workspace(db, tmp_path, name, session, repo_root=None):
    path = tmp_path / name
    path.mkdir()
    ws = Workspace(
        id=name, repo_root=repo_root or str(tmp_path), name=name, kind="worktree",
        branch=name, base_branch="main", path=str(path), port_base=None,
        tmux_session=session, created_at=time.time(),
    )
    db.add_workspace(ws)
    return ws


def fake_agent(db, ws, window, agent_id, parent=None, mode="interactive", status="idle"):
    a = Agent(agent_id, ws.id, "supervisor", "claude", parent, mode, status, window, None, time.time())
    db.add_agent(a)
    return a


def window_panes(session, window):
    return tmux._tmux("list-panes", "-t", f"{session}:{window}", "-F", "#{pane_id}").stdout.split()


def make_window(session, name):
    """A window with one plain shell pane, standing in for an agent's pane."""
    proc = tmux._tmux("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", f"={session}:", "-n", name)
    return proc.stdout.strip()


@pytest.fixture
def session(tmp_path):
    name = "copse_followtest"
    tmux.ensure_session(name, str(tmp_path), {})
    yield name
    tmux.kill_session(name)


def test_apply_theme_sets_follow_hooks_on_the_session_only(session):
    tmux.apply_theme(session)
    out = tmux._tmux("show-hooks", "-t", session).stdout
    assert "session-window-changed" in out
    assert "client-session-changed" in out
    assert "_sidebar-follow" in out
    assert session in out  # the session name is baked into the command itself
    assert ">/dev/null 2>&1 || true" in out
    assert "_sidebar-follow" not in tmux._tmux("show-hooks", "-g").stdout


def test_plain_tmux_session_is_never_hooked():
    tmux._tmux("new-session", "-d", "-s", "not_a_copse_session")
    try:
        out = tmux._tmux("show-hooks", "-t", "not_a_copse_session").stdout
        assert "_sidebar-follow" not in out
    finally:
        tmux.kill_session("not_a_copse_session")


def test_sidebar_follow_never_creates_one(db, tmp_path, session):
    """Only _open_window (via _ensure_sidebar) creates a root's first
    sidebar; follow only restores one a root already had (see
    test_sidebar_persist.py), so a `--no-watch` root never gets one."""
    win_a = make_window(session, "winA")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1")
    tmux._tmux("select-window", "-t", f"{session}:winA")

    agents.sidebar_follow(db, "no-such-session")  # unknown session: no crash
    agents.sidebar_follow(db, session)

    assert db.get_sidebar_pane("root1") is None
    assert len(window_panes(session, "winA")) == 1


def test_ensure_sidebar_creates_it_beside_the_target_pane(db, tmp_path, session):
    win_a = make_window(session, "winA")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1")

    agents._ensure_sidebar(db, "root1", ws, win_a)

    sidebar = db.get_sidebar_pane("root1")
    assert sidebar is not None
    assert len(window_panes(session, "winA")) == 2
    assert tmux.pane_window(sidebar) == tmux.pane_window(win_a)
    assert tmux.get_pane_tag(sidebar, agents.SIDEBAR_TAG) == "root1"


def test_sidebar_follow_moves_the_pane_to_the_new_active_window(db, tmp_path, session):
    win_a = make_window(session, "winA")
    win_b = make_window(session, "winB")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1")
    fake_agent(db, ws, win_b, "w1", parent="root1", mode="assign")

    agents._ensure_sidebar(db, "root1", ws, win_a)
    sidebar = db.get_sidebar_pane("root1")
    assert len(window_panes(session, "winA")) == 2
    assert len(window_panes(session, "winB")) == 1

    tmux._tmux("select-window", "-t", f"{session}:winB")
    agents.sidebar_follow(db, session)

    assert db.get_sidebar_pane("root1") == sidebar  # same pane, just relocated
    assert len(window_panes(session, "winA")) == 1
    assert len(window_panes(session, "winB")) == 2
    assert tmux.pane_window(sidebar) == tmux.pane_window(win_b)
    # winB's active pane is still its own agent pane, not the sidebar
    active = tmux._tmux("display-message", "-p", "-t", f"{session}:winB", "#{pane_id}").stdout.strip()
    assert active == win_b


def test_sidebar_follow_stays_with_the_person_when_an_unwatched_session_changes_window(
        db, tmp_path, session, monkeypatch):
    """A worker's session changes its own active window at launch (its
    placeholder shell window closes once the agent's window is up) and the
    follow hook fires with nobody attached to it. The sidebar must stay in
    the session the person is actually looking at."""
    worker_session = "copse_followtest_worker"
    tmux.ensure_session(worker_session, str(tmp_path), {})
    try:
        root_win = make_window(session, "root")
        worker_win = make_window(worker_session, "worker")
        root_ws = make_workspace(db, tmp_path, "rootws", session)
        worker_ws = make_workspace(db, tmp_path, "workerws", worker_session)
        fake_agent(db, root_ws, root_win, "root1")
        fake_agent(db, worker_ws, worker_win, "w1", parent="root1", mode="assign")
        agents._ensure_sidebar(db, "root1", root_ws, root_win)
        sidebar = db.get_sidebar_pane("root1")

        attached = {session: True, worker_session: False}
        monkeypatch.setattr(tmux, "session_attached", lambda name: attached.get(name, False))
        tmux._tmux("select-window", "-t", f"{worker_session}:worker")
        agents.sidebar_follow(db, worker_session)
        assert tmux.pane_window(sidebar) == tmux.pane_window(root_win)

        # Once the person switches into the worker's session, it follows.
        attached[worker_session] = True
        agents.sidebar_follow(db, worker_session)
        assert tmux.pane_window(sidebar) == tmux.pane_window(worker_win)

        # With no client anywhere (tests, headless runs) it follows the windows as before.
        attached[session] = attached[worker_session] = False
        tmux._tmux("select-window", "-t", f"{session}:root")
        agents.sidebar_follow(db, session)
        assert tmux.pane_window(sidebar) == tmux.pane_window(root_win)
    finally:
        tmux.kill_session(worker_session)


def test_sidebar_follow_is_a_noop_in_the_window_it_already_holds(db, tmp_path, session):
    win_a = make_window(session, "winA")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1")
    agents._ensure_sidebar(db, "root1", ws, win_a)
    sidebar = db.get_sidebar_pane("root1")
    width_before = tmux._tmux(
        "display-message", "-p", "-t", sidebar, "#{pane_width}"
    ).stdout.strip()

    tmux._tmux("select-window", "-t", f"{session}:winA")
    # Calling it again for the same window must not re-join the pane onto
    # itself (join-pane isn't idempotent: doing that scrambles the layout).
    agents.sidebar_follow(db, session)

    assert db.get_sidebar_pane("root1") == sidebar
    assert len(window_panes(session, "winA")) == 2
    width_after = tmux._tmux(
        "display-message", "-p", "-t", sidebar, "#{pane_width}"
    ).stdout.strip()
    assert width_after == width_before == "30"


def test_window_resized_hook_moves_with_the_sidebar(db, tmp_path, session):
    win_a = make_window(session, "winA")
    win_b = make_window(session, "winB")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1")
    fake_agent(db, ws, win_b, "w1", parent="root1", mode="assign")
    agents._ensure_sidebar(db, "root1", ws, win_a)
    sidebar = db.get_sidebar_pane("root1")

    win_a_id = tmux.pane_window(win_a)
    win_b_id = tmux.pane_window(win_b)
    assert f'-t "{sidebar}"' in tmux._tmux("show-hooks", "-w", "-t", win_a_id).stdout

    tmux._tmux("select-window", "-t", f"{session}:winB")
    agents.sidebar_follow(db, session)

    assert "window-resized" not in tmux._tmux("show-hooks", "-w", "-t", win_a_id, check=False).stdout
    assert f'-t "{sidebar}"' in tmux._tmux("show-hooks", "-w", "-t", win_b_id).stdout


def test_ensure_sidebar_recreates_a_dead_sidebar(db, tmp_path, session):
    win_a = make_window(session, "winA")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1")
    agents._ensure_sidebar(db, "root1", ws, win_a)
    old_sidebar = db.get_sidebar_pane("root1")

    tmux.kill_pane(old_sidebar)  # simulate a crash
    deadline = time.time() + 5
    while time.time() < deadline and tmux.window_alive(old_sidebar):
        time.sleep(0.1)

    agents._ensure_sidebar(db, "root1", ws, win_a)

    new_sidebar = db.get_sidebar_pane("root1")
    assert new_sidebar is not None and new_sidebar != old_sidebar
    assert tmux.window_alive(new_sidebar)
    assert len(window_panes(session, "winA")) == 2


def test_sidebar_follow_does_not_recreate_a_sidebar_the_user_quit(db, tmp_path, session):
    win_a = make_window(session, "winA")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1")
    agents._ensure_sidebar(db, "root1", ws, win_a)
    sidebar = db.get_sidebar_pane("root1")

    # What `copse watch --sidebar` does when the user presses q.
    agents.dismiss_sidebar(db, sidebar)
    tmux.kill_pane(sidebar)
    deadline = time.time() + 5
    while time.time() < deadline and tmux.window_alive(sidebar):
        time.sleep(0.1)

    tmux._tmux("select-window", "-t", f"{session}:winA")
    agents.sidebar_follow(db, session)

    assert len(window_panes(session, "winA")) == 1  # no new sidebar appeared


def test_stale_pane_id_after_reuse_is_not_trusted(db, tmp_path, session):
    """Pane ids restart after a tmux server restart, so a DB row can point at
    a totally unrelated, untagged pane. Both _ensure_sidebar and
    sidebar_follow must treat that as a dead sidebar: start a real one, and
    never move the imposter."""
    win_a = make_window(session, "winA")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1")

    # An untagged pane standing in for "some unrelated pane that now happens
    # to have the id our stale DB row remembers".
    imposter = make_window(session, "imposter")
    db.set_sidebar_pane("root1", imposter)

    tmux._tmux("select-window", "-t", f"{session}:winA")
    agents.sidebar_follow(db, session)
    real_sidebar = db.get_sidebar_pane("root1")
    assert real_sidebar != imposter
    assert tmux.get_pane_tag(real_sidebar, agents.SIDEBAR_TAG) == "root1"
    assert len(window_panes(session, "winA")) == 2

    agents._ensure_sidebar(db, "root1", ws, win_a)
    assert db.get_sidebar_pane("root1") == real_sidebar  # reused, not a second one
    assert len(window_panes(session, "winA")) == 2
    # The imposter pane was never touched.
    assert tmux.window_alive(imposter)
    assert window_panes(session, "imposter") == [imposter]


def test_plain_shell_window_gets_the_sidebar_too(db, tmp_path):
    """ensure_session's own first window (named 'shell') is a normal target."""
    name = "copse_followtest_shell"
    tmux.ensure_session(name, str(tmp_path), {})
    try:
        ws = make_workspace(db, tmp_path, "shellws", name)
        shell_pane = tmux._tmux("display-message", "-p", "-t", f"{name}:shell", "#{pane_id}").stdout.strip()
        fake_agent(db, ws, shell_pane, "root1")
        agents._ensure_sidebar(db, "root1", ws, shell_pane)
        assert db.get_sidebar_pane("root1") is not None
        assert len(window_panes(name, "shell")) == 2
    finally:
        tmux.kill_session(name)


def test_second_supervisor_in_the_same_repo_gets_its_own_sidebar(db, tmp_path):
    """Keyed by session root, not repo_root: a second supervisor working in
    the same repo must not steal the first one's sidebar."""
    repo_root = str(tmp_path)
    session_a, session_b = "copse_followtest_a", "copse_followtest_b"
    tmux.ensure_session(session_a, str(tmp_path), {})
    tmux.ensure_session(session_b, str(tmp_path), {})
    try:
        win_a = make_window(session_a, "agentA")
        win_b = make_window(session_b, "agentB")
        ws_a = make_workspace(db, tmp_path, "wsA", session_a, repo_root=repo_root)
        ws_b = make_workspace(db, tmp_path, "wsB", session_b, repo_root=repo_root)
        fake_agent(db, ws_a, win_a, "rootA")
        fake_agent(db, ws_b, win_b, "rootB")

        agents._ensure_sidebar(db, "rootA", ws_a, win_a)
        agents._ensure_sidebar(db, "rootB", ws_b, win_b)

        sidebar_a = db.get_sidebar_pane("rootA")
        sidebar_b = db.get_sidebar_pane("rootB")
        assert sidebar_a is not None and sidebar_b is not None
        assert sidebar_a != sidebar_b
        assert len(window_panes(session_a, "agentA")) == 2
        assert len(window_panes(session_b, "agentB")) == 2

        agents.pause(db, "rootA")

        assert not tmux.window_alive(sidebar_a)
        assert db.get_sidebar_pane("rootA") is None
        # rootB's sidebar is untouched.
        assert tmux.window_alive(sidebar_b)
        assert db.get_sidebar_pane("rootB") == sidebar_b
    finally:
        tmux.kill_session(session_a)
        tmux.kill_session(session_b)


def test_pause_never_kills_an_untagged_pane_it_mistakes_for_the_sidebar(db, tmp_path):
    """A stale DB row can point at a pane id tmux has since reused for
    something else entirely (see test_stale_pane_id_after_reuse_is_not_trusted).
    Pausing must not kill that pane just because the DB row still names it.
    Put the imposter in a wholly separate session so pausing root1's own
    session (which always closes entirely) can't kill it for that reason
    instead of proving the tag check."""
    session_a, session_b = "copse_followtest_pause_a", "copse_followtest_pause_b"
    tmux.ensure_session(session_a, str(tmp_path), {})
    tmux.ensure_session(session_b, str(tmp_path), {})
    try:
        win_a = make_window(session_a, "winA")
        ws = make_workspace(db, tmp_path, "winA", session_a)
        fake_agent(db, ws, win_a, "root1")

        imposter = make_window(session_b, "imposter")
        db.set_sidebar_pane("root1", imposter)

        agents.pause(db, "root1")

        assert tmux.window_alive(imposter)  # never touched
        assert db.get_agent("root1").status == "paused"
        assert db.get_sidebar_pane("root1") == imposter  # untouched, stale row kept
    finally:
        if tmux.has_session(session_a):
            tmux.kill_session(session_a)
        tmux.kill_session(session_b)


def test_pause_cleans_up_the_sidebar_wherever_it_is(db, tmp_path, session):
    win_a = make_window(session, "winA")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1")
    agents._ensure_sidebar(db, "root1", ws, win_a)
    sidebar = db.get_sidebar_pane("root1")
    assert tmux.window_alive(sidebar)

    agents.pause(db, "root1")

    assert not tmux.window_alive(sidebar)
    assert db.get_sidebar_pane("root1") is None


def test_follow_skips_a_paused_root(db, tmp_path, session):
    """The tombstone pause() sets (status="paused") before closing windows:
    a follow call that lands after that must not touch anything."""
    win_a = make_window(session, "winA")
    make_window(session, "winB")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1", status="paused")

    tmux._tmux("select-window", "-t", f"{session}:winB")
    agents.sidebar_follow(db, session)

    assert db.get_sidebar_pane("root1") is None
    assert len(window_panes(session, "winB")) == 1


def test_move_pane_takes_a_lone_pane_along(db, tmp_path, session):
    """A sidebar left alone in its window (whatever it sat beside exited)
    still moves: the pane keeps running, and only the now-empty window
    closes. Refusing would strand it out of sight."""
    win_a = make_window(session, "winA")
    win_b = make_window(session, "winB")
    ws = make_workspace(db, tmp_path, "winA", session)
    fake_agent(db, ws, win_a, "root1")
    agents._ensure_sidebar(db, "root1", ws, win_a)
    sidebar = db.get_sidebar_pane("root1")

    # Kill the agent pane winA started with, leaving the sidebar alone there.
    tmux.kill_pane(win_a)
    deadline = time.time() + 5
    while time.time() < deadline and len(window_panes(session, "winA")) != 1:
        time.sleep(0.1)
    assert window_panes(session, "winA") == [sidebar]

    tmux.move_pane(sidebar, win_b, 30)

    assert tmux.has_session(session)
    assert "winA" not in tmux.windows(session)
    assert sorted(window_panes(session, "winB")) == sorted([sidebar, win_b])
    assert tmux.window_alive(sidebar)
    assert tmux._tmux("display-message", "-p", "-t", sidebar, "#{pane_left}").stdout.strip() == "0"


def test_sidebar_follow_hook_command_exits_zero_on_a_bogus_session(copse_home):
    proc = subprocess.run(
        [sys.executable, "-m", "copse", "_sidebar-follow", "no-such-session"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0


def test_apply_theme_shows_the_version_in_the_status_bar(session):
    from copse import __version__

    tmux.apply_theme(session)
    left = tmux._tmux("show-options", "-v", "-t", session, "status-left").stdout
    assert "copse" in left and __version__ in left


def test_a_stranded_sidebar_comes_home_on_its_own(db, tmp_path, session, monkeypatch):
    """The sidebar followed the person into a worker's session, and the hook
    that should have brought it back was missed: its own loop moves it back
    to the session the person is attached to."""
    worker_session = "copse_followtest_worker"
    tmux.ensure_session(worker_session, str(tmp_path), {})
    try:
        root_win = make_window(session, "root")
        worker_win = make_window(worker_session, "worker")
        root_ws = make_workspace(db, tmp_path, "rootws", session)
        worker_ws = make_workspace(db, tmp_path, "workerws", worker_session)
        fake_agent(db, root_ws, root_win, "root1")
        fake_agent(db, worker_ws, worker_win, "w1", parent="root1", mode="assign")
        agents._ensure_sidebar(db, "root1", root_ws, worker_win)    # stranded with the worker
        sidebar = db.get_sidebar_pane("root1")
        assert tmux.pane_session(sidebar) == worker_session

        attached = {session: False, worker_session: False}
        monkeypatch.setattr(tmux, "session_attached", lambda name: attached.get(name, False))
        assert not agents.sidebar_come_home(db, "root1", sidebar)   # nobody anywhere: stay
        attached[worker_session] = True
        assert not agents.sidebar_come_home(db, "root1", sidebar)   # the person is right here
        attached[worker_session], attached[session] = False, True
        tmux._tmux("select-window", "-t", f"{session}:root")         # what the person looks at
        assert agents.sidebar_come_home(db, "root1", sidebar)
        assert tmux.pane_window(sidebar) == tmux.pane_window(root_win)
    finally:
        tmux.kill_session(worker_session)
