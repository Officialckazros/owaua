#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
ROOT_DIR=$(pwd -P)
PID_FILE="$ROOT_DIR/.bot.pid"

# Stop copies started by an earlier ./run.sh (or an older manual launch).
while read -r pid command; do
  [[ -n "$pid" && "$pid" != "$$" ]] || continue
  case "$command" in
    *"$ROOT_DIR/bot.py"*|*"Python bot.py"*) kill "$pid" 2>/dev/null || true ;;
  esac
done < <(ps -axo pid=,command=)

for _ in {1..20}; do
  active=0
  if [[ -f "$PID_FILE" ]]; then
    old_pid=$(<"$PID_FILE")
    if kill -0 "$old_pid" 2>/dev/null; then active=1; else rm -f "$PID_FILE"; fi
  fi
  [[ "$active" == 0 ]] && break
  sleep 0.1
done

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env. Fill in DISCORD_TOKEN and OPENAI_API_KEY, then run this again."
  exit 1
fi

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
fi

cleanup() {
  rm -f "$PID_FILE"
}
trap cleanup EXIT INT TERM

.venv/bin/python "$ROOT_DIR/bot.py" &
bot_pid=$!
printf '%s\n' "$bot_pid" > "$PID_FILE"
wait "$bot_pid"
