#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt

.venv/bin/pyinstaller --noconfirm --clean \
  --windowed \
  --name owaua \
  --osx-bundle-identifier app.owaua.owaua \
  --add-data "desktoppet.jpg:." \
  pet.py

APP_VERSION="$(.venv/bin/python pet.py --version | awk '{print $2}')"
PLIST="dist/owaua.app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString ${APP_VERSION}" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleVersion string ${APP_VERSION}" "$PLIST" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Set :CFBundleVersion ${APP_VERSION}" "$PLIST"
codesign --force --deep --sign - dist/owaua.app

cd dist
ditto -c -k --sequesterRsrc --keepParent owaua.app owaua-macOS.new.zip
mv -f owaua-macOS.new.zip owaua-macOS.zip
echo ""
echo "Done: dist/owaua-macOS.zip"
