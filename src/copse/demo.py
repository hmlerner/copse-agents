"""``copse demo``: a tiny repo with two failing test files and a goal, so a
first run shows the whole loop in a few minutes: two workers in parallel,
a review on each branch, gated merges, and milestones that turn green only
when their check command passes.

The repo is plain Python (unittest, no dependencies) under
``~/.copse/demo/``; nothing is created where the command runs.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from copse.config import copse_home

FILES = {
    "README.md": "# copse demo\n\nA small text toolkit. Run the tests with `python3 -m unittest -q`.\n",
    "textkit/__init__.py": "",
    "textkit/slug.py": '''def slugify(text: str) -> str:
    """Lowercase ``text``, turn runs of anything but letters and digits into
    one hyphen, and trim hyphens from the ends: "Hello, World!" -> "hello-world"."""
    raise NotImplementedError
''',
    "textkit/wrap.py": '''def wrap(text: str, width: int) -> list[str]:
    """Split ``text`` into lines of at most ``width`` characters, breaking only
    between words (a word longer than ``width`` gets a line to itself)."""
    raise NotImplementedError
''',
    "tests/__init__.py": "",
    "tests/test_slug.py": '''import unittest

from textkit.slug import slugify


class SlugTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")

    def test_runs_and_ends(self):
        self.assertEqual(slugify("  --copse  demo 2--  "), "copse-demo-2")

    def test_empty(self):
        self.assertEqual(slugify("!!!"), "")
''',
    "tests/test_wrap.py": '''import unittest

from textkit.wrap import wrap


class WrapTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(wrap("the quick brown fox", 10), ["the quick", "brown fox"])

    def test_long_word(self):
        self.assertEqual(wrap("a supercalifragilistic b", 5), ["a", "supercalifragilistic", "b"])

    def test_empty(self):
        self.assertEqual(wrap("", 5), [])
''',
    ".gitignore": "__pycache__/\n",
}

GOALS = """# Finish the textkit helpers

This is copse's demo. Show the person how copse works: assign each milestone
to its own worker at the same time (weight "light"), let copse review and
merge both branches, then call check_milestone. Name files in task briefs
relative to the repo root: each worker has its own worktree. Narrate briefly
as you go.

## slugify works
check: python3 -m unittest tests.test_slug -q

## wrap works
check: python3 -m unittest tests.test_wrap -q
"""


def _config(local: bool) -> dict:
    cfg = {
        "checks": ["python3 -m unittest -q"],
        "max_agents": 2,
        "fetch": False,
        # The demo shows the whole loop, so approved branches merge on their own.
        "auto_merge_default_branch": True,
    }
    if local:
        cfg.update(default_agent="developer-local", review_profile="reviewer-local",
                   routing={"light": ["developer-local"]})
    else:
        # Fast and predictable: no local model, even when one is configured.
        cfg.update(local_models=False, routing={"light": ["developer"]})
    return cfg


def create(local: bool = False) -> Path:
    """A fresh demo repo with one commit; returns its path."""
    base = copse_home() / "demo"
    base.mkdir(parents=True, exist_ok=True)
    # A new name every time, so no earlier demo's sessions or branches show up.
    stamp = time.strftime("%m%d-%H%M%S")
    root, n = base / f"textkit-{stamp}", 1
    while root.exists():
        n += 1
        root = base / f"textkit-{stamp}-{n}"
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (root / ".copse").mkdir()
    (root / ".copse" / "config.json").write_text(json.dumps(_config(local), indent=2) + "\n")
    (root / ".copse" / "goals.md").write_text(GOALS)
    git = ["git", "-c", "user.name=copse demo", "-c", "user.email=demo@copse.invalid"]
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "textkit: stubs and tests"], cwd=root, check=True)
    return root
