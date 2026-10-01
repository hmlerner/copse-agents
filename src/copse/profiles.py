"""Agent profiles: markdown files with a small frontmatter header.

    ---
    name: developer
    description: Implements a well-scoped coding task
    provider: claude
    ---
    You are a developer agent...

Lookup order: ``<repo>/.copse/agents/``, ``~/.copse/agents/``, then the
built-in profiles shipped with copse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path

from copse.config import CONFIG_DIR, user_profiles_dir


@dataclass
class Profile:
    name: str
    description: str
    provider: str
    prompt: str
    model: str | None = None
    permission_mode: str | None = None
    allowed_tools: list[str] | None = None
    # Claude Code only; all off by default (see README, "Cheap workers").
    strict_mcp: bool = False                 # --strict-mcp-config: only copse's MCP server
    setting_sources: list[str] | None = None  # --setting-sources, e.g. project,local
    effort: str | None = None                # --effort low|medium|high|xhigh|max
    headless: bool = False                   # run with `claude -p`, turn by turn
    tool_search: bool | None = None          # Claude Code's deferred tool loading (None: off for workers)
    # The native provider (copse's own loop; see copse.native): where the model is.
    api: str | None = None                   # openai (chat completions) | anthropic (messages)
    base_url: str | None = None              # e.g. http://localhost:11434/v1
    api_key_env: str | None = None           # name of the variable holding the key, if one is needed
    context_tokens: int | None = None        # the model's window, less room for its reply
    # The model runs on this machine or the private network: air-gap mode (copse.airgap)
    # lets this profile run even when its endpoint can't be checked (e.g. a hostname).
    local: bool = False
    # Extra environment for the agent's process, from ``env.NAME: value`` lines.
    env: dict[str, str] = field(default_factory=dict)
    # --add-dir. Full tool access, not read access: edits and Bash reach these too,
    # and Claude Code loads any CLAUDE.md it finds in them. Added to the repo's own
    # add_dirs rather than replacing it; see load_profile.
    add_dirs: list[str] | None = None


_COMMENT = re.compile(r"(?:^|\s)#.*$")


def _value(raw: str) -> str:
    """A frontmatter value without its trailing ``# comment``. As in YAML, a
    ``#`` only starts a comment at the start or after whitespace, and a quoted
    value is taken as is."""
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        if end > 0:
            return raw[1:end]
    return _COMMENT.sub("", raw).strip()


def _flag(value: str | None) -> bool:
    return (value or "").lower() in ("true", "yes", "on", "1")


def _list(value: str | None) -> list[str] | None:
    """A comma-separated list; YAML's ``[a, b]`` form works too."""
    value = (value or "").strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    items = [t.strip().strip("'\"") for t in value.split(",") if t.strip()]
    return items or None


def _int(value: str | None) -> int | None:
    value = (value or "").strip().replace("_", "").replace(",", "")
    if value.lower().endswith("k") and value[:-1].isdigit():
        return int(value[:-1]) * 1000
    return int(value) if value.isdigit() else None


def _bool(value: str) -> bool:
    return value.strip().lower() in ('true', 'yes', 'on', '1')


def _parse(text: str, fallback_name: str) -> Profile:
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        _, header, body = text.split("---", 2)
        for line in header.strip().splitlines():
            if line.lstrip().startswith("#"):
                continue
            key, _, value = line.partition(":")
            if key.strip():
                meta[key.strip()] = _value(value)
    return Profile(
        name=meta.get("name", fallback_name),
        description=meta.get("description", ""),
        provider=meta.get("provider", "claude"),
        prompt=body.strip(),
        model=meta.get("model") or None,
        permission_mode=meta.get("permission_mode") or None,
        allowed_tools=_list(meta.get("allowed_tools")),
        strict_mcp=_flag(meta.get("strict_mcp")),
        setting_sources=_list(meta.get("setting_sources")),
        add_dirs=_list(meta.get("add_dirs")),
        effort=meta.get("effort") or None,
        tool_search=_bool(meta.get('tool_search')) if meta.get('tool_search') else None,
        headless=_flag(meta.get("headless")),
        api=meta.get("api") or None,
        base_url=meta.get("base_url") or None,
        api_key_env=meta.get("api_key_env") or None,
        context_tokens=_int(meta.get("context_tokens")),
        local=_flag(meta.get("local")),
        env={k[4:]: v for k, v in meta.items() if k.startswith("env.") and k[4:]},
    )


