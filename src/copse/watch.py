"""`copse watch`: a live view of every workspace and agent.

``render`` turns a snapshot into styled lines and knows nothing about the
terminal; ``run`` is a thin curses loop around it. ``--once`` prints a
single render without curses.
"""

from __future__ import annotations

import curses
import os
import shutil
import subprocess
import textwrap
import time
from dataclasses import dataclass, field

from copse import agents, tmux, view
from copse.db import DB

REFRESH_SECONDS = 2.0
# How often a running dashboard culls leftover processes and stale workers
# (see copse.cull), on a thread of its own so the screen never waits on it.
CULL_SECONDS = 60.0


def _cull_in_background() -> None:
    import threading

    from copse import cull

    threading.Thread(target=lambda: cull.sweep_quietly(DB()), daemon=True).start()

# Styles are names, mapped to curses attributes (or ANSI for --once) later.
STATUS_STYLE = {
    "processing": "busy",
    "starting": "busy",
    "idle": "ok",
    "waiting": "alert",
    "exited": "bad",
    "paused": "dim",
    "done": "ok",
}

# How each status reads on screen: (icon, label).
STATUS_LABEL = {
    "processing": ("●", "working"),
    "starting": ("◌", "starting up"),
    "idle": ("○", "idle"),
    "waiting": ("◆", "needs you"),
    "exited": ("✕", "stopped"),
    "paused": ("‖", "paused"),
    "done": ("✓", "done"),
}


@dataclass
class Line:
    text: str
    style: str = "normal"          # normal | dim | bold | busy | ok | alert | bad
    agent: dict | None = None      # set on agent rows; these are selectable
    workspace: dict | None = None
    group: str | None = None       # set on workspace headers (the workspace id); selectable too
    needs: int = 0                 # rows here that need you (1 on such an agent row)


@dataclass
class NavState:
    """What the person has done to the sidebar, kept between refreshes."""
    selected: str | None = None    # row_key of the selected row
    pos: int = 0                   # its position among selectable rows, for when it goes away
    group: str | None = None       # the selected row's workspace id
    collapsed: set[str] = field(default_factory=set)  # workspace ids folded away
    filter: str = ""
    filtering: bool = False        # typing into the filter
    help: bool = False
    offset: int = 0
    follow: bool = True            # scroll the selection into view on the next draw
    notice: str = ""               # a message for the loop to show briefly


def fit(text: str, width: int) -> str:
    """``text`` cut to ``width`` columns, ending in "…" when it was cut."""
    if len(text) <= width:
        return text
    return text[:width - 1] + "…" if width > 0 else ""


def elide_middle(text: str, width: int) -> str:
    """``text`` cut to ``width`` by dropping its middle, so a long branch keeps
    both its prefix and the distinctive end."""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:max(width, 0)]
    tail = (width - 1) // 2
    return text[:width - 1 - tail] + "…" + (text[-tail:] if tail else "")


WORKERS = ("assign", "handoff")


def awaiting_review(agent: dict, ws: dict) -> str | None:
    """How a worker that reported and hasn't been approved yet reads, or None."""
    if (agent.get("mode") in WORKERS and agent.get("reported")
            and agent["status"] not in ("processing", "starting")):
        return {"approved": None, "changes": "changes requested"}.get(ws.get("review"), "to review")
    return None


def needs_you(agent: dict, ws: dict) -> str | None:
    """Why a person must act on ``agent``'s row, or None: it is waiting on a
    prompt or dialog, it is a supervisor with an open need_user question, or
    it is a worker awaiting review in a session without autopilot (with
    autopilot on, the supervisor reviews it)."""
    if agent["status"] == "waiting":
        return "needs you"
    if ws.get("asking") == agent["id"]:
        return "has a question"
    return None if ws.get("autopilot") else awaiting_review(agent, ws)


