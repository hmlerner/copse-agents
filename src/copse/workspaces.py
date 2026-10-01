"""Workspaces: a git worktree on its own branch, plus the tmux session its
agents run in.

Lifecycle:
  create  -> fetch base, ``git worktree add``, record base, copy local files,
             reserve a port block, run setup
  work    -> diff vs. base, sync (rebase/merge base in), commit, push, PR,
             merge back
  remove  -> refuse if dirty (unless forced), run teardown, kill tmux,
             remove worktree; the branch is kept unless asked otherwise
"""

from __future__ import annotations

import filecmp
import glob
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from copse import git, services, tmux
from copse.config import (
    PORT_BLOCK_SIZE,
    PORT_RANGE_START,
    RepoConfig,
    load_repo_config,
    worktrees_dir,
)
from copse.db import DB, Workspace

ROOT_NAME = "root"


class WorkspaceError(RuntimeError):
    pass


@dataclass
class SetupResult:
    ok: bool
    log: str


# -- lookup ----------------------------------------------------------------


def workspace_env(ws: Workspace) -> dict[str, str]:
    env = {
        "COPSE_ROOT_PATH": ws.repo_root,
        "COPSE_WORKSPACE_PATH": ws.path,
        "COPSE_WORKSPACE_NAME": ws.name,
        "COPSE_WORKSPACE_ID": ws.id,
        "COPSE_BRANCH": ws.branch,
    }
    if ws.base_branch:
        env["COPSE_BASE_BRANCH"] = ws.base_branch
    if ws.port_base is not None:
        env["COPSE_PORT_BASE"] = str(ws.port_base)
    for key in ("COPSE_HOME", "COPSE_TMUX_SOCKET", "COPSE_CLAUDE_BIN"):
        if key in os.environ:
            env[key] = os.environ[key]
    if ws.kind != "main" and os.path.isdir(ws.repo_root):
        try:
            env.update(services.env(ws, load_repo_config(ws.repo_root)))
        except ValueError:
            pass  # a broken config is reported elsewhere
    return env


def resolve(db: DB, ref: str, cwd: str | None = None) -> Workspace:
    """Find a workspace by id, by name within the current repo, or by a
    unique name across all repos."""
    ws = db.get_workspace(ref)
    if ws:
        return ws
    repo_root = None
    try:
        repo_root = git.main_repo_root(cwd or os.getcwd())
    except git.GitError:
        pass
    if repo_root:
        for ws in db.find_workspaces(repo_root):
            if ws.name == ref or ws.branch == ref:
                return ws
    matches = [w for w in db.find_workspaces() if w.name == ref or w.branch == ref]
    if len(matches) == 1:
        return matches[0]
    if matches:
        ids = ", ".join(w.id for w in matches)
        raise WorkspaceError(f"{ref!r} is ambiguous; use one of: {ids}")
    raise WorkspaceError(f"no workspace named {ref!r}")


def current(db: DB, cwd: str | None = None) -> Workspace | None:
    try:
        top = git.toplevel(cwd or os.getcwd())
    except git.GitError:
        return None
    return db.workspace_by_path(top)


# -- creation --------------------------------------------------------------


def _repo_slug(repo_root: str) -> str:
    return git.slug(Path(repo_root).name) or "repo"


def _session_name(repo_root: str, name: str) -> str:
    # tmux forbids '.' and ':' in session names.
    return f"copse_{_repo_slug(repo_root)}_{name}".replace(".", "_").replace(":", "_")


def _unique_name(db: DB, repo_root: str, branch: str) -> str:
    base = git.slug(branch) or "ws"
    taken = {w.name for w in db.find_workspaces(repo_root)} | {ROOT_NAME}
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    return name


def _next_port_base(db: DB) -> int:
    used = db.used_port_bases()
    port = PORT_RANGE_START
    while port in used:
        port += PORT_BLOCK_SIZE
    return port


