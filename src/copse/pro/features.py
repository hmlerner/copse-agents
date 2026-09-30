"""The features of a task: what hosted learning may ever see of it.

What is kept is deliberately minimal, because this is what leaves the
machine (see :mod:`copse.pro.learning`): a coarse kind guessed from keywords,
a size bucket, and languages from the declared files' extensions. Never the
task text, file names or contents. This module has no learner in it: how a
task went and what to pick from it is the learner's business, not the
featurizer's.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Keyword -> kind. The kind with the most whole-word hits wins; ties go to the
# earlier entry. Only these keywords ever leave the task text, as a category.
KIND_WORDS: dict[str, tuple[str, ...]] = {
    "bugfix": ("fix", "fixes", "bug", "bugs", "crash", "regression", "broken", "error", "leak"),
    "refactor": ("refactor", "rename", "cleanup", "simplify", "extract", "restructure", "reorganize"),
    "docs": ("docs", "doc", "documentation", "readme", "docstring", "changelog"),
    "feature": ("add", "implement", "build", "feature", "support", "introduce", "create", "new"),
    "test": ("test", "tests", "coverage", "pytest"),
}
KINDS = (*KIND_WORDS, "other")
SIZES = ("small", "medium", "large")

LANGS = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
    ".rb": "ruby", ".swift": "swift", ".c": "c", ".h": "c", ".cpp": "cpp", ".cs": "csharp",
    ".php": "php", ".sh": "shell", ".sql": "sql", ".md": "markdown", ".html": "html",
    ".css": "css", ".json": "json", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml",
}


@dataclass(frozen=True)
class Features:
    kind: str = "other"
    size: str = "medium"
    langs: tuple[str, ...] = ()

    @property
    def context(self) -> tuple[str, str]:
        return (self.kind, self.size)


def featurize(task: str | None, files=None) -> Features:
    """The features of a task: kind from keywords, size bucket from the task's
    length and the number of declared ``files``, languages from those files'
    extensions. Deterministic; nothing but these categories is kept."""
    text = task or ""
    files = [f for f in (files or []) if isinstance(f, str)]
    words = re.findall(r"[a-z]+", text.lower())
    best, best_hits = "other", 0
    for kind, keywords in KIND_WORDS.items():
        hits = sum(w in keywords for w in words)
        if hits > best_hits:
            best, best_hits = kind, hits
    n = len(text)
    if n > 1500 or len(files) >= 6:
        size = "large"
    elif n < 400 and len(files) <= 2:
        size = "small"
    else:
        size = "medium"
    langs = sorted({LANGS[ext] for f in files for ext in _extensions(f)})
    return Features(best, size, tuple(langs))


def _extensions(glob: str) -> list[str]:
    m = re.search(r"(\.[A-Za-z0-9]+)(?:[}\]*]|$)", glob.rsplit("/", 1)[-1])
    return [m.group(1).lower()] if m and m.group(1).lower() in LANGS else []


__all__ = ["KINDS", "KIND_WORDS", "LANGS", "SIZES", "Features", "featurize"]
