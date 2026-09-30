"""How close each provider is to its subscription limit.

Everything here comes from files and hook data the CLIs write locally: the
status line data Claude Code hands copse, Codex's session rollouts, and the
limit errors Antigravity reports. copse never reads a CLI's OAuth token or
auth files and never calls a provider's backend.

State lives in ``~/.copse/quota.json``, one entry per provider.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from copse.config import RepoConfig, copse_home

CLAUDE_FRESH_SECONDS = 15 * 60   # the status line only reports while a session runs
CODEX_REFRESH_SECONDS = 30       # how often get() re-reads the newest rollout
ROLLOUT_TAIL_BYTES = 1024 * 1024
DEFAULT_COOLDOWN_MINUTES = {"antigravity": 300}
FALLBACK_COOLDOWN_MINUTES = 300
NAMES = {"claude": "Claude", "codex": "Codex", "antigravity": "Antigravity", "native": "Local model"}
PROVIDERS = tuple(NAMES)
# Claude Code's status line names its windows; the rest are told by length.
CLAUDE_WINDOWS = {"five_hour": 300, "seven_day": 10080}


@dataclass
class Window:
    used: float                # percent of the window's allowance
    resets_at: float | None    # epoch seconds
    minutes: int | None = None


@dataclass
class Quota:
    provider: str
    windows: list[Window] = field(default_factory=list)
    limited_until: float | None = None
    updated_at: float = 0.0
    source: str = ""


def path() -> Path:
    return copse_home() / "quota.json"


def _load() -> dict:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(p)


def _number(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def record(provider: str, windows: list[Window] | None = None, *,
           limited_until: float | None = None, source: str = "", keep_limit: bool = False) -> None:
    """Replace what's known about ``provider``. ``windows=None`` keeps the
    windows it has (for recording only a limit); ``keep_limit`` keeps a
    ``limited_until`` still in the future."""
    data = _load()
    old = data.get(provider) if isinstance(data.get(provider), dict) else {}
    entry = {
        "windows": old.get("windows", []) if windows is None else
                   [{"used": w.used, "resets_at": w.resets_at, "minutes": w.minutes} for w in windows],
        "limited_until": limited_until,
        "updated_at": time.time(),
        "source": source or old.get("source", ""),
    }
    if limited_until is None and keep_limit and (_number(old.get("limited_until")) or 0) > time.time():
        entry["limited_until"] = old["limited_until"]
    data[provider] = entry
    _save(data)


def record_limit(provider: str, cfg: RepoConfig | None = None, *, now: float | None = None) -> float:
    """The provider just refused work on its limit: it's unavailable for the
    repo's ``limit_cooldown_minutes`` (default 300 for Antigravity)."""
    minutes = getattr(cfg, "limit_cooldown_minutes", None) or DEFAULT_COOLDOWN_MINUTES.get(
        provider, FALLBACK_COOLDOWN_MINUTES)
    until = (now if now is not None else time.time()) + minutes * 60
    record(provider, None, limited_until=until, source="limit error")
    return until


def _from_entry(provider: str, e: dict) -> Quota:
    windows = [Window(float(w["used"]), _number(w.get("resets_at")), w.get("minutes"))
               for w in e.get("windows", []) if isinstance(w, dict) and _number(w.get("used")) is not None]
    return Quota(provider, windows, _number(e.get("limited_until")),
                 _number(e.get("updated_at")) or 0.0, str(e.get("source") or ""))


def get(provider: str) -> Quota | None:
    """What's known about ``provider`` now: windows past their reset dropped,
    an expired limit cleared. None when nothing current is known."""
    if provider == "codex":
        _refresh_codex_lazily()
    entry = _load().get(provider)
    if not isinstance(entry, dict):
        return None
    q = _from_entry(provider, entry)
    now = time.time()
    live = []
    for w in q.windows:
        if w.resets_at:  # 0 or missing: unknown, so only its age counts
            if w.resets_at <= now:
                continue
        elif now - q.updated_at > CLAUDE_FRESH_SECONDS:
            continue
        if provider == "claude" and now - q.updated_at > CLAUDE_FRESH_SECONDS:
            continue
        live.append(w)
    q.windows = live
    if q.limited_until is not None and q.limited_until <= now:
        q.limited_until = None
    return q if q.windows or q.limited_until else None


def _native_down(repo_root: str | None) -> bool:
    from copse.native import serve

    return any(not serve.reachable(s) for s in serve.local_servers(repo_root))


def headroom(provider: str, cfg: RepoConfig | None = None, repo_root: str | None = None) -> float:
    """Percent of the provider's allowance left: 100 minus its fullest window,
    0 while it's limited. 100 when nothing is known."""
    if provider == "native":
        return 0.0 if _native_down(repo_root) else 100.0
    q = get(provider)
    if q is None:
        return 100.0
    if q.limited_until and q.limited_until > time.time():
        return 0.0
    if not q.windows:
        return 100.0
    return max(0.0, 100.0 - max(w.used for w in q.windows))


