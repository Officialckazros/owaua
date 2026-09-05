#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "$0")" && pwd -P)
OWAUA_DEPLOY_SCRIPT=${OWAUA_DEPLOY_SCRIPT:-"$ROOT_DIR/../owaua/scripts/deploy"}

if [[ ! -f "$OWAUA_DEPLOY_SCRIPT" ]]; then
  echo "Cannot find the Daki deployment client: $OWAUA_DEPLOY_SCRIPT" >&2
  exit 1
fi
if [[ ! -f "$ROOT_DIR/persona.py" ]]; then
  echo "Cannot find persona.py" >&2
  exit 1
fi

ROOT_DIR="$ROOT_DIR" OWAUA_DEPLOY_SCRIPT="$OWAUA_DEPLOY_SCRIPT" python3 - <<'PY'
import hashlib
import importlib.machinery
import importlib.util
import os
from pathlib import Path

root = Path(os.environ["ROOT_DIR"])
deploy_path = Path(os.environ["OWAUA_DEPLOY_SCRIPT"])
loader = importlib.machinery.SourceFileLoader("daki_deploy", str(deploy_path))
spec = importlib.util.spec_from_loader(loader.name, loader)
if spec is None:
    raise RuntimeError("could not load the Daki deployment client")
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)

config = module.load_config()
client = module.DakiClient(config["panel_url"], config["api_key"], config["server_id"])
if client.state() != "running":
    raise RuntimeError("Daki Bots server is not running; persona was not uploaded")

payload = (root / "persona.py").read_bytes()
client.write_file("persona-test-bot/persona.py", payload)
readback = client.request(
    "GET",
    client.server_path("/files/contents?file=%2Fpersona-test-bot%2Fpersona.py"),
    expect_json=False,
)
if readback != payload:
    raise RuntimeError("Daki verification failed for persona-test-bot/persona.py")

print(
    "Updated persona-test-bot/persona.py only "
    f"(sha256={hashlib.sha256(payload).hexdigest()[:12]}); no restart performed."
)
PY
