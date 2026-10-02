copse 0.14.6 lets you run two copse sessions in the same repo at once.

## New
- **A second `copse` in a busy folder asks first.** If a session is already running where you start `copse`, you choose: open that session, start the new one in its own worktree, or pause the old one and start fresh. A new session gets its own branch, `copse/session-N`, cut from what you have checked out, so both sessions run at the same time without sharing files. Before, the running session was always paused.

## Changed
- Without a terminal to ask in (scripts, `--no-attach`), copse still pauses the running session and starts fresh, as before.

## Upgrading
`uv tool upgrade copse-agents`, or `curl -fsSL pawdelta.com/copse/install | sh`.
