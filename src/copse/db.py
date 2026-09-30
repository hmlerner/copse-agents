"""SQLite state shared by the CLI, hook handlers, and every agent's MCP server.

Several processes touch the database at once (one MCP server per agent plus
hook invocations), so it runs in WAL mode and every write is a short
transaction.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, fields
from typing import Iterator

from copse.config import db_path

# Claude Code's own subagents (native_subagents table, see below). A crash
# can skip SubagentStop, so a subagent still "running" past this age is
# treated as crashed: view.py hides it from the sidebar, and
# start_native_subagent prunes it here. view.py's own NATIVE_SUBAGENT_LINGER
# (how long a *finished* one keeps showing "done") sits next to this concern
# but is a display-only choice, so it stays in view.py.
NATIVE_SUBAGENT_STALE = 2 * 3600
# Ended rows are dropped from the table entirely after this long.
NATIVE_SUBAGENT_PRUNE_AFTER = 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    repo_root TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,            -- 'worktree' (copse-managed) or 'main' (existing checkout)
    branch TEXT NOT NULL,
    base_branch TEXT,
    path TEXT NOT NULL,
    port_base INTEGER,
    tmux_session TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (repo_root, name)
);
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    profile TEXT NOT NULL,
    provider TEXT NOT NULL,
    parent_id TEXT,
    mode TEXT NOT NULL,            -- 'interactive', 'handoff', or 'assign'
    status TEXT NOT NULL,
    tmux_window TEXT NOT NULL,
    result TEXT,
    created_at REAL NOT NULL,
    status_since REAL,             -- when status last changed
    task TEXT,                     -- the prompt it was started with (for resuming)
    session_ref TEXT,              -- the CLI's own session id (claude --resume)
    stop_blocked INTEGER,          -- copse's Stop hook just kept it going (for CLIs that don't say)
    headless INTEGER,              -- runs `claude -p` turn by turn (agents.run_headless)
    transcript_path TEXT,          -- Claude Code's own JSONL transcript for session_ref (copse.usage)
    done_when TEXT,                -- the finish line it was given, if any (for review context)
    dismissed_at REAL,             -- closed from the sidebar (`copse close`): hidden there for good
    inbox_socket TEXT,             -- Claude Code's inbox for this session (copse.inbox)
    inbox_token TEXT,
    pipeline TEXT,                 -- a worker's branch in copse's hands: 'reviewing' or 'fixing'
    pipeline_rounds INTEGER,
    stuck_noted REAL               -- status_since of the 'waiting' spell its supervisor was told about (copse.cull)
);
CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    sender_id TEXT,
    body TEXT NOT NULL,
    created_at REAL NOT NULL,
    delivered_at REAL
);
-- Autopilot: one row per session (keyed by its supervisor) that has it on.
CREATE TABLE IF NOT EXISTS autopilot (
    root_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 1,
    goal TEXT,                     -- NULL until the user says what we're building
    detail TEXT,
    state TEXT NOT NULL DEFAULT 'running',  -- running | blocked | stalled | usage_paused | done
    note TEXT,                     -- why it's blocked or stalled
    progress INTEGER NOT NULL DEFAULT 0,    -- bumped whenever real progress happens
    nudges INTEGER NOT NULL DEFAULT 0,      -- "keep going" nudges since the last progress
    nudged_at INTEGER,             -- the progress count at the last nudge
    checking_since REAL,           -- a milestone check is running in the background
    usage_resets_at REAL,          -- when state is usage_paused: the usage window's reset time
    usage_paused_ids TEXT,         -- JSON list of the workers stopped for usage
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS milestones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_id TEXT NOT NULL REFERENCES autopilot(root_id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    check_cmd TEXT,                -- copse runs this itself; exit 0 means done
    detail TEXT,
    status TEXT NOT NULL DEFAULT 'pending', -- pending | passed | failed
    checked_at REAL,
    output TEXT,                   -- the tail of the last check's output
    checked_sha TEXT,              -- the checkout's HEAD when it was last checked
    passed_sha TEXT,               -- the checkout's HEAD when it last passed
    profile TEXT                   -- default worker profile for assign
);
-- A reviewer agent's verdict on a branch at one commit. A merge gate only
-- accepts an approval of the commit it is about to merge.
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    sha TEXT NOT NULL,
    reviewer_id TEXT,
    approved INTEGER NOT NULL,
    summary TEXT,
    created_at REAL NOT NULL
);
-- The sidebar reads each workspace's latest review on every refresh (last_review).
CREATE INDEX IF NOT EXISTS reviews_workspace_id ON reviews(workspace_id, id);
-- A check command's PASSING result at one commit, so gates.run and
-- request_review don't re-run the same command against the same tree. Only
-- written when the tree was clean before and after the run (see
-- gates.run_checked); failures are never cached, so a flaky or broken check
-- always gets a fresh run. "Clean" is `git status --porcelain`, which does
-- not see changes to gitignored files, so a check whose result depends on
-- one of those isn't fully captured by this key.
CREATE TABLE IF NOT EXISTS check_cache (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    sha TEXT NOT NULL,
    command TEXT NOT NULL,
    ok INTEGER NOT NULL,
    output TEXT,                   -- the tail of the command's output
    created_at REAL NOT NULL,
    PRIMARY KEY (workspace_id, sha, command)
);
-- Claude Code's own built-in subagents (its Agent tool), reported by the
-- SubagentStart/SubagentStop hooks. Purely informational for the sidebar:
-- kept out of `agents` so they never affect message delivery, is_alive,
-- pause/resume, autopilot worker counts, list_agents, kill or retention.
CREATE TABLE IF NOT EXISTS native_subagents (
    id TEXT PRIMARY KEY,            -- Claude's agent_id
    parent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,
    agent_type TEXT,
    started_at REAL,
    ended_at REAL
);
-- Incremental token-usage cache for one transcript JSONL file (a session's own,
-- or one of its subagents'), keyed by path so repeated reads only parse new
-- bytes. See copse.usage.
CREATE TABLE IF NOT EXISTS usage_cache (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,          -- bytes already parsed
    inode INTEGER,                  -- st_ino when parsed; a new one means the file was replaced
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    last_message_id TEXT,           -- dedupes a message streamed across several lines
    updated_at REAL NOT NULL
);
-- Durable, append-only record of what agents did. Deliberately has no foreign
-- keys: session pruning (sessions.py) deletes agents (and cascades reviews and
-- milestones with them), but history must survive that. Capped per repo_root
-- instead (see copse.history).
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_root TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,             -- worker_result | review | merge | check | milestone
    agent_id TEXT,
    branch TEXT,
    profile TEXT,
    task TEXT,                      -- first ~300 chars of the task/summary
    result TEXT,                    -- trimmed result/verdict text
    tokens TEXT                     -- JSON usage since the agent's previous row, or NULL
);
CREATE INDEX IF NOT EXISTS history_repo_root_id ON history(repo_root, id);
-- Each agent's cumulative usage as of its latest history row, so the next row
-- stores only the difference and summing rows never double counts. Separate
-- from history so capping history doesn't lose it. transcript_path is the
-- transcript the mark was taken from: a different one now (e.g. after
-- /clear starts a new transcript) means the mark doesn't apply any more, so
-- the next row starts a fresh baseline instead of computing a bogus delta.
CREATE TABLE IF NOT EXISTS history_usage_mark (
    agent_id TEXT PRIMARY KEY,
    transcript_path TEXT,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cache_creation_tokens INTEGER NOT NULL
);
-- A coordination task declared through assign/handoff: the files it expects
-- to touch and any earlier tasks (by agent id or branch name) that must be
-- merged first. Most tasks start right away and just get a 'started' row
-- here (so overlap checks on later tasks can see their files); one with
-- unmet dependencies gets a 'pending' row instead, with no worker yet, until
-- copse.tasks.on_merged starts it. See copse.tasks.
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    repo_root TEXT NOT NULL,
    agent_id TEXT,                 -- the worker's agent id, once started
    caller_id TEXT,
    caller_ws_id TEXT NOT NULL,
    profile TEXT NOT NULL,
    task_text TEXT NOT NULL,
    mode TEXT NOT NULL,
    isolate INTEGER NOT NULL,
    branch TEXT,
    done_when TEXT,
    files TEXT,                    -- JSON list of globs this task expects to touch
    depends_on TEXT,               -- JSON list of agent ids / branch names to wait on
    state TEXT NOT NULL,           -- pending | started | merged | cancelled
    created_at REAL NOT NULL,
    started_at REAL
);
CREATE INDEX IF NOT EXISTS tasks_repo_root ON tasks(repo_root);
-- The one `copse watch --sidebar` pane per interactive session root (see
-- agents.sidebar_follow): its tmux pane id, so any session in that root's
-- tree can find and relocate it instead of starting a second one. Keyed by
-- root, not repo, so a second supervisor in the same repo gets its own.
CREATE TABLE IF NOT EXISTS sidebars (
    root_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    pane TEXT NOT NULL,
    updated_at REAL NOT NULL
);
-- Pre-built worktrees (see pool.py): checked out on a placeholder branch at
-- the base branch's tip, with `copy` files and `setup` already applied, so
-- `create` can claim one instead of doing that work live. A claim keeps the
-- entry's path and port_base forever (never moved); it just renames the
-- branch. Never exposed as a workspace: find_workspaces/view.snapshot don't
-- touch this table.
CREATE TABLE IF NOT EXISTS pool_entries (
    path TEXT PRIMARY KEY,
    repo_root TEXT NOT NULL,
    base_branch TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    branch TEXT NOT NULL,           -- the placeholder branch, e.g. copse-pool/<token>
    fingerprint TEXT NOT NULL,      -- setup commands + lockfile contents at base_sha
    port_base INTEGER,              -- reserved for this entry so setup's env matches claim
    ready INTEGER NOT NULL DEFAULT 0,  -- 0 while fill_one is still building it
    created_at REAL NOT NULL
);
-- A pool fill's most recent setup failure for a (repo, base), so fill()
-- backs off instead of retrying (and failing) every time something triggers
-- a refill. See pool.FAILURE_BACKOFF.
CREATE TABLE IF NOT EXISTS pool_failures (
    repo_root TEXT NOT NULL,
    base_branch TEXT NOT NULL,
    failed_at REAL NOT NULL,
    PRIMARY KEY (repo_root, base_branch)
);
"""