def _copy_local_files(
    repo_root: str, dest: str, patterns: list[str], overwrite_changed: bool = False
) -> list[str]:
    """Copy gitignored/untracked files (``.env`` etc.) that a fresh checkout
    lacks. Never overwrites a directory, or a file whose content already
    matches. With ``overwrite_changed`` (a pool claim, where the file was
    copied whenever the entry was built and the root's copy may have moved on
    since), a file that already exists but differs is overwritten too."""
    copied = []
    for pattern in patterns:
        for src in glob.glob(os.path.join(repo_root, pattern), recursive=True):
            rel = os.path.relpath(src, repo_root)
            if rel.startswith(".."):
                continue
            target = os.path.join(dest, rel)
            if os.path.exists(target):
                if (
                    overwrite_changed
                    and os.path.isfile(src)
                    and os.path.isfile(target)
                    and not filecmp.cmp(src, target, shallow=False)
                ):
                    shutil.copy2(src, target)
                    copied.append(rel)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if os.path.isdir(src):
                shutil.copytree(src, target)
            else:
                shutil.copy2(src, target)
            copied.append(rel)
    return copied


def run_commands(commands: list[str], cwd: str, env: dict[str, str]) -> SetupResult:
    log: list[str] = []
    full_env = {**os.environ, **env}
    for cmd in commands:
        log.append(f"$ {cmd}")
        proc = subprocess.run(
            cmd, shell=True, cwd=cwd, env=full_env, capture_output=True, text=True
        )
        if proc.stdout:
            log.append(proc.stdout.rstrip())
        if proc.stderr:
            log.append(proc.stderr.rstrip())
        if proc.returncode != 0:
            log.append(f"(exit {proc.returncode})")
            return SetupResult(False, "\n".join(log))
    return SetupResult(True, "\n".join(log))


@dataclass
class Created:
    workspace: Workspace
    how: str               # "new", "existing", "remote" branch, or "pool"
    start_point: str
    copied: list[str]
    setup: SetupResult | None


def create(
    db: DB,
    repo_path: str,
    branch: str,
    base: str | None = None,
    *,
    fetch: bool | None = None,
    start: str | None = None,
    run_setup: bool = True,
    apply_prefix: bool = True,
) -> Created:
    repo_root = git.main_repo_root(repo_path)
    cfg: RepoConfig = load_repo_config(repo_root)
    prefix = cfg.branch_prefix if apply_prefix else ""
    branch = git.sanitize_branch(f"{prefix}{branch}")
    base = base or cfg.base_branch or git.default_branch(repo_root)
    if branch == base:
        raise WorkspaceError(f"branch {branch!r} is the base branch; pick a new branch name")

    name = _unique_name(db, repo_root, branch)

    start_point = start or git.resolve_start_point(
        repo_root, base, cfg.fetch if fetch is None else fetch
    )
    start_sha = git.out(["rev-parse", start_point], repo_root)

    # A pool entry is only a safe substitute for `git worktree add` when
    # `branch` doesn't already exist locally or on origin -- claiming would
    # otherwise reset an existing branch's history onto the pool's base sha --
    # and when the new branch is meant to start at the base branch's current
    # tip, the same thing a pool fill builds from: either the caller didn't
    # ask for a specific `start` at all (the common case -- a worker's base
    # is the caller's own branch), or an explicit `start` happens to resolve
    # to the local base branch's own tip. Not, e.g., create_from_pr, where
    # `start` is a fetched PR head unrelated to `base`.
    claimed = None
    if (
        run_setup
        and not git.branch_exists(repo_root, branch)
        and not git.remote_branch_exists(repo_root, branch)
        and (
            start is None
            or (
                git.branch_exists(repo_root, base)
                and start_sha == git.out(["rev-parse", base], repo_root)
            )
        )
    ):
        from copse import pool

        candidate = pool.claim(db, repo_root, base)
        if candidate is not None:
            try:
                pool.rebind(repo_root, candidate, branch, start_sha)
                claimed = candidate
            except (git.GitError, OSError):
                pool.discard(repo_root, candidate)
                claimed = None

    if claimed is not None:
        path = claimed.path
        how = "pool"
    else:
        path = str(worktrees_dir() / _repo_slug(repo_root) / branch)
        if os.path.exists(path):
            raise WorkspaceError(f"{path} already exists; remove it or choose another branch")
        how = git.add_worktree(repo_root, path, branch, start_point)
    git.set_base(repo_root, branch, base)

    ws = Workspace(
        id=f"{_repo_slug(repo_root)}/{name}",
        repo_root=repo_root,
        name=name,
        kind="worktree",
        branch=branch,
        base_branch=base,
        path=path,
        port_base=(
            claimed.port_base if claimed is not None and claimed.port_base is not None
            else _next_port_base(db)
        ),
        tmux_session=_session_name(repo_root, name),
        created_at=time.time(),
    )
    db.add_workspace(ws)
    if run_setup:
        services.up(ws, cfg)  # before setup, so setup commands can reach them

    if claimed is not None:
        from copse import pool

        copied = _copy_local_files(repo_root, path, cfg.copy, overwrite_changed=True)
        if cfg.setup and pool.fingerprint(repo_root, start_sha, cfg) != claimed.fingerprint:
            setup = run_commands(cfg.setup, path, workspace_env(ws))
        else:
            setup = None
        pool.fill_in_background(repo_root)
    else:
        copied = _copy_local_files(repo_root, path, cfg.copy)
        setup = run_commands(cfg.setup, path, workspace_env(ws)) if run_setup and cfg.setup else None
    return Created(ws, how, start_point, copied, setup)


