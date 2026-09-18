#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd -P)
cd "$ROOT_DIR"

VERSION=${1:-$(tr -d '[:space:]' < VERSION)}
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]]; then
  echo "Invalid release version: $VERSION" >&2
  exit 1
fi

TAG="v$VERSION"
PACKAGE_NAME="owaua-$VERSION"
DIST_DIR="$ROOT_DIR/dist"

if [[ -n "$(git status --short)" ]]; then
  echo "Refusing to package a dirty working tree. Commit changes first." >&2
  git status --short >&2
  exit 1
fi

mkdir -p "$DIST_DIR"
rm -f "$DIST_DIR/$PACKAGE_NAME.tar.gz" "$DIST_DIR/$PACKAGE_NAME.zip" \
  "$DIST_DIR/$PACKAGE_NAME.sha256"

git archive --format=tar.gz --prefix="$PACKAGE_NAME/" HEAD \
  -o "$DIST_DIR/$PACKAGE_NAME.tar.gz"
git archive --format=zip --prefix="$PACKAGE_NAME/" HEAD \
  -o "$DIST_DIR/$PACKAGE_NAME.zip"

(
  cd "$DIST_DIR"
  shasum -a 256 "$PACKAGE_NAME.tar.gz" "$PACKAGE_NAME.zip" \
    > "$PACKAGE_NAME.sha256"
)

echo "Packaged $TAG from $(git rev-parse --short HEAD):"
ls -lh "$DIST_DIR/$PACKAGE_NAME".*