@dataclass
class Workspace:
    id: str
    repo_root: str
    name: str
    kind: str
    branch: str
    base_branch: str | None
    path: str
    port_base: int | None
    tmux_session: str
    created_at: float


@dataclass
class Agent:
    id: str
    workspace_id: str
    profile: str
    provider: str
    parent_id: str | None
    mode: str
    status: str
    tmux_window: str
    result: str | None
    created_at: float
    status_since: float | None = None
    task: str | None = None
    session_ref: str | None = None
    stop_blocked: int | None = None
    headless: int | None = None
    transcript_path: str | None = None
    done_when: str | None = None
    dismissed_at: float | None = None
    inbox_socket: str | None = None
    inbox_token: str | None = None
    pipeline: str | None = None
    pipeline_rounds: int | None = None
    stuck_noted: float | None = None


@dataclass
class Autopilot:
    root_id: str
    enabled: int
    goal: str | None
    detail: str | None
    state: str
    note: str | None
    progress: int
    nudges: int
    nudged_at: int | None
    created_at: float
    checking_since: float | None = None
    usage_resets_at: float | None = None
    usage_paused_ids: str | None = None


@dataclass
class Milestone:
    id: int
    root_id: str
    position: int
    title: str
    check_cmd: str | None
    detail: str | None
    status: str
    checked_at: float | None
    output: str | None
    checked_sha: str | None = None
    passed_sha: str | None = None
    profile: str | None = None


