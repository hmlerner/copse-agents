"""Permission policy: copse decides a worker's tool-permission requests from
the structured request the agent CLI hands its hook (tool name and input),
never from the terminal.

Provider-neutral core. A provider's hook turns its own payload into a
``Request`` (``from_claude`` for Claude Code's ``PermissionRequest`` hook;
Codex and Antigravity adapters slot in beside it) and calls ``decide``, which
returns ``allow``, ``deny`` or ``ask`` with a reason. ``ask`` means "no
decision": the CLI shows its normal prompt to the person.

Rules are data::

    {"id": "3f2a91c0", "kind": "bash", "match": "npm test", "match_type": "exact",
     "decision": "allow", "source": "user", "created": 1767225600.0}

``kind`` is read | write | edit | bash | fetch | mcp | other. ``match_type`` is
``exact``, ``prefix`` or ``glob`` (``fnmatch``: ``*`` also crosses ``/``; no
regular expressions). What a rule is matched against depends on the kind: a
bash rule against the whole command; a path rule against the resolved
absolute path, the path as given, and the path relative to the worktree and
the repo root (``~`` in a rule means the home directory); a fetch rule
against the URL; mcp/other against the tool name. The built-in defaults also
use a few named matchers (``tracked``, ``check``, ``git-readonly``,
``git-push``, ``git-force``) that are code, not patterns.

Precedence: any matching deny wins; else any matching allow; else ask.
Specificity never matters. An allow rule for bash only ever applies to a
single simple command (letters, digits, spaces and ``_-./:=@%+,`` only): a
command with any other character (``;``, ``|``, ``&``, ``$``, quotes, globs,
redirections, newlines...) is never auto-allowed. Anything unknown or
unparseable is ask.

Where rules come from (``source``): ``default`` (built in, below), ``repo``
(``<repo>/.copse/permissions.json``: only its deny rules are used, so a repo
can narrow but never broaden), ``user`` (``copse permissions allow|deny``) and
``learned`` (a suggestion the person accepted). User and learned rules live in
``~/.copse/permissions.json`` with the approval counts learning keeps.

Learning: when a request fell to ask and the person approved it in the CLI
(for Claude Code: the tool's PostToolUse arrives for the same tool_use_id),
copse counts that approval for the request's exact pattern. Nothing becomes a
rule on its own: a pattern approved at least SUGGEST_AFTER times is shown by
``copse permissions suggestions`` and becomes a rule only on ``accept``.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

from copse.config import CONFIG_DIR, config_root, copse_home

KINDS = ("read", "write", "edit", "bash", "fetch", "mcp", "other")
PATH_KINDS = ("read", "write", "edit")
DECISIONS = ("allow", "deny", "ask")
MATCH_TYPES = ("exact", "prefix", "glob")
BUILTIN_MATCHERS = ("tracked", "check", "git-readonly", "git-push", "git-force")
SOURCES = ("default", "repo", "user", "learned")
SUGGEST_AFTER = 2
FILE = "permissions.json"

# A command an allow rule may cover: one simple command, nothing the shell
# would treat specially.
SIMPLE_COMMAND = re.compile(r"[A-Za-z0-9_\-./:=@%+, ]+")
READONLY_GIT = ("status", "diff", "log", "show")
# Options that make an otherwise read-only git command write or read files
# outside the repo.
GIT_UNSAFE_OPTIONS = ("--output", "--ext-diff", "--no-index", "-O")
FORCE_OPTIONS = ("--force", "--force-with-lease", "--force-if-includes")


@dataclass(frozen=True)
class Request:
    """One tool-permission request, as any provider's hook describes it."""
    provider: str
    kind: str
    tool: str
    cwd: str = ""
    worktree: str = ""
    repo_root: str = ""
    command: str | None = None
    path: str | None = None
    url: str | None = None

    def target(self) -> str:
        return self.command or self.path or self.url or self.tool

    def summary(self, width: int = 200) -> str:
        text = f"{self.tool}: {self.target()}" if self.target() != self.tool else self.tool
        text = " ".join(text.split())
        return text if len(text) <= width else text[: width - 1] + "…"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Request":
        names = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in names})


