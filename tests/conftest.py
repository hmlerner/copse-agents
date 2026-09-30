import subprocess
from pathlib import Path

import pytest

from copse.db import DB


def sh(cmd: str, cwd: Path) -> str:
    return subprocess.run(
        cmd, shell=True, cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _reap_dead_test_servers() -> None:
    """Servers (and socket files) of earlier runs that died before their
    teardown, e.g. killed by a command timeout."""
    from copse import procs, tmux

    for name in tmux.other_servers("copse-test-"):
        pid = name.removeprefix("copse-test-")
        if pid.isdigit() and not procs.alive(int(pid)):
            tmux.reap_server(name)


@pytest.fixture(scope="session", autouse=True)
def private_tmux_server():
    """Run every test's tmux sessions on a private server, so parallel test
    runs (e.g. two copse workers testing at once) can't collide. Nothing of
    it survives the run: a watchdog stops the server and removes its socket
    once this process is gone, even if it was killed before teardown."""
    import os

    from copse import tmux

    _reap_dead_test_servers()
    old = os.environ.get("COPSE_TMUX_SOCKET")
    name = f"copse-test-{os.getpid()}"
    os.environ["COPSE_TMUX_SOCKET"] = name
    watchdog = subprocess.Popen(
        ["/bin/sh", "-c",
         'while kill -0 "$1" 2>/dev/null; do sleep 1; done; '
         'tmux -L "$2" kill-server 2>/dev/null; rm -f "$3"',
         "copse-test-watchdog", str(os.getpid()), name, str(tmux.socket_dir() / name)],
        start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    yield
    try:
        tmux.kill_server()
    except tmux.TmuxError:
        pass
    watchdog.kill()
    watchdog.wait()
    if old is None:
        os.environ.pop("COPSE_TMUX_SOCKET", None)
    else:
        os.environ["COPSE_TMUX_SOCKET"] = old


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """Each test's sessions end with it, so none outlive the test that made
    them (or leak into the next one). Runs after every fixture's teardown,
    so a test's monkeypatching (of subprocess, say) is undone by then."""
    import os

    from copse import tmux

    yield
    if os.environ.get("COPSE_TMUX_SOCKET") == f"copse-test-{os.getpid()}":
        try:
            tmux.kill_server()
        except tmux.TmuxError:
            pass


@pytest.fixture(autouse=True)
def copse_home(tmp_path, monkeypatch):
    home = tmp_path / "copse-home"
    monkeypatch.setenv("COPSE_HOME", str(home))
    # Never touch the real ~/.claude.json (providers.trust_folder writes there).
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    # The shell provider runs $SHELL. The person's own shell reads their dotfiles,
    # so a slow or stuck one (a stale pyenv rehash lock waits 60s) would fail
    # tests that give the shell a few seconds.
    monkeypatch.setenv("SHELL", "/bin/sh")
    for k in ("GIT_DIR", "GIT_WORK_TREE", "COPSE_AGENT_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")
    # copse's own Pro plugins are always installed: keep them off the real
    # keychain and network (a file store under the temporary home, no login).
    monkeypatch.setenv("COPSE_PRO_CREDENTIAL_STORE", "file")
    monkeypatch.delenv("COPSE_PRO_DEV", raising=False)
    from copse.pro import license

    license.clear_cache()
    yield home
    license.clear_cache()


@pytest.fixture(autouse=True)
def push_messages(monkeypatch):
    """Most tests read the messages copse queues for a supervisor directly;
    tests/test_message_pull.py turns pull mode (the real default) back on."""
    from copse import agents

    monkeypatch.setattr(agents, "pulls_messages", lambda db, agent: False)


@pytest.fixture
def db(copse_home):
    return DB()


@pytest.fixture
def repo(tmp_path):
    """A repo on ``main`` with one commit, pushed to a bare ``origin``."""
    origin = tmp_path / "origin.git"
    sh(f"git init -q --bare -b main {origin}", tmp_path)
    work = tmp_path / "proj"
    work.mkdir()
    sh("git init -q -b main", work)
    (work / "app.py").write_text("print('hi')\n")
    (work / ".gitignore").write_text(".env\n")
    (work / ".env").write_text("SECRET=1\n")
    sh("git add -A && git commit -qm init", work)
    sh(f"git remote add origin {origin} && git push -q -u origin main", work)
    sh("git remote set-head origin main", work)
    return work
