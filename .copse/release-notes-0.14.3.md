copse 0.14.3 gives the copse window the same look as pawdelta.com/copse.

## Changed
- **Canopy colors in tmux and the sidebar.** The old indigo theme is gone. A copse session now has a dark forest-green background, a green status bar badge and active pane border, and grey-green secondary text. The sidebar draws its logo, selection bar and idle agents in canopy green, working agents in lavender and "needs you" in amber. Terminals with only 8 colors and `copse watch --once` follow the same scheme.

## Upgrading
`uv tool upgrade copse-agents`. A copse session that is already running keeps its old colors until you restart it.
