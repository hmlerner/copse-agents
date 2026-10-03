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
- ``PreToolUse`` (only with ``permission_policy: "on"``): copse's permission
  policy (copse.permissions) answers deny, or what agy would do anyway (allow
  for a file inside the workspace, else ask), never nothing.
agy has no hook for "waiting for approval": that is read from the screen.
Hooks can't approve anything in agy (a hook's "allow" is ignored), so with the
policy on copse mirrors the allow rules agy's syntax can express into the
person's agy settings (``permissions.allow``; see sync_permissions), touching
only the entries it added itself.
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
    "PreToolUse": "agy-pre-tool",
}
# The tools copse's policy has an opinion on (copse.permissions.from_agy);
# others never reach its hook. The hook must answer something (nothing is a
# deny), and agy's "ask" prompts unless an Always Allow rule covers the call,
# so for a file inside the workspace (which agy reads and writes without
# asking) it answers "allow": no wider than agy's own default. See
# copse.permissions.agy_output.
PRE_TOOL_MATCHER = ("run_command|view_file|view_file_outline|list_dir|grep_search|find_by_name|"
                    "codebase_search|read_file|write_to_file|create_file|write_file|delete_file|"
                    "replace_file_content|multi_replace_file_content|edit_file|read_url_content|"
                    "search_web|call_mcp_tool|mcp_.*")
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


def install(workspace: str, permission_policy: bool = False) -> None:
    """Give the checkout copse's MCP server, hooks and rule for agy (and,
    with ``permission_policy``, the PreToolUse hook that applies it)."""
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
    if permission_policy:
        hooks["PreToolUse"] = [{"matcher": PRE_TOOL_MATCHER, "hooks": handler("PreToolUse")}]
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


def pre_tool_decision(db, agent_id: str | None, payload: object) -> dict:
    """agy's PreToolUse: copse's permission policy as deny or ask."""
    from copse import permissions
    from copse.agents import _record_permission
    from copse.config import load_repo_config

    agent = db.get_agent(agent_id) if agent_id else None
    ws = db.get_workspace(agent.workspace_id) if agent else None
    if ws is None or not isinstance(payload, dict):
        return permissions.agy_output(None)
    cfg = load_repo_config(ws.repo_root)
    if cfg.permission_policy != "on":
        return permissions.agy_output(None)
    req = permissions.from_agy(payload, worktree=ws.path, repo_root=ws.repo_root)
    decision = permissions.decide(req, checks=cfg.checks)
    if req is not None and decision.decision == "deny":
        # Only denies are worth a history row here: agy runs this for every
        # tool call, and copse's allow is only advisory (agy decides).
        _record_permission(db, agent, ws, req.summary(), f"deny: {decision.reason}")
    spaces = payload.get("workspacePaths")
    spaces = [s for s in spaces if isinstance(s, str) and s] if isinstance(spaces, list) else []
    return permissions.agy_output(decision, req, spaces)


PRE_TOOL_FALLBACK = json.dumps({"decision": "ask", "reason": "copse: no decision"})