def _search_dirs(repo_root: str | None) -> list[Path]:
    dirs = []
    if repo_root:
        dirs.append(Path(repo_root) / CONFIG_DIR / "agents")
    dirs.append(user_profiles_dir())
    return dirs


def _with_repo_add_dirs(profile: Profile, repo_root: str | None) -> Profile:
    """Union the repo's ``add_dirs`` with the profile's, resolved and checked.

    The repo config is the primary home: a shared build cache or a folder of
    profiles beside the repo is a property of the repository, so every profile
    launched in it needs the same list and copies would drift. A profile adds to
    that list for a role that needs more, and never removes from it, which keeps
    the result easy to reason about.

    Entries are resolved against the repo root rather than passed through, because
    a worktree is the process's working directory and a relative path would
    otherwise mean ``~/.copse/worktrees/<repo>/<branch>/<path>``. A leading ``~``
    is expanded first, since nothing downstream runs a shell that would.
    Checking that they exist is left to launch (see ``missing_add_dirs``): this
    runs every time a profile is loaded, several times per launch and on every
    resume, and one launch should say so once.
    """
    if repo_root is None:
        return profile

    from copse.config import load_repo_config

    root = Path(repo_root)
    merged: list[str] = []
    for entry in [*load_repo_config(repo_root).add_dirs, *(profile.add_dirs or [])]:
        entry = str(entry).strip()
        if not entry:
            continue
        try:
            path = Path(entry).expanduser()
        except RuntimeError:
            # An unknown user (a typo like ~typo/cache) or no home directory.
            # Kept as written so missing_add_dirs reports it at launch; raising
            # here would fail every load_profile, and with it every launch.
            if entry not in merged:
                merged.append(entry)
            continue
        resolved = str(path if path.is_absolute() else (root / path).resolve())
        if resolved not in merged:
            merged.append(resolved)
    return replace(profile, add_dirs=merged or None)


def missing_add_dirs(profile: Profile) -> list[str]:
    """The profile's ``add_dirs`` that do not exist. Claude Code ignores an
    ``--add-dir`` that does not exist, so without this the failure would be the
    one the field exists to prevent, silently."""
    return [d for d in profile.add_dirs or [] if not Path(d).is_dir()]


def load_profile(name: str, repo_root: str | None = None) -> Profile:
    """The profile in ``<name>.md``. Its ``name`` is always ``name``, even if
    the file's frontmatter says otherwise (a copied profile whose name wasn't
    changed): agents record it, and a resume or relaunch loads the profile
    again by that name, so it must find this same file, permissions and all."""
    for d in _search_dirs(repo_root):
        f = d / f"{name}.md"
        if f.is_file():
            return _with_repo_add_dirs(
                replace(_parse(f.read_text(encoding="utf-8"), name), name=name), repo_root
            )
    builtin = resources.files("copse.builtin_agents").joinpath(f"{name}.md")
    if builtin.is_file():
        return _with_repo_add_dirs(
            replace(_parse(builtin.read_text(encoding="utf-8"), name), name=name), repo_root
        )
    raise KeyError(f"no agent profile named {name!r}")


def list_profiles(repo_root: str | None = None) -> list[Profile]:
    seen: dict[str, Profile] = {}
    builtin_dir = resources.files("copse.builtin_agents")
    for entry in builtin_dir.iterdir():
        if entry.name.endswith(".md"):
            p = _parse(entry.read_text(encoding="utf-8"), entry.name[:-3])
            seen[p.name] = p
    for d in reversed(_search_dirs(repo_root)):
        if d.is_dir():
            for f in sorted(d.glob("*.md")):
                p = _parse(f.read_text(encoding="utf-8"), f.stem)
                seen[p.name] = p
    return sorted(seen.values(), key=lambda p: p.name)
