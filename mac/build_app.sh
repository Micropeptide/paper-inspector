#!/bin/bash
# Build Paper Inspector.app from the Swift package.  The app starts the Python engine from this checkout's .venv
# (or PAPERINSPECTOR_ENGINE / a `paperinspector` on PATH / Settings > Engine), so run `pip install -e .` in the
# project folder first.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(cd .. && pwd)"
swift build -c release
APP="build/Paper Inspector.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$(swift build -c release --show-bin-path)/PaperInspector" "$APP/Contents/MacOS/PaperInspector"

ICONSET=build/PaperInspector.iconset
rm -rf "$ICONSET"
"$ROOT/.venv/bin/python" "$ROOT/tools/make_icon.py" "$ICONSET"
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/PaperInspector.icns"

ENGINE="${PAPERINSPECTOR_ENGINE:-$ROOT/.venv/bin/paperinspector}"
# The app's version follows the engine it ships with.
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/engine/paperinspector/__init__.py" | head -1)"
VERSION="${VERSION:-0.1.0}"
BUILD="$(git -C "$ROOT" rev-list --count HEAD 2>/dev/null || echo 1)"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Paper Inspector</string>
  <key>CFBundleDisplayName</key><string>Paper Inspector</string>
  <key>CFBundleIdentifier</key><string>app.paperinspector.mac</string>
  <key>CFBundleExecutable</key><string>PaperInspector</string>
  <key>CFBundleIconFile</key><string>PaperInspector</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$BUILD</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSSupportsAutomaticTermination</key><false/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>CFBundleDocumentTypes</key>
  <array>
    <dict>
      <key>CFBundleTypeName</key><string>Paper or Supplementary File</string>
      <key>CFBundleTypeRole</key><string>Viewer</string>
      <key>LSHandlerRank</key><string>Alternate</string>
      <key>LSItemContentTypes</key>
      <array>
        <string>com.adobe.pdf</string>
        <string>public.image</string>
        <string>public.comma-separated-values-text</string>
        <string>public.tab-separated-values-text</string>
        <string>org.openxmlformats.spreadsheetml.sheet</string>
        <string>com.microsoft.excel.xls</string>
        <string>org.openxmlformats.wordprocessingml.document</string>
        <string>public.zip-archive</string>
        <string>public.folder</string>
      </array>
    </dict>
  </array>
  <key>CFBundleURLTypes</key>
  <array>
    <dict>
      <key>CFBundleURLName</key><string>app.paperinspector.mac</string>
      <key>CFBundleURLSchemes</key><array><string>paperinspector</string></array>
    </dict>
  </array>
  <key>PaperInspectorEngine</key><string>$ENGINE</string>
</dict></plist>
PLIST
plutil -lint "$APP/Contents/Info.plist" >/dev/null
codesign --force --sign - "$APP" >/dev/null 2>&1 || true
echo "Built $(pwd)/$APP"
