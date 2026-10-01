"""``copse doctor``: what copse needs, and whether it's there.

Plain Claude Code needs nothing set up; copse needs tmux, the agent CLIs,
a writable home, and (per repo) a few optional pieces. This checks each one
and says what to do about anything missing, so a first run doesn't fail
halfway through a launch.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    level: str
    name: str
    detail: str


def _version(cmd: list[str]) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (out.stdout or out.stderr).strip().splitlines()
    return text[-1].strip() if text else ""


def _tool(name: str, required: bool, why: str, version_args: list[str] | None = None,
          install: str = "") -> Check:
    path = shutil.which(name)
    if not path:
        level = FAIL if required else WARN
        return Check(level, name, f"not found: {why}." + (f" Install: {install}" if install else ""))
    version = _version([path, *(version_args or ["--version"])]) or ""
    return Check(OK, name, f"{path}" + (f" ({version[:40]})" if version else ""))


def checks(repo_root: str | None) -> list[Check]:
    from copse import config, procs, tmux
    from copse.db import DB

    out: list[Check] = []
    v = sys.version_info
    out.append(Check(OK if v >= (3, 11) else FAIL, "python",
                     f"{v.major}.{v.minor}.{v.micro}" + ("" if v >= (3, 11) else " (copse needs 3.11 or newer)")))
    out.append(_tool("tmux", True, "copse runs every agent in a tmux window", ["-V"], "brew install tmux"))
    out.append(_tool("claude", True, "the supervisor and the built-in profiles use Claude Code",
                     install="see https://code.claude.com"))
    out.append(_tool("codex", False, "only needed for Codex agents (reviewer-codex)"))
    out.append(_tool("agy", False, "only needed for Google Antigravity agents"))
    out.append(_tool("gh", False, "only needed for `copse pr` and `copse new --pr`"))
    out.append(_tool("pre-commit", False, "only needed if the repo uses pre-commit hooks"))
    out.append(_tool("graphify", False, "only needed for the code map agents can query"))

    clip = tmux.clipboard_command()
    out.append(Check(OK if clip else WARN, "clipboard",
                     f"{clip.split()[0]}: dragging in the chat copies to the clipboard" if clip else
                     "no pbcopy, wl-copy or xclip: mouse selection copies only within tmux"))

    out.extend(native_checks(repo_root))
    out.extend(quota_checks(repo_root))
    out.extend(airgap_checks(repo_root))

    home = config.copse_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe = home / ".doctor-probe"
        probe.write_text("ok")
        probe.unlink()
        DB().conn.execute("SELECT 1")
        out.append(Check(OK, "copse home", f"{home} (writable, database opens)"))
    except Exception as e:  # noqa: BLE001
        out.append(Check(FAIL, "copse home", f"{home}: {e}"))

    if shutil.which("tmux"):
        try:
            sock = os.environ.get("COPSE_TMUX_SOCKET")
            cmd = ["tmux", *(["-L", sock] if sock else []), "-V"]
            subprocess.run(cmd, capture_output=True, timeout=10)
            focus = tmux._tmux("show-options", "-gs", "focus-events", check=False).stdout.strip()
            out.append(Check(OK, "tmux focus-events", focus or "not set yet (copse sets it at launch)"))
        except Exception as e:  # noqa: BLE001
            out.append(Check(WARN, "tmux focus-events", str(e)))

    try:
        db = DB()
        table = procs.table()
        stray = []
        for aid in procs.all_agent_ids(table):
            a = db.get_agent(aid)
            if a is None or a.status in ("paused", "done") or a.dismissed_at is not None:
                stray.append(aid)
        if stray:
            out.append(Check(WARN, "leftover processes",
                             f"{len(stray)} agent(s) have processes running but aren't active: "
                             f"{', '.join(stray[:5])}. `copse prune` stops them."))
        else:
            out.append(Check(OK, "leftover processes", "none"))
    except Exception as e:  # noqa: BLE001
        out.append(Check(WARN, "leftover processes", f"couldn't check: {e}"))

    if repo_root:
        try:
            cfg = config.load_repo_config(repo_root)
            path = Path(repo_root) / config.CONFIG_DIR / config.CONFIG_FILE
            if path.exists():
                out.append(Check(OK, "repo config", f"{path}"))
            else:
                out.append(Check(OK, "repo config", "none (defaults apply; `copse init` writes a starter)"))
            if cfg.checks:
                out.append(Check(OK, "checks", "; ".join(cfg.checks)))
            else:
                out.append(Check(WARN, "checks",
                                 "none configured: nothing verifies a branch before it merges. "
                                 'Add e.g. {"checks": ["uv run pytest -q"]} to .copse/config.json'))
            if cfg.add_dirs:
                from copse.profiles import load_profile, missing_add_dirs
                # The repo's entries as any profile gets them: resolved, ~ expanded.
                profile = load_profile("developer", repo_root)
                missing = missing_add_dirs(profile)
                if missing:
                    out.append(Check(WARN, "add_dirs",
                                     f"{', '.join(missing)} not found: Claude Code ignores a "
                                     "missing --add-dir, so agents won't reach it"))
                else:
                    out.append(Check(OK, "add_dirs", ", ".join(profile.add_dirs or [])))
            if cfg.services:
                docker = shutil.which("docker")
                names = ", ".join(str(s.get("name")) for s in cfg.services)
                out.append(Check(OK if docker else WARN, "docker",
                                 f"{docker} (services: {names})" if docker else
                                 f"not found: per-worktree services ({names}) won't start"))
            if (Path(repo_root) / "graphify-out" / "graph.json").is_file():
                out.append(Check(OK, "code map", "graphify-out/graph.json"))
            else:
                out.append(Check(WARN, "code map",
                                 "no graphify graph: agents find code by grepping. Run /graphify once."))
        except ValueError as e:
            out.append(Check(FAIL, "repo config", str(e)))
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=repo_root,
                               capture_output=True, text=True).stdout.strip()
        if dirty:
            out.append(Check(WARN, "working tree",
                             "uncommitted changes: workers branch from committed work only"))
        else:
            out.append(Check(OK, "working tree", "clean"))
    return out


def native_checks(repo_root: str | None) -> list[Check]:
    """One check per endpoint the native profiles use: reachable, and does
    it have the model. A missing endpoint is a warning (those profiles are
    optional); a profile with no endpoint at all is a failure, since it can
    never run."""
    from copse.native import runner
    from copse.profiles import list_profiles

    out: list[Check] = []
    seen: dict[tuple[str, str], list[str]] = {}
    profiles: list = []
    for p in list_profiles(repo_root):
        if p.provider != "native":
            continue
        try:
            ep = runner.endpoint_for(p)
        except ValueError as e:
            out.append(Check(FAIL, f"profile {p.name}", str(e)))
            continue
        seen.setdefault((ep.base_url, ep.model), []).append(p.name)
        profiles.append((p, ep))
    reachable: set[tuple[str, str]] = set()
    for (base_url, model), names in seen.items():
        ep = runner.Endpoint(base_url, model)
        ok, detail = runner.probe(ep)
        who = ", ".join(names)
        if ok:
            reachable.add((base_url, model))
            out.append(Check(OK if "is available" in detail else WARN, f"model {model}", f"{base_url}: {detail} (for {who})"))
        else:
            out.append(Check(WARN, f"model {model}",
                             f"{base_url} {detail}: only needed for {who}. For Ollama: "
                             f"`ollama serve`, then `ollama pull {model}`"))
    # Ollama silently drops the start of a conversation that outgrows its context.
    # Group profiles by endpoint to avoid duplicate warnings.
    endpoint_profiles: dict[tuple[str, str], list[tuple[object, runner.Endpoint]]] = {}
    for p, ep in profiles:
        if (ep.base_url, ep.model) not in reachable or not p.context_tokens or not runner.is_ollama(ep):
            continue
        endpoint_profiles.setdefault((ep.base_url, ep.model), []).append((p, ep))
    
    for (base_url, model), profile_eps in endpoint_profiles.items():
        # Find the maximum context_tokens among all profiles using this endpoint
        max_context_tokens = max(p.context_tokens for p, _ in profile_eps if p.context_tokens)
        fix = f"OLLAMA_CONTEXT_LENGTH={max_context_tokens + 8192} ollama serve"
        ep = profile_eps[0][1]  # Use the first profile's endpoint for context check
        ctx = runner.server_context(ep)
        profile_names = ", ".join(p.name for p, _ in profile_eps)
        if ctx is None:
            out.append(Check(WARN, f"context {profile_names}",
                             f"{ep.base_url} doesn't report its context length; Ollama truncates "
                             f"silently past it, so it must be at least {max_context_tokens} "
                             f"(context_tokens): {fix}"))
        elif ctx < max_context_tokens:
            out.append(Check(WARN, f"context {profile_names}",
                             f"{ep.base_url} runs {ep.model} with a {ctx}-token context, less than "
                             f"the profile's context_tokens ({max_context_tokens}); Ollama truncates "
                             f"silently. Restart with: {fix}"))
    return out


def airgap_checks(repo_root: str | None) -> list[Check]:
    """Air-gap mode (copse Enterprise): whether it's on and licensed, the
    configured profiles it refuses (hosted providers), and whether the
    offline policy file and license are there. One ``ok`` line when off."""
    from copse import airgap, config
    from copse.pro import license

    cfg = None
    if repo_root:
        try:
            cfg = config.load_repo_config(repo_root)
        except ValueError:
            cfg = None                 # the repo config check reports the bad file
    if not airgap.enabled(cfg):
        return [Check(OK, "air-gap", "off")]
    out: list[Check] = []
    warning = airgap.warning()
    if warning:
        out.append(Check(WARN, "air-gap", f"on via {airgap.source(cfg)}; {warning}"))
    else:
        out.append(Check(OK, "air-gap", f"on via {airgap.source(cfg)}: no outbound traffic, "
                                        "local models only"))
    hosted = airgap.hosted_profiles(repo_root)
    if hosted:
        out.append(Check(WARN, "hosted profiles",
                         f"{', '.join(hosted)}: refused in air-gap mode. Only a native profile "
                         "on a loopback or private-network base_url (or one marked `local: "
                         "true`) can run; point default_agent, routing and reviewer at one"))
    else:
        out.append(Check(OK, "hosted profiles", "none (every profile is local)"))
    # The chat itself is an agent: `copse` won't start on a hosted default_agent.
    default = (cfg.default_agent if cfg else None) or config.RepoConfig().default_agent
    ok, why = airgap.check_profile(default, repo_root)
    if ok:
        out.append(Check(OK, "default agent", f"{default} (local: `copse` can start)"))
    else:
        out.append(Check(FAIL, "default agent",
                         f"{default}: `copse` won't start in air-gap mode, since the chat is a "
                         f"hosted agent ({why.split(': ', 2)[-1]}). Set default_agent in "
                         ".copse/config.json to a local native profile"))
    try:
        ent = license.installed()
    except license.LicenseError as e:
        out.append(Check(FAIL, "offline license", f"{e}; reinstall with "
                                                  "`copse account license install <file>`"))
    else:
        if ent is None:
            out.append(Check(WARN, "offline license",
                             "none installed (`copse account license install <file>`); "
                             "nothing is entitled while the network is off"))
        else:
            out.append(Check(OK, "offline license",
                             f"org {ent.org_id}, plan {ent.plan}, features "
                             f"{', '.join(sorted(ent.features)) or '-'}"
                             + (" (expired; in grace)" if ent.in_grace else "")))
    if repo_root:
        path = airgap.policy_path(repo_root)
        if path.is_file():
            out.append(Check(OK, "offline policy", str(path)))
        else:
            out.append(Check(WARN, "offline policy",
                             f"none at {path}: with a Team license, delegations and merges are "
                             "refused until the org's policy is put there"))
    return out


def quota_checks(repo_root: str | None) -> list[Check]:
    """One line per provider that has quota data (a warning past 90% used or
    while limited). The local model server has its own checks above."""
    from copse import quota

    out = []
    for p in quota.PROVIDERS:
        if p == "native":
            continue
        n = quota.note(p)
        if n:
            out.append(Check(WARN if quota.headroom(p) <= 10 else OK, f"{p} quota", n))
    return out


MARK = {OK: "✓", WARN: "!", FAIL: "✗"}


def render(results: list[Check]) -> str:
    width = max(len(c.name) for c in results) if results else 0
    lines = [f"{MARK[c.level]} {c.name.ljust(width)}  {c.detail}" for c in results]
    fails = sum(c.level == FAIL for c in results)
    warns = sum(c.level == WARN for c in results)
    if fails:
        lines.append(f"\n{fails} problem(s) will stop copse from working; {warns} warning(s).")
    elif warns:
        lines.append(f"\ncopse can run; {warns} warning(s) worth a look.")
    else:
        lines.append("\nAll good.")
    return "\n".join(lines)
