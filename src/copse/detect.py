"""What ``copse init`` can tell about a repo without asking.

Looks at the lockfiles and manifests at the repo root and suggests the
``setup`` commands a fresh worktree needs, the ``checks`` that must pass
before a branch merges, and the git-ignored env files to ``copy`` into each
worktree. It only reads files; nothing is installed or run (apart from one
``git check-ignore``).
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# The test script `npm init` writes; it always fails, so it's no check.
NPM_PLACEHOLDER = "no test specified"

ENV_FILES = (".env", ".env.local", ".env.development", ".env.development.local", ".env.test.local")
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")


@dataclass
class Detected:
    stacks: list[str] = field(default_factory=list)   # e.g. "Python (uv)", for the summary
    setup: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    copy: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)    # things worth saying that init doesn't write


def _python(root: Path, d: Detected) -> None:
    pyproject = root / "pyproject.toml"
    has_reqs = (root / "requirements.txt").is_file()
    if not (pyproject.is_file() or has_reqs or (root / "setup.py").is_file()):
        return
    text = pyproject.read_text(encoding="utf-8", errors="replace") if pyproject.is_file() else ""
    uses_pytest = ("pytest" in text or (root / "pytest.ini").is_file() or (root / "conftest.py").is_file()
                   or (root / "tests").is_dir() or (root / "test").is_dir())
    if (root / "uv.lock").is_file():
        d.stacks.append("Python (uv)")
        d.setup.append("uv sync")
        if uses_pytest:
            d.checks.append("uv run pytest -q")
    elif (root / "poetry.lock").is_file():
        d.stacks.append("Python (Poetry)")
        d.setup.append("poetry install")
        if uses_pytest:
            d.checks.append("poetry run pytest -q")
    else:
        d.stacks.append("Python")
        if uses_pytest:
            d.checks.append("python -m pytest -q")
            d.notes.append("No uv.lock or poetry.lock: add a `setup` that builds the worktree's "
                           "environment if `python -m pytest` needs one.")


def _node(root: Path, d: Detected) -> None:
    pkg = root / "package.json"
    if not pkg.is_file():
        return
    for lock, pm, install in (("pnpm-lock.yaml", "pnpm", "pnpm install --frozen-lockfile"),
                              ("yarn.lock", "yarn", "yarn install --frozen-lockfile"),
                              ("bun.lockb", "bun", "bun install"),
                              ("bun.lock", "bun", "bun install"),
                              ("package-lock.json", "npm", "npm ci")):
        if (root / lock).is_file():
            break
    else:
        pm, install = "npm", "npm install"
    d.stacks.append(f"Node ({pm})")
    d.setup.append(install)
    try:
        scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts") or {}
    except (json.JSONDecodeError, AttributeError):
        scripts = {}
    if not isinstance(scripts, dict):
        scripts = {}
    test = scripts.get("test")
    if isinstance(test, str) and test.strip() and NPM_PLACEHOLDER not in test:
        d.checks.append(f"{pm} test")
    if "typecheck" in scripts:
        d.checks.append(f"{pm} run typecheck")


def _rust(root: Path, d: Detected) -> None:
    if (root / "Cargo.toml").is_file():
        d.stacks.append("Rust")
        d.checks.append("cargo test -q")


def _go(root: Path, d: Detected) -> None:
    if (root / "go.mod").is_file():
        d.stacks.append("Go")
        d.setup.append("go mod download")
        d.checks.append("go test ./...")


def _ruby(root: Path, d: Detected) -> None:
    if not (root / "Gemfile").is_file():
        return
    d.stacks.append("Ruby")
    d.setup.append("bundle install")
    if (root / "spec").is_dir():
        d.checks.append("bundle exec rspec")
    elif (root / "Rakefile").is_file():
        d.checks.append("bundle exec rake test")


def _make(root: Path, d: Detected) -> None:
    """``make test`` only when nothing else gave a check: a Makefile's test
    target usually wraps the same suite."""
    makefile = root / "Makefile"
    if d.checks or not makefile.is_file():
        return
    if re.search(r"^test\s*:", makefile.read_text(encoding="utf-8", errors="replace"), re.M):
        d.checks.append("make test")


def _env_files(root: Path, d: Detected) -> None:
    """Env files that exist and git ignores: a new worktree wouldn't have them."""
    present = [name for name in ENV_FILES if (root / name).is_file()]
    if not present:
        return
    proc = subprocess.run(["git", "check-ignore", *present], cwd=root, capture_output=True, text=True)
    ignored = set(proc.stdout.split())
    d.copy.extend(name for name in present if name in ignored)


def _compose(root: Path, d: Detected) -> None:
    found = next((name for name in COMPOSE_FILES if (root / name).is_file()), None)
    if found:
        d.notes.append(f"{found} found: parallel workers share one set of containers. "
                       "Per-worktree databases are `services` in .copse/config.json (copse Pro).")


def detect(repo_root: str | Path) -> Detected:
    root = Path(repo_root)
    d = Detected()
    for probe in (_python, _node, _rust, _go, _ruby, _make, _env_files, _compose):
        probe(root, d)
    if not d.checks:
        d.notes.append("No test command found: add one to `checks` so nothing merges unverified.")
    return d
