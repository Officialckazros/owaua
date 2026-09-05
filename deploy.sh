#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required for this one-command deployment."
  exit 1
fi
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env. Fill in DISCORD_TOKEN and OPENAI_API_KEY, then run ./deploy.sh again."
  exit 1
fi

docker build -t persona-test-bot .
docker rm -f persona-test-bot 2>/dev/null || true
docker volume create persona-test-bot-data >/dev/null
exec docker run \
  --name persona-test-bot \
  --restart unless-stopped \
  --env-file .env \
  --mount source=persona-test-bot-data,target=/app/data \
  persona-test-bot