def ago(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return ""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h{int(seconds % 3600 // 60):02d}m"
    return f"{int(seconds // 86400)}d"


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' * (n != 1)}"


def summary(snap: list[dict]) -> str:
    agents_ = [a for ws in snap for a in ws["agents"]]
    waiting = sum(bool(needs_you(a, ws)) for ws in snap for a in ws["agents"])
    busy = sum(a["status"] in ("processing", "starting") for a in agents_)
    parts = []
    if waiting:
        parts.append(f"{waiting} need{'s' * (waiting == 1)} you")
    if busy:
        parts.append(f"{busy} working")
    if not parts:
        parts.append("all quiet")
    return " · ".join(parts)


def git_summary(ws: dict) -> str:
    if ws["ahead"] is None:
        return "folder missing" if not os.path.isdir(ws["path"]) else ""
    parts = []
    if ws["ahead"]:
        parts.append(f"{ws['ahead']} ahead")
    if ws["behind"]:
        parts.append(f"{ws['behind']} behind")
    parts = [" · ".join(parts) + f" {ws['base_branch']}"] if parts else [f"up to date with {ws['base_branch']}"]
    if ws["dirty"]:
        parts.append(f"{plural(ws['dirty'], 'file')} changed")
    return " · ".join(parts)


def _wrap(text: str, width: int, indent: str) -> list[str]:
    """Wrap at spaces only; a word too long for a line of its own is elided
    rather than split across two."""
    wrapped = textwrap.wrap(text, max(width, len(indent) + 8), initial_indent=indent,
                            subsequent_indent=indent, break_long_words=False,
                            break_on_hyphens=False) or [indent]
    return [fit(t, width) for t in wrapped]


MILESTONE_MARK = {"passed": ("✓", "ok"), "failed": ("✗", "bad"), "pending": ("○", "dim")}


def render_autopilot(pilot: dict, width: int) -> list[Line]:
    """The goal and its milestones, above the agents."""
    if not pilot["enabled"]:
        return [Line("Autopilot off", "dim"), Line("")]
    if not pilot["goal"]:
        lines = [Line("Autopilot on", "accent")]
        lines += [Line(t, "dim") for t in _wrap("Tell the supervisor what we're building.", width, "  ")]
        return lines + [Line("")]
    lines = [Line(t, "accent") for t in _wrap(f"Autopilot · {pilot['goal']}", width, "")]
    for m in pilot["milestones"]:
        mark, style = MILESTONE_MARK.get(m["status"], ("·", "dim"))
        wrapped = _wrap(m["title"], width - 4, "")
        lines.append(Line(f"  {mark} {wrapped[0]}", style))
        lines += [Line(f"    {t}", style) for t in wrapped[1:]]
    done = sum(m["status"] == "passed" for m in pilot["milestones"])
    total = len(pilot["milestones"])
    state = {"done": ("goal reached", "ok"), "blocked": ("needs you", "alert"),
             "stalled": ("stalled: needs you", "alert")}.get(pilot["state"])
    if pilot["state"] == "usage_paused":
        until = pilot.get("usage_resets_at")
        when = (f" until {time.strftime('%-I:%M%p', time.localtime(until)).lower()}"
                if isinstance(until, (int, float)) else "")
        state = (f"paused for usage{when}", "alert")
    parts = [f"{done} of {total} verified"]
    if state:
        parts.append(state[0])
    elif pilot.get("workers"):
        parts.append(f"{plural(pilot['workers'], 'worker')} on it")
    lines += [Line(t, state[1] if state else "dim") for t in _wrap(" · ".join(parts), width, "  ")]
    if pilot["state"] in ("blocked", "stalled") and pilot.get("note"):
        lines += [Line(t, "dim") for t in _wrap(pilot["note"], width, "  ")]
    u = pilot.get("usage")
    if u and u["used"] >= 75:
        from copse.autopilot import usage_note

        lines += [Line(t, "alert") for t in _wrap(usage_note(u), width, "  ")]
    return lines + [Line("")]


def matching_agents(ws: dict, text: str) -> list[dict] | None:
    """The agents of ``ws`` the filter ``text`` keeps, or None to hide the
    whole workspace. A match on the branch keeps all of them."""
    text = text.strip().lower()
    if not text or text in ws["branch"].lower() or text in (ws.get("name") or "").lower():
        return ws["agents"]
    kept = [a for a in ws["agents"]
            if any(text in (a.get(k) or "").lower() for k in ("id", "profile", "provider"))]
    return kept or None


def group_title(ws: dict, count: int, needing: int, width: int, collapsed: bool) -> str:
    """A workspace's header: its branch, elided in the middle to fit, and on a
    folded group how many agents (and how many needing you) it hides."""
    arrow = "▸ " if collapsed else "▾ "
    suffix = f" ({count})" + (f" {needing}◆" if needing else "") if collapsed else ""
    room = width - len(arrow) - len(suffix)
    tags = ("  (your checkout)", " (yours)") if ws.get("name") == "root" else ()
    tag = next((t for t in tags if len(ws["branch"]) + len(t) <= room), "")
    return fit(arrow + elide_middle(ws["branch"], max(room - len(tag), 1)) + tag + suffix, width)


def render_agent(a: dict, ws: dict, now: float, width: int) -> list[Line]:
    icon, label = STATUS_LABEL.get(a["status"], ("·", a["status"]))
    style = STATUS_STYLE.get(a["status"], "normal")
    if a["status"] == "idle" and a.get("reported"):
        icon, label = "✓", "done"
    if (reason := needs_you(a, ws)):
        icon, label, style = "◆", reason, "alert"
    elif (pending_review := awaiting_review(a, ws)):  # autopilot will review it
        icon, label, style = "◇", pending_review, "dim"
    name = a["profile"].replace("-", " ").capitalize()
    if a["provider"] != "claude":
        name += f" ({a['provider']})"
    elif a.get("headless"):
        name += " (headless)"
    lines = [Line(fit(f"  {icon} {name}", width), style, agent=a, workspace=ws,
                  needs=1 if reason else 0)]
    since = a.get("status_since")
    detail = [label + (f" for {ago(now - since)}" if since else "")]
    if a.get("pending"):
        detail.append(f"{plural(a['pending'], 'message')} queued")
    if a.get("tokens"):
        detail.append(a["tokens"])
    detail.append(a["id"][:6])
    lines += [Line(t, "dim", workspace=ws) for t in _wrap(" · ".join(detail), width, "    ")]
    for sub in a.get("subagents") or []:
        sub_name = sub.get("agent_type") or "subagent"
        if sub["ended_at"] is None:
            text, style = f"↳ {sub_name} · running {ago(now - sub['started_at'])}", "busy"
        else:
            text, style = f"↳ {sub_name} · ✓ done", "dim"
        # Not selectable: no `agent=`, so it can't be attached to or peeked.
        lines += [Line(t, style, workspace=ws) for t in _wrap(text, width, "    ")]
    return lines


def render_group(ws: dict, ags: list[dict], now: float, width: int, collapsed: bool) -> list[Line]:
    """A workspace header and, unless folded, its agents: the ones needing
    you first, otherwise in their usual order."""
    needing = sum(bool(needs_you(a, ws)) for a in ags)
    lines = [Line(group_title(ws, len(ags), needing, width, collapsed),
                  "alert" if collapsed and needing else "bold", workspace=ws, group=ws["id"],
                  needs=needing if collapsed else 0)]
    if collapsed:
        return lines
    if (info := git_summary(ws)):
        lines += [Line(t, "dim", workspace=ws) for t in _wrap(info, width, "  ")]
    if not ags:
        lines.append(Line("  no agents", "dim", workspace=ws))
    for a in sorted(ags, key=lambda a: not needs_you(a, ws)):
        lines += render_agent(a, ws, now, width)
    return lines


def render(snap: list[dict], now: float, width: int = 80, pilot: dict | None = None,
           state: NavState | None = None) -> list[Line]:
    state = state or NavState()
    agents_ = [a for ws in snap for a in ws["agents"]]
    needing = any(needs_you(a, ws) for ws in snap for a in ws["agents"])
    lines = [Line(fit(summary(snap), width), "alert" if needing else "bold")]
    if snap:
        lines.append(Line(fit(f"{plural(len(agents_), 'agent')} in {plural(len(snap), 'workspace')}",
                              width), "dim"))
    lines.append(Line(""))
    if pilot:
        lines += render_autopilot(pilot, width)
    if not snap:
        for t in _wrap("Nothing running yet. Start an agent with `copse new <branch>`.", width, ""):
            lines.append(Line(t, "dim"))
        return lines
    shown = [(ws, ags) for ws in snap if (ags := matching_agents(ws, state.filter)) is not None]
    if not shown:
        lines += [Line(t, "dim") for t in _wrap(f"Nothing matches “{state.filter}”.", width, "")]
        return lines
    for ws, ags in shown:
        lines += render_group(ws, ags, now, width, ws["id"] in state.collapsed)
        lines.append(Line(""))
    return lines


# -- --once ------------------------------------------------------------------

ANSI = {"bold": "1", "dim": "2", "busy": "36", "ok": "32", "alert": "1;33", "bad": "31",
        "accent": "1;35"}


def print_once(db: DB, repo_root: str | None, color: bool) -> str:
    out = []
    width = shutil.get_terminal_size().columns - 1
    panes = tmux.list_panes()
    for line in render(view.snapshot(db, repo_root, panes=panes), time.time(), width,
                       view.autopilot_entry(db, repo_root, panes=panes)):
        code = ANSI.get(line.style) if color else None
        out.append(f"\033[{code}m{line.text}\033[0m" if code else line.text)
    return "\n".join(out)


# -- interactive ---------------------------------------------------------------

# The footer: the first of these that fits, when there's room for one.
HELP = ["↑↓ ⏎ open  x close  ? keys", "? keys"]

# What `?` lists: (keys, what they do). Kept short enough for a 30-column
# sidebar: the keys in a 9-column field, then at most 19 columns of text.
KEYS = [
    ("↑↓ j k", "move"),
    ("PgUp/Dn", "page"),
    ("Home/End", "top / bottom"),
    ("⏎ a", "open the agent"),
    ("p", "peek at its screen"),
    ("x", "close (2× if busy)"),
    ("n", "next needing you"),
    ("Spc Tab", "fold group"),
    ("/", "filter, Esc clears"),
    ("r", "refresh"),
    ("?", "this help"),
    ("q", "quit"),
]
# In the sidebar, q quits copse itself: the session pauses, as when the chat
# ends, and the person gets their prompt back.
SIDEBAR_QUIT = ("q", "quit copse (2×)")
# h hides the sidebar (the chat zooms; prefix S or prefix z brings it back).
SIDEBAR_HIDE = ("h", "hide (prefix S)")
KEY_COLUMN = 9


def help_lines(width: int, in_tmux: bool = False, sidebar: bool = False) -> list[Line]:
    """The `?` overlay, drawn in place of the list."""
    keys = [SIDEBAR_QUIT if sidebar and k == "q" else (k, what) for k, what in KEYS]
    if sidebar:
        keys.insert(-1, SIDEBAR_HIDE)
    lines = [Line("Keys", "bold")]
    lines += [Line(fit(f"{k:<{KEY_COLUMN}}{what}", width)) for k, what in keys]
    lines += [Line(""), Line(fit("◆ needs you", width), "alert"),
              Line(fit("◇ autopilot will review", width), "dim")]
    if in_tmux:
        lines += [Line(t, "dim") for t in _wrap("prefix L: back from an agent", width, "")]
        if sidebar:
            lines += [Line(t, "dim") for t in _wrap(
                "prefix S: show the sidebar again. To copy chat text: drag in the chat, "
                "or prefix z zooms it", width, "")]
    lines += [Line(t, "dim") for t in _wrap("any key to go back", width, "")]
    return lines


def footer(state: NavState, width: int) -> Line | None:
    """The bottom line: the filter while there is one, else a key hint."""
    if state.filtering or state.filter:
        text = "/" + state.filter + ("▏" if state.filtering else "  Esc clears")
        if len(text) > width:  # keep the end, where the typing happens
            text = "…" + text[len(text) - width + 1:] if width > 1 else ""
        return Line(text, "accent" if state.filtering else "dim")
    return next((Line(h, "dim") for h in HELP if len(h) <= width), None)

# Closing a running agent stops it, so it takes a second `x` on the same row
# within this many seconds. A stopped one closes on the first press.
CLOSE_CONFIRM_SECONDS = 4.0
STOPPED = ("exited", "paused", "done")


def close_request(agent: dict, armed: tuple[str, float] | None,
                  now: float) -> tuple[bool, tuple[str, float] | None, str]:
    """What an `x` press on ``agent``'s row does: (close it now, the new
    armed state, a notice to show). ``armed`` is (agent id, when) from an
    earlier press waiting for its confirmation; closing a stopped row leaves
    another row's arming as it was."""
    if agent["status"] in STOPPED:
        return True, armed, f"closed {agent['id'][:6]}"
    if armed and armed[0] == agent["id"] and now - armed[1] <= CLOSE_CONFIRM_SECONDS:
        return True, None, f"stopped and closed {agent['id'][:6]}"
    return False, (agent["id"], now), "still running: x again to stop and close it"


def quit_request(armed: tuple[str, float] | None, now: float,
                 key: str = "q") -> tuple[bool, tuple[str, float] | None, str]:
    """What quitting copse from the sidebar does: the first press arms it,
    a second within CLOSE_CONFIRM_SECONDS goes. ``armed`` is shared with
    close_request, under the id "quit"."""
    if armed and armed[0] == "quit" and now - armed[1] <= CLOSE_CONFIRM_SECONDS:
        return True, None, "quitting copse: this session is paused"
    return False, ("quit", now), f"{key} again to quit copse (copse continue resumes)"

# Wheel-down: ncurses only defines this when built with mouse version > 1
# (not always true, e.g. some macOS builds); the bit value itself is stable
# across ncurses versions, so fall back to it rather than dropping the key.
BUTTON5_PRESSED = getattr(curses, "BUTTON5_PRESSED", 0x00200000)


def clamp_scroll(offset: int, total: int, visible: int) -> int:
    """Keep a scroll offset from running past the content or negative."""
    return max(0, min(offset, max(0, total - visible)))


def scroll_into_view(offset: int, index: int, visible: int, total: int) -> int:
    """Adjust ``offset`` so row ``index`` is on screen, then clamp."""
    if index < offset:
        offset = index
    elif visible and index >= offset + visible:
        offset = index - visible + 1
    return clamp_scroll(offset, total, visible)


def content_layout(total: int, height: int, offset: int) -> tuple[int, bool, bool]:
    """How many rows of ``lines`` actually fit in ``height``, and whether the
    "more above"/"more below" indicators are needed -- each gets its own
    reserved row rather than overwriting a content row. Top's need doesn't
    depend on how many rows remain (only on ``offset``), so this doesn't have
    to iterate: reserving a row for it can only ever make "more below" more
    true, never flip it back to false."""
    show_above = offset > 0
    content_rows = max(0, height - (1 if show_above else 0))
    show_below = content_rows > 0 and offset + content_rows < total
    if show_below:
        content_rows -= 1
    return content_rows, show_above, show_below


def nearest_visible_row(rows: list[int], offset: int, visible: int) -> int:
    """After a page-only scroll (PageUp/PageDown/Home/End), the index into
    ``rows`` (agent row positions) closest to the new page, so the selection
    follows it instead of scrolling off screen."""
    if not rows:
        return 0
    in_view = [i for i, r in enumerate(rows) if offset <= r < offset + max(visible, 1)]
    if in_view:
        return in_view[0]
    return min(range(len(rows)), key=lambda i: abs(rows[i] - offset))


# -- navigation ----------------------------------------------------------------

ESC = 27
ENTER_KEYS = (curses.KEY_ENTER, 10, 13)
BACKSPACE_KEYS = (curses.KEY_BACKSPACE, 127, 8)


def selectable(lines: list[Line]) -> list[int]:
    """Indices of the rows the cursor can sit on: agents and workspace headers."""
    return [i for i, ln in enumerate(lines) if ln.agent or ln.group]


def row_key(ln: Line) -> str:
    """What identifies a row across refreshes, however the rows reorder."""
    return f"a:{ln.agent['id']}" if ln.agent else f"g:{ln.group}"


def _select(state: NavState, lines: list[Line], rows: list[int], pos: int) -> int:
    i = rows[pos]
    state.selected, state.pos = row_key(lines[i]), pos
    state.group = lines[i].workspace["id"] if lines[i].workspace else None
    return i


def selection(state: NavState, lines: list[Line]) -> int | None:
    """The index in ``lines`` of the selected row, or None if nothing can be
    selected. It stays on the same agent (or header) when rows move. If that
    row is gone: its group's header when the group was folded, else whatever
    now sits where it was. With nothing chosen yet, the first agent."""
    rows = selectable(lines)
    if not rows:
        return None
    keys = [row_key(lines[i]) for i in rows]
    if state.selected in keys:
        return _select(state, lines, rows, keys.index(state.selected))
    if state.selected is None:
        first = next((p for p, i in enumerate(rows) if lines[i].agent), 0)
        return _select(state, lines, rows, first)
    if state.group in state.collapsed and f"g:{state.group}" in keys:
        return _select(state, lines, rows, keys.index(f"g:{state.group}"))
    return _select(state, lines, rows, min(state.pos, len(rows) - 1))


def _toggle_group(state: NavState, lines: list[Line], i: int) -> None:
    ws = lines[i].workspace
    if not ws:
        return
    state.collapsed ^= {ws["id"]}
    state.selected, state.group, state.follow = f"g:{ws['id']}", ws["id"], True


def handle_key(state: NavState, key: int, lines: list[Line], visible: int,
               sidebar: bool = True) -> str | None:
    """Apply ``key`` to ``state``. Returns what the loop has to do beyond
    redrawing: "quit", "refresh", or "attach"/"peek"/"close" on the selected
    agent. ``visible`` is how many rows of ``lines`` fit on screen."""
    if key in (-1, curses.KEY_RESIZE):
        return "refresh"
    if state.help:  # any key closes it, and does nothing else
        state.help = False
        return None
    if state.filtering:
        if key == ESC:
            state.filter, state.filtering = "", False
        elif key in ENTER_KEYS:
            state.filtering = False
        elif key in BACKSPACE_KEYS:
            state.filter = state.filter[:-1]
        elif 32 <= key < 127:
            state.filter += chr(key)
        elif key in (curses.KEY_UP, curses.KEY_DOWN):  # move through the matches meanwhile
            _move(state, lines, -1 if key == curses.KEY_UP else 1)
        state.follow = True
        return None
    i = selection(state, lines)
    agent = lines[i].agent if i is not None else None
    if key == ESC and state.filter:
        state.filter, state.follow = "", True
        return None
    if key in quit_keys(sidebar):
        return "quit"
    if key == ord("r"):
        return "refresh"
    if key == ord("h") and sidebar:
        return "hide"
    if key == ord("?"):
        state.help = True
    elif key == ord("/"):
        state.filtering = True
    elif key in (curses.KEY_UP, ord("k")):
        _move(state, lines, -1)
    elif key in (curses.KEY_DOWN, ord("j")):
        _move(state, lines, 1)
    elif key == ord("n"):
        _next_needing(state, lines)
    elif key in (ord(" "), ord("\t")) and i is not None:
        _toggle_group(state, lines, i)
    elif key in (*ENTER_KEYS, ord("a")) and i is not None:
        if agent:
            return "attach"
        _toggle_group(state, lines, i)
    elif key == ord("p") and agent:
        return "peek"
    elif key == ord("x") and agent:
        return "close"
    elif key in (curses.KEY_NPAGE, curses.KEY_PPAGE, curses.KEY_HOME, curses.KEY_END):
        step = max(1, visible)
        state.offset = clamp_scroll({curses.KEY_NPAGE: state.offset + step,
                                     curses.KEY_PPAGE: state.offset - step,
                                     curses.KEY_HOME: 0,
                                     curses.KEY_END: len(lines)}[key], len(lines), visible)
        rows = selectable(lines)
        if rows:
            _select(state, lines, rows, nearest_visible_row(rows, state.offset, visible))
    return None


def _move(state: NavState, lines: list[Line], step: int) -> None:
    i = selection(state, lines)
    if i is None:
        return
    rows = selectable(lines)
    _select(state, lines, rows, max(0, min(len(rows) - 1, rows.index(i) + step)))
    state.follow = True


def _next_needing(state: NavState, lines: list[Line]) -> None:
    """Select the next row needing you after the current one, wrapping
    round; a folded group hiding some counts as one."""
    i = selection(state, lines)
    rows = selectable(lines)
    if i is None:
        return
    start = rows.index(i)
    order = rows[start + 1:] + rows[:start + 1]
    target = next((r for r in order if lines[r].needs), None)
    if target is None:
        state.notice = "nothing needs you"
        return
    _select(state, lines, rows, rows.index(target))
    state.follow = True


# PawDelta palette as xterm-256 colours (closest matches): indigo accent,
# soft green / amber / rose for states, slate greys for secondary text.
PALETTE_256 = {
    "accent": 105,    # ~#818cf8 indigo-light
    "busy": 141,      # soft purple: working
    "ok": 79,         # soft green: idle / done
    "alert": 215,     # amber: needs you
    "bad": 174,       # dusty rose: stopped
    "dim": 245,       # slate
    "trunk": 94,      # brown: the logo's trunk
    "text": 255,
    "select_bg": 237, # subtle row highlight
}


def _styles() -> dict[str, int]:
    attrs = {"normal": curses.A_NORMAL, "bold": curses.A_BOLD, "dim": curses.A_DIM,
             "accent": curses.A_BOLD, "select": curses.A_REVERSE, "bar": curses.A_BOLD}
    if not curses.has_colors():
        attrs.update(busy=curses.A_NORMAL, ok=curses.A_NORMAL,
                     alert=curses.A_BOLD, bad=curses.A_DIM)
        return attrs
    curses.use_default_colors()
    if curses.COLORS >= 256:
        p = PALETTE_256
        pairs = [("accent", p["accent"], -1), ("busy", p["busy"], -1), ("ok", p["ok"], -1),
                 ("alert", p["alert"], -1), ("bad", p["bad"], -1), ("dim", p["dim"], -1),
                 ("normal", p["text"], -1), ("select", p["text"], p["select_bg"]),
                 ("trunk", p["trunk"], -1),
                 ("bar", p["accent"], p["select_bg"])]
    else:
        pairs = [("accent", curses.COLOR_MAGENTA, -1), ("busy", curses.COLOR_MAGENTA, -1),
                 ("ok", curses.COLOR_GREEN, -1), ("alert", curses.COLOR_YELLOW, -1),
                 ("bad", curses.COLOR_RED, -1), ("dim", -1, -1), ("normal", -1, -1),
                 ("select", curses.COLOR_WHITE, curses.COLOR_BLUE),
                 ("trunk", curses.COLOR_YELLOW, -1),
                 ("bar", curses.COLOR_MAGENTA, curses.COLOR_BLUE)]
    for i, (name, fg, bg) in enumerate(pairs, start=1):
        curses.init_pair(i, fg, bg)
        attrs[name] = curses.color_pair(i)
    attrs["accent"] |= curses.A_BOLD
    attrs["bold"] = attrs["normal"] | curses.A_BOLD
    attrs["alert"] |= curses.A_BOLD
    attrs["bar"] |= curses.A_BOLD
    if curses.COLORS < 256:
        attrs["dim"] |= curses.A_DIM
    return attrs


def _attach(agent: dict, ws: dict, db: DB) -> None:
    """Leave curses, attach to the agent's tmux window, come back on detach."""
    record = db.get_workspace(ws["id"])
    if not record or not agent.get("window"):
        return
    tmux.select_window(agent["window"])
    curses.endwin()
    if os.environ.get("TMUX"):
        subprocess.run([*tmux._base(), "switch-client", "-t", agent["window"]])
    else:
        subprocess.run(tmux.attach_command(record.tmux_session))


def _peek(stdscr, agent: dict, styles: dict[str, int]) -> None:
    try:
        screen = tmux.capture(agent["window"], lines=200).rstrip().splitlines()
    except tmux.TmuxError as e:
        screen = [f"(can't read the agent's terminal: {e})"]
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    title = f" {agent['id']} ({agent['profile']}) — last lines of its terminal · any key to go back "
    stdscr.addnstr(0, 0, title, w - 1, styles["bold"] | curses.A_REVERSE)
    body = screen[-(h - 2):]
    for i, text in enumerate(body, start=1):
        stdscr.addnstr(i, 0, text, w - 1)
    stdscr.refresh()
    stdscr.timeout(-1)
    stdscr.getch()
    stdscr.timeout(int(REFRESH_SECONDS * 1000))


LOGO = [  # one solid pine; the wordmark sits beside its widest row
    ("   ◢◣", ""),
    ("  ◢██◣", ""),
    (" ◢████◣  ", "copse"),
    ("   ██", ""),
]


def _draw_logo(stdscr, w: int, styles: dict[str, int]) -> int:
    """The pine-and-wordmark header. Returns the first free row."""
    clock = time.strftime("%H:%M")
    for y, (tree, word) in enumerate(LOGO):
        trunk = y == len(LOGO) - 1
        stdscr.addnstr(y, 1, tree, w - 2, styles.get("trunk", styles["accent"]) if trunk else styles["accent"])
        if word and w > len(tree) + len(word) + 2:
            stdscr.addnstr(y, 1 + len(tree), word, len(word), styles["bold"])
    if w > len(clock) + 16:
        stdscr.addnstr(0, w - 1 - len(clock), clock, len(clock), styles["dim"])
    stdscr.addnstr(len(LOGO), 1, "─" * max(0, w - 3), w - 2, styles["dim"])
    return len(LOGO) + 1


def _draw_compact_logo(stdscr, w: int, styles: dict[str, int]) -> int:
    """One-line header for short panes."""
    stdscr.addnstr(0, 1, "◢◣", w - 2, styles["accent"])
    stdscr.addnstr(0, 4, "copse", max(0, w - 5), styles["bold"])
    stdscr.addnstr(1, 1, "─" * max(0, w - 3), w - 2, styles["dim"])
    return 2


def quit_keys(sidebar: bool) -> tuple[int, ...]:
    """Keys that close the dashboard. In the sidebar only `q` does: a stray
    Esc (easy to hit after clicking into the pane) shouldn't dismiss it."""
    return (ord("q"),) if sidebar else (ord("q"), 27)


def _loop(stdscr, repo_root: str | None, sidebar: bool = False) -> None:
    # The session this sidebar belongs to: quitting the sidebar quits it.
    own_root = agents.sidebar_root(os.environ.get("TMUX_PANE")) if sidebar else None
    curses.curs_set(0)
    styles = _styles()
    stdscr.timeout(int(REFRESH_SECONDS * 1000))
    try:
        curses.mousemask(curses.BUTTON1_CLICKED | curses.BUTTON4_PRESSED | BUTTON5_PRESSED)
    except curses.error:
        pass
    db = DB()
    state = NavState()
    snap: list[dict] = []
    pilot: dict | None = None
    stale = True
    armed: tuple[str, float] | None = None
    notice, notice_until = "", 0.0
    culled_at = 0.0
    while True:
        if time.time() - culled_at >= CULL_SECONDS:
            culled_at = time.time()
            _cull_in_background()
        h, w = stdscr.getmaxyx()
        width = max(1, w - 2)  # text starts at column 1, after the selection bar
        if stale:
            panes = tmux.list_panes()
            snap = view.snapshot(db, repo_root, panes=panes)
            pilot = view.autopilot_entry(db, repo_root, panes=panes)
            stale = False
        # Re-rendered every pass: folding, filtering and `?` change it between refreshes.
        if state.help:
            lines = help_lines(width, bool(os.environ.get("TMUX")), sidebar)
            selected = None
        else:
            lines = render(snap, time.time(), width, pilot, state)
            selected = selection(state, lines)
        if state.notice:
            notice, notice_until, state.notice = state.notice, time.time() + CLOSE_CONFIRM_SECONDS, ""

        top = (len(LOGO) + 1) if h >= 18 else 2
        bottom = [Line(t, "dim") for t in _wrap(notice, width, "")] \
            if notice and time.time() < notice_until else []
        # The key hint only when it leaves room for a few rows; the filter always.
        if (last := footer(state, width)) and (h - top - len(bottom) >= 8 or state.filter
                                               or state.filtering):
            bottom.append(last)
        raw_visible = max(0, h - top - 1 - len(bottom))
        state.offset = clamp_scroll(state.offset, len(lines), raw_visible)
        visible, show_above, show_below = content_layout(len(lines), raw_visible, state.offset)
        if state.follow and selected is not None:
            state.offset = scroll_into_view(state.offset, selected, visible, len(lines))
            visible, show_above, show_below = content_layout(len(lines), raw_visible, state.offset)
            state.follow = False
        offset = state.offset
        content_top = top + (1 if show_above else 0)
        stdscr.erase()
        _draw_logo(stdscr, w, styles) if h >= 18 else _draw_compact_logo(stdscr, w, styles)
        page = lines[offset:offset + visible]
        for y, (i, ln) in enumerate(enumerate(page, start=offset), start=content_top):
            attr = styles.get(ln.style, curses.A_NORMAL)
            if i == selected:
                # A purple bar and a subtle highlight, not inverted colours.
                stdscr.addnstr(y, 0, "▌", 1, styles["bar"])
                stdscr.addnstr(y, 1, ln.text.ljust(w - 2), w - 2,
                               styles["select"] | (attr & curses.A_BOLD))
                continue
            stdscr.addnstr(y, 1, ln.text, w - 2, attr)
        if show_above:
            text = "↑ more"
            stdscr.addnstr(top, max(0, w - 1 - len(text)), text, w - 1, styles["dim"])
        if show_below:
            text = "↓ more"
            stdscr.addnstr(content_top + visible, max(0, w - 1 - len(text)), text, w - 1, styles["dim"])
        for y, ln in enumerate(bottom, start=h - len(bottom)):
            stdscr.addnstr(y, 1, ln.text, w - 2, styles.get(ln.style, curses.A_NORMAL))
        stdscr.refresh()

        key = stdscr.getch()
        if key == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bstate = curses.getmouse()
            except curses.error:
                bstate = 0
            if bstate & curses.BUTTON4_PRESSED:
                state.offset = clamp_scroll(offset - 3, len(lines), visible)
            elif bstate & BUTTON5_PRESSED:
                state.offset = clamp_scroll(offset + 3, len(lines), visible)
            elif bstate & curses.BUTTON1_CLICKED and not state.help:
                clicked = my - content_top + offset
                if clicked in (rows := selectable(lines)):
                    _select(state, lines, rows, rows.index(clicked))
                    state.follow = True
            continue
        action = handle_key(state, key, lines, visible, sidebar)
        if action == "refresh":
            stale = True
        elif action == "quit":
            if not own_root:
                return
            now = time.time()
            go, armed, notice = quit_request(armed, now)
            notice_until = now + CLOSE_CONFIRM_SECONDS
            if go:
                agents.quit_later(own_root)
                return
        elif action == "hide":
            if sidebar and os.environ.get("TMUX_PANE"):
                tmux.toggle_sidebar(os.environ["TMUX_PANE"])
        elif action == "attach" and selected is not None:
            _attach(lines[selected].agent, lines[selected].workspace, db)
            stdscr.clear()
            stale = True
        elif action == "peek" and selected is not None:
            _peek(stdscr, lines[selected].agent, styles)
            stale = True
        elif action == "close" and selected is not None:
            agent = lines[selected].agent
            now = time.time()
            if own_root and agent["id"] == own_root:
                # This sidebar's own session: closing its chat is quitting copse.
                go, armed, notice = quit_request(armed, now, key="x")
                notice_until = now + CLOSE_CONFIRM_SECONDS
                if go:
                    agents.quit_later(own_root)
                    return
                continue
            close_now, armed, notice = close_request(agent, armed, now)
            notice_until = now + CLOSE_CONFIRM_SECONDS
            if close_now:
                try:
                    agents.close(db, agent["id"])
                except agents.AgentError as e:
                    notice = str(e)
                stale = True


def run(repo_root: str | None, sidebar: bool = False) -> None:
    # Esc clears the filter; don't make it wait curses' default second to
    # tell a lone Esc from the start of an arrow key's escape sequence.
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(_loop, repo_root, sidebar)