@dataclass
class Review:
    id: int
    workspace_id: str
    sha: str
    reviewer_id: str | None
    approved: int
    summary: str | None
    created_at: float


@dataclass
class HistoryEntry:
    id: int
    repo_root: str
    ts: float
    kind: str
    agent_id: str | None
    branch: str | None
    profile: str | None
    task: str | None
    result: str | None
    tokens: str | None


@dataclass
class CheckResult:
    workspace_id: str
    sha: str
    command: str
    ok: int
    output: str | None
    created_at: float


@dataclass
class NativeSubagent:
    id: str
    parent_id: str
    agent_type: str | None
    started_at: float
    ended_at: float | None


@dataclass
class PoolEntry:
    path: str
    repo_root: str
    base_branch: str
    base_sha: str
    branch: str
    fingerprint: str
    port_base: int | None
    ready: int
    created_at: float


@dataclass
class Message:
    id: int
    agent_id: str
    sender_id: str | None
    body: str
    created_at: float
    delivered_at: float | None


@dataclass
class Task:
    id: str
    repo_root: str
    agent_id: str | None
    caller_id: str | None
    caller_ws_id: str
    profile: str
    task_text: str
    mode: str
    isolate: int
    branch: str | None
    done_when: str | None
    files: str | None
    depends_on: str | None
    state: str
    created_at: float
    started_at: float | None = None


def _load(cls, row):
    """Build ``cls`` from a row, ignoring columns this version doesn't know.
    A newer copse may have added columns; an older copse reading the same
    ~/.copse database must not crash on them."""
    names = {f.name for f in fields(cls)}
    return cls(**{k: row[k] for k in row.keys() if k in names})


