#!/usr/bin/env bash
set -euo pipefail

# Daki's second startup command supervises both independent Discord clients.
# Each bot has its own working directory and .env, so their tokens and RAM-only
# conversation state cannot be mixed.
python -m pip install -q -r persona-test-bot/requirements.txt

(set -a; . ./.env; set +a; PYTHONPATH=src python -m owaua.bot) &
owaua_pid=$!

(cd persona-test-bot; set -a; . ./.env; set +a; python bot.py) &
persona_pid=$!

stop_children() {
  kill "$owaua_pid" "$persona_pid" 2>/dev/null || true
}
trap stop_children EXIT INT TERM

wait -n "$owaua_pid" "$persona_pid"
status=$?
stop_children
wait "$owaua_pid" "$persona_pid" 2>/dev/null || true
exit "$status"
