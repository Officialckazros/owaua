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

ROOT_DIR="$ROOT_DIR" OWAUA_DEPLOY_SCRIPT="$OWAUA_DEPLOY_SCRIPT" python3 - <<'PY'
import hashlib
import importlib.machinery
import importlib.util
import os
import time
from pathlib import Path

root = Path(os.environ["ROOT_DIR"])
deploy_path = Path(os.environ["OWAUA_DEPLOY_SCRIPT"])

env_file = root / ".env"
if not env_file.is_file():
    raise RuntimeError(".env is missing; deployment was not uploaded")
env_payload = env_file.read_bytes()

excluded = {
    Path("scripts/deploy.sh"),
    Path("scripts/update-persona.sh"),
    Path("personas/update-persona.sh"),
}
required_runtime = {
    Path("bot.py"),
    Path("ask.py"),
    Path("memory.py"),
    Path("music.py"),
    Path("security.py"),
    Path("music_worker.py"),
    Path("media_exec.py"),
    Path("personas/rudeish.txt"),
    Path("personas/nerdish.txt"),
    Path("personas/explicit.txt"),
    Path("requirements.txt"),
    Path("scripts/run-bots.sh"),
    Path("scripts/check-runtime.py"),
}
files = sorted(
    path
    for path in root.rglob("*")
    if path.is_file()
    and not any(part.startswith(".") for part in path.relative_to(root).parts)
    and path.relative_to(root) not in excluded
    and (
        path.suffix == ".py"
        or path.suffix in {".sh", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".gif"}
    )
)
if not files:
    raise RuntimeError("No deployable runtime files found")
relative_files = {path.relative_to(root) for path in files}
missing_runtime = required_runtime - relative_files
if missing_runtime:
    raise RuntimeError(
        "Deployment is incomplete; missing required runtime file(s): "
        + ", ".join(sorted(map(str, missing_runtime)))
    )

loader = importlib.machinery.SourceFileLoader("daki_deploy", str(deploy_path))
spec = importlib.util.spec_from_loader(loader.name, loader)
if spec is None:
    raise RuntimeError("could not load the Daki deployment client")
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)

config = module.load_config()
client = module.DakiClient(config["panel_url"], config["api_key"], config["server_id"])
state = client.state()
if state not in {"running", "offline", "starting"}:
    raise RuntimeError(
        f"Daki Bots server is {state}; deployment was not uploaded"
    )
if state != "offline":
    print("Stopping the bot before replacing runtime files...", flush=True)
    client.request("POST", client.server_path("/power"), {"signal": "stop"})
    deadline = time.monotonic() + 90
    while client.state() != "offline":
        if time.monotonic() >= deadline:
            raise RuntimeError("Bot did not stop; no runtime files were replaced")
        time.sleep(2)

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

# Keep runtime credentials out of panel startup variables. The bot loads this
# verified local file through python-dotenv when it starts.
remote_env_path = "persona-test-bot/.env"
client.write_file(remote_env_path, env_payload)
encoded_env_path = remote_env_path.replace("/", "%2F")
env_readback = client.request(
    "GET",
    client.server_path(f"/files/contents?file=%2F{encoded_env_path}"),
    expect_json=False,
)
if env_readback != env_payload:
    raise RuntimeError("Daki verification failed for persona-test-bot/.env")
print("Uploaded persona-test-bot/.env (verified)")

client.update_startup_variable("STARTUP_CMD", "")
client.update_startup_variable(
    "SECOND_CMD",
    "cd persona-test-bot && OWAUA_VERIFY_DEPLOY=1 bash scripts/run-bots.sh",
)
print(f"Restarting {config.get('server_name', config['server_id'])}...")
client.restart()
deadline = time.monotonic() + 180
while time.monotonic() < deadline:
    if client.state() == "running":
        print("Container started. Verify OWAUA_RUNTIME_VERIFIED, OWAUA_DEPLOY_TESTS_PASSED and Discord readiness in its console before declaring deployment healthy.")
        break
    time.sleep(2)
else:
    raise RuntimeError("Daki server did not return to running state within 180 seconds")
PY
