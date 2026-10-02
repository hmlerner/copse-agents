copse 0.15.0 lets you choose how readily the supervisor delegates, keeps your preferences in step across machines, and cleans up merged branches.

## Added
- **Delegation setting.** `copse delegation conservative|balanced|fast` sets how readily the supervisor hands work to workers: conservative does most work in its own chat (fewest tokens), fast splits any multi-part request across parallel workers straight away (quickest), and balanced, the new default, sits between. It's saved in `~/.copse/config.json` for every repo and session (`--repo` for one repo only), and a running supervisor is told at once.
- **`~/.copse/config.json`** holds your own defaults for every repo. A repo's `.copse/config.json` and `config.local.json` override it.
- **Settings sync (Pro and up).** Your user-wide preferences (delegation, sidebar position and a few others from a fixed list) follow you to other machines. `copse account sync` syncs now; `copse` syncs in the background as it starts. Command lists, paths and repo settings never leave the machine.
- **Merged branches are deleted.** Removing a worktree (after a merge, `copse rm`, `copse prune`, session cleanup) also deletes its branch once every commit is in its base, so finished branches no longer pile up. Unmerged branches are always kept; `"delete_merged_branches": false` keeps them all, and `copse rm -K` keeps one. The first `copse pr` in a repo offers to turn on GitHub's automatic deletion of merged PR branches.

## Changed
- **No more "Stop hook error" in your chat.** Claude Code labels every blocked stop as an error. copse's reminders to a chat you're watching (unread messages, autopilot's "keep going") now arrive as a message from copse instead.
- **Hosted learning is per person (Pro) or per organization (Team), and your data stays private.** It runs only on our servers, never learns across accounts, and is encrypted so even PawDelta staff can't read it. `learning` now takes `auto`, `cloud` or `off`.
- Any number of `copse` sessions can share a folder: each new one gets its own worktree (`copse/session-2`, `-3`, ...).

## Fixed
- **A working supervisor no longer shows as idle** after a background task or queued message starts its next turn.
- **The sidebar no longer goes missing.** If it was left behind in a worker's session (a missed tmux hook), it moves itself back beside the chat you're looking at within a few seconds.

## Upgrading
`uv tool upgrade copse-agents`. The sidebar fix and the new delegation rule take effect in sessions started after the upgrade.
