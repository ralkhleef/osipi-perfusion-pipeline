#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
cd "$PROJECT_ROOT"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed."
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  docker compose down
elif command -v docker-compose >/dev/null 2>&1; then
  docker-compose down
else
  echo "Docker Compose is not available."
  exit 1
fi

echo "OSIPI Pipeline local app stopped."
