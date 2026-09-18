#!/usr/bin/env bash
set -euo pipefail

# Start the installed DeepGrove Maple MLX server if local mode needs it.
ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd -P)
ENV_FILE=${OWAUA_ENV_FILE:-"$ROOT_DIR/.env"}

env_value() {
  local key=$1
  sed -n -E "s/^${key}=([^#]*)#.*/\\1/p; s/^${key}=([^#]*)$/\\1/p" "$ENV_FILE" \
    | tail -n 1 | sed 's/[[:space:]]*$//'
}

BASE_URL=${OWAUA_LOCAL_BASE_URL:-$(env_value OWAUA_LOCAL_BASE_URL)}
BASE_URL=${BASE_URL:-http://127.0.0.1:8080/v1}
HOST=${OWAUA_LOCAL_AI_HOST:-127.0.0.1}
PORT=${OWAUA_LOCAL_AI_PORT:-8080}
BACKEND=${OWAUA_LOCAL_AI_BACKEND:-$(env_value OWAUA_LOCAL_AI_BACKEND)}
MLX_ROOT=${OWAUA_LOCAL_AI_ROOT:-$(env_value OWAUA_LOCAL_AI_ROOT)}
MODEL_PATH=${OWAUA_LOCAL_AI_MODEL_PATH:-$(env_value OWAUA_LOCAL_AI_MODEL_PATH)}
MLX_ROOT=${MLX_ROOT:-/Users/ckazro/mlx-lm-deepgrove}
MODEL_PATH=${MODEL_PATH:-$MLX_ROOT/maple-2bit-mlx}
LOG_FILE=${OWAUA_LOCAL_AI_LOG:-$ROOT_DIR/data/deepgrove-mlx.log}
PID_FILE=${OWAUA_LOCAL_AI_PID:-$ROOT_DIR/data/deepgrove-mlx.pid}
HF_HUB_CACHE=${HF_HUB_CACHE:-$MLX_ROOT/.cache/huggingface/hub}
export HF_HUB_CACHE
mkdir -p "$HF_HUB_CACHE"

if curl -fsS --max-time 2 "$BASE_URL/models" >/dev/null 2>&1; then
  echo "Local AI server is already running at $BASE_URL"
  exit 0
fi

if [[ "$BACKEND" == "llama" ]]; then
  LLAMA_SERVER=${OWAUA_LOCAL_AI_LLAMA:-$(env_value OWAUA_LOCAL_AI_LLAMA)}
  LLAMA_MODEL=${OWAUA_LOCAL_AI_GGUF:-$(env_value OWAUA_LOCAL_AI_GGUF)}
  LLAMA_LIB_DIR=${OWAUA_LOCAL_AI_LLAMA_LIB:-$(env_value OWAUA_LOCAL_AI_LLAMA_LIB)}
  LLAMA_BACKEND=${OWAUA_LOCAL_AI_LLAMA_BACKEND:-$(env_value OWAUA_LOCAL_AI_LLAMA_BACKEND)}
  LLAMA_THREADS=${OWAUA_LOCAL_AI_LLAMA_THREADS:-$(env_value OWAUA_LOCAL_AI_LLAMA_THREADS)}
  LLAMA_THREADS=${LLAMA_THREADS:-4}
  if [[ ! -x "$LLAMA_SERVER" || ! -f "$LLAMA_MODEL" ]]; then
    echo "llama.cpp installation not found: $LLAMA_SERVER / $LLAMA_MODEL" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$PID_FILE")"
  echo "Starting native Linux llama.cpp server at $BASE_URL..."
  nohup env LD_LIBRARY_PATH="$LLAMA_LIB_DIR" GGML_BACKEND_PATH="$LLAMA_BACKEND" \
    "$LLAMA_SERVER" -m "$LLAMA_MODEL" --host "$HOST" --port "$PORT" \
    --ctx-size 2048 --threads "$LLAMA_THREADS" --parallel 1 --n-gpu-layers 0 \
    >>"$LOG_FILE" 2>&1 < /dev/null &
  echo $! > "$PID_FILE"
  for _ in {1..30}; do
    if curl -fsS --max-time 2 "$BASE_URL/models" >/dev/null 2>&1; then
      echo "Native llama.cpp server is ready at $BASE_URL"
      exit 0
    fi
    sleep 1
  done
  echo "Native llama.cpp server did not become ready; see $LOG_FILE" >&2
  exit 1
fi

PYTHON=${OWAUA_LOCAL_AI_PYTHON:-$MLX_ROOT/.venv/bin/python}
if [[ ! -x "$PYTHON" || ! -d "$MODEL_PATH" ]]; then
  echo "DeepGrove Maple installation not found: $MODEL_PATH" >&2
  echo "Set OWAUA_LOCAL_AI_ROOT and OWAUA_LOCAL_AI_MODEL_PATH in $ENV_FILE." >&2
  exit 1
fi

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$PID_FILE")"
echo "Starting DeepGrove Maple MLX server at $BASE_URL..."
pushd "$MLX_ROOT" >/dev/null
# The bundled DeepGrove checkout is the mlx_lm source tree; it is not
# installed as a wheel because its legacy setup.py metadata is not PEP 517
# compatible. Make the source importable explicitly on every Linux host.
PYTHONPATH="$MLX_ROOT${PYTHONPATH:+:$PYTHONPATH}" nohup "$PYTHON" -m mlx_lm.server \
  --model "$MODEL_PATH" \
  --trust-remote-code \
  --chat-template-args '{"enable_thinking":false}' \
  --host "$HOST" \
  --port "$PORT" \
  >>"$LOG_FILE" 2>&1 < /dev/null &
popd >/dev/null
echo $! > "$PID_FILE"

for _ in {1..30}; do
  if curl -fsS --max-time 2 "$BASE_URL/models" >/dev/null 2>&1; then
    echo "DeepGrove Maple server is ready at $BASE_URL"
    exit 0
  fi
  sleep 1
done

echo "DeepGrove Maple server did not become ready; see $LOG_FILE" >&2
exit 1