@dataclass(frozen=True)
class Rule:
    kind: str
    match: str
    match_type: str
    decision: str
    source: str = "user"
    created: float = 0.0
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            object.__setattr__(self, "id", rule_id(self.kind, self.match_type, self.match, self.decision))

    def describe(self) -> str:
        if self.match_type in BUILTIN_MATCHERS:
            return self.match
        return f"{self.kind} {self.match_type} {self.match!r}"

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "match": self.match, "match_type": self.match_type,
                "decision": self.decision, "source": self.source, "created": self.created}

    @classmethod
    def from_dict(cls, data: dict, source: str | None = None) -> "Rule | None":
        """A rule read from a file, or None if it isn't a valid one."""
        try:
            kind, match = str(data["kind"]), str(data["match"])
            match_type = str(data.get("match_type", "exact"))
            decision = str(data["decision"])
        except (KeyError, TypeError):
            return None
        if kind not in KINDS or match_type not in MATCH_TYPES or decision not in ("allow", "deny") or not match:
            return None
        src = source or str(data.get("source", "user"))
        if src not in ("user", "learned", "repo"):
            src = "user"
        try:
            created = float(data.get("created") or 0)
        except (TypeError, ValueError):
            created = 0.0
        return cls(kind, match, match_type, decision, src, created)


@dataclass(frozen=True)
class Decision:
    decision: str  # allow | deny | ask
    reason: str
    rule: Rule | None = None


def rule_id(kind: str, match_type: str, match: str, decision: str) -> str:
    return hashlib.sha1(f"{kind}\0{match_type}\0{match}\0{decision}".encode()).hexdigest()[:8]


def _default(id_: str, kind: str, match: str, match_type: str, decision: str) -> Rule:
    return Rule(kind, match, match_type, decision, "default", 0.0, id_)


DEFAULT_RULES: tuple[Rule, ...] = (
    _default("d-tracked", "read", "a file git tracks in the worktree or repo root", "tracked", "allow"),
    _default("d-checks", "bash", "exactly one of the repo's `checks` commands", "check", "allow"),
    _default("d-git-ro", "bash", "git status / diff / log / show", "git-readonly", "allow"),
    _default("d-git-push", "bash", "git push", "git-push", "deny"),
    _default("d-git-force", "bash", "a git command with a force flag", "git-force", "deny"),
    _default("d-ssh", "read", "~/.ssh*", "glob", "deny"),
    _default("d-aws", "read", "~/.aws*", "glob", "deny"),
    _default("d-gnupg", "read", "~/.gnupg*", "glob", "deny"),
    _default("d-env", "read", "*/.env*", "glob", "deny"),
    _default("d-env-rel", "read", ".env*", "glob", "deny"),
)


# -- providers: Claude Code ------------------------------------------------------------------

CLAUDE_KINDS = {
    "Read": "read", "Glob": "read", "Grep": "read", "LS": "read", "NotebookRead": "read",
    "Write": "write",
    "Edit": "edit", "MultiEdit": "edit", "NotebookEdit": "edit",
    "Bash": "bash",
    "WebFetch": "fetch", "WebSearch": "fetch",
}


def from_claude(payload: dict, worktree: str = "", repo_root: str = "") -> Request | None:
    """Claude Code's PermissionRequest payload as a Request; None when it
    names no tool (nothing to decide)."""
    tool = payload.get("tool_name")
    if not isinstance(tool, str) or not tool:
        return None
    ti = payload.get("tool_input")
    ti = ti if isinstance(ti, dict) else {}
    cwd = str(payload.get("cwd") or worktree or "")
    kind = "mcp" if tool.startswith("mcp__") else CLAUDE_KINDS.get(tool, "other")
    command = path = url = None
    if kind == "bash":
        command = ti.get("command") if isinstance(ti.get("command"), str) else None
    elif kind in PATH_KINDS:
        raw = ti.get("file_path") or ti.get("notebook_path") or ti.get("path")
        if raw is None and tool in ("Glob", "Grep", "LS"):
            raw = cwd  # searches the working directory
        if isinstance(raw, str) and raw:
            raw = os.path.expanduser(raw)
            path = raw if os.path.isabs(raw) else os.path.join(cwd or os.getcwd(), raw)
    elif kind == "fetch":
        url = ti.get("url") if isinstance(ti.get("url"), str) else None
    return Request("claude", kind, tool, cwd, worktree, repo_root, command, path, url)


