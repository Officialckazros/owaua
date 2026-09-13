#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd -P)
if [[ -z "${OWAUA_DEPLOY_SCRIPT:-}" ]]; then
  for candidate in \
    "$HOME/.config/owaua-deploy/daki_client.py" \
    "$ROOT_DIR/../owaua/scripts/deploy"
  do
    if [[ -f "$candidate" ]]; then
      OWAUA_DEPLOY_SCRIPT=$candidate
      break
    fi
  done
fi
OWAUA_DEPLOY_SCRIPT=${OWAUA_DEPLOY_SCRIPT:-"$HOME/.config/owaua-deploy/daki_client.py"}

if [[ ! -f "$OWAUA_DEPLOY_SCRIPT" ]]; then
  echo "Cannot find the Daki deployment client: $OWAUA_DEPLOY_SCRIPT" >&2
  exit 1
fi
PERSONA_MODELS="${*:-rudeish nerdish explicit}"

ROOT_DIR="$ROOT_DIR" OWAUA_DEPLOY_SCRIPT="$OWAUA_DEPLOY_SCRIPT" PERSONA_MODELS="$PERSONA_MODELS" python3 - <<'PY'
import hashlib
import importlib.machinery
import importlib.util
import os
from pathlib import Path

root = Path(os.environ["ROOT_DIR"])
deploy_path = Path(os.environ["OWAUA_DEPLOY_SCRIPT"])
model_files = {
    "rudeish": "personas/rudeish.txt",
    "nerdish": "personas/nerdish.txt",
    "explicit": "personas/explicit.txt",
}
requested = os.environ["PERSONA_MODELS"].replace(",", " ").replace("/", " ").split()
models = []
for model in requested:
    model = model.lower()
    if model == "and":
        continue
    if model not in model_files:
        valid = ", ".join(model_files)
        raise RuntimeError(f"Unknown persona '{model}'. Choose: {valid}")
    if model not in models:
        models.append(model)
if not models:
    models = ["rudeish", "nerdish", "explicit"]

files = {model: root / model_files[model] for model in models}
missing = [str(path) for path in files.values() if not path.is_file()]
if missing:
    raise RuntimeError("Missing persona file(s): " + ", ".join(missing))

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

for model, local_path in files.items():
    remote_path = f"persona-test-bot/{model_files[model]}"
    payload = local_path.read_bytes()
    client.write_file(remote_path, payload)
    encoded = remote_path.replace("/", "%2F")
    readback = client.request(
        "GET", client.server_path(f"/files/contents?file=%2F{encoded}"), expect_json=False
    )
    if readback != payload:
        raise RuntimeError(f"Daki verification failed for {remote_path}")
    print(f"Updated {remote_path} (sha256={hashlib.sha256(payload).hexdigest()[:12]})")
print("Uploaded successfully. No restart is needed; personas reload for each reply.")
PY
