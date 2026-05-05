#!/usr/bin/env bash
# Scheduled maintainer run: wait until 07:00 UTC (same calendar day if not yet passed),
# pull Arkansas tile caches from the build server, commit on debug, build install zip to ~/Downloads,
# uninstall prior install, reinstall from that zip.
set -euo pipefail

REPO=/home/paul/Documents/ATAK/pipeline
REPORT="${HOME}/Downloads/atak-imagery-test-install-report.txt"
SSH_DST="${TILE_PLAN_FETCH_SSH:-root@31.220.30.74}"
REMOTE_V1="${TILE_PLAN_FETCH_REMOTE:-/root/atak-tile-plan/scripts/data/tile_plans/v1}"
ZIP_BASENAME="atak-imagery-v1.2.0-linux-install.zip"

mkdir -p "${HOME}/Downloads"
touch "$REPORT"
exec >>"$REPORT" 2>&1

echo "======== $(date -u -Iseconds) maintainer_followup_0700z ========"

now=$(date -u +%s)
target=$(date -u -d "$(date -u +%F) 07:00:00" +%s)
if [ "$now" -lt "$target" ]; then
  echo "Sleeping $((target - now))s until 07:00 UTC..."
  sleep $((target - now))
fi
echo "Main steps at $(date -u -Iseconds)"

cd "$REPO"
mkdir -p scripts/data/tile_plans/v1

echo "=== Remote: list Arkansas caches ==="
ssh -o BatchMode=yes -o ConnectTimeout=30 "$SSH_DST" "ls -la ${REMOTE_V1}/Arkansas_z*.tiles.gz 2>/dev/null || true"

echo "=== Copy Arkansas caches from server ==="
if command -v rsync >/dev/null 2>&1; then
  rsync -avz -e "ssh -o BatchMode=yes -o ConnectTimeout=120" \
    --include='Arkansas_z*.tiles.gz' --exclude='*' \
    "${SSH_DST}:${REMOTE_V1}/" \
    scripts/data/tile_plans/v1/
else
  echo "rsync not found; using scp (OpenSSH remote glob)."
  scp -o BatchMode=yes -o ConnectTimeout=120 \
    "${SSH_DST}:${REMOTE_V1}"/Arkansas_z*.tiles.gz \
    scripts/data/tile_plans/v1/
fi

echo "=== Verify caches (GeoJSON CRC must match) ==="
set +e
python3 scripts/verify_tile_plan_caches.py --state Arkansas
ev=$?
set -e
if [ "$ev" -ne 0 ]; then
  echo "WARN: verify_tile_plan_caches exited $ev (incomplete build or GeoJSON mismatch — continuing)"
fi

echo "=== Git: merge main into debug, commit caches, push ==="
git fetch origin
git checkout main
git pull origin main

git checkout debug/tile-plan-cache
git pull origin debug/tile-plan-cache
git merge origin/main -m "Merge main into debug (tile cache verify tooling)"

git add scripts/data/tile_plans/v1/Arkansas_z*.tiles.gz
if git diff --cached --quiet; then
  echo "No Arkansas cache changes staged"
else
  git commit -m "Tile plans: Arkansas precomputed caches (debug test build)"
fi
git push origin debug/tile-plan-cache

echo "=== Build linux-install zip from debug tree ==="
python3 scripts/build_release.py
cp -f "${REPO}/dist/${ZIP_BASENAME}" "${HOME}/Downloads/${ZIP_BASENAME}"
ls -la "${HOME}/Downloads/${ZIP_BASENAME}"

echo "=== Uninstall previous install (clean) ==="
INST="${XDG_DATA_HOME:-${HOME}/.local/share}/atak-imagery"
rm -rf "$INST"
for f in \
  "${HOME}/.local/share/applications/ATAK Imagery Downloader.desktop" \
  "${HOME}/.local/share/applications/ATAK Device Installer.desktop" \
  "${HOME}/Desktop/ATAK Imagery Downloader.desktop" \
  "${HOME}/Desktop/ATAK Device Installer.desktop"; do
  [ -e "$f" ] && rm -f "$f"
done
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "${HOME}/.local/share/applications" >/dev/null 2>&1 || true
fi
rm -rf "${HOME}/Downloads/atak-imagery"
rm -f /tmp/atak_pipeline_launcher.log /tmp/atak_pipeline_device_launcher.log 2>/dev/null || true

echo "=== Unpack and install from Downloads ==="
cd "${HOME}/Downloads"
unzip -o -q "${ZIP_BASENAME}"
cd atak-imagery
chmod +x install_linux.sh scripts/install_linux.sh
export ATAK_INSTALL_KEEP_SOURCE=1
# Root install_linux.sh relaunches a GUI terminal when TERM=dumb (breaks automation).
bash scripts/install_linux.sh

echo ""
echo "======== DONE $(date -u -Iseconds) ========"
echo "Report: $REPORT"
echo "Zip:    ${HOME}/Downloads/${ZIP_BASENAME}"
echo "Install: ${INST}"
echo "Source tree kept under ${HOME}/Downloads/atak-imagery (ATAK_INSTALL_KEEP_SOURCE=1)"
