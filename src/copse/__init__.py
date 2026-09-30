"""copse: run CLI coding agents in tmux, each isolated on its own git worktree."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("copse-agents")
except PackageNotFoundError:  # running from a checkout that was never installed
    __version__ = "unknown"
