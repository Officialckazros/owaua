#!/usr/bin/env bash
set -euo pipefail

# Install Owaua and the bundled DeepGrove Maple model on a Linux machine.
# Run this from the project copied from the USB. The model is never downloaded.

SOURCE_ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
INSTALL_ROOT=${OWAUA_INSTALL_DIR:-"$HOME/owaua-persona-testbot"}
BACKEND=${OWAUA_MLX_BACKEND:-auto}

die() {
  echo "install-cachyos: $*" >&2
  exit 1
}

[[ "$(uname -s)" == "Linux" ]] || die "this installer is for Linux/CachyOS"
command -v python3 >/dev/null 2>&1 || die "python3 is required"
command -v curl >/dev/null 2>&1 || die "curl is required"

MODEL_SOURCE="$SOURCE_ROOT/deepgrove/maple-2bit-mlx"
[[ -d "$MODEL_SOURCE" ]] || die "bundled DeepGrove model not found at $MODEL_SOURCE"
[[ -f "$MODEL_SOURCE/config.json" ]] || die "DeepGrove model is incomplete: config.json is missing"

if [[ "$SOURCE_ROOT" != "$INSTALL_ROOT" ]]; then
  echo "Copying the USB project to $INSTALL_ROOT ..."
  mkdir -p "$INSTALL_ROOT"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a \
      --exclude '.venv/' \
      --exclude 'deepgrove/.venv/' \
      --exclude '._*' \
      --exclude '.DS_Store' \
      "$SOURCE_ROOT/" "$INSTALL_ROOT/"
  else
    cp -a "$SOURCE_ROOT/." "$INSTALL_ROOT/"
    rm -rf "$INSTALL_ROOT/.venv" "$INSTALL_ROOT/deepgrove/.venv"
  fi
else
  echo "Installing in place at $INSTALL_ROOT ..."
fi

# Remove macOS resource-fork files; names such as ._pack-*.idx confuse Git on Linux.
find "$INSTALL_ROOT" -name '._*' -type f -delete
find "$INSTALL_ROOT" -name '.DS_Store' -type f -delete

PROJECT_VENV="$INSTALL_ROOT/.venv"
MLX_ROOT="$INSTALL_ROOT/deepgrove"
MODEL_PATH="$MLX_ROOT/maple-2bit-mlx"

if ! "$PROJECT_VENV/bin/python" -c 'import sys; raise SystemExit(0 if sys.platform == "linux" else 1)' >/dev/null 2>&1; then
  rm -rf "$PROJECT_VENV"
  echo "Creating the Owaua Linux virtual environment ..."
  python3 -m venv "$PROJECT_VENV"
fi

echo "Installing Owaua dependencies ..."
"$PROJECT_VENV/bin/python" -m pip install --upgrade pip setuptools wheel
"$PROJECT_VENV/bin/python" -m pip install -r "$INSTALL_ROOT/requirements.txt"

case "$BACKEND" in
  auto)
    if command -v nvidia-smi >/dev/null 2>&1; then
      BACKEND=cuda12
    else
      BACKEND=cpu
    fi
    ;;
  cpu|cuda12|cuda13) ;;
  *) die "OWAUA_MLX_BACKEND must be auto, cpu, cuda12, or cuda13" ;;
esac

MLX_VENV="$MLX_ROOT/.venv"
if ! "$MLX_VENV/bin/python" -c 'import sys; raise SystemExit(0 if sys.platform == "linux" else 1)' >/dev/null 2>&1; then
  rm -rf "$MLX_VENV"
  echo "Creating the DeepGrove Linux virtual environment ($BACKEND backend) ..."
  python3 -m venv "$MLX_VENV"
fi

echo "Installing the bundled DeepGrove runtime ($BACKEND backend) ..."
"$MLX_VENV/bin/python" -m pip install --upgrade pip setuptools wheel
"$MLX_VENV/bin/python" -m pip install \
  "mlx[${BACKEND}]" \
  numpy \
  'transformers>=5.7.0' \
  sentencepiece \
  protobuf \
  pyyaml \
  jinja2

# Register the bundled source checkout in the Linux venv. This avoids relying
# on the process working directory when importing mlx_lm.server.
MLX_SITE_PACKAGES=$(
  "$MLX_VENV/bin/python" -c 'import site; print(site.getsitepackages()[0])'
)
printf '%s\n' "$MLX_ROOT" > "$MLX_SITE_PACKAGES/deepgrove-source.pth"
"$MLX_VENV/bin/python" -c 'import mlx_lm.server' || die "DeepGrove source import failed from $MLX_ROOT"

ENV_FILE="$INSTALL_ROOT/.env"
[[ -f "$ENV_FILE" ]] || cp "$INSTALL_ROOT/.env.example" "$ENV_FILE"

echo "Configuring local-only mode to use the USB-provided model ..."
"$PROJECT_VENV/bin/python" - "$ENV_FILE" "$MLX_ROOT" "$MODEL_PATH" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
values = {
    "OWAUA_LOCAL_ONLY": "1",
    "OWAUA_LOCAL_BASE_URL": "http://127.0.0.1:8080/v1",
    "OWAUA_LOCAL_MODEL": "default_model",
    "OWAUA_LOCAL_AI_ROOT": sys.argv[2],
    "OWAUA_LOCAL_AI_MODEL_PATH": sys.argv[3],
    "OWAUA_LOCAL_AI_PYTHON": str(pathlib.Path(sys.argv[2]) / ".venv/bin/python"),
}
lines = path.read_text().splitlines()
for key, value in values.items():
    replacement = f"{key}={value}"
    for index, line in enumerate(lines):
        if line.startswith(key + "="):
            lines[index] = replacement
            break
    else:
        lines.append(replacement)
path.write_text("\n".join(lines) + "\n")
PY

chmod +x "$INSTALL_ROOT/scripts/run-bots.sh" "$INSTALL_ROOT/scripts/start-local-ai.sh"
echo
echo "Installation complete."
echo "Project: $INSTALL_ROOT"
echo "Model:   $MODEL_PATH"
echo
echo "Start it with:"
echo "  cd $(printf '%q' "$INSTALL_ROOT")"
echo "  ./scripts/run-bots.sh"