def window_name(minutes: int | None) -> str:
    return {300: "5-hour", 10080: "weekly", 43200: "monthly"}.get(minutes or 0, f"{minutes}-minute" if minutes else "usage")


def fullest(q: Quota) -> Window | None:
    return max(q.windows, key=lambda w: w.used, default=None)


def _when(ts: float) -> str:
    t = time.localtime(ts)
    clock = time.strftime("%-I:%M%p", t).lower()
    return clock if time.strftime("%F", t) == time.strftime("%F") else f"{time.strftime('%a', t)} {clock}"


def note(provider: str, repo_root: str | None = None) -> str | None:
    """A one-line status such as "Codex at 82% of its weekly limit, resets
    Thu 9:00am", or None when there's nothing to say."""
    name = NAMES.get(provider, provider)
    if provider == "native":
        return "local model server not answering" if _native_down(repo_root) else None
    q = get(provider)
    if q is None:
        return None
    if q.limited_until:
        return f"{name} limit reached, available again {_when(q.limited_until)}"
    w = fullest(q)
    if w is None:
        return None
    resets = f", resets {_when(w.resets_at)}" if w.resets_at else ""
    return f"{name} at {w.used:.0f}% of its {window_name(w.minutes)} limit{resets}"


def notes(repo_root: str | None = None, *, native: bool = True) -> list[str]:
    """``note`` for every provider with data. ``native=False`` skips the
    local model server probe (for callers that must not block)."""
    return [n for p in PROVIDERS if native or p != "native" if (n := note(p, repo_root))]


# -- Claude: the status line data -------------------------------------------------------


def record_claude(status: dict) -> bool:
    """Keep the plan usage Claude Code gives its status line (Claude.ai
    subscriptions only). True when it carried any."""
    limits = status.get("rate_limits")
    if not isinstance(limits, dict):
        return False
    windows = []
    for name, minutes in CLAUDE_WINDOWS.items():
        w = limits.get(name)
        if isinstance(w, dict) and _number(w.get("used_percentage")) is not None:
            windows.append(Window(float(w["used_percentage"]), _number(w.get("resets_at")), minutes))
    if not windows:
        return False
    record("claude", windows, source="status line", keep_limit=True)
    return True


# -- Codex: rate limits in the session rollouts ------------------------------------------


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def newest_rollout(home: Path | None = None) -> Path | None:
    """The most recently written ``sessions/**/rollout-*.jsonl``. Only
    that directory: nothing else under ~/.codex (auth.json lives there) is opened."""
    sessions = (home or codex_home()) / "sessions"
    best, best_time = None, -1.0
    for f in sessions.glob("**/rollout-*.jsonl"):
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if mtime > best_time:
            best, best_time = f, mtime
    return best


def _windows_from_limits(limits: dict) -> list[Window]:
    windows = []
    for slot in ("primary", "secondary"):
        w = limits.get(slot)
        if not isinstance(w, dict) or _number(w.get("used_percent")) is None:
            continue
        minutes = w.get("window_minutes")
        windows.append(Window(float(w["used_percent"]), _number(w.get("resets_at")),
                              int(minutes) if isinstance(minutes, (int, float)) else None))
    return windows


def last_codex_limits(rollout: Path) -> list[Window]:
    """The windows of the last event in ``rollout`` carrying ``rate_limits``,
    reading from the end."""
    try:
        size = rollout.stat().st_size
        with rollout.open("rb") as f:
            f.seek(max(0, size - ROLLOUT_TAIL_BYTES))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    for line in reversed(tail.splitlines()):
        if '"rate_limits"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        payload = event.get("payload") if isinstance(event, dict) else None
        for holder in (payload, event):
            limits = holder.get("rate_limits") if isinstance(holder, dict) else None
            if isinstance(limits, dict):
                windows = _windows_from_limits(limits)
                if windows:
                    return windows
    return []


def refresh_codex(home: Path | None = None) -> bool:
    """Record Codex's limits from its newest rollout. True when it had any."""
    rollout = newest_rollout(home)
    windows = last_codex_limits(rollout) if rollout else []
    if not windows:
        return False
    record("codex", windows, source="codex rollout", keep_limit=True)
    return True


def _refresh_codex_lazily() -> None:
    entry = _load().get("codex")
    if isinstance(entry, dict) and time.time() - (_number(entry.get("updated_at")) or 0) < CODEX_REFRESH_SECONDS:
        return
    try:
        refresh_codex()
    except OSError:
        pass
