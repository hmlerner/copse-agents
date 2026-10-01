"""Per-worktree runtime services (copse Pro): one Docker container per
configured service, so parallel agents don't share one database.

Services come from the ``services`` key of ``.copse/config.json``. Each gets a
host port from the worktree's own port block (``port_base + 1 + index``, leaving
``port_base`` itself to the app), bound to
127.0.0.1, and its connection env is added to ``workspace_env``. Nothing here
fails workspace creation: a missing Docker or entitlement just prints a note.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

from copse.config import PORT_BLOCK_SIZE, RepoConfig
from copse.db import Workspace

FEATURE = "services"
RUN_TIMEOUT = 300  # seconds; the first `docker run` may pull an image

PRESETS: dict[str, dict] = {
    "postgres": {
        "image": "postgres:16-alpine",
        "port": 5432,
        "docker_env": {"POSTGRES_PASSWORD": "copse", "POSTGRES_DB": "app"},
        "env": {"DATABASE_URL": "postgres://postgres:copse@127.0.0.1:{port}/app"},
    },
    "redis": {
        "image": "redis:7-alpine",
        "port": 6379,
        "docker_env": {},
        "env": {"REDIS_URL": "redis://127.0.0.1:{port}"},
    },
    "mongo": {
        "image": "mongo:7",
        "port": 27017,
        "docker_env": {},
        "env": {"MONGODB_URL": "mongodb://127.0.0.1:{port}/app"},
    },
}


@dataclass
class Service:
    name: str
    image: str
    port: int                  # container port
    host_port: int
    env: dict[str, str] = field(default_factory=dict)         # rendered, for the agent
    docker_env: dict[str, str] = field(default_factory=dict)  # for the container


def _say(msg: str) -> None:
    print(f"copse: {msg}", file=sys.stderr)


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-")


def container_name(ws: Workspace, service: str) -> str:
    return f"copse-{_slug(ws.id)}-{_slug(service)}"


def _render(template: str, *, port: int, name: str, workspace: str) -> str:
    return (template.replace("{port}", str(port)).replace("{name}", name)
            .replace("{workspace}", workspace))


def resolve(ws: Workspace, cfg: RepoConfig) -> list[Service]:
    """The configured services with presets filled in and host ports assigned.
    Entries that are malformed or don't fit in the port block are skipped."""
    out: list[Service] = []
    if ws.port_base is None:
        return out
    for i, raw in enumerate(cfg.services):
        if i >= PORT_BLOCK_SIZE - 1:
            _say(f"only {PORT_BLOCK_SIZE - 1} services fit in a worktree's port block; ignoring the rest")
            break
        if not isinstance(raw, dict) or not raw.get("name"):
            _say(f"service #{i + 1} needs a name; skipped")
            continue
        preset = PRESETS.get(raw.get("preset") or "", {})
        image = raw.get("image") or preset.get("image")
        port = raw.get("port") or preset.get("port")
        name = str(raw["name"])
        if not image or not port:
            _say(f"service {name!r} needs a preset, or an image and a port; skipped")
            continue
        host_port = ws.port_base + 1 + i
        env_templates = {**preset.get("env", {}), **(raw.get("env") or {})}
        env = {
            k: _render(str(v), port=host_port, name=name, workspace=ws.name)
            for k, v in env_templates.items()
        }
        env[f"COPSE_SVC_{re.sub(r'[^A-Za-z0-9]+', '_', name).upper()}_PORT"] = str(host_port)
        out.append(Service(name, str(image), int(port), host_port, env,
                           dict(preset.get("docker_env", {}))))
    return out


def entitled() -> bool:
    from copse.pro import license

    try:
        return license.has(FEATURE)
    except Exception:  # noqa: BLE001 -- an unreadable license is "not entitled"
        return False


def env(ws: Workspace, cfg: RepoConfig) -> dict[str, str]:
    """Connection env for ``workspace_env``: empty unless services are
    configured and the account is entitled."""
    if not cfg.services or not entitled():
        return {}
    merged: dict[str, str] = {}
    for svc in resolve(ws, cfg):
        merged.update(svc.env)
    return merged


def docker_path() -> str | None:
    return shutil.which("docker")


def _docker(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        _say(f"docker {args[0]} failed: {e}")
        return None


def up(ws: Workspace, cfg: RepoConfig) -> list[str]:
    """Start the workspace's services; returns the names started. Never raises."""
    if not cfg.services:
        return []
    if not entitled():
        _say("per-worktree services need copse Pro; none started "
             "(`copse account` shows your plan; `copse account upgrade` gets it)")
        return []
    if not docker_path():
        _say("docker not found; per-worktree services not started")
        return []
    started = []
    for svc in resolve(ws, cfg):
        name = container_name(ws, svc.name)
        _docker(["rm", "-f", name])  # a leftover from an earlier `up` would block the name
        cmd = ["run", "-d", "--rm", "--name", name, "--label", f"copse.workspace={ws.id}",
               "-p", f"127.0.0.1:{svc.host_port}:{svc.port}"]
        for k, v in svc.docker_env.items():
            cmd += ["-e", f"{k}={v}"]
        proc = _docker([*cmd, svc.image], RUN_TIMEOUT)
        if proc is not None and proc.returncode == 0:
            started.append(svc.name)
        elif proc is not None:
            _say(f"service {svc.name} didn't start: {(proc.stderr or proc.stdout).strip()[-200:]}")
    return started


def down(ws: Workspace, cfg: RepoConfig | None = None) -> list[str]:
    """Stop and remove every container labelled with the workspace, whatever
    the config says now (a renamed or deleted service is still cleaned up).
    Not gated on the entitlement, so a lapsed license can't leak containers.
    Returns the container names removed."""
    if not docker_path():
        return []
    proc = _docker(["ps", "-aq", "--filter", f"label=copse.workspace={ws.id}"])
    ids = proc.stdout.split() if proc is not None and proc.returncode == 0 else []
    if not ids:
        return []
    proc = _docker(["rm", "-f", *ids])
    return ids if proc is not None and proc.returncode == 0 else []


def status(ws: Workspace) -> list[str]:
    """``name<TAB>status<TAB>ports`` lines for the workspace's running containers."""
    if not docker_path():
        return []
    proc = _docker(["ps", "--filter", f"label=copse.workspace={ws.id}",
                    "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}"])
    if proc is None or proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]
