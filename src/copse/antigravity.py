"""Google Antigravity's terminal agent, ``agy``, as a copse provider.

agy has no flags for settings, MCP servers or a system prompt; it reads them
from the workspace's ``.agents/`` folder. copse adds three files there, each
kept out of git through ``.git/info/exclude`` and each the same for every
agent (several agents can share a checkout):

- ``mcp_config.json``: copse's MCP server, marked eager so its tools reach
  the model. The server inherits the agent's environment, so it knows which
  agent is calling.
- ``hooks.json``: lifecycle hooks that call ``copse _hook agy-<event>``. agy
  doesn't pass its environment to hooks, so the hook reads the agent's id
  from the agy process that ran it.
- ``rules/copse.md``: how to call copse's tools through agy's generic
  ``call_mcp_tool``.

agy connects MCP servers only once a conversation has started: in its first
turn, and in any turn a Stop hook continues, the agent has no MCP tools at
all. So its first message is a short warm-up carrying its instructions (its
profile, as for Codex, since agy has no system-prompt flag), and everything
copse sends it after that is typed in as a new turn once it's idle.

agy's hooks, mapped onto copse's:
- ``PreInvocation`` before each model call: working. ``invocationNum`` 0 means
  a new turn, typed by the user unless copse just delivered a message.
- ``Stop``: a queued message is typed in as a new turn; otherwise copse's Stop
  hook (the report reminder, autopilot), where ``{"decision": "continue",
  "reason": ...}`` keeps it going.
- ``PostToolUse``: a tool ran, so a permission prompt was answered.
agy has no hook for "waiting for approval": that is read from the screen.
Hooks can't approve shell commands in agy, so permissions follow the
person's own agy settings (``permissions.allow``).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

BUNDLED = os.path.expanduser("~/.local/bin/agy")
AGENTS_DIR = Path(".agents")
# Seconds after copse types a message into an idle agent during which a new
# turn is taken to be that message rather than the user.
DELIVERY_WINDOW = 15.0
EVENTS = {
    "PreInvocation": "agy-pre-invocation",
    "Stop": "agy-stop",
    "PostToolUse": "agy-post-tool",
}
TOOLS_NOTE = """\
When you run under copse, its tools (assign, handoff, report_result,
send_message, workspace_diff, merge_workspace, get_progress and the rest) are
on the MCP server named `copse`. They appear as tools named mcp_copse_...,
and until they do, call them with call_mcp_tool, for example
call_mcp_tool(ServerName="copse", ToolName="get_progress", Arguments={}).
Whenever your instructions say to call one of these tools by name, that's how."""
RULE = f"---\ntrigger: always_on\n---\n{TOOLS_NOTE}\n"


WARMUP_END = "Reply with just the word: ready. Your task, if any, comes in the next message."


def warmup(instructions: str | None) -> str:
    return "\n\n".join(p for p in (TOOLS_NOTE, instructions, WARMUP_END) if p)


class AntigravityError(RuntimeError):
    pass


def binary() -> str:
    """COPSE_AGY_BIN, else `agy` on PATH, else where Google's installer puts it."""
    explicit = os.environ.get("COPSE_AGY_BIN")
    if explicit:
        return explicit
    return shutil.which("agy") or (BUNDLED if os.path.exists(BUNDLED) else "agy")


def tool_names() -> list[str]:
    import asyncio

    from copse.mcp_server import mcp

    return [t.name for t in asyncio.run(mcp.list_tools())]


def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _exclude(workspace: str, rel: Path) -> None:
    common = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], workspace)
    if common.returncode != 0:
        return
    pattern = "/" + rel.as_posix()
    exclude = Path(common.stdout.strip()) / "info" / "exclude"
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if pattern in existing.splitlines():
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    sep = "" if not existing or existing.endswith("\n") else "\n"
    exclude.write_text(f"{existing}{sep}{pattern}\n", encoding="utf-8")