def claude_output(decision: Decision) -> dict | None:
    """What Claude Code's PermissionRequest hook prints; None (no output)
    for ask, so the normal prompt is shown."""
    if decision.decision not in ("allow", "deny"):
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PermissionRequest",
        "decision": {"behavior": decision.decision, "message": f"copse: {decision.reason}"},
    }}


# -- matching ---------------------------------------------------------------------------------


def simple_argv(command: str | None) -> list[str] | None:
    """The words of a single simple command with no shell metacharacters,
    else None."""
    if not command or not SIMPLE_COMMAND.fullmatch(command):
        return None
    argv = command.split()
    return argv or None


def _inside(path: str, root: str) -> str | None:
    """``path``'s location relative to ``root`` (both resolved), if inside."""
    if not root:
        return None
    rr = os.path.realpath(root)
    if path.startswith(rr + os.sep):
        return os.path.relpath(path, rr)
    return None


def _roots(req: Request) -> list[str]:
    return list(dict.fromkeys(r for r in (req.worktree, req.repo_root) if r))


def path_candidates(req: Request, resolved_only: bool = False) -> list[str]:
    """The forms of ``req.path`` a rule may match. ``resolved_only`` (for
    allows) leaves out the name as given: a symlink under an allowed folder
    can lead anywhere, so only where the path really goes counts. Denies
    match either."""
    if not req.path:
        return []
    real = os.path.realpath(req.path)
    given = os.path.join(os.path.realpath(os.path.dirname(os.path.abspath(req.path))),
                         os.path.basename(req.path))
    paths = [real] if resolved_only else [real, os.path.abspath(req.path), given]
    out = list(paths)
    for p in paths:
        for root in _roots(req):
            rel = _inside(p, root)
            if rel:
                out.append(rel)
    return list(dict.fromkeys(out))


def candidates(req: Request) -> list[str]:
    if req.kind == "bash":
        return [req.command.strip()] if req.command and req.command.strip() else []
    if req.kind in PATH_KINDS:
        return path_candidates(req)
    if req.kind == "fetch":
        return [req.url] if req.url else []
    return [req.tool] if req.tool else []