class DB:
    def __init__(self, path: str | None = None) -> None:
        p = path or str(db_path())
        if p != ":memory:":
            from pathlib import Path

            Path(p).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(p, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Bring databases created by older versions up to the current schema."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(agents)")}
        for col, kind in (("status_since", "REAL"), ("task", "TEXT"), ("session_ref", "TEXT"),
                          ("stop_blocked", "INTEGER"), ("headless", "INTEGER"),
                          ("transcript_path", "TEXT"), ("done_when", "TEXT"),
                          ("dismissed_at", "REAL"), ("stuck_noted", "REAL"),
                          ("inbox_socket", "TEXT"), ("inbox_token", "TEXT"),
                          ("pipeline", "TEXT"), ("pipeline_rounds", "INTEGER")):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE agents ADD COLUMN {col} {kind}")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(autopilot)")}
        if "checking_since" not in cols:
            self.conn.execute("ALTER TABLE autopilot ADD COLUMN checking_since REAL")
        if "usage_resets_at" not in cols:
            self.conn.execute("ALTER TABLE autopilot ADD COLUMN usage_resets_at REAL")
        if "usage_paused_ids" not in cols:
            self.conn.execute("ALTER TABLE autopilot ADD COLUMN usage_paused_ids TEXT")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(milestones)")}
        for col in ("checked_sha", "passed_sha", "profile"):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE milestones ADD COLUMN {col} TEXT")
        cache_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(usage_cache)")}
        if "inode" not in cache_cols:
            self.conn.execute("ALTER TABLE usage_cache ADD COLUMN inode INTEGER")
        mark_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(history_usage_mark)")}
        if "transcript_path" not in mark_cols:
            self.conn.execute("ALTER TABLE history_usage_mark ADD COLUMN transcript_path TEXT")
        tables = {r["name"] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "sidebars" not in tables:
            self.conn.execute(
                """CREATE TABLE sidebars (
                    root_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
                    pane TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )
        pool_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(pool_entries)")}
        for col, kind in (("port_base", "INTEGER"), ("ready", "INTEGER NOT NULL DEFAULT 1")):
            if col not in pool_cols:
                self.conn.execute(f"ALTER TABLE pool_entries ADD COLUMN {col} {kind}")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    # -- workspaces --------------------------------------------------------

    def add_workspace(self, ws: Workspace) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO workspaces VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ws.id, ws.repo_root, ws.name, ws.kind, ws.branch, ws.base_branch,
                 ws.path, ws.port_base, ws.tmux_session, ws.created_at),
            )

    def get_workspace(self, ws_id: str) -> Workspace | None:
        row = self.conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
        return _load(Workspace, row) if row else None

    def find_workspaces(self, repo_root: str | None = None) -> list[Workspace]:
        if repo_root:
            rows = self.conn.execute(
                "SELECT * FROM workspaces WHERE repo_root=? ORDER BY created_at", (repo_root,)
            )
        else:
            rows = self.conn.execute("SELECT * FROM workspaces ORDER BY created_at")
        return [_load(Workspace, r) for r in rows]

    def workspace_by_path(self, path: str) -> Workspace | None:
        row = self.conn.execute("SELECT * FROM workspaces WHERE path=?", (path,)).fetchone()
        return _load(Workspace, row) if row else None

    def workspace_by_tmux_session(self, session: str) -> Workspace | None:
        row = self.conn.execute(
            "SELECT * FROM workspaces WHERE tmux_session=?", (session,)
        ).fetchone()
        return _load(Workspace, row) if row else None

    def delete_workspace(self, ws_id: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM workspaces WHERE id=?", (ws_id,))

    def used_port_bases(self) -> set[int]:
        rows = self.conn.execute(
            "SELECT port_base FROM workspaces WHERE port_base IS NOT NULL "
            "UNION SELECT port_base FROM pool_entries WHERE port_base IS NOT NULL"
        )
        return {r[0] for r in rows}

    # -- worktree pool -------------------------------------------------------

    def add_pool_entry(self, e: PoolEntry) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO pool_entries (path, repo_root, base_branch, base_sha, branch, "
                "fingerprint, port_base, ready, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (e.path, e.repo_root, e.base_branch, e.base_sha, e.branch,
                 e.fingerprint, e.port_base, e.ready, e.created_at),
            )

    def mark_pool_ready(self, path: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE pool_entries SET ready=1 WHERE path=?", (path,))

    def pool_entries(
        self, repo_root: str, base_branch: str | None = None, ready_only: bool = True
    ) -> list[PoolEntry]:
        clauses, params = ["repo_root=?"], [repo_root]
        if base_branch:
            clauses.append("base_branch=?")
            params.append(base_branch)
        if ready_only:
            clauses.append("ready=1")
        rows = self.conn.execute(
            f"SELECT * FROM pool_entries WHERE {' AND '.join(clauses)} ORDER BY created_at",
            params,
        )
        return [_load(PoolEntry, r) for r in rows]

    def count_pool_entries(self, repo_root: str, base_branch: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM pool_entries WHERE repo_root=? AND base_branch=? AND ready=1",
            (repo_root, base_branch),
        ).fetchone()
        return int(row[0])

    def take_pool_entry(self, repo_root: str, base_branch: str) -> PoolEntry | None:
        """Atomically claim (remove and return) the oldest matching ready
        entry, if any, so two concurrent creates never claim the same one."""
        with self.tx() as c:
            row = c.execute(
                "SELECT * FROM pool_entries WHERE repo_root=? AND base_branch=? AND ready=1 "
                "ORDER BY created_at LIMIT 1",
                (repo_root, base_branch),
            ).fetchone()
            if not row:
                return None
            c.execute("DELETE FROM pool_entries WHERE path=?", (row["path"],))
            return _load(PoolEntry, row)

    def delete_pool_entry(self, path: str) -> int:
        """Returns the number of rows deleted (0 or 1). A caller trimming or
        sweeping the pool must check this: a concurrent claim (take_pool_entry)
        may have already removed the row, in which case the worktree now
        belongs to a workspace and must not be discarded."""
        with self.tx() as c:
            cur = c.execute("DELETE FROM pool_entries WHERE path=?", (path,))
            return cur.rowcount

    def record_pool_failure(self, repo_root: str, base_branch: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO pool_failures (repo_root, base_branch, failed_at) VALUES (?,?,?) "
                "ON CONFLICT(repo_root, base_branch) DO UPDATE SET failed_at=excluded.failed_at",
                (repo_root, base_branch, time.time()),
            )

    def last_pool_failure(self, repo_root: str, base_branch: str) -> float | None:
        row = self.conn.execute(
            "SELECT failed_at FROM pool_failures WHERE repo_root=? AND base_branch=?",
            (repo_root, base_branch),
        ).fetchone()
        return float(row[0]) if row else None

    def clear_pool_failure(self, repo_root: str, base_branch: str) -> None:
        with self.tx() as c:
            c.execute(
                "DELETE FROM pool_failures WHERE repo_root=? AND base_branch=?",
                (repo_root, base_branch),
            )

    # -- agents ------------------------------------------------------------

    def add_agent(self, a: Agent) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO agents (id, workspace_id, profile, provider, parent_id, mode, "
                "status, tmux_window, result, created_at, status_since, task, session_ref, "
                "headless, transcript_path, done_when, inbox_socket, inbox_token) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (a.id, a.workspace_id, a.profile, a.provider, a.parent_id, a.mode,
                 a.status, a.tmux_window, a.result, a.created_at,
                 a.status_since or a.created_at, a.task, a.session_ref, a.headless,
                 a.transcript_path, a.done_when, a.inbox_socket, a.inbox_token),
            )

    def get_agent(self, agent_id: str) -> Agent | None:
        row = self.conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        return _load(Agent, row) if row else None

    def list_agents(self, workspace_id: str | None = None) -> list[Agent]:
        if workspace_id:
            rows = self.conn.execute(
                "SELECT * FROM agents WHERE workspace_id=? ORDER BY created_at", (workspace_id,)
            )
        else:
            rows = self.conn.execute("SELECT * FROM agents ORDER BY created_at")
        return [_load(Agent, r) for r in rows]

    def children(self, parent_id: str) -> list[Agent]:
        rows = self.conn.execute(
            "SELECT * FROM agents WHERE parent_id=? ORDER BY created_at", (parent_id,)
        )
        return [_load(Agent, r) for r in rows]

    def update_agent(self, agent_id: str, **fields: object) -> None:
        if "status" in fields:
            status = fields.pop("status")
            self.set_status(agent_id, str(status))
            if not fields:
                return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE agents SET {cols} WHERE id=?", (*fields.values(), agent_id))

    def claim_idle(self, agent_id: str) -> bool:
        """Atomically flip idle -> processing. Only the caller that wins may type
        into the agent's terminal, so two senders never interleave keystrokes."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE agents SET status='processing', status_since=? WHERE id=? AND status='idle'",
                (time.time(), agent_id),
            )
            return cur.rowcount == 1

    _STAMP = "status_since = CASE WHEN status = ? THEN status_since ELSE ? END"

    def set_status(self, agent_id: str, status: str, only_if: str | None = None) -> None:
        guard, args = ("", ()) if only_if is None else (" AND status=?", (only_if,))
        with self.tx() as c:
            c.execute(
                f"UPDATE agents SET {self._STAMP}, status=? WHERE id=?{guard}",
                (status, time.time(), status, agent_id, *args),
            )

    def set_result(self, agent_id: str, result: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE agents SET result=? WHERE id=?", (result, agent_id))

    def delete_agent(self, agent_id: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM agents WHERE id=?", (agent_id,))

    # -- inbox -------------------------------------------------------------

    def enqueue(self, agent_id: str, body: str, sender_id: str | None) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO inbox (agent_id, sender_id, body, created_at) VALUES (?,?,?,?)",
                (agent_id, sender_id, body, time.time()),
            )
            return int(cur.lastrowid)

    def mark_delivered(self, message_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE inbox SET delivered_at=? WHERE id=?", (time.time(), message_id))

    def pop_pending(self, agent_id: str) -> Message | None:
        """Atomically claim the oldest undelivered message, if any."""
        with self.tx() as c:
            row = c.execute(
                "SELECT * FROM inbox WHERE agent_id=? AND delivered_at IS NULL "
                "ORDER BY id LIMIT 1",
                (agent_id,),
            ).fetchone()
            if not row:
                return None
            c.execute("UPDATE inbox SET delivered_at=? WHERE id=?", (time.time(), row["id"]))
            return _load(Message, row)

    def drop_pending(self, agent_id: str, sender_id: str) -> int:
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM inbox WHERE agent_id=? AND sender_id=? AND delivered_at IS NULL",
                (agent_id, sender_id),
            )
            return cur.rowcount

    def recently_delivered(self, agent_id: str, body: str, within: float = 120.0) -> bool:
        """Whether ``body`` is a message copse delivered to the agent just now."""
        want = body.strip()
        if not want:
            return False
        rows = self.conn.execute(
            "SELECT body FROM inbox WHERE agent_id=? AND delivered_at > ?",
            (agent_id, time.time() - within),
        )
        return any(r[0].strip() == want for r in rows)

    def delivered_since(self, agent_id: str, since: float) -> bool:
        """Whether copse delivered any message to the agent after ``since``."""
        row = self.conn.execute(
            "SELECT 1 FROM inbox WHERE agent_id=? AND delivered_at > ? LIMIT 1", (agent_id, since),
        ).fetchone()
        return row is not None

    def pending_count(self, agent_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM inbox WHERE agent_id=? AND delivered_at IS NULL", (agent_id,)
        ).fetchone()
        return int(row[0])

    def message_delivered(self, message_id: int) -> bool:
        """Whether the specific message ``enqueue`` returned has since been
        delivered (by ``pop_pending`` or a reconcile-triggered flush)."""
        row = self.conn.execute(
            "SELECT delivered_at FROM inbox WHERE id=?", (message_id,)
        ).fetchone()
        return bool(row and row[0] is not None)

    # -- autopilot -----------------------------------------------------------

    def get_autopilot(self, root_id: str) -> Autopilot | None:
        row = self.conn.execute("SELECT * FROM autopilot WHERE root_id=?", (root_id,)).fetchone()
        return _load(Autopilot, row) if row else None

    def add_autopilot(self, root_id: str, enabled: bool = True) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO autopilot (root_id, enabled, created_at) VALUES (?,?,?)",
                (root_id, int(enabled), time.time()),
            )

    def update_autopilot(self, root_id: str, **fields: object) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE autopilot SET {cols} WHERE root_id=?", (*fields.values(), root_id))

    def bump_progress(self, root_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE autopilot SET progress=progress+1, nudges=0 WHERE root_id=?", (root_id,))

    def set_milestones(self, root_id: str, items: list[tuple]) -> None:
        """Replace the session's milestones with ``(title, check_cmd, detail[, profile])`` items."""
        with self.tx() as c:
            c.execute("DELETE FROM milestones WHERE root_id=?", (root_id,))
            for i, (title, check, detail, *rest) in enumerate(items, start=1):
                c.execute(
                    "INSERT INTO milestones (root_id, position, title, check_cmd, detail, profile) "
                    "VALUES (?,?,?,?,?,?)",
                    (root_id, i, title, check, detail, rest[0] if rest else None),
                )

    def milestones(self, root_id: str) -> list[Milestone]:
        rows = self.conn.execute(
            "SELECT * FROM milestones WHERE root_id=? ORDER BY position", (root_id,)
        )
        return [_load(Milestone, r) for r in rows]

    def record_check(self, milestone_id: int, passed: bool, output: str,
                     sha: str | None = None, *, passed_sha: str | None = None) -> None:
        """``passed_sha`` defaults to ``sha`` on a pass and is kept on a fail."""
        with self.tx() as c:
            c.execute(
                "UPDATE milestones SET status=?, checked_at=?, output=?, checked_sha=?, "
                "passed_sha=COALESCE(?, passed_sha) WHERE id=?",
                ("passed" if passed else "failed", time.time(), output, sha,
                 passed_sha or (sha if passed else None), milestone_id),
            )

    # -- reviews -------------------------------------------------------------

    def add_review(self, workspace_id: str, sha: str, reviewer_id: str | None,
                   approved: bool, summary: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO reviews (workspace_id, sha, reviewer_id, approved, summary, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (workspace_id, sha, reviewer_id, int(approved), summary, time.time()),
            )

    def latest_review(self, workspace_id: str, sha: str) -> Review | None:
        row = self.conn.execute(
            "SELECT * FROM reviews WHERE workspace_id=? AND sha=? ORDER BY id DESC LIMIT 1",
            (workspace_id, sha),
        ).fetchone()
        return _load(Review, row) if row else None

    def last_review(self, workspace_id: str) -> Review | None:
        """The most recent review of ``workspace_id`` at any sha, for incremental
        re-review: compare its sha against the current HEAD to see what's new."""
        row = self.conn.execute(
            "SELECT * FROM reviews WHERE workspace_id=? ORDER BY id DESC LIMIT 1",
            (workspace_id,),
        ).fetchone()
        return _load(Review, row) if row else None

    # -- check cache -----------------------------------------------------------

    def get_check(self, workspace_id: str, sha: str, command: str) -> CheckResult | None:
        row = self.conn.execute(
            "SELECT * FROM check_cache WHERE workspace_id=? AND sha=? AND command=?",
            (workspace_id, sha, command),
        ).fetchone()
        return _load(CheckResult, row) if row else None

    def set_check(self, workspace_id: str, sha: str, command: str, ok: bool, output: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO check_cache (workspace_id, sha, command, ok, output, created_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(workspace_id, sha, command) DO UPDATE SET "
                "ok=excluded.ok, output=excluded.output, created_at=excluded.created_at",
                (workspace_id, sha, command, int(ok), output, time.time()),
            )

    # -- native subagents ----------------------------------------------------

    def start_native_subagent(self, sub_id: str, parent_id: str, agent_type: str | None) -> None:
        """Record a SubagentStart. One write: also prunes ``parent_id``'s own
        ended rows (older than NATIVE_SUBAGENT_PRUNE_AFTER) and abandoned
        still-"running" rows (older than NATIVE_SUBAGENT_STALE, e.g. a crash
        that skipped SubagentStop), so this table doesn't grow forever."""
        now = time.time()
        with self.tx() as c:
            c.execute(
                "DELETE FROM native_subagents WHERE parent_id=? AND "
                "((ended_at IS NOT NULL AND ended_at<?) OR (ended_at IS NULL AND started_at<?))",
                (parent_id, now - NATIVE_SUBAGENT_PRUNE_AFTER, now - NATIVE_SUBAGENT_STALE),
            )
            c.execute(
                "INSERT OR REPLACE INTO native_subagents (id, parent_id, agent_type, started_at, ended_at) "
                "VALUES (?,?,?,?,NULL)",
                (sub_id, parent_id, agent_type, now),
            )

    def stop_native_subagent(self, sub_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE native_subagents SET ended_at=? WHERE id=?", (time.time(), sub_id))

    def end_native_subagents(self, parent_id: str) -> None:
        """Mark every still-running native subagent of ``parent_id`` as ended:
        for when the parent itself stops (paused or killed), since a dead
        parent's own SubagentStop hooks will never fire."""
        with self.tx() as c:
            c.execute(
                "UPDATE native_subagents SET ended_at=? WHERE parent_id=? AND ended_at IS NULL",
                (time.time(), parent_id),
            )

    def native_subagents(self, parent_id: str) -> list[NativeSubagent]:
        rows = self.conn.execute(
            "SELECT * FROM native_subagents WHERE parent_id=? ORDER BY started_at", (parent_id,)
        )
        return [_load(NativeSubagent, r) for r in rows]

    # -- sidebar (one `copse watch --sidebar` pane per session root) --------

    def get_sidebar_pane(self, root_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT pane FROM sidebars WHERE root_id=?", (root_id,)
        ).fetchone()
        return row["pane"] if row else None

    def set_sidebar_pane(self, root_id: str, pane: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO sidebars (root_id, pane, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(root_id) DO UPDATE SET pane=excluded.pane, updated_at=excluded.updated_at",
                (root_id, pane, time.time()),
            )

    def clear_sidebar_pane(self, root_id: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM sidebars WHERE root_id=?", (root_id,))

    def all_native_subagents(self) -> dict[str, list[NativeSubagent]]:
        """Every native subagent worth showing, grouped by parent id: one
        bounded query for a whole dashboard snapshot instead of one per agent."""
        now = time.time()
        rows = self.conn.execute(
            "SELECT * FROM native_subagents WHERE (ended_at IS NULL AND started_at>?) "
            "OR (ended_at IS NOT NULL AND ended_at>?) ORDER BY started_at",
            (now - NATIVE_SUBAGENT_STALE, now - NATIVE_SUBAGENT_PRUNE_AFTER),
        )
        out: dict[str, list[NativeSubagent]] = {}
        for r in rows:
            sub = _load(NativeSubagent, r)
            out.setdefault(sub.parent_id, []).append(sub)
        return out

    # -- usage cache (copse.usage) --------------------------------------------

    def get_usage_cache(self, path: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM usage_cache WHERE path=?", (path,)).fetchone()

    def set_usage_cache(self, path: str, size: int, input_tokens: int, output_tokens: int,
                        cache_read_tokens: int, cache_creation_tokens: int,
                        model: str | None, last_message_id: str | None,
                        inode: int | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO usage_cache (path, size, input_tokens, output_tokens, "
                "cache_read_tokens, cache_creation_tokens, model, last_message_id, updated_at, "
                "inode) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET size=excluded.size, "
                "input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens, "
                "cache_read_tokens=excluded.cache_read_tokens, "
                "cache_creation_tokens=excluded.cache_creation_tokens, model=excluded.model, "
                "last_message_id=excluded.last_message_id, updated_at=excluded.updated_at, "
                "inode=excluded.inode",
                (path, size, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                 model, last_message_id, time.time(), inode),
            )

    # -- history (copse.history) ----------------------------------------------

    def add_history(self, repo_root: str, kind: str, *, agent_id: str | None = None,
                    branch: str | None = None, profile: str | None = None,
                    task: str | None = None, result: str | None = None,
                    tokens: str | None = None,
                    mark: tuple[str | None, int, int, int, int] | None = None) -> None:
        """Append a row, and (in the same transaction, so one never happens
        without the other) advance ``agent_id``'s usage mark to ``mark`` —
        ``(transcript_path, input_tokens, output_tokens, cache_read_tokens,
        cache_creation_tokens)`` — if given."""
        with self.tx() as c:
            c.execute(
                "INSERT INTO history (repo_root, ts, kind, agent_id, branch, profile, task, "
                "result, tokens) VALUES (?,?,?,?,?,?,?,?,?)",
                (repo_root, time.time(), kind, agent_id, branch, profile, task, result, tokens),
            )
            if mark is not None:
                transcript_path, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens = mark
                c.execute(
                    "INSERT OR REPLACE INTO history_usage_mark (agent_id, transcript_path, "
                    "input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens) "
                    "VALUES (?,?,?,?,?,?)",
                    (agent_id, transcript_path, input_tokens, output_tokens, cache_read_tokens,
                     cache_creation_tokens),
                )

    def get_usage_mark(self, agent_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM history_usage_mark WHERE agent_id=?", (agent_id,)
        ).fetchone()

    def list_history(self, repo_root: str | None = None, kind: str | None = None,
                     limit: int = 50) -> list[HistoryEntry]:
        clauses, args = [], []
        if repo_root:
            clauses.append("repo_root=?")
            args.append(repo_root)
        if kind:
            clauses.append("kind=?")
            args.append(kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM history {where} ORDER BY id DESC LIMIT ?", (*args, limit)
        )
        return [_load(HistoryEntry, r) for r in rows]

    @staticmethod
    def _prune_usage_marks(c: sqlite3.Connection) -> None:
        """Drop marks for agents that are gone from ``agents`` *and* have no
        surviving ``history`` row either: nothing will ever read them again."""
        c.execute(
            "DELETE FROM history_usage_mark WHERE agent_id NOT IN (SELECT id FROM agents) "
            "AND agent_id NOT IN (SELECT agent_id FROM history WHERE agent_id IS NOT NULL)"
        )

    def prune_history(self, repo_root: str, cap: int) -> int:
        """Keep only the ``cap`` newest rows for ``repo_root``. Returns how many
        were dropped."""
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM history WHERE repo_root=? AND id NOT IN "
                "(SELECT id FROM history WHERE repo_root=? ORDER BY id DESC LIMIT ?)",
                (repo_root, repo_root, cap),
            )
            self._prune_usage_marks(c)
            return cur.rowcount

    def prune_usage_marks(self) -> int:
        """Same cleanup as `prune_history`, for callers (like session pruning)
        that delete agents without touching history."""
        with self.tx() as c:
            before = c.execute("SELECT COUNT(*) FROM history_usage_mark").fetchone()[0]
            self._prune_usage_marks(c)
            after = c.execute("SELECT COUNT(*) FROM history_usage_mark").fetchone()[0]
            return before - after

    # -- tasks (copse.tasks) ---------------------------------------------------

    def add_task(self, t: Task) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO tasks (id, repo_root, agent_id, caller_id, caller_ws_id, profile, "
                "task_text, mode, isolate, branch, done_when, files, depends_on, state, "
                "created_at, started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t.id, t.repo_root, t.agent_id, t.caller_id, t.caller_ws_id, t.profile,
                 t.task_text, t.mode, int(t.isolate), t.branch, t.done_when, t.files,
                 t.depends_on, t.state, t.created_at, t.started_at),
            )

    def get_task(self, task_id: str) -> Task | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return _load(Task, row) if row else None

    def list_tasks(self, repo_root: str | None = None, state: str | None = None) -> list[Task]:
        clauses, args = [], []
        if repo_root:
            clauses.append("repo_root=?")
            args.append(repo_root)
        if state:
            clauses.append("state=?")
            args.append(state)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(f"SELECT * FROM tasks {where} ORDER BY created_at", args)
        return [_load(Task, r) for r in rows]

    def update_task(self, task_id: str, **fields: object) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))
