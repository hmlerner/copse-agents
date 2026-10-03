copse 0.15.2 handles CLIs that aren't signed in, and makes Codex a full supervisor.

## Fixed
- **A CLI that isn't signed in isn't offered.** Profiles on Claude Code or Codex are left out of the supervisor's profile list, routing by weight and the default reviewer when that CLI isn't signed in. Naming one directly stops with how to sign in (`claude auth login`, `codex login`) instead of opening its login screen. `copse doctor` shows each CLI's sign-in. Keys in the environment count as signed in. (#42)
- **A silent worker is reported.** A Codex worker that shows nothing new for 10 minutes without reporting (for example, signed in without a plan that includes Codex) is reported to its supervisor once, with its screen. (#42)
- **Codex as an autopilot supervisor keeps going.** Codex has no Stop hook, so copse now nudges it at the end of a turn, like Claude Code.
- **The Codex bundled with the ChatGPT app counts as installed** for reviewer choice and routing.
- **copse shows the main checkout's current branch,** not the branch it was on when copse first saw it.
- **`copse --provider native` or `subagent` stops with a clear message:** those providers run workers, not the supervisor. The README now lists what each provider can do.

## Upgrading
`uv tool upgrade copse-agents`.