def pre_tool_main(stdin_text: str, db_factory=None) -> str:
    """``copse _hook agy-pre-tool``: always a JSON answer (deny, ask, or allow
    for a file in the workspace); any failure (no agent, bad input, a broken
    DB) is ask, since agy reads no answer as deny."""
    try:
        try:
            payload = json.loads(stdin_text) if stdin_text.strip() else {}
        except ValueError:
            payload = None  # unreadable: ask
        agent_id = os.environ.get("COPSE_AGENT_ID") or agent_from_parent()
        if db_factory is None:
            from copse.db import DB as db_factory
        out = pre_tool_decision(db_factory(), agent_id, payload)
        if isinstance(out, dict) and out.get("decision") in ("deny", "ask", "allow"):
            return json.dumps(out)
    except Exception as e:  # noqa: BLE001
        try:
            print(f"copse hook agy-pre-tool: {e}", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass
    return PRE_TOOL_FALLBACK


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


# -- mirroring copse's permission rules into agy's settings ---------------------------


def settings_path() -> Path:
    return Path.home() / ".gemini" / "antigravity-cli" / "settings.json"


class SettingsError(RuntimeError):
    pass


def _indent_of(text: str) -> str | int:
    m = re.search(r"^([ \t]+)\S", text, re.M)
    return m.group(1) if m else 2


def _apply(store, desired: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    """Make agy's settings carry exactly ``desired`` among the entries copse
    owns (``store.agy_managed``), leaving every other entry and key alone.
    Returns (added, removed)."""
    path = settings_path()
    managed = {k: list(store.agy_managed.get(k, [])) for k in ("allow", "deny")}
    created = list(store.agy_created)
    if path.exists():
        text = path.read_text(encoding="utf-8")
        try:
            data = json.loads(text)
        except ValueError as e:
            raise SettingsError(f"{path} isn't valid JSON; copse left it alone") from e
        if not isinstance(data, dict):
            raise SettingsError(f"{path} isn't a JSON object; copse left it alone")
    elif not any(desired.values()):
        store.agy_managed, store.agy_created = {}, []
        return [], []
    else:
        text, data = None, {}
        created.append("file")
    new = json.loads(json.dumps(data))
    perms = new.get("permissions")
    if perms is not None and not isinstance(perms, dict):
        raise SettingsError(f"{path}: \"permissions\" isn't an object; copse left it alone")
    added: list[str] = []
    removed: list[str] = []
    for key in ("allow", "deny"):
        want, mine = desired.get(key, []), managed[key]
        if perms is None:
            if not want:
                managed[key] = []
                continue
            perms = new["permissions"] = {}
            created.append("permissions")
        current = perms.get(key)
        if current is not None and not isinstance(current, list):
            raise SettingsError(f"{path}: \"permissions.{key}\" isn't a list; copse left it alone")
        if current is None:
            if not want:
                managed[key] = []
                continue
            current = perms[key] = []
            created.append(f"permissions.{key}")
        gone = [e for e in current if e in mine and e not in want]
        kept = [e for e in current if not (e in mine and e not in want)]
        new_entries = [w for w in want if w not in kept]
        perms[key] = kept + new_entries
        removed += gone
        added += new_entries
        managed[key] = [w for w in want if w in mine or w in new_entries]
    # Take away what copse created and has emptied again.
    if isinstance(perms, dict):
        for key in ("allow", "deny"):
            if f"permissions.{key}" in created and perms.get(key) == []:
                del perms[key]
                created.remove(f"permissions.{key}")
        if "permissions" in created and perms == {}:
            del new["permissions"]
            created.remove("permissions")
    store.agy_managed = {k: v for k, v in managed.items() if v}
    store.agy_created = list(dict.fromkeys(created))
    if new == data and text is not None:
        return added, removed
    if "file" in store.agy_created and new == {}:
        path.unlink(missing_ok=True)
        store.agy_created.remove("file")
        return added, removed
    backup = path.with_name(path.name + ".copse-backup")
    if text is not None and not backup.exists():
        shutil.copy2(path, backup)
    out = json.dumps(new, indent=_indent_of(text or ""), ensure_ascii=False)
    if text is None or text.endswith("\n"):
        out += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.copse-{os.getpid()}.tmp")
    tmp.write_text(out, encoding="utf-8")
    if text is not None:
        os.chmod(tmp, path.stat().st_mode & 0o7777)
    os.replace(tmp, path)
    return added, removed


def sync_permissions(repo_root: str | None = None, on: bool | None = None,
                     checks: list[str] | None = None) -> tuple[list[str], list[str]]:
    """Mirror copse's rules into agy's settings.json. With ``repo_root``,
    first record whether that repo's policy is ``on`` (and its ``checks``);
    the mirror covers every repo recorded as on, and once none is, every
    entry copse added is taken out again. Returns (added, removed)."""
    from copse import permissions

    with permissions.editing() as store:
        if repo_root is not None and on is not None:
            if on:
                store.agy_repos[repo_root] = list(checks or [])
            else:
                store.agy_repos.pop(repo_root, None)
        if store.agy_repos:
            all_checks = list(dict.fromkeys(c for cs in store.agy_repos.values() for c in cs))
            desired = permissions.mirror_agy([*permissions.DEFAULT_RULES, *store.rules], all_checks)
        else:
            desired = {"allow": [], "deny": []}
        return _apply(store, desired)


def resync_permissions() -> None:
    """After the person changes copse's rules: bring agy's copy up to date,
    if copse keeps one. Never raises."""
    from copse import permissions

    try:
        store = permissions.load_store()
        if store.agy_repos or store.agy_managed:
            sync_permissions()
    except Exception as e:  # noqa: BLE001
        print(f"copse: couldn't update agy's settings: {e}", file=sys.stderr)


def policy_on(workspace: str) -> list[str] | None:
    """The repo's ``checks`` if its permission policy is on, else None."""
    from copse.config import load_repo_config

    try:
        cfg = load_repo_config(workspace)
    except Exception:  # noqa: BLE001
        return None
    return list(cfg.checks or []) if cfg.permission_policy == "on" else None


def sync_for_launch(workspace: str, checks: list[str] | None) -> None:
    """An agy worker is starting in ``workspace``: bring agy's settings in
    line with the repo's policy (``checks`` is None when it's off). Never
    raises; nothing is written when the policy is off and copse never added
    anything."""
    from copse import git, permissions

    try:
        try:
            root = git.main_repo_root(workspace)
        except git.GitError:
            root = workspace
        store = permissions.load_store()
        if checks is None and root not in store.agy_repos and not (store.agy_managed and not store.agy_repos):
            return
        sync_permissions(root, on=checks is not None, checks=checks)
    except Exception as e:  # noqa: BLE001
        print(f"copse: couldn't update agy's permission settings: {e}", file=sys.stderr)
