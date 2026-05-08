#!/usr/bin/env bash
set -euo pipefail

# Build one release zip (source + installers + tile plans from disk) and upload it as a
# GitHub Release asset. This keeps large tile-plan blobs out of git history.
#
# Usage:
#   scripts/build_and_upload_release_asset.sh v1.3.14
#   scripts/build_and_upload_release_asset.sh v1.3.14 --repo atakmaps/atak-imagery
#   scripts/build_and_upload_release_asset.sh v1.3.14 --clobber
#
# Notes:
# - Requires: gh CLI authenticated for repo access.
# - Uses scripts/build_release.py, which packages the full tree under dist/.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="${1:-}"
if [[ -z "${TAG}" ]]; then
  echo "Usage: $0 <tag> [--repo owner/name] [--clobber]" >&2
  exit 2
fi
shift || true

REPO_ARG=()
UPLOAD_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      if [[ $# -lt 2 ]]; then
        echo "--repo requires owner/name" >&2
        exit 2
      fi
      REPO_ARG=(--repo "$2")
      shift 2
      ;;
    --clobber)
      UPLOAD_ARGS+=(--clobber)
      shift
      ;;
    *)
      echo "Unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

cd "$ROOT"
python3 scripts/build_release.py

VERSION="$(tr -d '[:space:]' < VERSION)"
LABEL="${VERSION#v}"
ASSET_PATH="dist/atak-imagery-v${LABEL}-linux-install.zip"

if [[ ! -f "$ASSET_PATH" ]]; then
  echo "Expected asset not found: $ASSET_PATH" >&2
  exit 1
fi

echo "Uploading asset: $ASSET_PATH to release tag: $TAG"
gh release upload "$TAG" "$ASSET_PATH" "${REPO_ARG[@]}" "${UPLOAD_ARGS[@]}"
echo "Done."
