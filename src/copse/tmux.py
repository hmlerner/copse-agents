"""Minimal tmux driver: one session per workspace, one window per agent."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path


class TmuxError(RuntimeError):
    pass


def _base() -> list[str]:
    """``tmux``, or ``tmux -L <name>`` when COPSE_TMUX_SOCKET selects a
    private server (used by the test suite so runs can't collide)."""
    sock = os.environ.get("COPSE_TMUX_SOCKET")
    return ["tmux", "-L", sock] if sock else ["tmux"]


def _tmux(*args: str, input: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    if not shutil.which("tmux"):
        raise TmuxError("tmux is not installed (macOS: `brew install tmux`)")
    proc = subprocess.run([*_base(), *args], capture_output=True, text=True, input=input)
    if check and proc.returncode != 0:
        raise TmuxError(f"tmux {' '.join(args)}: {proc.stderr.strip()}")
    return proc


def has_session(session: str) -> bool:
    return _tmux("has-session", "-t", f"={session}", check=False).returncode == 0


def ensure_session(session: str, cwd: str, env: dict[str, str]) -> None:
    """Create a detached session whose first window is a plain shell."""
    if has_session(session):
        return
    env_args = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
    _tmux("new-session", "-d", "-s", session, "-n", "shell", "-c", cwd, *env_args)


def new_window(session: str, name: str, cwd: str, command: list[str], env: dict[str, str],
               tag: tuple[str, str] | None = None) -> str:
    """Open a window running ``command``. Returns the agent's PANE id (``%<n>``),
    not the window's: a window can hold more than one pane (e.g. the watch
    dashboard beside a supervisor), and keys sent to a window go to whichever
    pane happens to be active. ``tag`` (a pane option name and value, see
    set_pane_tag) is set on the pane before this returns, so the pane says
    whose it is from the moment anything else can see it."""
    env_args = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
    proc = _tmux(
        "new-window", "-d", "-P", "-F", "#{pane_id}", "-t", f"={session}:",
        "-n", name, "-c", cwd, *env_args, "--", *command,
    )
    target = proc.stdout.strip()
    import sys as _sys
    _sys.stderr.write(f"DEBUG new_window {target} in {session}: {_tmux('list-panes', '-a', '-F', '#{session_name} #{pane_id} dead=#{pane_dead} cmd=#{pane_current_command}', check=False).stdout!r}\n")
    if tag:
        r = set_pane_tag(target, *tag)
    _sys.stderr.write(f"DEBUG after tag: {_tmux('list-panes', '-a', '-F', '#{session_name} #{pane_id} dead=#{pane_dead} cmd=#{pane_current_command} tag=#{@copse_agent}', check=False).stdout!r}\n")
    # Keep the agent's pane around after it exits so its output can be read.
    _tmux("set-option", "-p", "-t", target, "remain-on-exit", "on", check=False)
    return target


def windows(session: str) -> list[str]:
    proc = _tmux("list-windows", "-t", f"={session}", "-F", "#{window_name}", check=False)
    return proc.stdout.split() if proc.returncode == 0 else []


# PawDelta palette (pawdelta.com): near-black ground, indigo accent, slate text.
THEME = {
    "bg": "#0a0b0f", "bg2": "#111318", "line": "#1f2230",
    "accent": "#6366f1", "accent_light": "#818cf8",
    "text": "#f1f5f9", "muted": "#64748b", "muted2": "#94a3b8",
}


def apply_theme(session: str) -> None:
    """Style one copse session (never the person's global tmux config)."""
    t = THEME
    opts = {
        "status-style": f"bg={t['bg2']},fg={t['muted2']}",
        "status-left": f"#[bg={t['accent']},fg={t['text']},bold] copse #[bg={t['bg2']},fg={t['accent']}] ",
        "status-left-length": "20",
        "status-right": f"#[fg={t['muted']}]#{{session_name}}  %H:%M ",
        "status-right-length": "60",
        "window-status-format": f"#[fg={t['muted']}] #W ",
        "window-status-current-format": f"#[fg={t['accent_light']},bold] #W ",
        "pane-border-style": f"fg={t['line']}",
        "pane-active-border-style": f"fg={t['accent']}",
        "pane-border-lines": "single",
        "window-style": f"bg={t['bg']}",
        "window-active-style": f"bg={t['bg']}",
        "message-style": f"bg={t['accent']},fg={t['text']}",
        "mode-style": f"bg={t['accent']},fg={t['text']}",
        # Wheel scrolling and click-to-focus between the sidebar and the chat.
        "mouse": "on",
    }
    # Lets Claude Code notice when its pane gains or loses focus (it asks for
    # this). Server-wide in tmux, and harmless for other sessions.
    _tmux("set-option", "-s", "focus-events", "on", check=False)
    # set-option doesn't accept the "=name" exact-match form other commands do.
    for key, value in opts.items():
        _tmux("set-option", "-t", session, key, value, check=False)
    for key in ("window-style", "window-active-style", "pane-border-style",
                "pane-active-border-style", "pane-border-lines", "mode-style",
                "window-status-format", "window-status-current-format"):
        # window options: set on every window of the session
        for win in _tmux("list-windows", "-t", f"={session}", "-F", "#{window_id}", check=False).stdout.split():
            _tmux("set-option", "-w", "-t", win, key, opts[key], check=False)
    set_follow_hooks(session)


# Fired whenever a copse session's active window changes (picking a different
# window with the mouse, `next-window`, ...) or a client switches into it
# (switch-client from elsewhere): both are how the sidebar's home window can
# change, so both relocate it (see agents.sidebar_follow). Session-scoped
# only, like the rest of this function: never -g, so a plain tmux session the
# person opened themselves is never touched.
FOLLOW_HOOKS = ("session-window-changed", "client-session-changed")


def set_follow_hooks(session: str) -> None:
    """``session`` is baked in literally rather than read back from tmux's
    own ``#{hook_session_name}`` format variable: that variable came back
    empty for client-session-changed in testing (with and without -b), while
    a hook set with ``-t <session>`` only ever fires for that session anyway,
    so there's nothing it would tell us that we don't already know. The
    command redirects its own output and always exits 0, so a bug in it can
    never surface as a visible tmux error or message popup."""
    from copse.providers import copse_invocation

    cmd = " ".join(shlex.quote(a) for a in [*copse_invocation(), "_sidebar-follow", session])
    shell = f"{cmd} >/dev/null 2>&1 || true"
    for hook in FOLLOW_HOOKS:
        _tmux("set-hook", "-t", session, hook, f"run-shell -b {shlex.quote(shell)}", check=False)


def split_left(target: str, cwd: str, command: list[str], env: dict[str, str],
               columns: int = 30) -> str:
    """Open a narrow pane to the LEFT of ``target`` running ``command``,
    keeping focus on ``target``. Returns the new pane's id."""
    env_args = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
    proc = _tmux(
        "split-window", "-d", "-h", "-b", "-l", str(columns), "-P", "-F", "#{pane_id}",
        "-t", target, "-c", cwd, *env_args, "--", *command,
    )
    pane = proc.stdout.strip()
    # tmux grows every pane proportionally when a client attaches at a bigger
    # size; keep the sidebar at its width whenever the window is resized.
    _tmux("set-hook", "-w", "-t", target, "window-resized",
          f"resize-pane -t {pane} -x {columns}", check=False)
    return pane


def split_below(target: str, cwd: str, command: list[str], env: dict[str, str],
                lines: int = 14) -> str:
    """Open a pane under ``target`` running ``command``, keeping focus on
    ``target``. Returns the new pane's id."""
    env_args = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
    proc = _tmux(
        "split-window", "-d", "-v", "-l", str(lines), "-P", "-F", "#{pane_id}",
        "-t", target, "-c", cwd, *env_args, "--", *command,
    )
    return proc.stdout.strip()


def window_alive(target: str) -> bool:
    proc = _tmux("display-message", "-p", "-t", target, "#{pane_dead}", check=False)
    return proc.returncode == 0 and proc.stdout.strip() == "0"


# Pane-level user options copse sets to say what a pane is: an agent's (its
# id; see agents.AGENT_TAG) or the dashboard's (its session root's id; see
# agents.SIDEBAR_TAG). list_panes reads them along with liveness, in the same
# call, so no caller needs a second one.
AGENT_TAG = "@copse_agent"
SIDEBAR_TAG = "@copse_sidebar"
PANE_TAGS = (AGENT_TAG, SIDEBAR_TAG)


class PaneSnapshot(dict):
    """list_panes' result: pane (and window) id -> alive, plus ``tags``,
    pane id -> {tag: value} for every live-or-dead pane that carries one of
    PANE_TAGS."""

    tags: dict[str, dict[str, str]]

    def __init__(self) -> None:
        super().__init__()
        self.tags = {}


def list_panes() -> PaneSnapshot:
    """Every pane's liveness across every session on this server, in one
    call, keyed by pane id (and also by window id, for agents whose stored
    ``tmux_window`` predates pane-id tracking -- a window counts as alive if
    any of its panes are), with each pane's copse tags (see PANE_TAGS). A
    snapshot with several agents can share this instead of one
    ``display-message`` per agent. Empty (not an error) when there is no
    server running, or tmux isn't installed at all."""
    result = PaneSnapshot()
    fmt = "\t".join(["#{window_id}", "#{pane_id}", "#{pane_dead}", *(f"#{{{k}}}" for k in PANE_TAGS)])
    try:
        proc = _tmux("list-panes", "-a", "-F", fmt, check=False)
    except TmuxError:
        return result
    if proc.returncode != 0:
        return result
    for line in proc.stdout.splitlines():
        window_id, pane_id, dead, *values = line.split("\t")
        alive = dead.strip() == "0"
        result[pane_id] = alive
        result[window_id] = result.get(window_id, False) or alive
        tags = {k: v for k, v in zip(PANE_TAGS, values) if v}
        if tags:
            result.tags[pane_id] = tags
    return result


def window_pids(target: str) -> list[int]:
    """The process ids of the programs in ``target``'s panes."""
    out = _tmux("list-panes", "-t", target, "-F", "#{pane_pid}", check=False).stdout
    return [int(p) for p in out.split() if p.isdigit()]


def kill_window(target: str) -> None:
    _tmux("kill-window", "-t", target, check=False)


def kill_pane(target: str) -> None:
    _tmux("kill-pane", "-t", target, check=False)


def pane_window(pane: str) -> str | None:
    """The id of the window ``pane`` is currently in, or None if it's gone."""
    proc = _tmux("display-message", "-p", "-t", pane, "#{window_id}", check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def active_window(session: str) -> str | None:
    """The id of ``session``'s currently active window, or None if the
    session doesn't exist."""
    proc = _tmux("display-message", "-p", "-t", session, "#{window_id}", check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def set_pane_tag(pane: str, key: str, value: str) -> None:
    _tmux("set-option", "-p", "-t", pane, key, value, check=False)


def get_pane_tag(pane: str, key: str) -> str | None:
    """The pane-level user option ``key``, or None if unset or ``pane`` is
    gone. Pane ids (``%N``) are a per-server counter that restarts at 0 after
    a tmux server restart, so an id from a stale DB row can silently mean a
    completely different, unrelated pane; tagging the pane at creation with
    an id of ours (see agents.SIDEBAR_TAG) lets callers tell the two apart."""
    proc = _tmux("show-options", "-p", "-q", "-t", pane, "-v", key, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def agent_pane_in_window(window: str, sidebar: str | None) -> str | None:
    """The pane to anchor the sidebar beside in ``window``: its active pane,
    or (if that's the sidebar itself) any other pane there. None if the
    sidebar is the only pane (never join it onto itself)."""
    proc = _tmux("display-message", "-p", "-t", window, "#{pane_id}", check=False)
    active = proc.stdout.strip() if proc.returncode == 0 else None
    if active and active != sidebar:
        return active
    panes = _tmux("list-panes", "-t", window, "-F", "#{pane_id}", check=False).stdout.split()
    others = [p for p in panes if p != sidebar]
    return others[0] if others else None


def move_pane(pane: str, target: str, columns: int = 30) -> None:
    """Relocate ``pane`` (e.g. the sidebar) to sit at the left of ``target``'s
    window, keeping focus on whatever's already active there. Re-points the
    window-resize pin (see split_left) at the new window and clears it from
    the old one, so a resize never tries to resize a pane that's moved on.
    If ``pane`` is the only pane left in its window (whatever it sat beside
    has exited), that window closes behind it: the pane itself moves and
    keeps running, and a window holding nothing but the sidebar is no use to
    anyone. Refusing instead would strand the sidebar there, out of sight."""
    old_window = pane_window(pane)
    _tmux("join-pane", "-h", "-b", "-d", "-l", str(columns), "-s", pane, "-t", target, check=False)
    if old_window and old_window != pane_window(pane):
        _tmux("set-hook", "-w", "-t", old_window, "-u", "window-resized", check=False)
    _tmux("set-hook", "-w", "-t", target, "window-resized",
          f"resize-pane -t {pane} -x {columns}", check=False)


def kill_server() -> None:
    _tmux("kill-server", check=False)
    sock = os.environ.get("COPSE_TMUX_SOCKET")
    if sock:
        # tmux leaves a killed server's socket file behind; a private
        # server's is ours to tidy (the default server's never is).
        remove_socket(sock)


def socket_dir() -> Path:
    """Where tmux keeps its named sockets (``tmux -L <name>``)."""
    return Path(os.environ.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}"


def remove_socket(name: str) -> None:
    try:
        (socket_dir() / name).unlink()
    except OSError:
        pass


def other_servers(prefix: str = "copse-") -> list[str]:
    """Names of the named tmux servers (sockets) starting with ``prefix``,
    other than the one copse is using, live or not."""
    try:
        names = [p.name for p in socket_dir().iterdir() if p.name.startswith(prefix)]
    except OSError:
        return []
    return sorted(n for n in names if n != os.environ.get("COPSE_TMUX_SOCKET"))


def _on(server: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", "-L", server, *args], capture_output=True, text=True)


def server_homes(server: str) -> list[str | None] | None:
    """The COPSE_HOME of each session on the named ``server`` (None for a
    session without one), or None when no server is listening there."""
    if not shutil.which("tmux"):
        return None
    proc = _on(server, "list-sessions", "-F", "#{session_name}")
    if proc.returncode != 0:
        return None
    homes: list[str | None] = []
    for name in proc.stdout.split():
        env = _on(server, "show-environment", "-t", f"={name}", "COPSE_HOME").stdout.strip()
        home = env.partition("=")[2] if env.startswith("COPSE_HOME=") else ""
        homes.append(home or None)
    return homes


def reap_server(server: str) -> None:
    """Stop the named ``server`` (if it's running) and remove its socket."""
    if shutil.which("tmux"):
        _on(server, "kill-server")
    remove_socket(server)


def list_sessions() -> list[tuple[str, bool]]:
    """``(name, attached)`` for every session on copse's server; empty when
    there is no server."""
    try:
        proc = _tmux("list-sessions", "-F", "#{session_name} #{session_attached}", check=False)
    except TmuxError:
        return []
    if proc.returncode != 0:
        return []
    out = []
    for line in proc.stdout.splitlines():
        name, _, attached = line.rpartition(" ")
        out.append((name, attached.strip() not in ("", "0")))
    return out


def session_pane_ids(session: str) -> list[str]:
    proc = _tmux("list-panes", "-s", "-t", f"={session}", "-F", "#{pane_id}", check=False)
    return proc.stdout.split() if proc.returncode == 0 else []


SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh", "nu"}


def session_idle(session: str) -> bool:
    """Whether every pane of ``session`` is dead, an idle shell, or a copse
    sidebar: nothing the person started (a dev server, an editor) runs in it."""
    proc = _tmux("list-panes", "-s", "-t", f"={session}", "-F",
                 "#{pane_dead}\t#{pane_current_command}\t#{pane_start_command}", check=False)
    if proc.returncode != 0:
        return False
    for line in proc.stdout.splitlines():
        dead, _, rest = line.partition("\t")
        current, _, start = rest.partition("\t")
        if dead == "1" or "watch --sidebar" in start:
            continue
        if current.lstrip("-") not in SHELLS:
            return False
    return True


def kill_session(session: str) -> None:
    _tmux("kill-session", "-t", f"={session}", check=False)


def capture(target: str, lines: int = 200, escapes: bool = False) -> str:
    """``escapes`` includes SGR colour/attribute codes (capture-pane -e):
    needed to tell styled placeholder text apart from something typed."""
    flags = ["-e"] if escapes else []
    return _tmux("capture-pane", "-p", "-J", *flags, "-t", target, "-S", f"-{lines}").stdout


def paste(target: str, text: str, submit: bool = True, lead: str | None = None) -> None:
    """Paste ``text`` as one bracketed paste (so newlines don't submit early),
    then press Enter. ``lead``, a single line, is typed before it instead of
    pasted: agent CLIs treat pasted text as untrusted content, and typed
    text as the person's own words, so the lead is what vouches for it."""
    if lead:
        _tmux("send-keys", "-t", target, "-l", lead.replace("\n", " ") + " ")
    buf = f"copse-{uuid.uuid4().hex[:8]}"
    _tmux("load-buffer", "-b", buf, "-", input=text)
    _tmux("paste-buffer", "-p", "-d", "-b", buf, "-t", target)
    if submit:
        # TUIs debounce paste events; Enter too soon gets folded into the paste.
        time.sleep(0.3)
        try:
            _tmux("send-keys", "-t", target, "Enter")
        except TmuxError:
            import os as _os
            info = ["SHELL=" + _os.environ.get("SHELL", "<unset>")]
            for args in (("list-sessions",), ("list-panes", "-a", "-F", "#{session_name} #{window_id} #{pane_id} dead=#{pane_dead} cmd=#{pane_current_command} start=#{pane_start_command} roe=#{remain-on-exit}"),
                         ("show-options", "-g", "remain-on-exit"), ("show-options", "-g", "exit-empty"), ("show-options", "-g", "destroy-unattached"),
                         ("show-hooks", "-g"), ("server-info",)):
                r = _tmux(*args, check=False)
                info.append(f"$ tmux {' '.join(args)}\n{r.stdout[-3000:]}{r.stderr}")
            raise TmuxError("PANE DEBUG\n" + "\n".join(info))


def send_keys(target: str, *keys: str) -> None:
    _tmux("send-keys", "-t", target, *keys)


def attach_command(session: str, window: str | None = None) -> list[str]:
    target = f"={session}" if window is None else window
    return [*_base(), "attach-session", "-t", target]


def select_window(target: str) -> None:
    """Focus the window holding ``target`` and, for a pane id, that pane."""
    _tmux("select-window", "-t", target, check=False)
    if target.startswith("%"):
        _tmux("select-pane", "-t", target, check=False)