def is_tracked(path: str | None, roots: list[str]) -> bool:
    """``path`` (symlinks resolved) is a regular file inside one of ``roots``,
    not under .git, and git tracks exactly that path there."""
    if not path:
        return False
    real = os.path.realpath(path)
    if not os.path.isfile(real):
        return False
    for root in roots:
        rel = _inside(real, root)
        if not rel or ".git" in Path(rel).parts:
            continue
        try:
            res = subprocess.run(
                ["git", "-C", os.path.realpath(root), "--literal-pathspecs", "ls-files", "-z", "--", rel],
                capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if res.returncode == 0 and rel in res.stdout.split("\0"):
            return True
    return False


def _segments(command: str) -> list[list[str]]:
    """Each simple command in ``command``, split at the shell's separators,
    as words (best effort: only used to find things to deny)."""
    out = []
    for part in re.split(r"[;&|\n()`]|\$\(", command):
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
            words = words[1:]
        if words:
            out.append(words)
    return out


def _git_calls(command: str) -> list[tuple[str, list[str]]]:
    """(subcommand, its arguments) for every git invocation in ``command``."""
    calls = []
    for words in _segments(command):
        for i, w in enumerate(words):
            if os.path.basename(w) != "git":
                continue
            rest, j = words[i + 1:], 0
            while j < len(rest) and rest[j].startswith("-"):
                j += 2 if rest[j] in ("-C", "-c") else 1  # global options
            if j < len(rest):
                calls.append((rest[j], rest[j + 1:]))
            break
    return calls


def _is_force(arg: str) -> bool:
    return (arg.startswith(FORCE_OPTIONS)
            or bool(re.fullmatch(r"-[A-Za-z]*f[A-Za-z]*", arg)))


def _builtin_matches(rule: Rule, req: Request, checks: list[str]) -> bool:
    if rule.match_type == "tracked":
        return req.kind == "read" and is_tracked(req.path, _roots(req))
    command = (req.command or "").strip()
    if not command:
        return False
    if rule.match_type == "check":
        return simple_argv(command) is not None and command in [c.strip() for c in checks]
    if rule.match_type == "git-readonly":
        argv = simple_argv(command)
        return bool(argv and len(argv) >= 2 and argv[0] == "git" and argv[1] in READONLY_GIT
                    and not any(a.startswith(GIT_UNSAFE_OPTIONS) or _is_force(a) for a in argv[2:]))
    if rule.match_type == "git-push":
        return any(sub == "push" for sub, _ in _git_calls(command))
    if rule.match_type == "git-force":
        return any(_is_force(a) for _, args in _git_calls(command) for a in args)
    return False


def rule_matches(rule: Rule, req: Request, checks: list[str] | None = None) -> bool:
    if rule.kind != req.kind:
        return False
    if rule.match_type in BUILTIN_MATCHERS:
        return _builtin_matches(rule, req, checks or [])
    if req.kind == "bash" and rule.decision == "allow" and simple_argv(req.command) is None:
        return False  # never auto-allow a compound or quoted command
    value = os.path.expanduser(rule.match) if req.kind in PATH_KINDS else rule.match
    forms = (path_candidates(req, resolved_only=rule.decision == "allow")
             if req.kind in PATH_KINDS else candidates(req))
    for c in forms:
        if rule.match_type == "exact" and c == value:
            return True
        if rule.match_type == "prefix" and c.startswith(value):
            return True
        if rule.match_type == "glob" and fnmatch.fnmatchcase(c, value):
            return True
    return False


# -- the engine -------------------------------------------------------------------------------


def all_rules(repo_root: str | None = None) -> list[Rule]:
    """Every rule in force: defaults, the repo's denies, the user's and
    learned rules."""
    return [*DEFAULT_RULES, *(repo_rules(repo_root) if repo_root else []), *load_store().rules]


def decide(req: Request | None, *, checks: list[str] | None = None,
           rules: list[Rule] | None = None) -> Decision:
    """The policy's answer for ``req``: deny > allow > ask."""
    if req is None or req.kind not in KINDS:
        return Decision("ask", "copse couldn't read the request")
    if rules is None:
        rules = all_rules(req.repo_root or None)
    for wanted in ("deny", "allow"):
        for rule in rules:
            if rule.decision == wanted and rule_matches(rule, req, checks):
                verb = "denied" if wanted == "deny" else "allowed"
                return Decision(wanted, f"{verb} by {rule.source} rule {rule.id} ({rule.describe()})", rule)
    if req.kind == "bash" and req.command and simple_argv(req.command) is None:
        why = "the command isn't a single simple command, so no allow rule applies"
    elif req.kind == "read" and req.path:
        why = "not a file git tracks in the worktree, and no rule allows it"
    else:
        why = "no rule allows or denies it"
    return Decision("ask", f"{why}; asking the user")


# -- storage ----------------------------------------------------------------------------------


def store_path() -> Path:
    return copse_home() / FILE


def repo_rules_path(repo_root: str) -> Path:
    return config_root(repo_root) / CONFIG_DIR / FILE


def repo_rules(repo_root: str) -> list[Rule]:
    """The repo's own rules: only denies (a repo may narrow, never broaden)."""
    try:
        data = json.loads(repo_rules_path(repo_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = data.get("rules") if isinstance(data, dict) else None
    out = []
    for row in rows if isinstance(rows, list) else []:
        rule = Rule.from_dict(row, source="repo") if isinstance(row, dict) else None
        if rule and rule.decision == "deny":
            out.append(rule)
    return out


@dataclass
class Store:
    rules: list[Rule] = field(default_factory=list)
    # pattern id -> {"kind", "match", "count", "last"}: approvals the person gave
    approvals: dict[str, dict] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({"version": 1, "rules": [r.to_dict() for r in self.rules],
                           "approvals": self.approvals}, indent=2) + "\n"


def load_store() -> Store:
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Store()
    if not isinstance(data, dict):
        return Store()
    rules = [r for r in (Rule.from_dict(d) for d in data.get("rules") or [] if isinstance(d, dict))
             if r and r.source in ("user", "learned")]
    approvals = data.get("approvals")
    approvals = {k: v for k, v in approvals.items() if isinstance(v, dict)} if isinstance(approvals, dict) else {}
    return Store(rules, approvals)


@contextmanager
def editing() -> Iterator[Store]:
    """Load, let the caller change, and write back the user's store, under a
    lock so concurrent hooks don't lose each other's writes."""
    import fcntl

    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        store = load_store()
        yield store
        tmp = path.with_suffix(".tmp")
        tmp.write_text(store.to_json(), encoding="utf-8")
        os.replace(tmp, path)


def add_rule(kind: str, match: str, decision: str, match_type: str = "exact",
             source: str = "user") -> Rule:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}: one of {', '.join(KINDS)}")
    if match_type not in MATCH_TYPES:
        raise ValueError(f"unknown match type {match_type!r}: one of {', '.join(MATCH_TYPES)}")
    if decision not in ("allow", "deny"):
        raise ValueError("decision must be allow or deny")
    if not match:
        raise ValueError("empty match")
    rule = Rule(kind, match, match_type, decision, source, time.time())
    with editing() as store:
        store.rules = [r for r in store.rules if r.id != rule.id] + [rule]
    return rule


def forget(id_: str) -> str | None:
    """Remove the user or learned rule ``id_``, or the approval count behind
    suggestion ``id_``; what was removed, or None if neither exists."""
    with editing() as store:
        found = next((r for r in store.rules if r.id == id_), None)
        store.rules = [r for r in store.rules if r.id != id_]
        approval = store.approvals.pop(id_, None)
    if found:
        return f"{found.decision} {found.describe()} ({found.source})"
    if approval:
        return f"approvals of {approval.get('kind')} {approval.get('match')!r}"
    return None


def reset() -> None:
    """Back to the defaults: no user or learned rules, no approval counts."""
    with editing() as store:
        store.rules, store.approvals = [], {}


# -- learning ---------------------------------------------------------------------------------


def learnable_pattern(req: Request) -> tuple[str, str] | None:
    """The exact (kind, match) an approval of ``req`` counts toward, or None
    when an allow rule for it could never apply."""
    if req.kind == "bash":
        argv = simple_argv(req.command)
        return ("bash", " ".join(argv)) if argv and req.command.strip() == " ".join(argv) else None
    if req.kind in PATH_KINDS:
        if not req.path:
            return None
        real = os.path.realpath(req.path)
        for root in _roots(req):
            rel = _inside(real, root)
            if rel:
                return req.kind, rel
        return req.kind, real
    if req.kind == "fetch":
        return ("fetch", req.url) if req.url else None
    return (req.kind, req.tool) if req.tool else None


def record_approval(req: Request) -> str | None:
    """Count the person's approval of ``req``; the pattern's id, if any."""
    pattern = learnable_pattern(req)
    if pattern is None:
        return None
    kind, match = pattern
    pid = rule_id(kind, "exact", match, "allow")
    with editing() as store:
        entry = store.approvals.get(pid) or {"kind": kind, "match": match, "count": 0}
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["last"] = time.time()
        store.approvals[pid] = entry
    return pid


@dataclass(frozen=True)
class Suggestion:
    id: str
    kind: str
    match: str
    count: int


def suggestions(store: Store | None = None) -> list[Suggestion]:
    """Patterns approved at least SUGGEST_AFTER times that no rule covers yet."""
    store = store or load_store()
    have = {r.id for r in store.rules}
    out = []
    for pid, e in store.approvals.items():
        if pid in have or int(e.get("count", 0)) < SUGGEST_AFTER:
            continue
        if e.get("kind") in KINDS and e.get("match"):
            out.append(Suggestion(pid, str(e["kind"]), str(e["match"]), int(e["count"])))
    return sorted(out, key=lambda s: (-s.count, s.kind, s.match))


def accept(id_: str) -> Rule | None:
    """Turn suggestion ``id_`` into a learned allow rule."""
    s = next((s for s in suggestions() if s.id == id_), None)
    if s is None:
        return None
    return add_rule(s.kind, s.match, "allow", "exact", source="learned")
