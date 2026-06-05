#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
cd "$PROJECT_ROOT"

RELEASE_NAME="osipi-pipeline-local-app"
RELEASE_DIR="release/$RELEASE_NAME"
ZIP_PATH="release/$RELEASE_NAME.zip"

rm -rf "$RELEASE_DIR" "$ZIP_PATH"
mkdir -p "$RELEASE_DIR"

cp -R backend frontend src scripts "$RELEASE_DIR/"
cp Dockerfile docker-compose.yml .dockerignore README.md README_DOCKER.md "$RELEASE_DIR/"
cp requirements.txt pyproject.toml "$RELEASE_DIR/"
cp start.sh start.command start.bat stop.sh stop.bat "$RELEASE_DIR/"

mkdir -p \
  "$RELEASE_DIR/data/outputs" \
  "$RELEASE_DIR/data/reference_data" \
  "$RELEASE_DIR/submissions/incoming" \
  "$RELEASE_DIR/submissions/extracted" \
  "$RELEASE_DIR/submissions/validated"

chmod +x \
  "$RELEASE_DIR/start.sh" \
  "$RELEASE_DIR/start.command" \
  "$RELEASE_DIR/stop.sh" \
  "$RELEASE_DIR/scripts/start/start.sh" \
  "$RELEASE_DIR/scripts/start/start.command" \
  "$RELEASE_DIR/scripts/stop/stop.sh" \
  "$RELEASE_DIR/scripts/release/create_release_zip.sh"

(cd release && zip -qr "$RELEASE_NAME.zip" "$RELEASE_NAME")

echo "Created $ZIP_PATH"
