copse 0.14.5 fixes a supervisor that could stop hearing from copse, a crash in `copse sessions`, and `copse attach` opening the wrong window. `copse demo` now shows two independent merges.

## Fixed
- **The supervisor no longer misses messages.** copse tells a supervisor about new messages with a one-line notice. Claude Code drops a message that's identical to the previous one from the same sender, so when two notices in a row read the same (a merge report, then autopilot's nudge to keep going), the second was dropped and the supervisor sat idle with work merged and milestones unchecked. Each notice now carries the time, e.g. `copse (16:25:03): 1 new message (from pipeline). Call read_messages.`
- **`copse demo` merges each branch on its own.** Its merge check was the full test suite, which neither branch could pass until the other had landed, so the supervisor ended up merging the branches together. Merges now need the code to compile, and the milestone checks verify behaviour.

- **`copse sessions` no longer crashes** with `TypeError: unhashable type: 'Workspace'` when a paused session has workers (#34).
- **`copse attach <workspace>` opens the agent that needs you.** It used to land on the session's empty shell window. Now it opens the agent waiting on a prompt, else the busiest, else the newest. Run without a terminal (from a chat's `!`), it says so instead of switching some other window (#35).
- **Messaging a paused agent says why it failed**: "agent X is paused ... `copse continue`", not a bare tool error.
- **A worker stuck on a prompt** is reported more accurately: auto mode still asks about some actions on purpose, such as entering another worktree. Workers are also told not to create or enter other worktrees.

## Upgrading
`uv tool upgrade copse-agents`, or `curl -fsSL pawdelta.com/copse/install | sh`. A running session keeps the old behaviour until you restart it.
