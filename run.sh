#!/usr/bin/env bash
# Runs the scheduler loop with sleep inhibited, at low CPU priority, on a private Xvfb display if available.
set -euo pipefail
cd "$(dirname "$0")"
NICE=$(.venv/bin/python -c "from agent.config import load_config; print(load_config().get('schedule', {}).get('nice', 10))")
CMD=(nice -n "$NICE" .venv/bin/python -m agent loop "$@")
if command -v xvfb-run >/dev/null; then
  export APPLY_AGENT_XVFB=1
  CMD=(xvfb-run -a -s "-screen 0 1366x900x24" "${CMD[@]}")
fi
exec systemd-inhibit --what=sleep:idle --who=apply-agent --why="applying to internships" "${CMD[@]}"
