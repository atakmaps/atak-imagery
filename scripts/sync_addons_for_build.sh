#!/usr/bin/env bash
# Sync add-on assets from the canonical local folder before every build/release.
# Source of truth: /home/paul/Documents/ATAK/Plugins/Add Ons for Build/
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="${ATAK_ADDONS_SOURCE:-/home/paul/Documents/ATAK/Plugins/Add Ons for Build}"
MOBILE_DEST="$ROOT/scripts/data/mobile_xml"
PLUGIN_DEST="$ROOT/scripts/data/bundled_plugins"

if [[ ! -d "$SOURCE" ]]; then
  echo "ERROR: add-ons source not found: $SOURCE" >&2
  exit 1
fi

echo "=== Map / import bundle -> scripts/data/mobile_xml/ ==="
mkdir -p "$MOBILE_DEST/MapXML"
rsync -av --delete "$SOURCE/MapXML/" "$MOBILE_DEST/MapXML/"
for z in "$SOURCE"/*.zip; do
  [[ -f "$z" ]] || continue
  rsync -av "$z" "$MOBILE_DEST/"
done
rm -f "$MOBILE_DEST/AmRRON Forms.xml" "$MOBILE_DEST/AmRRON-Default-v1.0.csv" 2>/dev/null || true
rm -rf "$MOBILE_DEST/Additional Plugins" 2>/dev/null || true
find "$MOBILE_DEST" -mindepth 1 -maxdepth 1 \
  ! -name MapXML ! -name 'state_kml_files.zip' \
  -exec rm -rf {} + 2>/dev/null || true

echo "=== Plugin APKs (5.6+ tree) -> scripts/data/bundled_plugins/ (local dev fallback) ==="
mkdir -p "$PLUGIN_DEST"
rsync -av --delete \
  --include='*/' \
  --include='*.apk' \
  --exclude='*[Uu][Vv][Pp][Rr][Oo]*' \
  --exclude='*' \
  "$SOURCE/" "$PLUGIN_DEST/"

echo "=== Mirror into windows_build/data/ (mobile + tile plans via sync_windows_build.py) ==="
python3 "$ROOT/scripts/sync_windows_build.py"

echo "Done."
echo "  mobile_xml: $(du -sh "$MOBILE_DEST" | awk '{print $1}')"
echo "  bundled_plugins (dev fallback): $(du -sh "$PLUGIN_DEST" | awk '{print $1}')"
find "$PLUGIN_DEST" -name '*.apk' | wc -l | xargs echo "  APK count:"
