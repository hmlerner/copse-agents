#!/bin/sh
# debug: log tmux lifecycle hooks with the process behind the client that ran the command
hook=$1; pane=$2; client=$3; status=$4
log=/tmp/copse-tmux-hooks.log
line="$(date +%T) $hook pane=$pane status=$status client=$client"
if [ -n "$client" ] && [ "$client" != "0" ]; then
  p=$(ps -o ppid= -p "$client" 2>/dev/null | tr -d ' ')
  line="$line parent=$p [$(ps -o command= -p "$p" 2>/dev/null | cut -c1-200)]"
  gp=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
  line="$line gparent=$gp [$(ps -o command= -p "$gp" 2>/dev/null | cut -c1-200)]"
fi
echo "$line" >> "$log"
