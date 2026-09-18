#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd -P)
cd "$ROOT_DIR"

ENV_FILE=${OWAUA_ENV_FILE:-"$ROOT_DIR/.env"}
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Runtime env file is missing: $ENV_FILE" >&2
  exit 1
fi
export OWAUA_ENV_FILE="$ENV_FILE"
export PYTHONPATH="$ROOT_DIR/src/owaua${PYTHONPATH:+:$PYTHONPATH}"

PYTHON=${OWAUA_PYTHON:-python3}
if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON="$ROOT_DIR/.venv/bin/python"
fi

if command -v uv >/dev/null 2>&1; then
  uv pip install -q -r requirements.txt --python "$PYTHON"
  uv pip check --python "$PYTHON"
else
  "$PYTHON" -m pip install -q -r requirements.txt
  "$PYTHON" -m pip check
fi
chmod 600 "$ENV_FILE"
if [[ "${OWAUA_LOCAL_ONLY:-$(grep -E '^OWAUA_LOCAL_ONLY=' "$ENV_FILE" | tail -n 1 | cut -d= -f2-)}" =~ ^(1|true|yes|on)$ ]]; then
  OWAUA_ENV_FILE="$ENV_FILE" "$ROOT_DIR/scripts/start-local-ai.sh"
fi
if [[ "${OWAUA_VERIFY_DEPLOY:-0}" == "1" ]]; then
  "$PYTHON" scripts/check-runtime.py
  # Use this release's suite; old server-only test files are not part of it.
  PYTHONPATH="$ROOT_DIR/src/owaua:$ROOT_DIR/tests" "$PYTHON" -m unittest -q \
    test_ask test_bot_helpers test_channel_commands test_cloudflare \
    test_memory test_music_attachments test_security
  echo "OWAUA_DEPLOY_TESTS_PASSED"
fi
exec "$PYTHON" src/owaua/bot.py