def _merge_json(workspace: str, rel: Path, section: str, key: str, value: object) -> None:
    """Set ``data[section][key] = value`` (or ``data[key]`` with no section) in
    a JSON file under the workspace, keeping everything else. Refuses to
    change a file committed to git."""
    path = Path(workspace) / rel
    where = f"under {section}" if section else "at the top level"
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise AntigravityError(
                f"{path} isn't plain JSON, so copse can't add to it. Add this {where} "
                f"yourself: \"{key}\": {json.dumps(value)}"
            ) from e
    target = data.setdefault(section, {}) if section else data
    if target.get(key) == value:
        return
    if _git(["ls-files", "--error-unmatch", str(rel)], workspace).returncode == 0:
        raise AntigravityError(
            f"{rel} is committed in this repo, and copse won't change a tracked file. "
            f"Add this {where}: \"{key}\": {json.dumps(value)}"
        )
    created = not path.exists()
    target[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if created:
        _exclude(workspace, rel)


def _hook_command(event: str) -> str:
    from copse.providers import copse_invocation

    # Where copse keeps its state is the same for every agent; which agent is
    # calling comes from the agy process (see agent_from_parent).
    env = {k: os.environ[k] for k in ("COPSE_HOME", "COPSE_TMUX_SOCKET") if k in os.environ}
    assigns = "".join(f"{k}={shlex.quote(v)} " for k, v in sorted(env.items()))
    return assigns + " ".join(shlex.quote(a) for a in [*copse_invocation(), "_hook", event])


def install(workspace: str) -> None:
    """Give the checkout copse's MCP server, hooks and rule for agy."""
    from copse.providers import copse_invocation

    cmd = copse_invocation()
    server = {"command": cmd[0], "args": [*cmd[1:], "mcp"],
              # Eager: the tools reach the model up front. Otherwise agy holds
              # them back, and the agent doesn't know copse's tools exist.
              "tools": {name: {"eager": True} for name in tool_names()}}
    _merge_json(workspace, AGENTS_DIR / "mcp_config.json", "mcpServers", "copse", server)
    handler = lambda ev: [{"type": "command", "command": _hook_command(EVENTS[ev]), "timeout": 60}]  # noqa: E731
    hooks = {"PreInvocation": handler("PreInvocation"), "Stop": handler("Stop"),
             "PostToolUse": [{"matcher": "*", "hooks": handler("PostToolUse")}]}
    _merge_json(workspace, AGENTS_DIR / "hooks.json", "", "copse", hooks)
    rule = Path(workspace) / AGENTS_DIR / "rules" / "copse.md"
    if not rule.exists() or rule.read_text(encoding="utf-8") != RULE:
        created = not rule.exists()
        rule.parent.mkdir(parents=True, exist_ok=True)
        rule.write_text(RULE, encoding="utf-8")
        if created:
            _exclude(workspace, AGENTS_DIR / "rules" / "copse.md")


def can_resume(conversation_id: str) -> bool:
    return (Path.home() / ".gemini" / "antigravity-cli" / "brain" / conversation_id).is_dir()


# -- which agent is calling ---------------------------------------------------------


def _process_env(pid: int) -> dict[str, str]:
    """copse's variables in another process's environment, as far as the OS shows it."""
    proc = Path(f"/proc/{pid}/environ")
    if proc.exists():
        try:
            raw = proc.read_bytes().split(b"\0")
        except OSError:
            return {}
        return dict(p.decode(errors="replace").split("=", 1) for p in raw if b"=" in p)
    out = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(pid)],
                         capture_output=True, text=True).stdout
    return dict(re.findall(r"(?:^|\s)(COPSE_[A-Z_]+)=(\S+)", out))


def _parent(pid: int) -> int | None:
    out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout
    return int(out.strip()) if out.strip().isdigit() else None


def agent_from_parent(max_depth: int = 4) -> str | None:
    """COPSE_AGENT_ID from the nearest ancestor that has it: the agy process
    that ran this hook."""
    pid: int | None = os.getppid()
    for _ in range(max_depth):
        if not pid or pid <= 1:
            return None
        agent_id = _process_env(pid).get("COPSE_AGENT_ID")
        if agent_id:
            return agent_id
        pid = _parent(pid)
    return None


# -- hooks ---------------------------------------------------------------------------

LIMIT_ERROR = re.compile(r"429|quota|rate.?limit|resource.?exhausted", re.I)


def handle_hook(db, agent_id: str, event: str, payload: dict) -> dict | None:
    """Translate one agy hook into copse's own (``agents.handle_hook``) and
    its answer back into agy's format."""
    from copse import agents, autopilot

    agent = db.get_agent(agent_id)
    if agent is None:
        return None
    base = {"session_id": payload["conversationId"]} if payload.get("conversationId") else {}
    if base and base["session_id"] != agent.session_ref:
        db.update_agent(agent_id, session_ref=base["session_id"])

    if event == "agy-pre-invocation":
        db.set_status(agent_id, "processing")
        if payload.get("invocationNum", 0) == 0:
            # A new turn: the user's, unless copse just typed a message in.
            db.update_agent(agent_id, stop_blocked=0)
            if agent.mode == "interactive" and not db.delivered_since(agent_id, time.time() - DELIVERY_WINDOW):
                autopilot.user_spoke(db, agent)
        return None
    if event == "agy-post-tool":
        agents.handle_hook(db, agent_id, "tool-done", base)
        return None
    if event == "agy-stop":
        error = str(payload.get("error") or "")
        if error:
            limited = bool(LIMIT_ERROR.search(error))
            if limited:
                from copse import quota
                from copse.config import RepoConfig, load_repo_config

                ws = db.get_workspace(agent.workspace_id)
                quota.record_limit("antigravity", load_repo_config(ws.repo_root) if ws else RepoConfig())
            agents.handle_hook(db, agent_id, "stop-failure",
                               {**base, "error_type": "rate_limit" if limited else "error"})
            return None
        if db.pending_count(agent_id):
            # A new turn, not a continuation: only then does agy give the
            # agent its MCP tools.
            db.update_agent(agent_id, stop_blocked=0)
            db.set_status(agent_id, "idle")
            _flush_soon(agent_id)
            return None
        out = agents.handle_hook(db, agent_id, "stop",
                                 {**base, "stop_hook_active": bool(agent.stop_blocked)})
        if out and out.get("decision") == "block":
            db.update_agent(agent_id, stop_blocked=1)
            return {"decision": "continue", "reason": out["reason"]}
        db.update_agent(agent_id, stop_blocked=0)
        return None
    return None


def _flush_soon(agent_id: str, delay: float = 1.5) -> None:
    """Type the agent's next queued message once agy has finished stopping."""
    from copse.providers import copse_invocation

    subprocess.Popen([*copse_invocation(), "_flush", agent_id, "--delay", str(delay)],
                     start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def hook_main(db, event: str, stdin_text: str) -> str:
    """``copse _hook agy-<event>``: always answers with a JSON object."""
    agent_id = os.environ.get("COPSE_AGENT_ID") or agent_from_parent()
    if not agent_id:
        return "{}"  # agy running outside copse, in a checkout copse set up
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    try:
        out = handle_hook(db, agent_id, event, payload)
    except Exception as e:  # never break the person's agy session over copse
        print(f"copse hook {event}: {e}", file=sys.stderr)
        out = None
    return json.dumps(out or {})
