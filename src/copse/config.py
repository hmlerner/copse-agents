"""Paths and per-repo configuration.

Repo config lives in ``<repo>/.copse/config.json`` (committed, shared with the
team) and ``<repo>/.copse/config.local.json`` (gitignored, personal). Local
keys override shared ones; for command lists, local may instead give
``{"before": [...], "after": [...]}`` to wrap the team's commands.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = ".copse"
CONFIG_FILE = "config.json"
LOCAL_CONFIG_FILE = "config.local.json"

PORT_RANGE_START = 20000
PORT_BLOCK_SIZE = 10


def copse_home() -> Path:
    return Path(os.environ.get("COPSE_HOME", Path.home() / ".copse"))


def db_path() -> Path:
    return copse_home() / "copse.db"


def worktrees_dir() -> Path:
    return copse_home() / "worktrees"


def user_profiles_dir() -> Path:
    return copse_home() / "agents"


WEIGHTS = ("light", "medium", "heavy")
DEFAULT_ROUTING = {
    "light": ["developer-local", "developer"],
    "medium": ["developer-codex", "developer"],
    "heavy": ["developer-heavy", "developer"],
}


@dataclass
class RepoConfig:
    setup: list[str] = field(default_factory=list)
    teardown: list[str] = field(default_factory=list)
    copy: list[str] = field(default_factory=list)
    base_branch: str | None = None
    branch_prefix: str = ""
    default_agent: str = "developer"
    fetch: bool = True
    # Autopilot and merge gates.
    autopilot: bool = True             # `copse` starts the supervisor with autopilot on
    checks: list[str] = field(default_factory=list)  # must pass in a branch before it merges
    review: bool | None = None         # require a reviewer's approval (None: only under autopilot)
    reviewer: str = "reviewer"         # agent profile that reviews branches
    review_profile: str | None = None  # force request_review's profile (skips its automatic cross-model pick)
    pre_commit: bool = True            # run pre-commit (the framework) over a branch before merging
    max_agents: int = 4                # workers running at once per session; 0 means no cap
    check_timeout: int = 900           # seconds allowed for each check command
    usage_limit: int = 90              # autopilot stops pushing on at this % of the Claude usage limit
    limit_cooldown_minutes: int | None = None  # how long a provider that hit its limit counts as unavailable (default 300 for Antigravity)
    graphify: bool | None = None       # point agents at graphify-out/graph.json (None: if it's there)
    stale_after: int = 30              # minutes before an idle, reported worker is closed; 0: never
    pipeline: bool = True              # copse reviews and merges reported branches itself (copse.pipeline)
    review_rounds: int = 2             # fix-and-re-review rounds the pipeline runs before asking the supervisor
    merge_into: str | None = None      # branch worker branches are cut from and merge into (None: the supervisor's / default branch)
    auto_merge_default_branch: bool = False  # let the pipeline merge into the repo's default branch on its own
    plan_first: bool = False           # workers propose a plan and wait for approval before editing
    overlap: str = "block"           # a task whose files overlap a running one: "block" or "warn"
    # Worktree pool: pre-built worktrees (checked out, files copied, setup run)
    # that `create` claims instead of doing that work live. None here means
    # "not set"; load_repo_config resolves it to 1 if the repo has `setup`
    # commands (worth pre-building) or 0 otherwise (a bare `worktree add` is
    # already fast). 0 disables the pool.
    pool_size: int | None = None
    add_dirs: list[str] = field(default_factory=list)
    # Per-worktree Docker services (copse Pro): [{"name", "preset"?, "image"?, "port"?, "env"?}]
    # -- see copse.services.
    services: list[dict] = field(default_factory=list)
    # Start Ollama in the background when a native profile points at it on
    # this machine and it isn't running (see copse.native.serve).
    local_models: bool = True
    pr_footer: bool = True             # `copse pr` / `copse ci` end the PR body with one "built with copse" line
    sidebar: str = "left"              # where the dashboard sits: "left" of the chat or "bottom"
    learning: str = "auto"             # "auto" (copse Pro's cloud learner when entitled, else off), "off", or an installed learning plugin's name (see copse.learning)
    learning_candidates: list[str] = field(default_factory=list)  # profiles the learner may pick from
    # Which installed plugin to use per group ("events", "policy", "account"), or "off";
    # unset: the only one installed, if exactly one (see copse.plugins).
    plugins: dict[str, str] = field(default_factory=dict)
    message_delivery: str = "pull"     # agent messages to an interactive supervisor: "pull" (a notice, then read_messages) or "push" (the text itself)
    # Air-gap mode (copse Enterprise; see copse.airgap): no outbound traffic, local models only.
    airgap: bool = False
    # Weight routing: task weight -> profiles to try, in order (see autopilot.choose_profile).
    routing: dict[str, list[str]] = field(default_factory=lambda: {k: list(v) for k, v in DEFAULT_ROUTING.items()})


def _merge_commands(shared: list[str], local: object) -> list[str]:
    if isinstance(local, list):
        return [str(c) for c in local]
    if isinstance(local, dict):
        return [*local.get("before", []), *shared, *local.get("after", [])]
    return shared


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path}: invalid JSON ({e})") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


def config_root(path: str | Path) -> Path:
    """The directory whose ``.copse`` applies to ``path``: ``path`` itself when
    it has one, else (for a linked git worktree, where the git-ignored config
    isn't checked out) the main worktree found via ``git rev-parse
    --git-common-dir`` (read from the ``.git`` file, so no git process runs)."""
    path = Path(path)
    if (path / CONFIG_DIR).is_dir():
        return path
    try:
        gitdir = Path((path / ".git").read_text(encoding="utf-8").split("gitdir:", 1)[1].strip())
        common = (gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()).resolve()
    except (OSError, IndexError):  # not a linked worktree: nothing to discover
        return path
    main = common.parent
    return main if common.name == ".git" and (main / CONFIG_DIR).is_dir() else path


def load_repo_config(repo_root: str | Path) -> RepoConfig:
    base = config_root(repo_root) / CONFIG_DIR
    shared = _read_json(base / CONFIG_FILE)
    local = _read_json(base / LOCAL_CONFIG_FILE)

    cfg = RepoConfig()
    for key in ("setup", "teardown", "copy", "checks", "add_dirs"):
        merged = _merge_commands(list(shared.get(key, [])), local.get(key))
        setattr(cfg, key, merged)
    for key in ("base_branch", "branch_prefix", "default_agent", "fetch", "autopilot", "review",
                "reviewer", "review_profile", "pre_commit", "max_agents", "check_timeout",
                "usage_limit", "pool_size", "graphify", "stale_after", "pipeline",
                "review_rounds", "overlap", "local_models", "merge_into",
                "auto_merge_default_branch", "sidebar", "pr_footer", "plan_first", "learning",
                "learning_candidates", "limit_cooldown_minutes", "message_delivery"):
        if key in local:
            setattr(cfg, key, local[key])
        elif key in shared:
            setattr(cfg, key, shared[key])
    services = local["services"] if "services" in local else shared.get("services")
    if isinstance(services, list):
        cfg.services = [s for s in services if isinstance(s, dict)]
    for source in (shared, local):  # per group, so a repo can override one and keep the rest
        plugins = source.get("plugins")
        if isinstance(plugins, dict):
            for group, name in plugins.items():
                if isinstance(group, str) and isinstance(name, str):
                    cfg.plugins[group] = name
    for source in (shared, local):  # per tier, so a repo can override one and keep the rest
        routing = source.get("routing")
        if isinstance(routing, dict):
            for tier, names in routing.items():
                if tier in WEIGHTS and isinstance(names, list):
                    cfg.routing[tier] = [n for n in names if isinstance(n, str)]
    if cfg.pool_size is None:
        cfg.pool_size = 1 if cfg.setup else 0
    # Air-gap mode: either file may turn it on, and neither may turn it off.
    cfg.airgap = bool(shared.get("airgap", False)) or bool(local.get("airgap", False))
    if cfg.airgap:
        from copse import airgap

        airgap.arm()
    return cfg


TEMPLATE = {
    "setup": [],
    "teardown": [],
    "copy": [],
    "base_branch": None,
    "branch_prefix": "",
    "default_agent": "developer",
    "fetch": True,
    "checks": [],
}


def write_template(repo_root: str | Path, values: dict | None = None) -> Path:
    """Write ``.copse/config.json`` (the template, with ``values`` over it)
    unless it already exists, and the ``.gitignore`` for the local file."""
    base = Path(repo_root) / CONFIG_DIR
    base.mkdir(parents=True, exist_ok=True)
    path = base / CONFIG_FILE
    if not path.exists():
        path.write_text(json.dumps({**TEMPLATE, **(values or {})}, indent=2) + "\n", encoding="utf-8")
    ignore = base / ".gitignore"
    if not ignore.exists():
        ignore.write_text(f"{LOCAL_CONFIG_FILE}\n", encoding="utf-8")
    return path
