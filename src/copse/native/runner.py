"""The loop in a native worker's pane: ``copse _native <agent>``.

Like the headless Claude runner (agents.run_headless), it stays up between
turns: the first prompt and every message sent while no turn runs wait in
the agent's inbox, and the runner takes the oldest and runs a turn on it.
Unlike that runner, there's no CLI in between: the turn is
``NativeAgent.run``, which reports the agent's status itself, drains the
inbox between model calls, and calls copse's tools (report_result,
send_message, submit_review, workspace_diff) as plain functions.

A worker that ends a turn without reporting is reminded once, as the Stop
hook reminds a Claude worker; after that its supervisor is told. If the
endpoint fails for good the runner exits non-zero and the pane stays, with
the error in it, which is how copse notices any agent died.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from copse import agents, codemap, git, workspaces
from copse.config import copse_home
from copse.db import DB, Workspace
from copse.native.client import Client, ClientError, Endpoint
from copse.native.loop import LoopConfig, NativeAgent
from copse.native.permissions import Permissions
from copse.native.tools import Tool, Toolbox, ToolResult, core_tools
from copse.profiles import Profile
from copse.providers import DELIVERY_NOTE

DEFAULT_CONTEXT_TOKENS = 32_000

NATIVE_NOTE = (
    "You work in a terminal harness run by copse. Use the Read, Glob and Grep tools to look "
    "at code, Edit to change it (exact-match replacements; include enough surrounding lines "
    "to be unique), Write only for new files, and Bash for commands and tests, one at a time. "
    "Tool results say when something failed; read them and adapt. Keep replies brief: the "
    "work is in the tool calls, not in prose."
)

REMINDERS = {
    "review": ("You haven't called the `submit_review` tool yet. Call it now with your "
               "verdict and findings."),
    "work": ("You haven't called the `report_result` tool yet. If your task is finished, "
             "commit your work and call it now. If you are blocked, call it with a "
             "description of what's blocking you."),
}


def endpoint_for(profile: Profile) -> Endpoint:
    """The profile's endpoint. Raises ValueError when it has none."""
    if not profile.base_url:
        raise ValueError(f"profile {profile.name!r} uses the native provider but gives no base_url")
    if not profile.model:
        raise ValueError(f"profile {profile.name!r} uses the native provider but gives no model")
    api = (profile.api or "openai").lower()
    if api not in ("openai", "anthropic"):
        raise ValueError(f"profile {profile.name!r}: api must be openai or anthropic, not {api!r}")
    key = os.environ.get(profile.api_key_env) if profile.api_key_env else None
    if profile.api_key_env and not key:
        raise ValueError(f"profile {profile.name!r} needs the {profile.api_key_env} environment variable")
    return Endpoint(profile.base_url, profile.model, api=api, api_key=key)


def probe(endpoint: Endpoint, timeout: float = 3.0) -> tuple[bool, str]:
    """Whether the endpoint answers, and a line about it: the models it
    lists (and whether ``endpoint.model`` is among them) when it lists any.
    Ollama, LM Studio, llama.cpp, vLLM and OpenRouter all answer
    ``GET <base>/models`` (or ``/v1/models``); an endpoint that answers with
    any HTTP status is at least reachable."""
    import json
    import urllib.error
    import urllib.request

    base = endpoint.base_url.rstrip("/")
    url = base + ("/models" if base.endswith("/v1") else "/v1/models")
    headers = {"Accept": "application/json"}
    if endpoint.api_key:
        headers["Authorization"] = f"Bearer {endpoint.api_key}"
        headers["x-api-key"] = endpoint.api_key
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return True, f"reachable (HTTP {e.code} from {url}; couldn't list models)"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, f"not reachable: {getattr(e, 'reason', e)}"
    try:
        data = json.loads(body)
        items = data.get("data") if isinstance(data, dict) else data
        names = [str(m.get("id") or m.get("name") or "") for m in items if isinstance(m, dict)]
    except (ValueError, AttributeError, TypeError):
        return True, "reachable (couldn't read its model list)"
    if not names:
        return True, "reachable, but lists no models"
    if endpoint.model in names or any(n.split(":")[0] == endpoint.model for n in names):
        return True, f"reachable; {endpoint.model} is available"
    shown = ", ".join(names[:6]) + (", ..." if len(names) > 6 else "")
    return True, f"reachable, but {endpoint.model} isn't listed (it has: {shown})"


