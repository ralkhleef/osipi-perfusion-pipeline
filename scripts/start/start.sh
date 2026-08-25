#!/usr/bin/env sh
set -eu

APP_URL="http://localhost:8000"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)

cd "$PROJECT_ROOT"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed."
  echo "Please install Docker Desktop, then run this script again."
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  echo "Docker Compose is not available."
  echo "Please install or update Docker Desktop, then run this script again."
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker is installed but not running."
  if [ "$(uname -s)" = "Darwin" ]; then
    echo "Opening Docker Desktop..."
    open -a Docker >/dev/null 2>&1 || true
  fi
  echo "Waiting for Docker to be ready..."
  i=0
  while ! docker info >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -gt 60 ]; then
      echo "Docker did not become ready in time."
      echo "Please open Docker Desktop and run this script again."
      exit 1
    fi
    sleep 2
  done
fi

mkdir -p data/outputs data/reference_data submissions/incoming submissions/extracted

if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port 8000 is already being used by another app."
  echo "Please stop the other app, then run this script again."
  exit 1
fi

echo "Starting OSIPI Pipeline local app..."
$COMPOSE up --build -d

echo "Waiting for the app to respond..."
i=0
while ! curl -fsS "$APP_URL/api/health" >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 60 ]; then
    echo "The app did not start in time."
    echo "Try running: docker compose logs"
    exit 1
  fi
  sleep 2
done

echo "OSIPI Pipeline is running:"
echo "$APP_URL"

if command -v open >/dev/null 2>&1; then
  open "$APP_URL" >/dev/null 2>&1 || true
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$APP_URL" >/dev/null 2>&1 || true
fi