def gh_pr_view(repo_path: str, number: int) -> dict:
    """``gh pr view <number> --json headRefName,baseRefName``, parsed."""
    args = ["gh", "pr", "view", str(number), "--json", "headRefName,baseRefName"]
    try:
        proc = subprocess.run(args, cwd=repo_path, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as e:
        raise WorkspaceError("`gh` (GitHub CLI) is not installed; it's needed for --pr") from e
    except (OSError, subprocess.TimeoutExpired) as e:
        raise WorkspaceError(f"gh pr view {number}: {e}") from e
    if proc.returncode != 0:
        msg = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise WorkspaceError(f"gh pr view {number} failed: {msg}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise WorkspaceError(f"gh pr view {number}: unexpected output: {proc.stdout[:200]!r}") from e


def checkout_for(db: DB, repo_path: str, *, branch: str | None = None,
                 worktree: str | None = None) -> Workspace:
    """The checkout a supervisor should run in for ``branch`` and/or
    ``worktree`` (a path): the worktree where it already is, else a new one.
    Created worktrees are copse's own (like ``copse new``) unless ``worktree``
    names where to put it. The repo's ``.copse`` config applies there either
    way (see ``config.config_root``)."""
    repo_root = git.main_repo_root(repo_path)
    if worktree:
        path = Path(worktree).expanduser().resolve()
        if (path / ".git").exists():
            live = git.current_branch(path)
            if branch and live != branch:
                raise WorkspaceError(f"{path} has {live or 'a detached HEAD'} checked out, not {branch!r}")
            return adopt_root(db, str(path))
        if not branch:
            raise WorkspaceError(f"{path} isn't a worktree yet; pass --branch to create one there")
        cfg = load_repo_config(repo_root)
        base = cfg.base_branch or git.default_branch(repo_root)
        git.add_worktree(repo_root, path, branch, git.resolve_start_point(repo_root, base, cfg.fetch))
        return adopt_root(db, str(path))
    if not branch:
        raise WorkspaceError("give a branch or a worktree path")
    existing = git.worktree_for_branch(repo_root, branch)
    if existing:
        return adopt_root(db, existing)
    return create(db, repo_path, branch, apply_prefix=False).workspace


def checkout_for_target(db: DB, repo_path: str, target: str) -> Workspace:
    """``checkout_for`` for one argument that is a worktree path (absolute,
    starting with . or ~, or an existing directory; a new one is named for its
    folder) or a branch name, which may contain slashes (``fix/foo``)."""
    path = os.path.expanduser(target)
    if not (os.path.isabs(path) or target.startswith((".", "~")) or os.path.exists(path)):
        return checkout_for(db, repo_path, branch=target)
    branch = None if os.path.exists(path) else git.sanitize_branch(os.path.basename(path.rstrip(os.sep)))
    return checkout_for(db, repo_path, branch=branch, worktree=target)


def create_from_pr(
    db: DB,
    repo_path: str,
    number: int,
    *,
    run_setup: bool = True,
) -> Created:
    """A workspace on pull request ``number``'s head branch, based on the PR's
    base branch. The head branch is fetched from origin and checked out
    tracking it, under its own name (no ``branch_prefix``) so pushes update
    the PR."""
    info = gh_pr_view(repo_path, number)
    head, base = info.get("headRefName"), info.get("baseRefName")
    if not head or not base:
        raise WorkspaceError(f"gh pr view {number}: missing headRefName/baseRefName in {info!r}")
    repo_root = git.main_repo_root(repo_path)
    try:
        start = git.fetch_remote_branch(repo_root, head)
    except git.GitError as e:
        raise WorkspaceError(
            f"couldn't fetch PR #{number}'s branch {head!r} from origin "
            f"(PRs from forks aren't supported): {e}"
        ) from e
    return create(db, repo_path, head, base, start=start, run_setup=run_setup, apply_prefix=False)


def adopt_root(db: DB, repo_path: str) -> Workspace:
    """Register an existing checkout (usually the main one) so agents can run
    there. Nothing is created on disk; removing it never deletes files."""
    top = git.toplevel(repo_path)
    existing = db.workspace_by_path(top)
    if existing:
        return existing
    repo_root = git.main_repo_root(repo_path)
    cfg = load_repo_config(repo_root)
    branch = git.current_branch(top) or "HEAD"
    is_main = top == repo_root
    name = ROOT_NAME if is_main else _unique_name(db, repo_root, branch)
    base = git.get_base(repo_root, branch) if branch != "HEAD" else None
    if base is None and not is_main:
        base = cfg.base_branch or git.default_branch(repo_root)
    ws = Workspace(
        id=f"{_repo_slug(repo_root)}/{name}",
        repo_root=repo_root,
        name=name,
        kind="main",
        branch=branch,
        base_branch=base,
        path=top,
        port_base=None,
        tmux_session=_session_name(repo_root, name),
        created_at=time.time(),
    )
    db.add_workspace(ws)
    return ws


def refresh_branch(db: DB, ws: Workspace) -> Workspace:
    """A checkout's branch can change under us (e.g. ``git switch`` in root)."""
    live = git.current_branch(ws.path) or ws.branch
    if live != ws.branch:
        with db.tx() as c:
            c.execute("UPDATE workspaces SET branch=? WHERE id=?", (live, ws.id))
        ws.branch = live
    return ws


# -- removal ---------------------------------------------------------------


@dataclass
class Removed:
    branch_deleted: bool
    branch_note: str | None
    teardown: SetupResult | None


def remove(db: DB, ws: Workspace, *, force: bool = False, delete_branch: bool = False,
           keep_session: bool = False) -> Removed:
    """``keep_session`` leaves the workspace's tmux session running: for a
    caller that itself runs in it (the pipeline's reviewer), which killing
    the session would take down mid-cleanup."""
    if ws.kind == "main":
        if not keep_session:
            tmux.kill_session(ws.tmux_session)
        db.delete_workspace(ws.id)
        return Removed(False, "existing checkout left untouched", None)

    exists = os.path.isdir(ws.path)
    if exists and not force:
        dirty = git.dirty_files(ws.path)
        if dirty:
            shown = ", ".join(dirty[:8]) + (" ..." if len(dirty) > 8 else "")
            raise WorkspaceError(
                f"{ws.name} has uncommitted changes ({shown}). "
                "Commit them, or pass --force to discard."
            )

    cfg = load_repo_config(ws.repo_root)
    teardown = None
    if exists and cfg.teardown:
        teardown = run_commands(cfg.teardown, ws.path, workspace_env(ws))
        if not teardown.ok and not force:
            raise WorkspaceError(f"teardown failed; fix it or pass --force:\n{teardown.log}")

    services.down(ws, cfg)
    if not keep_session:
        tmux.kill_session(ws.tmux_session)
    if exists:
        git.remove_worktree(ws.repo_root, ws.path, force=force)
    else:
        git.run(["worktree", "prune"], ws.repo_root, check=False)

    deleted, note = False, None
    if delete_branch:
        try:
            git.delete_branch(ws.repo_root, ws.branch, force=force)
            deleted = True
        except git.GitError as e:
            note = f"kept branch {ws.branch}: {e} (use --force to delete anyway)"
    else:
        note = f"branch {ws.branch} kept"
    db.delete_workspace(ws.id)
    return Removed(deleted, note, teardown)


# -- review and integration ------------------------------------------------


def require_base(ws: Workspace) -> str:
    if not ws.base_branch:
        raise WorkspaceError(f"{ws.name} has no base branch to compare against")
    return ws.base_branch


@dataclass
class SyncResult:
    status: str  # "skipped", "up_to_date", "synced" or "conflict"
    new_sha: str | None = None
    conflicts: list[str] | None = None
    old_sha: str | None = None   # HEAD before a "synced" merge


def sync_with_base(ws: Workspace) -> SyncResult:
    """Merge the branch's local base into it before merge gates run, so a
    passing check reflects the code as it will actually be merged. Only acts
    on a clean worktree; a dirty one is left for the gates to report as
    usual ("skipped"). Compares against the local base branch (what
    merge_back targets), never origin."""
    if git.dirty_files(ws.path):
        return SyncResult("skipped")
    base = require_base(ws)
    behind, _ahead = git.ahead_behind(ws.path, base)
    if behind == 0:
        return SyncResult("up_to_date")
    before = git.out(["rev-parse", "HEAD"], ws.path)
    new_sha, conflicts = git.merge_local_base(ws.path, base)
    if conflicts:
        return SyncResult("conflict", conflicts=conflicts)
    if new_sha == before:
        return SyncResult("up_to_date")
    return SyncResult("synced", new_sha=new_sha, old_sha=before)


def pull_request(ws: Workspace, title: str | None = None, draft: bool = False) -> str:
    """Push, then open a PR with ``gh`` when available, else return the
    compare URL for the browser."""
    base = require_base(ws)
    git.push(ws.path, ws.branch)
    if shutil.which("gh"):
        args = ["gh", "pr", "create", "--base", base, "--head", ws.branch]
        args += ["--title", title, "--body", ""] if title else ["--fill"]
        if draft:
            args.append("--draft")
        proc = subprocess.run(args, cwd=ws.path, capture_output=True, text=True)
        if proc.returncode == 0:
            return proc.stdout.strip()
        existing = subprocess.run(
            ["gh", "pr", "view", ws.branch, "--json", "url", "-q", ".url"],
            cwd=ws.path, capture_output=True, text=True,
        )
        if existing.returncode == 0 and existing.stdout.strip():
            return existing.stdout.strip()
    web = git.remote_web_url(ws.repo_root)
    if not web:
        raise WorkspaceError("pushed, but couldn't work out a web URL for origin")
    return f"{web}/compare/{base}...{ws.branch}?expand=1"


def merge_back(db: DB, ws: Workspace, squash: bool = False) -> str:
    """Merge the workspace branch into its base, in whichever checkout has the
    base branch checked out (usually the main one)."""
    base = require_base(ws)
    target = git.worktree_for_branch(ws.repo_root, base)
    if not target:
        raise WorkspaceError(
            f"{base!r} isn't checked out anywhere; check it out in {ws.repo_root} first"
        )
    if git.dirty_files(ws.path):
        raise WorkspaceError(f"{ws.name} has uncommitted changes; commit them first")
    git.merge_into(ws.repo_root, target, ws.branch, squash)
    return target
