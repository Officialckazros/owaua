#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd -P)
cd "$ROOT_DIR"

python -m pip install -q -r requirements.txt
python -m pip check
chmod 600 .env
if [[ "${OWAUA_VERIFY_DEPLOY:-0}" == "1" ]]; then
  python scripts/check-runtime.py
  # Use this release's suite; old server-only test files are not part of it.
  PYTHONPATH="$ROOT_DIR/tests" python -m unittest -q \
    test_ask test_bot_helpers test_channel_commands test_memory \
    test_music_attachments test_security
  echo "OWAUA_DEPLOY_TESTS_PASSED"
fi
exec python bot.py