def is_ollama(endpoint: Endpoint, timeout: float = 3.0) -> bool:
    """Whether the endpoint is an Ollama server: its host answers
    ``GET /api/version``, or the URL uses Ollama's default port."""
    import json
    import urllib.request

    host = _host(endpoint)
    if host.endswith(":11434"):
        return True
    try:
        with urllib.request.urlopen(host + "/api/version", timeout=timeout) as resp:
            return "version" in json.loads(resp.read().decode("utf-8", "replace"))
    except (OSError, ValueError, TypeError):
        return False


def _host(endpoint: Endpoint) -> str:
    base = endpoint.base_url.rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def server_context(endpoint: Endpoint, timeout: float = 3.0) -> int | None:
    """The context length an Ollama server runs the model with: the
    ``num_ctx`` parameter of ``POST /api/show``. None for other endpoints, on
    any error, and when the model sets no ``num_ctx`` (Ollama then uses
    OLLAMA_CONTEXT_LENGTH or its default, which the API doesn't report)."""
    import json
    import re
    import urllib.request

    if not is_ollama(endpoint, timeout):
        return None
    req = urllib.request.Request(_host(endpoint) + "/api/show",
                                 data=json.dumps({"model": endpoint.model}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            params = json.loads(resp.read().decode("utf-8", "replace")).get("parameters")
    except (OSError, ValueError, AttributeError):
        return None
    m = re.search(r"^\s*num_ctx\s+(\d+)", params, re.M) if isinstance(params, str) else None
    return int(m.group(1)) if m else None


def copse_tools(db: DB, agent_id: str, ws: Workspace, mode: str) -> list[Tool]:
    """copse's own tools, called in-process. Named as the MCP server names
    them, so the worker footers' instructions hold."""
    obj = {"type": "object"}

    def report(args: dict) -> ToolResult:
        result = str(args.get("result") or "").strip()
        if not result:
            return ToolResult("result must say what you did", True)
        return ToolResult(agents.report_result(db, agent_id, result))

    def review(args: dict) -> ToolResult:
        approved = args.get("approved")
        if isinstance(approved, str):
            approved = approved.strip().lower() in ("true", "yes", "1")
        summary = str(args.get("summary") or "").strip()
        if not isinstance(approved, bool) or not summary:
            return ToolResult("approved (true/false) and summary are both required", True)
        return ToolResult(agents.submit_review(db, agent_id, approved, summary))

    def send(args: dict) -> ToolResult:
        to, message = str(args.get("to_agent_id") or ""), str(args.get("message") or "")
        if not to or not message:
            return ToolResult("to_agent_id and message are both required", True)
        try:
            return ToolResult(agents.send_message(db, to, message, agent_id))
        except agents.AgentError as e:
            return ToolResult(str(e), True)

    def diff(args: dict) -> ToolResult:
        ref = str(args.get("workspace") or ws.id)
        try:
            target = workspaces.resolve(db, ref, cwd=ws.path)
            base = workspaces.require_base(target)
        except Exception as e:  # unknown workspace, or one with no base
            return ToolResult(str(e), True)
        st = git.status(target.path, base)
        head = (f"workspace {target.id} on branch {target.branch}: {st.ahead} commit(s) ahead of "
                f"{base}, {st.behind} behind, {len(st.dirty_files)} uncommitted file(s)")
        if args.get("stat_only"):
            return ToolResult(f"{head}\n{git.diff(target.path, base, stat=True) or '(no changes)'}")
        return ToolResult(f"{head}\n\n{git.diff(target.path, base) or '(no changes)'}")

    tools = [
        Tool("send_message", "Send a message to another copse agent (your supervisor, say). "
             "Delivered when that agent is idle.",
             {**obj, "properties": {"to_agent_id": {"type": "string"}, "message": {"type": "string"}},
              "required": ["to_agent_id", "message"]}, send),
        Tool("workspace_diff", "Everything this branch changes relative to its base: commits "
             "plus uncommitted edits. stat_only gives the file list.",
             {**obj, "properties": {"workspace": {"type": "string", "description": "Workspace id or branch (default: yours)"},
                                    "stat_only": {"type": "boolean"}}}, diff),
    ]
    if mode == "review":
        tools.append(Tool("submit_review", "Reviewers: call this once with your verdict. approved=true "
                          "only if the branch can merge as is. summary: your findings, most severe first.",
                          {**obj, "properties": {"approved": {"type": "boolean"}, "summary": {"type": "string"}},
                           "required": ["approved", "summary"]}, review))
    else:
        tools.append(Tool("report_result", "Call this once when your task is done (after committing), "
                          "with a concise summary of what you did and anything left to check.",
                          {**obj, "properties": {"result": {"type": "string"}}, "required": ["result"]}, report))
    return tools


def _preview(text: str, lines: int = 6) -> str:
    rows = text.strip().splitlines()
    return "\n".join(rows[:lines] + (["..."] if len(rows) > lines else []))


def conversation_path(agent_id: str) -> Path:
    return copse_home() / "native" / f"{agent_id}.json"


def run_native(db: DB, agent_id: str, resume: str | None = None, *,
               poll: float = 0.5, exit_when_idle: bool = False) -> int:
    """The runner (see above). Returns non-zero when the endpoint or the
    profile is unusable, 0 once the agent is gone. ``exit_when_idle`` (for
    tests) returns as soon as there's nothing left to run."""
    agent = db.get_agent(agent_id)
    ws = db.get_workspace(agent.workspace_id) if agent else None
    if agent is None or ws is None:
        return 0
    profile = agents._profile_for(db, agent, ws)
    try:
        endpoint = endpoint_for(profile)
    except ValueError as e:
        print(f"copse: {e}", flush=True)
        db.set_status(agent_id, "idle")
        return 2

    transcript = copse_home() / "native" / f"{agent_id}.jsonl"
    saved = conversation_path(agent_id)
    db.update_agent(agent_id, transcript_path=str(transcript), session_ref=str(saved))

    toolbox = Toolbox().add(*core_tools(ws.path)).add(*copse_tools(db, agent_id, ws, agent.mode))
    permissions = Permissions(profile.permission_mode, [*(profile.allowed_tools or []), *codemap.ALLOWED_TOOLS])
    system = "\n\n".join(filter(None, [profile.prompt, NATIVE_NOTE, DELIVERY_NOTE]))
    config = LoopConfig(context_tokens=profile.context_tokens or DEFAULT_CONTEXT_TOKENS)
    # Text streamed since the last tool call: what the pane already shows.
    streamed = {"text": ""}

    def show(delta: str) -> None:
        streamed["text"] += delta
        print(delta, end="", flush=True)

    def log(line: str) -> None:
        if streamed["text"] and not streamed["text"].endswith("\n"):
            print(flush=True)
        streamed["text"] = ""
        print(f"  · {line}", flush=True)

    loop = NativeAgent(
        Client(endpoint), toolbox, permissions, system, config=config, transcript=transcript,
        on_status=lambda s: db.set_status(agent_id, s),
        inbox=lambda: _drain(db, agent_id),
        log=log, on_text=show,
    )
    if resume and loop.load(resume):
        print(f"copse: resumed the conversation ({len(loop.messages)} messages)", flush=True)
    print(f"copse: native worker {agent_id} on {endpoint.model} at {endpoint.base_url}", flush=True)

    reminded = False
    turn = 0
    while True:
        agent = db.get_agent(agent_id)
        if agent is None:
            return 0
        msg = db.pop_pending(agent_id)
        if msg is None:
            if exit_when_idle:
                return 0
            time.sleep(poll)
            continue
        turn += 1
        print(f"\n── copse: turn {turn} ──\n{_preview(msg.body)}\n", flush=True)
        streamed["text"] = ""
        try:
            answer = loop.run(msg.body)
        except ClientError as e:
            print(f"\n── copse: the model endpoint failed: {e}; this worker has stopped ──", flush=True)
            return 1
        loop.save(saved)
        if streamed["text"].strip():  # the answer is already on screen
            print("\n── copse: turn finished ──", flush=True)
        else:
            print(f"\n{_preview(answer, 12)}\n── copse: turn finished ──", flush=True)
        agent = db.get_agent(agent_id)
        if agent is None:
            return 0
        if agent.mode in agents.REPORTING_MODES and agent.result is None and not db.pending_count(agent_id):
            if not reminded:
                reminded = True
                db.enqueue(agent_id, REMINDERS["review" if agent.mode == "review" else "work"], None)
                continue
            agents.tell_parent_unreported(db, agent)
        print("── copse: waiting for messages ──", flush=True)


def _drain(db: DB, agent_id: str) -> list[str]:
    """Every message queued for the agent, oldest first."""
    out = []
    while (msg := db.pop_pending(agent_id)) is not None:
        out.append(msg.body)
    return out
