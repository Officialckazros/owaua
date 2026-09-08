#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd -P)
OWAUA_DEPLOY_SCRIPT=${OWAUA_DEPLOY_SCRIPT:-"$ROOT_DIR/../owaua/scripts/deploy"}

if [[ ! -f "$OWAUA_DEPLOY_SCRIPT" ]]; then
  echo "Cannot find the Daki deployment client: $OWAUA_DEPLOY_SCRIPT" >&2
  exit 1
fi

ROOT_DIR="$ROOT_DIR" OWAUA_DEPLOY_SCRIPT="$OWAUA_DEPLOY_SCRIPT" python3 - <<'PY'
import hashlib
import importlib.machinery
import importlib.util
import os
import time
from pathlib import Path

root = Path(os.environ["ROOT_DIR"])
deploy_path = Path(os.environ["OWAUA_DEPLOY_SCRIPT"])

excluded = {Path("scripts/deploy.sh"), Path("scripts/update-persona.sh")}
required_runtime = {
    Path("bot.py"),
    Path("memory_store.py"),
    Path("requirements.txt"),
    Path("scripts/run-bots.sh"),
}
files = sorted(
    path
    for path in root.rglob("*")
    if path.is_file()
    and not any(part.startswith(".") for part in path.relative_to(root).parts)
    and path.relative_to(root) not in excluded
    and (path.suffix == ".py" or path.suffix in {".sh", ".txt"})
)
if not files:
    raise RuntimeError("No deployable runtime files found")
relative_files = {path.relative_to(root) for path in files}
missing_runtime = required_runtime - relative_files
if missing_runtime:
    raise RuntimeError(
        "Deployment is incomplete; missing required runtime file(s): "
        + ", ".join(sorted(missing_runtime))
    )

loader = importlib.machinery.SourceFileLoader("daki_deploy", str(deploy_path))
spec = importlib.util.spec_from_loader(loader.name, loader)
if spec is None:
    raise RuntimeError("could not load the Daki deployment client")
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)

config = module.load_config()
client = module.DakiClient(config["panel_url"], config["api_key"], config["server_id"])
if client.state() != "running":
    raise RuntimeError("Daki Bots server is not running; deployment was not uploaded")

for local_path in files:
    relative_path = local_path.relative_to(root).as_posix()
    remote_path = f"persona-test-bot/{relative_path}"
    payload = local_path.read_bytes()
    client.write_file(remote_path, payload)
    encoded = remote_path.replace("/", "%2F")
    readback = client.request(
        "GET",
        client.server_path(f"/files/contents?file=%2F{encoded}"),
        expect_json=False,
    )
    if readback != payload:
        raise RuntimeError(f"Daki verification failed for {remote_path}")
    print(f"Uploaded {remote_path} (sha256={hashlib.sha256(payload).hexdigest()[:12]})")

client.update_startup_variable("STARTUP_CMD", "")
client.update_startup_variable(
    "SECOND_CMD", "cd persona-test-bot && bash scripts/run-bots.sh"
)
print(f"Restarting {config.get('server_name', config['server_id'])}...")
client.restart()
deadline = time.monotonic() + 180
while time.monotonic() < deadline:
    if client.state() == "running":
        print("Deployment completed successfully.")
        break
    time.sleep(2)
else:
    raise RuntimeError("Daki server did not return to running state within 180 seconds")
PY
