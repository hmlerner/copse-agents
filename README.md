# copse

*[pawdelta.com/copse](https://pawdelta.com/copse/) · Published on PyPI as `copse-agents`; the command is `copse`. This project is
unrelated to the Copse desktop app at copse.dev.*

A supervisor for your coding agents. copse runs Claude Code, Codex, and
its own loop over any open-weight model side by side in tmux, gives each agent its own git worktree and branch, and
lets a supervisor agent split up work, hand it out, review each branch, and
merge the results. Nobody edits the same files, and nothing lands without
review.

- **Parallel agents, no collisions.** Every workspace is a separate worktree on
  its own branch, cut from a freshly fetched base, with its own block of ports
  for dev servers.
- **Delegation built in.** Agents get a `copse` MCP server: `assign` work to
  parallel workers, `handoff` a task and wait for it (in bounded steps, with
  `wait_for_worker` to keep waiting), `send_message` between agents, then
  `workspace_diff`, `merge_workspace`, and `remove_workspace`.
- **The whole branch lifecycle.** Diff against the base, sync (rebase or
  merge), commit, push, open a PR, merge back. Removal refuses to throw away
  uncommitted work and keeps the branch unless you say otherwise.
- **Per-repo setup.** `.copse/config.json` defines setup and teardown scripts
  and which local files (like `.env`) to copy into new workspaces.
- **Reliable status.** copse knows whether each agent is working, idle, or
  waiting for your approval from the agent's own lifecycle hooks, not by
  scraping the terminal.

## Install

```sh
brew install tmux
uv tool install copse-agents                  # once published
uv tool install --editable ~/Projects/copse   # from a local checkout
```

`copse --version` prints the installed version; the tmux status bar of every copse
session shows it too (`copse 0.11.5`). A session started before an upgrade keeps
running the old code, and shows the old number, until you restart it.

## Quick start

`copse doctor` checks that everything copse needs is there (tmux, the agent CLIs,
a writable home) and, in a repo, what's set up for it, and says what to do about
anything missing.

```sh
cd ~/code/myapp
copse
```

That's it. `copse` opens a supervisor chat (Claude Code) in your repo, with a
narrow sidebar on the left showing every agent: who's working, who's idle, and
who's waiting for your approval. Tell the supervisor what you want. It splits the
work between workers, each on its own branch, then reviews and merges their
branches. It starts in under a second.

**The sidebar follows you.** There's one sidebar pane per session root, not
one per window: switch to any other copse window or session (⏎ in the
sidebar, `copse attach`, prefix-L back, clicking a pane) and it relocates
there too, always beside whatever you're looking at, never spawning a second
dashboard. Scroll it
with the mouse wheel, PageUp/PageDown, or Home/End when there's more than fits;
moving the ↑↓ selection scrolls to keep it in view.

**Sidebar keys.** `?` shows them in the sidebar too. ◆ marks an agent that needs
you: stuck on a prompt, a supervisor with a question, or (without autopilot) a
worker whose branch is waiting for your review. Under autopilot, a reported worker shows
a dim ◇ instead, because the supervisor reviews it.

**Copying chat text.** Drag with the mouse in the chat: the selection stays inside
that pane and is copied to the system clipboard (`pbcopy`, `wl-copy` or `xclip`;
`copse doctor` shows which). `h` in the sidebar hides it without quitting copse
(the chat zooms; `Ctrl-b S` brings it back), and `Ctrl-b z` zooms the chat by hand.
To keep the dashboard below the chat instead, set `"sidebar": "bottom"` in
`.copse/config.json`.

| Key | |
|---|---|
| `↑↓ j k` | move |
| `PgUp/Dn` | page |
| `Home/End` | top / bottom |
| `⏎ a` | open the agent |
| `p` | peek at its screen |
| `x` | close (2× if busy) |
| `n` | next needing you |
| `Spc Tab` | fold group |
| `/` | filter, Esc clears |
| `r` | refresh |
| `?` | this help |
| `q` | quit |

When an agent uses Claude Code's own Agent tool, its built-in subagents (Explore,
Plan, ...) show up nested underneath it in the sidebar too, e.g. `↳ Explore ·
running 1m`, so you can see what it's fanned out to without leaving copse.

**Closing and coming back.** When you quit the supervisor's chat, or press `q` twice
in the sidebar (or `x` twice on your own supervisor), the copse window closes cleanly
and you're back at your prompt. The whole session is paused: its
workers stop too, and everything is kept (branches, worktrees,
queued messages, and each agent's Claude conversation). `copse continue` (or
`copse -c`) picks up the most recent paused session and lists the others by id
(`copse continue <id>`). Plain `copse` always starts fresh. `copse sessions` lists
what's paused. copse keeps the newest 3 paused sessions per repo for up to 7 days;
cleanup never merges anything or deletes branches, and worktrees with uncommitted
changes are kept.

**Not in a git repo?** `copse` still works. It starts a *scratch session*: a
fresh git repo under `~/.copse/scratch/`, and nothing is created in the folder you ran
it from. When the work belongs in a real repository, run `copse transfer ~/path/to/repo`
(or ask the supervisor). The commits land on a new branch there, ready to review and
merge. Running `copse` in a repo also offers to bring in any scratch work that hasn't
been moved yet.

Or drive a single workspace yourself:

```sh
copse new fix-login -p "Fix the login redirect bug; add a test"
copse ls                                    # workspaces, agents, ahead/behind
copse diff fix-login --stat
copse pr fix-login                          # push + gh pr create
copse rm fix-login                          # keeps the branch
```

## Recommended use

copse pays off when work splits into pieces that can proceed in parallel, or
runs long enough that you want it reviewed and merged without babysitting. For a
one-line fix, plain `claude` is quicker; the supervisor will also just do small
things itself instead of starting workers.

