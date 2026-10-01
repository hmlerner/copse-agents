copse 0.14.4 makes copse quicker to install and to try for the first time.

## New
- **One-line install.** `curl -fsSL pawdelta.com/copse/install | sh` installs uv and tmux if they're missing, then copse, then runs `copse doctor`. Run it again to upgrade.
- **`copse init` detects your stack.** It reads your lockfiles and manifests (uv, Poetry, npm/pnpm/yarn/bun, Cargo, Go, Bundler, Make) and writes `.copse/config.json` with the setup a new worktree needs, the checks that must pass before a branch merges, and the git-ignored env files to copy into each worktree. It never overwrites an existing config.
- **`copse demo`.** Watch copse finish a tiny practice repo in a few minutes: two workers in parallel, a review of each branch, gated merges, and milestones that turn green only once their tests pass. `copse demo --local` runs it on Ollama.
- **`copse history --share`.** Sums up a session in a few lines to paste into Slack or a post: goal, milestones verified, workers, merges, reviews, parallel speedup and tokens.

## Changed
- **Bare `copse` checks before it launches.** If tmux or the chat's CLI is missing, it says how to install it instead of opening an empty window.
- **Quieter `copse doctor`.** Optional tools (Codex, local models, gh, ...) are listed separately and no longer count as warnings.
- **The supervisor asks for checks.** In a repo with no `checks`, it proposes one (the test command copse detected) before assigning work.
- **PR footer.** `copse pr` and `copse ci` end the PR description with one "built with copse" line. Set `"pr_footer": false` in `.copse/config.json` to leave it out.
- Workers no longer stop for approval on `python3 -m pytest` or `python -m unittest`.

## Upgrading
`uv tool upgrade copse-agents`, or run the install line again. If you keep your own profiles in `~/.copse/agents/`, add `Bash(python3 -m pytest:*)` and `Bash(python3 -m unittest:*)` to their `allowed_tools` to get the new approvals.
