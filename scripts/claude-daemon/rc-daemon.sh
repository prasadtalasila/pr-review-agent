#!/usr/bin/env bash
# Keep `claude remote-control` running: restart it whenever it exits
# (e.g. "Persistent errors for 10 minutes, giving up." — that 10-minute
# give-up is hardcoded in Claude Code and cannot be changed by a setting).
#
# Usage: rc-daemon.sh start|stop|restart|status|attach|log
#   start   - launch the supervisor in a detached tmux session (idempotent)
#   attach  - attach to the tmux session (detach with Ctrl-b d)
#
# Override defaults with env vars, e.g. RC_DIR=/other/dir rc-daemon.sh start
set -u

RC_DIR="${RC_DIR:-/home/prasad/claude-daemon}"
RC_NAME="${RC_NAME:-cg-dev}"
RC_PREFIX="${RC_PREFIX:-cg-dev}"
RC_PERMISSION_MODE="${RC_PERMISSION_MODE:-bypassPermissions}"
TMUX_SESSION="${TMUX_SESSION:-rc-daemon}"
LOG="${RC_LOG:-$HOME/claude-daemon/rc-daemon.log}"
SELF="$(readlink -f "$0")"

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG"; }

# The restart loop itself; runs inside tmux so claude gets a real TTY.
supervise() {
  local backoff=5
  cd "$RC_DIR" || { log "cannot cd to $RC_DIR"; exit 1; }
  while true; do
    log "starting claude remote-control --name $RC_NAME in $RC_DIR"
    local started=$SECONDS
    claude remote-control \
      --name "$RC_NAME" \
      --permission-mode "$RC_PERMISSION_MODE" \
      --remote-control-session-name-prefix "$RC_PREFIX"
    local rc=$? ran=$((SECONDS - started))
    log "claude remote-control exited with code $rc after ${ran}s"
    # Reset backoff after a healthy run; otherwise back off up to 5 minutes.
    if (( ran > 300 )); then backoff=5; else backoff=$(( backoff * 2 > 300 ? 300 : backoff * 2 )); fi
    log "restarting in ${backoff}s"
    sleep "$backoff"
  done
}

running() { tmux has-session -t "$TMUX_SESSION" 2>/dev/null; }

case "${1:-}" in
  start)
    if running; then echo "already running (tmux session '$TMUX_SESSION')"; exit 0; fi
    tmux new-session -d -s "$TMUX_SESSION" \
      "RC_DIR='$RC_DIR' RC_NAME='$RC_NAME' RC_PREFIX='$RC_PREFIX' RC_PERMISSION_MODE='$RC_PERMISSION_MODE' RC_LOG='$LOG' '$SELF' _supervise"
    echo "started in tmux session '$TMUX_SESSION'; log: $LOG"
    ;;
  stop)
    # Ctrl-C lets claude shut down cleanly, then kill the session/loop.
    running && { tmux send-keys -t "$TMUX_SESSION" C-c; sleep 3; tmux kill-session -t "$TMUX_SESSION" 2>/dev/null; }
    log "stopped"
    ;;
  restart) "$SELF" stop; "$SELF" start ;;
  status)
    if running; then echo "running (tmux session '$TMUX_SESSION')"; else echo "not running"; exit 1; fi
    ;;
  attach) exec tmux attach -t "$TMUX_SESSION" ;;
  log) exec tail -n 50 -f "$LOG" ;;
  _supervise) supervise ;;
  *) echo "usage: $0 start|stop|restart|status|attach|log"; exit 2 ;;
esac