1. **Start in the repo, on a clean base.** Commit or stash first: workers branch
   from the supervisor's committed work, not your uncommitted edits.
2. **Give it a goal with checks.** For anything bigger than one sitting, tell the
   supervisor what "done" means in commands (a test file, `npm test`, a build), or
   write `.copse/goals.md` yourself. Milestones with real checks are what let
   autopilot keep going without you and stop when the work is actually done.
3. **Set `checks` in `.copse/config.json`** (usually your full test suite) so no
   branch merges red, and a `setup` if new worktrees need `npm install` or similar.
4. **Watch the sidebar, not every window.** It flags agents waiting on you (◆);
   `n` jumps to the next one, ⏎ opens it, `p` peeks, `?` lists every key. Workers that need a
   decision surface through the supervisor's `need_user`.
5. **Step away freely.** Quit the chat to pause everything; `copse continue`
   resumes the session, workers included. `copse history` shows what ran, what
   merged and what it cost.
6. **Clean up.** Finished workers close on their own after `stale_after` minutes,
   and `copse` sweeps leftover processes at start. `copse close --exited` and
   `copse prune` do it on demand. Branches are never deleted for you.

For a large task: write the milestones first (each with a check), keep each worker's
task to one area of the code with its own test command, and let the supervisor run up
to `max_agents` workers at once. Use a cheaper profile (see [Cheap workers](#cheap-workers))
for mechanical edits and a Codex reviewer for a second model's opinion.

## Autopilot

`copse` starts the supervisor with autopilot on. Tell it what we're building,
or put the goal in `.copse/goals.md`, and it works like a project manager:

1. **Goal and milestones.** The goal is split into milestones, and each one has
   a check command that copse runs itself. A milestone is done only when its
   check exits 0, so progress is verified, not just claimed. A milestone may also name a
   `profile`: the worker profile to use for its tasks, so a small milestone can
   run on a cheaper profile. `assign` and `handoff` called without an
   `agent_profile` use the first unverified milestone's profile, else the repo's
   `default_agent`.
2. **Workers in parallel.** The supervisor splits each milestone into tasks
   and starts workers on their own branches, up to `max_agents` at once.
   Claude workers run the task as a Claude Code `/goal` with a finish line, so
   they keep going until it's met. `assign`/`handoff` take `files` (the
   paths/globs a task expects to touch, checked for overlap against other
   active workers' declared and actually-changed files — a warning, not a
   block) and `depends_on` (earlier tasks, by agent id or branch, that must
   merge first): a task with unmet dependencies is queued instead of started,
   and starts automatically, cut from the updated base, once
   `merge_workspace` resolves them. `list_tasks` shows what's queued.
   `assign`/`handoff` also take `plan_first` (default: the `plan_first` config
   key): the worker reads the code, then calls `submit_plan` with a short plan
   and waits. The plan reaches the supervisor as a message; `approve_plan`
   approves it, or (`approved=false`, with feedback) sends it back for a
   revision. For Claude workers copse's PreToolUse hook refuses Edit, Write
   and NotebookEdit until the plan is approved; other CLIs are only told to
   wait. Autopilot doesn't count a worker waiting on approval as stalled, and
   reminds the supervisor about plans awaiting a decision.
3. **Gated merges.** A branch merges only when everything is committed, a
   reviewer agent has approved that exact commit, your pre-commit hooks pass,
   and your `checks` pass. copse runs these itself before `merge_workspace`,
   and caches a clean commit's passing result so it isn't re-run for every
   review and merge attempt at the same sha. `request_review` starts the
   reviewer immediately and runs `checks` in the background, delivering a
   pass/fail summary (output only for failures) as a message once they
   finish, instead of asking the reviewer to run the whole suite itself.
   `request_review` picks the reviewer profile itself unless you pass one: an
   explicit `profile` argument, else `review_profile` in the repo config,
   else the built-in `reviewer-codex` profile (Codex reviewing Claude's
   work, a different model from the worker) when `codex` is on `PATH` and
   the worker ran on Claude, else the built-in `reviewer-local` profile when
   the worker ran on Claude and its local model answers a quick probe, else
   `reviewer`.
4. **It keeps going.** If the supervisor stops while milestones are still
   unverified and no worker is running, copse tells it to continue. It stops
   when every check passes, when it needs a decision from you, after three
   reminders with no progress, or when your Claude usage nears its limit.

The sidebar shows the goal, each milestone (✓ verified, ✗ failing, ○ not
checked yet), and anything that needs you.

```markdown
<!-- .copse/goals.md -->
# Settings page

## Settings API
check: uv run pytest tests/test_settings_api.py -q

## Settings UI
check: npm test -- settings
profile: developer-cheap
```

A session that loaded its goal from `goals.md` writes each milestone's status
back to that file after every check, as a line under the milestone, for
example `status: passed at abc1234 (2026-09-29)` (`passed`, `failed` or
`pending`, a short commit sha and a date). The rest of the file is left byte
for byte as you wrote it. Since `goals.md` may be committed, it stays free of
session data: no agent or session ids, check output, notes, usage or
questions ever go in. A status line is information only: a new session
starts every milestone as pending and re-runs the checks, never trusting it.
A session stops writing when it's handed over, paused, or has autopilot off,
or once its goal was replaced from the chat. From a linked worktree, it writes
the `goals.md` it loaded (the main checkout's), never another session's. A
write that fails never fails the check.

`copse autopilot` shows progress, `copse autopilot check` runs the checks
now, and `copse autopilot off` (or `on`) hands the wheel back (or takes it
again). `copse --no-autopilot`, or `"autopilot": false` in the repo config,
starts without it.

When your Claude usage reaches `usage_limit`, autopilot pauses for usage: its
running Claude workers stop (worktrees, branches, queued messages and sessions
are kept) and the sidebar says "paused for usage until <time>". Once the usage
window resets, copse restarts those workers on its own, sets autopilot running
again and tells the supervisor what it resumed. Speaking to the supervisor
doesn't end the pause.

To track your Claude usage, copse gives the agents it launches a status line.
It records the usage percentage Claude Code reports, then prints whatever
your own status line prints, so what you see doesn't change.

## Commands

| | |
|---|---|
| `copse` | a fresh supervisor chat here, dashboard alongside |
| `copse init` | write a starter `.copse/config.json` |
| `copse new BRANCH [-b BASE] [-a PROFILE] [-p PROMPT]` | worktree + branch + agent |
| `copse continue [ID]` / `copse -c` | resume a paused session (default: the most recent) |
| `copse sessions` / `copse prune` | list paused sessions / apply the retention rules now and remove merged worktrees and leftover tmux sessions |
| `copse start [-a PROFILE] [-p PROMPT] [--no-watch] [--no-autopilot] [-b BRANCH] [-w PATH]` | the same, with options; `-b`/`-w` run it in that branch's worktree (created if needed, or the one you already made), which gets the repo's `.copse` config |
| `copse handover --to BRANCH\|PATH [-n NOTE]` | hand the session to a new supervisor there: goal and milestones, workers, queued tasks and your note move across; the old one is paused |
| `copse autopilot [on\|off\|check]` | the goal's progress; turn autopilot on or off; run the checks now |
| `copse transfer [REPO] [--from SESSION] [-b BRANCH]` | move a scratch session's work into a real repo |
| `copse ls [--all]` | workspaces and agents |
| `copse history [--limit N] [--kind K] [--all]` | durable log of worker results, reviews, merges and milestone checks |
| `copse learning [--reset]` | what the repo's learning plugin has learned (see `learning` below); `--reset` asks it to forget this repo |
| `copse watch [--all] [--once]` | the dashboard on its own (the same view as the sidebar): enter attaches, `p` peeks, `x` closes |
| `copse attach / cd / open [WS]` | tmux session / path / editor |
| `copse status / diff [--stat] [WS]` | compared with the base branch (committed + uncommitted) |
| `copse sync [--merge] [WS]` | rebase (or merge) the latest base into the branch |
| `copse commit / push / pr [WS]` | commit everything (`-m MSG`), push with upstream, open a PR |
| `copse merge [--squash] [WS]` | merge into the base locally |
| `copse rm WS [-f] [-D]` | remove the worktree; `-D` deletes the branch too, only if merged unless `-f` |
| `copse doctor` | check that copse has what it needs (tmux, the agent CLIs, native profiles' model endpoints, a writable home) and, in a repo, its config, checks and code map |
| `copse close AGENT` / `copse close --exited` | hide an agent (or every stopped one) from the dashboard, stopping it if it's running; its worktree and branch stay |
| `copse send AGENT MSG` | message an agent; waits in its inbox until it's idle |
| `copse agent spawn/kill/peek/profiles` | manage agents |
| `copse setup [WS]` | re-run the repo's setup commands in a workspace |
| `copse mcp` | the MCP server agents talk to (launched for them; you don't run it) |

With no `WS` argument, commands act on the workspace you're in.

### Agent tools

Agents launched by copse get these MCP tools. You don't call them yourself, but
knowing them helps when you tell the supervisor how to work.

| Tool | Used by | |
|---|---|---|
| `assign` / `handoff` / `wait_for_worker` | supervisor | start a worker (return now / wait for its result / keep waiting) |
| `send_message` | any agent | message another agent; delivered when it's idle |
| `read_messages` | supervisor | read the messages agents and copse sent you and mark them read; with `message_delivery` `"pull"` (the default) you get a one-line "N new messages" notice instead of each message |
| `list_agents` / `list_tasks` / `list_agent_profiles` | supervisor | who's running, what's queued, which profiles exist |
| `cancel_task` | supervisor | cancel a queued task (and its dependents) to re-plan |
| `workspace_diff` | supervisor | a worker branch's changes against its base |
| `request_review` / `submit_review` | supervisor / reviewer | start a reviewer on a branch / record its verdict |
| `merge_workspace` / `remove_workspace` | supervisor | merge through the gates / delete the worktree |
| `report_result` | worker | finish a task and hand back the result |
| `submit_plan` | worker | a `plan_first` worker proposes its plan and waits for approval before editing |
| `approve_plan` | supervisor | approve a worker's plan, or send it back with feedback (`approved=false`) |
| `complete_subagent` | supervisor | record the result of a `subagent`-profile task |
| `set_goal` / `get_progress` / `check_milestone` | supervisor | autopilot's goal, its progress, and running the checks |
| `need_user` | supervisor | stop autopilot and ask you a question |
| `transfer_to_repo` | supervisor | move a scratch session's work into a repository |
| `handover` | supervisor | hand the session to a new supervisor on another branch or worktree, with a note |

## Token usage and history

Every Claude Code agent's token usage (input, cached, output, model) is read
straight from its own transcript JSONL under `~/.claude/projects/`, summed
incrementally so it's cheap to check often. It shows up:

- in the sidebar and `copse ls`, next to each agent (e.g. `191k tok`)
- appended to the result a worker or reviewer forwards to its supervisor
  (e.g. `tokens: 182k in (160k cached, 20k written) · 9k out · sonnet`)
- in `copse history`, per row, with a total across the rows shown. Each row
  holds only what its agent used since that agent's previous row, so the
  total never double counts

`copse history` is an append-only log of what happened: a worker's report, a
reviewer's verdict, a successful merge, and a milestone check (reports and
merges carry tokens). Unlike `copse ls`, it survives session pruning (`copse prune`), so
it's the place to look for what an agent did after its session is gone. It's
capped at 5000 rows per repo, oldest dropped first. Recording usage or
history never blocks a report, merge or check: a failure there is logged and
skipped.

## Provider quota

copse keeps one place (`~/.copse/quota.json`) that knows how close each
provider is to its subscription limit, and shows a note per provider that has
data (e.g. `Codex at 82% of its weekly limit, resets Thu 9:00am`) in `assign`
replies, `get_progress`, the sidebar's autopilot block and `copse doctor`.
It only uses what the CLIs write locally, and never reads a CLI's login
token or auth files or calls a provider's servers:

- **Claude**: the status line data Claude Code gives copse.
- **Codex**: the last `rate_limits` event in its newest session rollout under
  `~/.codex/sessions` (windows are told apart by their length: 5-hour,
  weekly, monthly), refreshed on each turn and when asked.
- **Antigravity**: no numbers; when it reports a limit error the provider counts
  as unavailable for `limit_cooldown_minutes` (default 300).
- **Native**: full headroom while the local model server answers, none while it doesn't.

## Repo config: `.copse/config.json`

A linked git worktree you made yourself doesn't have the git-ignored parts of
`.copse`; copse finds the config through the main worktree
(`git rev-parse --git-common-dir`), so there's nothing to symlink.

```json
{
  "setup": ["pnpm install", "cp \"$COPSE_ROOT_PATH/.env.local\" ."],
  "teardown": ["docker compose down"],
  "copy": [".env", "apps/*/.env"],
  "base_branch": "main",
  "branch_prefix": "",
  "default_agent": "developer",
  "fetch": true,
  "checks": ["uv run pytest -q"],
  "max_agents": 4,
  "pool_size": 1
}
```

Autopilot, merge gates and cleanup:

| Key | Default | |
|---|---|---|
| `autopilot` | `true` | start the supervisor with autopilot on |
| `checks` | `[]` | commands that must pass in a worker's branch before it merges |
| `review` | only under autopilot | require a reviewer's approval before merging |
| `reviewer` | `"reviewer"` | the agent profile that reviews (a Codex profile gives a second model's view) |
| `review_profile` | none | force `request_review`'s profile, skipping its automatic cross-model pick (see below) |
| `pre_commit` | `true` | run [pre-commit](https://pre-commit.com) over the branch, if the repo uses it |
| `max_agents` | `4` | workers running at once per session (`0`: no cap) |
| `check_timeout` | `900` | seconds each check may take |
| `usage_limit` | `90` | autopilot stops pushing on at this % of your Claude usage limit |
| `limit_cooldown_minutes` | `300` for Antigravity | how long a provider that hit its limit counts as unavailable |
| `graphify` | if the graph is there | point agents at the repo's [graphify](https://github.com/safishamsi/graphify) code map (`false` turns it off) |
| `stale_after` | `30` | minutes before a worker that reported and sat idle is closed (`0`: never) |
| `pipeline` | `true` | copse reviews and merges reported branches itself; the supervisor gets one message per branch |
| `review_rounds` | `2` | fix-and-re-review rounds the pipeline runs before handing findings to the supervisor |
| `merge_into` | none | branch that worker branches are cut from and merge into, whatever branch the supervisor is on |
| `auto_merge_default_branch` | `false` | let the pipeline merge into the repo's default branch (origin HEAD, else `main`/`master`) on its own; by default it sends a "needs you" message instead, and you run `merge_workspace` yourself (manual merges are never gated) |
| `plan_first` | `false` | workers propose a plan (`submit_plan`) and wait for `approve_plan` before editing |
| `overlap` | `"block"` | a task whose `files` overlap a running task's is refused (`"warn"` starts it with a warning) |
| `pool_size` | `1` if `setup` is set, else `0` | pre-built worktrees (checked out, files copied, setup run) kept ready so a new worker doesn't wait on `setup`; `0` disables it |
| `add_dirs` | `[]` | directories outside the worktree that Claude Code agents may use (`--add-dir`; full tool access, see "Directories outside the workspace") |
| `local_models` | `true` | when a native profile points at Ollama on this machine and it isn't running, `copse` starts `ollama serve` in the background (with the context length the profiles need) and loads their models; `false` leaves it to you |
| `sidebar` | `"left"` | where the dashboard sits in each window: `"left"` of the chat, or `"bottom"` (full-width rows under it) |
| `message_delivery` | `"pull"` | how agent and copse messages reach an interactive supervisor: `"pull"` keeps them unread and delivers one notice ("copse: 2 new messages (from 9f742c5c, pipeline). Call read_messages."; the sidebar shows an unread count), `"push"` delivers each message's text. Messages you send (`copse send`, typing) and messages to workers are always pushed |
| `learning` | `"off"` | the name of an installed learning plugin, which records how worker tasks turned out and suggests profiles (see below) |
| `learning_candidates` | `[]` | the profile names a learning plugin may pick from |
| `routing` | see below | for each task weight (`light`, `medium`, `heavy`), the profiles `assign`/`handoff` try in order |

### Routing by weight

The supervisor sizes a task and passes `weight` (`"light"`, `"medium"` or
`"heavy"`) to `assign`/`handoff`; copse picks an available profile for that tier.
Light is small, well-specified, mechanical work (docs, renames, simple tests);
medium is a normal feature or bugfix in one area; heavy is design-heavy,
cross-cutting work, subtle bugs or hard reasoning. The `routing` config maps each
tier to profile names, tried in order (defaults shown; set one tier and the others
keep theirs):

```json
{
  "routing": {
    "light":  ["developer-local", "developer"],
    "medium": ["developer-codex", "developer"],
    "heavy":  ["developer-heavy", "developer"]
  }
}
```

`developer-codex` runs on Codex; `developer-heavy` on Claude Fable at high effort.
A profile is skipped when its CLI isn't installed (`claude`, `codex`, `agy`), the
local model server isn't answering, or its provider is at your `usage_limit`. If a
learning plugin is selected it chooses among the profiles left; otherwise the first
wins. When every candidate is out, the repo's `default_agent` runs. The reply says
what was picked and why, e.g. `weight medium -> developer (Codex at 93%, skipped developer-codex)`.
An `agent_profile` you pass, or a milestone's `profile`, always wins over weight.

**Learning plugins.** copse can hand what happens to each worker task (review
verdicts, times the supervisor had to step in, merged or abandoned, tokens, time)
to a learning plugin, and ask it to pick a profile from `learning_candidates` when
`assign` gets none and no milestone names one; the reply then says
`profile chosen by learning: X`. A profile named by you or by a milestone always
wins. copse ships no plugin: a plugin is a package registering a `copse.learning`
entry point (see `copse/learning.py` for the interface), installed with
`uv tool install copse-agents --with <plugin>` and selected with
`"learning": "<name>"`. A plugin that's missing or fails never breaks a review,
merge or delegation.

**Closing and cleaning up.** Press `x` on an agent in the sidebar (twice for one
that's still running) or run `copse close <id>` to stop it and hide it. Stopping means
every process of the agent, not just its window: Claude Code can host a session in its
background daemon, where it would otherwise keep running. copse also cleans up on its
own, from the sidebar every minute and whenever `copse` starts: it stops anything left
running for agents that are paused, closed or whose window is gone, and closes workers
that reported and have been idle for `stale_after` minutes. Closing never touches a
worktree or branch, so unmerged work stays reviewable and mergeable. A worktree whose
branch is already merged into its base and whose agents are all finished drops out of
the sidebar; `copse prune` then removes it (the branch stays, and a worktree with
uncommitted changes is kept and listed). `prune` also kills copse tmux sessions that
hold only idle shells and no running agent, stops leftover copse tmux servers, and
removes stale locks and empty worktree folders.

**The pipeline.** The slow part of delegating isn't the workers, it's the supervisor's
turns between the stages: report, review, merge, remove, each waiting on a model turn
that carries the whole session's context. So copse runs those stages itself. When a
worker reports, copse starts the review at once and runs the checks in the
background; when the reviewer approves and the checks pass, copse merges the branch,
removes the worktree, and sends the supervisor one message with the worker's report
and the review. Findings go straight back to the worker to fix (`review_rounds`
times) before they reach the supervisor, and anything the pipeline can't settle (a
conflict, a failing check, no reviewer) arrives as "needs you" with the details.
`"pipeline": false` restores the manual flow. Supervisors are also told to keep task
briefs short: writing a long brief holds up every worker waiting on it.

**Spending fewer tokens.** Workers run only the tests that cover their change while
they work. The full suite runs once: as the repo's `checks` before a branch merges,
or, with no `checks`, by the worker just before it commits. copse runs the checks
the moment a worker reports and caches the result by commit, so the review and the
merge gate reuse it instead of each running the suite; `check_milestone` runs in
the background and delivers its result as a message, so nothing waits on a long
suite. An approved review carries over when copse merges the base branch into a
branch cleanly before merging it (the checks still run on the merged result), so a
branch that only fell behind isn't reviewed twice. Workers and reviewers get
copse's tools loaded up front (`tool_search: false` on their profiles; a chat keeps
Claude Code's on-demand loading, since it may carry your own MCP servers), so they
don't spend a round trip finding `report_result` at the end. And the supervisor is
told to size work first: a change it can make in a few minutes it makes itself,
since a worker plus its review costs about ten times as much. If the repo has a graphify
knowledge graph (`graphify-out/graph.json`, built with `/graphify`) and `graphify` is
installed, copse tells the supervisor and every worker to find code with
`graphify query` before grepping or reading whole files. Those commands are
pre-approved, and copse refreshes the graph's code (`graphify update`, no LLM) in
the background after each merge.

When `pool_size` is greater than `0`, a claimed worktree keeps the path and
port block it was built with -- it's never moved, and its port block is fixed
before `setup` ever runs. That means `setup` (and anything it writes) must
not depend on the workspace's branch name or assume it's running at
`<worktrees_dir>/<repo>/<branch>`; use `$COPSE_WORKSPACE_PATH` and
`$COPSE_BRANCH` instead of hardcoding either.

A pool entry's `setup` runs under a placeholder identity (a `copse-pool/*`
branch and a `pool-*` name) before any workspace claims it, but its
`teardown` can run later against the real workspace's branch and name -- or,
if the entry is discarded unclaimed, against that same placeholder identity.
Only `$COPSE_WORKSPACE_PATH` and `$COPSE_PORT_BASE` are guaranteed to be the
same value in both runs; `setup` must not write anything `teardown` needs to
find by branch or workspace name/id.

`.copse/config.local.json` is gitignored and overrides keys for you only. For
`setup`/`teardown` it can also give `{"before": [...], "after": [...]}` to run
commands around the team's list.

Setup, teardown, and agents all see these variables: `COPSE_ROOT_PATH`,
`COPSE_WORKSPACE_PATH`, `COPSE_WORKSPACE_NAME`, `COPSE_WORKSPACE_ID`,
`COPSE_BRANCH`, `COPSE_BASE_BRANCH`, and `COPSE_PORT_BASE`. Each workspace gets
ten ports, from `COPSE_PORT_BASE` to `COPSE_PORT_BASE+9`, so parallel dev
servers don't collide. Agents also get `COPSE_AGENT_ID`.

## Agent profiles

Markdown files with frontmatter. copse looks in `.copse/agents/`, then
`~/.copse/agents/`, then its built-ins (`supervisor`, `developer`, `reviewer`,
`reviewer-codex`, `developer-local`, `reviewer-local`, `subagent`):

```markdown
---
name: frontend
description: React/TypeScript specialist
provider: claude          # claude | codex | antigravity | native | shell | subagent
model: sonnet             # optional
permission_mode: acceptEdits   # optional, Claude Code only
---
You are a frontend engineer...
```

**Permissions.** The built-in `developer` profile runs in Claude Code's auto mode
(`permission_mode: auto`): a classifier approves ordinary actions and prompts only
for risky ones, while the profile's `allowed_tools` still apply. Every worker is
also told to run commands plainly from its own worktree, never to `cd` into or read
the main checkout, and to write files with its tools rather than shell heredocs;
those were the commands that stalled on prompts most. Workers run with Claude Code's normal permission prompts. When a
worker is waiting on one, `copse ls` shows it as `waiting`, and you attach to
approve it; if it's still waiting after 90 seconds, its supervisor gets a message
saying so (once); if the profile asked for auto mode, the message says so too, since a
prompt then means Claude Code switched auto mode off for that session. copse marks each worktree it starts Claude Code in as trusted,
so a worker never stops on the first-run "trust this folder?" dialog. The
built-in `reviewer` runs with `dontAsk`: anything outside its `allowed_tools` is
refused rather than waiting for an answer. The built-in `developer` profile edits files without asking
(`acceptEdits`) and has an `allowed_tools` list covering git inspect/commit and
common test/build commands: `pytest`, `uv run`, `npm/pnpm/yarn test|run`,
`cargo`, `go`, `make`, `swift`, `xcodebuild`. It can't push or run arbitrary
commands. Note that `npm run`, `make`, and `uv run` execute whatever the repo
defines, so only point workers at repos you trust. Override the list in
`.copse/agents/developer.md`. One thing Claude Code's rule matching does not
say up front: a `Write(path)` allow rule is not consulted by file permission
checks, only `Edit(path)` rules are, and an `Edit` rule covers every file-editing
tool. So write `Edit(docs/**)`, not `Write(docs/**)`, to let a worker create and
change files under a directory without prompts.

### Cheap workers

By default a Claude Code worker loads everything your own `claude` does: your
plugins, MCP servers and `~/.claude/CLAUDE.md`. That context is re-read on every
turn and can be most of a worker's input tokens. These optional fields (Claude
Code only, all off by default) trim it:

| Field | Passes | Effect |
|---|---|---|
| `strict_mcp: true` | `--strict-mcp-config` | Only copse's MCP server loads; your other MCP servers don't. |
| `setting_sources: project,local` | `--setting-sources` | Skips your user settings (`~/.claude`: plugins, hooks, `CLAUDE.md`). copse's own hooks come through `--settings`, which is always applied. |
| `effort: low` | `--effort` | `low`, `medium`, `high`, `xhigh` or `max`. |
| `headless: true` | `claude -p` | No interactive TUI; each turn is one `claude -p` run (see below). |

```markdown
---
name: cheap
description: Small, well-specified edits at low cost
provider: claude
model: sonnet
effort: low
strict_mcp: true
setting_sources: project,local   # no user plugins or ~/.claude/CLAUDE.md
headless: true
permission_mode: acceptEdits
allowed_tools: Bash(git add:*), Bash(git commit:*), Bash(git status:*), Bash(git diff:*), Bash(uv run:*), Bash(pytest:*)
---
You are a developer agent running under copse. Implement the task, run the
tests, commit, and report.
```

**Headless workers** still run in a tmux window, where a small copse runner
starts `claude -p` for the task, then `claude -p --resume <session>` for each
message sent to the worker while it's idle. Messages sent mid-turn are handed
over when the turn ends, as for any Claude worker, and so is the reminder to call
`report_result`. The window shows each turn's prompt and final answer (`copse
agent peek`). If `claude` exits with an error, the worker stops, and `handoff`
reports the error. Differences from an interactive worker:

- Nothing can answer a permission prompt, so any tool not covered by
  `permission_mode` and `allowed_tools` is refused rather than waiting for you.
  A headless worker never shows as `waiting`.
- You can't attach and type into it. Talk to it with `copse send` or `send_message`.
- A `done_when` finish line is added to the task, without `/goal` (an interactive command).

Blank values and anything after ` #` are ignored, so frontmatter can carry comments.
`env.NAME: value` lines set environment variables for the agent's process (see
"Open-weight models" for what that's for).

### Directories outside the workspace

A worktree is the agent's world, which is the point. Anything shared between
workspaces is outside it — a build cache instead of one per worker, a checked-out
reference repo, a folder of profiles kept beside the repo — and an agent that needs
it stops for a permission nobody can grant: a headless worker is refused, an
interactive one waits for a human who may not be watching.

`add_dirs` names those directories. It belongs in `.copse/config.json`, because a
shared cache is a property of the repository rather than of a role, and every
profile the repo launches needs the same list:

```json
{ "add_dirs": ["/srv/cargo-cache", "vendor/reference"] }
```

A profile may add to that list for a role that needs more, and never removes from
it:

```markdown
---
name: developer
provider: claude
add_dirs: /srv/extra
---
```

Both are passed as `--add-dir`, once per entry. Four things to know:

- **It is full tool access, not read access.** Claude Code's own help says
  "directories to allow tool access to": edits and Bash reach them too, so a worker
  in `acceptEdits` or `auto` can write into a directory you thought of as reference
  material, and four parallel workers can write into a shared cache with no prompt.
- **`CLAUDE.md` in those directories is loaded**, which is worth knowing before you
  add a directory that has one.
- **Relative entries resolve against the repo root**, not the worktree the agent
  runs in, and a leading `~` means your home directory. That holds in a profile in
  `~/.copse/agents` too, so `~/refs` there is the same directory in every repo,
  while a relative entry there resolves against whichever repo the agent runs in.
- **A directory that does not exist is reported**, because Claude Code ignores a
  missing `--add-dir` silently, which would be the failure this field exists to
  prevent. `copse doctor` checks the list; a launch from the terminal says so on
  stderr; `handoff`, `assign` and a queued task's start put it in what they tell
  the supervisor.

### Subagent workers

The built-in `subagent` profile (`provider: subagent`) gives a Claude Code
supervisor copse's worktree and branch handling for work done by its **own**
subagent (its Agent tool), with no separate `claude` process. `handoff` or
`assign` with it creates the workspace and returns immediately. The reply
contains the worktree path, the branch, the agent id and a ready-made prompt
for the Agent tool. That prompt tells the subagent to work only in that
directory, commit there and end with a summary. The supervisor then calls
`complete_subagent(agent_id, result)`. The worker shows as working until then
and done after. `workspace_diff`, `request_review`, `merge_workspace` and
`remove_workspace` work as usual. copse can't message a subagent, and
`send_message` says so. Worktrees live under `~/.copse/worktrees/`, outside the
supervisor's own directory. Unless the supervisor runs with permission to edit
there (`--add-dir ~/.copse/worktrees`, or `additionalDirectories` in Claude
Code settings), the subagent's edits ask for approval.

## Open-weight models

copse can run workers and reviewers on free, open-weight models (Qwen3-Coder,
GLM, DeepSeek, Kimi, gpt-oss, ...) served locally by Ollama, LM Studio or
llama.cpp, or by a hosted API. There are two ways in.

### The native provider

`provider: native` runs copse's own agent loop instead of a third-party CLI:
copse talks to the model's chat endpoint directly, runs its tool calls (Read,
Write, Edit, Glob, Grep, Bash, plus copse's `report_result`, `send_message`,
`submit_review` and `workspace_diff`), and reports the worker's status itself.
No hooks, no screen scraping, and messages sent to the worker arrive between
its model calls. It works with any OpenAI-compatible chat-completions endpoint
or Anthropic Messages endpoint.

```markdown
---
name: developer-local
provider: native
api: openai                        # openai (chat completions) | anthropic (messages)
base_url: http://localhost:11434/v1
model: qwen3-coder:30b
context_tokens: 32k                # the model's window, less room for its reply
api_key_env: OPENROUTER_API_KEY    # optional: the variable holding the key
permission_mode: acceptEdits
allowed_tools: Bash(git add:*), Bash(git commit:*), Bash(uv run:*), Bash(pytest:*)
---
You are a developer agent running under copse...
```

The built-in `developer-local` and `reviewer-local` profiles are set up for
Ollama with `qwen3-coder:30b` (19 GB; runs on a 32 GB machine). To use them:

```
brew install ollama            # or https://ollama.com/download
ollama pull qwen3-coder:30b
copse doctor                   # "model qwen3-coder:30b ... is available"
```

You don't need to run `ollama serve` yourself. When `copse` (or `copse continue`)
starts and a native profile points at Ollama on this machine that isn't answering,
copse starts it in the background with `OLLAMA_CONTEXT_LENGTH` set to the largest
`context_tokens` any profile asks of it plus room for the reply (40960 for the
built-ins), then loads each profile's model so the first task doesn't wait on the
read from disk. Its output goes to `~/.copse/ollama.log`. Only Ollama on a
loopback address is started; a remote endpoint is yours to run. `"local_models":
false` in `.copse/config.json` turns this off, and a server you started yourself
(with whatever settings) is left alone.

A server copse started is stopped again, along with the model it holds in memory,
when the last copse session that uses it ends: its chat is closed or paused, or
its tmux goes away (the next cleanup sweep catches that). Starting a new session
in the same checkout keeps it running for the new one.

Then a supervisor can `assign` a task to `developer-local`, or the repo config
can make the free model the reviewer: `"review_profile": "reviewer-local"`.
Other endpoints, same fields:

| Backend | `api` | `base_url` | Notes |
|---|---|---|---|
| Ollama (local) | openai | `http://localhost:11434/v1` | free; `ollama pull <model>` first |
| LM Studio | openai | `http://localhost:1234/v1` | free; load the model in the app |
| llama.cpp `llama-server` | openai | `http://127.0.0.1:8080/v1` | free; start with `--jinja` for tool calls |
| OpenRouter | openai | `https://openrouter.ai/api/v1` | `:free` models; `api_key_env: OPENROUTER_API_KEY` |
| Z.ai GLM | anthropic | `https://api.z.ai/api/anthropic` | GLM-4.7-Flash is free; `api_key_env: ZAI_API_KEY` |
| DeepSeek | anthropic | `https://api.deepseek.com/anthropic` | paid; `api_key_env: DEEPSEEK_API_KEY` |
| Anthropic | anthropic | `https://api.anthropic.com` | `api_key_env: ANTHROPIC_API_KEY` |

What to expect: a 30B-class local model does well on small, well-specified
tasks (the kind copse hands out: one change, the test named up front) and on
reviews of modest diffs, and less well on long multi-step work. The native
loop keeps it on rails: exact-match edits that fail loudly, one command at a
time, a reminder to report when a turn ends without one, and old context
folded into a summary when the window fills. Tool-calling quality varies by
model; if a model keeps mis-forming tool calls, try another (`qwen3-coder`,
`gpt-oss:20b` and `glm-4.7-flash` all support tools in Ollama). Set
`OLLAMA_CONTEXT_LENGTH` to at least `context_tokens` plus reply room, or Ollama
silently truncates the conversation. Native workers are always headless (no
TUI to attach to): `copse agent peek` shows each turn's prompt, tool calls and
answer, and `copse send` talks to them.

### Claude Code on another backend

Claude Code itself can be pointed at any Anthropic-compatible endpoint. A
profile's `env.NAME: value` lines set that up, and everything else about the
worker (hooks, permissions, the MCP tools) stays the same:

```markdown
---
name: developer-glm
provider: claude
model: glm-4.7-flash
env.ANTHROPIC_BASE_URL: https://api.z.ai/api/anthropic
env.ANTHROPIC_AUTH_TOKEN: ${ZAI_API_KEY}     # copse doesn't expand this: put the key itself here, or in ~/.copse/agents
env.ANTHROPIC_API_KEY:
---
```

Ollama (0.14+) serves the Anthropic API too: `env.ANTHROPIC_BASE_URL:
http://localhost:11434` with `env.ANTHROPIC_AUTH_TOKEN: ollama`. Anthropic
documents this route as unsupported for non-Claude models, and each vendor
documents its own quirks (no prompt caching on most, smaller context windows),
so prefer the native provider for open-weight models and keep this route for
Claude itself behind a gateway.

Codex agents report status through Codex's `notify` hook (copse passes
`-c notify=[...]` at launch, leaving your own Codex config alone): a completed turn
marks the agent idle and delivers any queued message.

## Google Antigravity

copse runs Google Antigravity's terminal agent, `agy`, as well as Claude Code and
Codex. Install it and sign in once:

```sh
curl -fsSL https://antigravity.google/cli/install.sh | bash
agy        # sign in with your Google account, then quit
```

Then run the whole session on it with `copse --provider antigravity`, or mix models:
give a profile `provider: antigravity` (for example a `gemini-reviewer` for a second
model's review) and the supervisor can hand it tasks.

`agy` has no command-line options for hooks, MCP servers or instructions, so copse
adds three files to the checkout's `.agents/` folder: `mcp_config.json` (copse's tools),
`hooks.json` (status, messages, autopilot) and `rules/copse.md`. They're listed in
`.git/info/exclude`, so they never show up in `git status`. copse adds to these files if
you already have them, and won't change one that's committed. Each agent's first
message is a short warm-up with its instructions, because `agy` connects MCP servers
only once a conversation has started.

**Permissions.** `agy` doesn't let hooks approve shell commands, so an Antigravity
agent asks before running anything your own `agy` settings don't already allow, and
the sidebar shows it as needing you. To let agents run tests and commit without asking,
add rules to `~/.gemini/antigravity-cli/settings.json`, for example:

```json
{ "permissions": { "allow": ["command(uv run pytest)", "command(git status)",
                               "command(git diff)", "command(git add)", "command(git commit)"] } }
```

## How it works

- **Look:** copse's tmux sessions get their own dark purple theme and mouse
  scrolling. Your own tmux setup and other sessions are untouched (apart from
  tmux's `focus-events`, which Claude Code asks for).
- **State** lives in `~/.copse/copse.db` (SQLite, WAL mode). The CLI, the hooks,
  and every agent's MCP server share it. Worktrees live in
  `~/.copse/worktrees/<repo>/<branch>`, and the base branch is recorded in git
  config as `branch.<b>.copse-base`.
- **Messages arrive through Claude Code's own inbox.** Every Claude Code session
  listens on a socket for messages from other sessions, and tells its hooks where
  it is; copse's SessionStart hook records it, and from then on messages go there
  instead of being typed into the pane. An idle agent starts a new turn with the
  message; a busy one gets it between tool calls. Typing into the pane is the
  fallback when there's no socket (Codex, Antigravity, older Claude Code). A
  `crossSessionInbound` of `hold` or `refuse` in your Claude settings would hold or
  drop them.
- **Agent status comes from hooks, not screen-scraping.** Guessing an agent's
  state by pattern-matching terminal output breaks whenever a CLI redesigns its
  interface. copse launches Claude Code with `--settings` hooks
  (`SessionStart`, `UserPromptSubmit`, `Stop`, `StopFailure`, `Notification`) that call
  `copse _hook <event>`. The `Stop` hook also delivers queued messages: it
  returns `{"decision": "block", "reason": <message>}`, so Claude continues with
  the message as its next instruction and nothing is typed into a busy terminal.
  A message queued for an *idle* agent is typed in instead, but only once copse
  checks the screen and finds a clear chat input: not text you're still typing,
  and not Claude Code's background-session launcher (which would otherwise
  start a whole new session). Otherwise it stays queued for the next chance.
- **Results are explicit.** Workers call the `report_result` MCP tool instead of
  having their output parsed from the screen. If a worker stops without
  reporting, the Stop hook reminds it once.
- **Worker isolation:** `handoff`/`assign` with `isolate=true` (the default)
  create a worktree whose branch starts from the *supervisor's* current branch,
  so workers build on the supervisor's committed work. `merge_workspace`
  brings a worker's branch back.

## Development

```sh
uv sync
uv run pytest
```

### Releasing

1. Bump `version` in `pyproject.toml`, commit, and push.
2. Create a GitHub release tagged `v<version>` (e.g. `gh release create v0.1.1 --generate-notes`).
3. The Publish workflow tests, builds, and uploads to PyPI via Trusted Publishing.


## License

Apache-2.0
