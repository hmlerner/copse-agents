"""``copse account``: hand the command line to an installed account plugin.

copse itself has no accounts. ``copse account [args...]`` passes its
arguments, untouched, to the plugin installed in the ``copse.account``
group (a factory ``make(repo_root) -> AccountPlugin | None``; see
``copse.plugins`` for how one is selected) and exits with what it returns.
Without one it says that copse Pro isn't installed and exits 0.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from copse import plugins
from copse.config import RepoConfig

log = logging.getLogger(__name__)

GROUP = plugins.ACCOUNT
NOT_INSTALLED = ("copse Pro isn't installed: `copse account` has nothing to do. "
                 "Install it next to copse (uv tool install copse-agents --with <plugin>).")


class AccountPlugin(ABC):
    """What an account plugin implements."""

    @abstractmethod
    def run(self, args: list[str]) -> int | None:
        """Handle ``copse account <args>``; return the exit code (None: 0)."""


def plugin(cfg: RepoConfig, repo_root: str) -> AccountPlugin | None:
    return plugins.select(GROUP, cfg, repo_root)  # type: ignore[return-value]


def run(cfg: RepoConfig, repo_root: str, args: list[str], echo=print) -> int:
    """Run ``copse account args`` through the plugin; the exit code."""
    p = plugin(cfg, repo_root)
    if p is None:
        echo(NOT_INSTALLED)
        return 0
    try:
        code = p.run(list(args))
    except Exception as e:
        log.exception("copse: the account plugin failed")
        echo(f"the account plugin failed: {e}")
        return 1
    return int(code or 0)


__all__ = ["GROUP", "NOT_INSTALLED", "AccountPlugin", "plugin", "run"]
