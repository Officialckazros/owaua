#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd -P)
cd "$ROOT_DIR"

ENV_FILE=${OWAUA_LOCAL_ENV_FILE:-"$ROOT_DIR/.env"}
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Local env file is missing: $ENV_FILE" >&2
  exit 1
fi
chmod 600 "$ENV_FILE"

PYTHON=${OWAUA_PYTHON:-python3}
if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON="$ROOT_DIR/.venv/bin/python"
fi

echo "Installing local dependencies..."
if command -v uv >/dev/null 2>&1; then
  uv pip install -q -r requirements.txt --python "$PYTHON"
  uv pip check --python "$PYTHON"
else
  "$PYTHON" -m pip install -q -r requirements.txt
  "$PYTHON" -m pip check
fi

echo "Verifying local runtime with $(basename "$ENV_FILE")..."
OWAUA_ENV_FILE="$ENV_FILE" "$PYTHON" -m py_compile \
  src/owaua/ask.py src/owaua/bot.py src/owaua/cloudflare.py \
  src/owaua/memory.py src/owaua/music.py src/owaua/security.py \
  src/owaua/music_worker.py src/owaua/media_exec.py

if [[ "${OWAUA_RUN_LOCAL_TESTS:-0}" == "1" ]]; then
  OWAUA_ENV_FILE="$ENV_FILE" OWAUA_LOCAL_ONLY=0 \
    OPENAI_API_KEY=test DEEPSEEK_API_KEY=test PERPLEXITY_API_KEY=test \
    GROQ_API_KEY=test MISTRAL_API_KEY=test \
    PYTHONPATH="$ROOT_DIR/src/owaua:$ROOT_DIR/tests" "$PYTHON" -m unittest -q \
    test_ask test_bot_helpers test_cloudflare test_memory
fi

echo "Local deployment verified. The running local bot uses: $ENV_FILE"
echo "Start it with: OWAUA_ENV_FILE=$ENV_FILE ./scripts/run-bots.sh"
