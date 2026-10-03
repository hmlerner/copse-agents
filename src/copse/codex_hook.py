"""Codex's PermissionRequest hook for copse's permission policy, and the
one-time trust it needs.

Codex runs a hook it didn't get from managed config only once the person has
trusted it. Codex records that trust in its user ``config.toml`` as
``[hooks.state."<key>"] trusted_hash = "sha256:..."``, where the key is the
hook's source file, event and position (``<file>:permission_request:0:0``)
and the hash covers the hook's definition (its command, timeout...), not the
directory Codex runs in.

copse hands Codex its hook on the command line (``-c hooks.PermissionRequest
=...``, Codex's "session flags" layer), so nothing is added to a hooks file
and only copse's own Codex agents get it. The key is then always
``/<session-flags>/config.toml:permission_request:0:0`` and the command is the
same for every agent (the hook finds its agent from ``COPSE_AGENT_ID``, which
Codex passes through), so one trust covers every worktree copse ever creates.
It needs trusting again only when the command changes (copse installed
somewhere else, a different COPSE_HOME).

``copse permissions install-codex-hook --yes`` asks Codex itself (its
app-server's ``hooks/list``) for the key and hash, writes the trust through
Codex's own config writer (``config/batchWrite``) and records what it trusted
in ``~/.copse/permissions.json``. Until then copse doesn't pass the hook at
all: an untrusted hook is skipped by ``codex exec`` and makes the interactive
CLI ask for a review at startup.
"""

from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

EVENT = "codex-permission-request"
TIMEOUT = 30
SOURCE = "sessionFlags"


class CodexHookError(RuntimeError):
    pass


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _stable_invocation() -> list[str]:
    """copse_invocation, with the interpreter named the same however copse
    was started (a venv's ``python`` and ``python3`` are the same program,
    but a different name would be a different hook to Codex)."""
    from copse.providers import copse_invocation

    argv = copse_invocation()
    exe = argv[0]
    plain = os.path.join(os.path.dirname(exe), "python")
    try:
        if plain != exe and os.path.realpath(plain) == os.path.realpath(exe):
            argv = [plain, *argv[1:]]
    except OSError:
        pass
    return argv


def hook_command() -> str:
    """The hook's command: the same for every agent, so one trust covers all."""
    assigns = f"COPSE_HOME={shlex.quote(os.environ['COPSE_HOME'])} " if "COPSE_HOME" in os.environ else ""
    return assigns + " ".join(shlex.quote(a) for a in [*_stable_invocation(), "_hook", EVENT])


def config_flags(command: str | None = None) -> list[str]:
    """``-c`` arguments that give a Codex session copse's hook."""
    command = command or hook_command()
    hooks = f'[{{hooks=[{{type="command",command={json.dumps(command)},timeout={TIMEOUT}}}]}}]'
    return ["-c", f"hooks.PermissionRequest={hooks}"]


def trusted(command: str | None = None) -> bool:
    """The person trusted this exact hook, and Codex's config still says so."""
    import tomllib

    from copse import permissions

    command = command or hook_command()
    rec = permissions.load_store().codex_hook
    if not rec or rec.get("command") != command or not rec.get("key") or not rec.get("hash"):
        return False
    try:
        with open(codex_home() / "config.toml", "rb") as f:
            cfg = tomllib.load(f)
    except (OSError, ValueError):
        return False
    state = cfg.get("hooks", {}).get("state", {}) if isinstance(cfg.get("hooks"), dict) else {}
    entry = state.get(rec["key"]) if isinstance(state, dict) else None
    return isinstance(entry, dict) and entry.get("trusted_hash") == rec["hash"]


def launch_flags(cwd: str | None) -> list[str]:
    """The flags a Codex agent in ``cwd`` launches with: copse's hook when the
    repo's policy is on and the person trusted the hook; else none."""
    from copse.config import load_repo_config

    try:
        if not cwd or load_repo_config(cwd).permission_policy != "on":
            return []
        command = hook_command()
        return config_flags(command) if trusted(command) else []
    except Exception:  # noqa: BLE001 - never stop a launch over this
        return []


# -- asking Codex itself ---------------------------------------------------------------


class _AppServer:
    """A short-lived ``codex app-server`` over stdio (JSON-RPC, one per line)."""

    def __init__(self, binary: str, flags: list[str], timeout: float = 30.0):
        self.timeout = timeout
        try:
            self.proc = subprocess.Popen([binary, "app-server", *flags], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError as e:
            raise CodexHookError(f"couldn't run {binary}: {e}") from e
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.next_id = 0
        self.call("initialize", {"clientInfo": {"name": "copse", "version": "0"}})
        self._send({"method": "initialized"})

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _send(self, msg: dict) -> None:
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def call(self, method: str, params: dict) -> dict:
        self.next_id += 1
        self._send({"id": self.next_id, "method": method, "params": params})
        while True:
            try:
                line = self.lines.get(timeout=self.timeout)
            except queue.Empty as e:
                raise CodexHookError(f"codex app-server didn't answer {method}") from e
            if line is None:
                raise CodexHookError(f"codex app-server exited during {method}")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == self.next_id:
                if "error" in msg:
                    raise CodexHookError(f"codex {method}: {msg['error']}")
                return msg.get("result") or {}

    def close(self) -> None:
        try:
            self.proc.kill()
        except OSError:
            pass


@dataclass
class HookState:
    command: str
    key: str
    hash: str
    status: str  # Codex's trustStatus: untrusted | trusted | modified | managed
    config: Path


def inspect(binary: str, command: str | None = None) -> HookState:
    """What Codex makes of copse's hook: its trust key, hash and status."""
    command = command or hook_command()
    server = _AppServer(binary, config_flags(command))
    try:
        result = server.call("hooks/list", {"cwds": [str(Path.home())]})
    finally:
        server.close()
    for entry in result.get("data") or []:
        for hook in entry.get("hooks") or []:
            if hook.get("source") == SOURCE and hook.get("command") == command:
                return HookState(command, str(hook["key"]), str(hook["currentHash"]),
                                 str(hook.get("trustStatus")), codex_home() / "config.toml")
    raise CodexHookError("Codex didn't list copse's hook (is this Codex too old for hooks?)")


def trust(binary: str, state: HookState) -> HookState:
    """Record the person's trust of the hook in Codex's config (through Codex's
    own writer), check Codex now sees it as trusted, and remember it."""
    from copse import permissions

    server = _AppServer(binary, config_flags(state.command))
    try:
        server.call("config/batchWrite", {"edits": [{
            "keyPath": "hooks.state", "mergeStrategy": "upsert",
            "value": {state.key: {"trusted_hash": state.hash}}}]})
    finally:
        server.close()
    after = inspect(binary, state.command)
    if after.status != "trusted":
        raise CodexHookError(f"Codex still reports the hook as {after.status}")
    with permissions.editing() as store:
        store.codex_hook = {"command": after.command, "key": after.key, "hash": after.hash}
    return after
