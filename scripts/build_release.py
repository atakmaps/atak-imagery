#!/usr/bin/env python3
from __future__ import annotations

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "VERSION"
DIST_DIR = ROOT / "dist"

EXCLUDE_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".cursor",
    "dist",
    "logs",
    "backups",
    "reports",
    "build",
    "DTED2",
    "DTED_by_state",
    "Hawaii_DEM",
    "_states_tmp",
    "output",
    "installer-dist",
    "New Test",
    "windows_build",
    ".addon_plugin_asset_cache",
    ".protected_import_asset_cache",
}

EXCLUDE_FILES = {
    ".DS_Store",
}

def read_version() -> str:
    if not VERSION_FILE.exists():
        raise FileNotFoundError(f"Missing VERSION file: {VERSION_FILE}")
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not version:
        raise ValueError("VERSION file is empty")
    return version


def zip_version_label(version: str) -> str:
    """VERSION may be 'v1.0.0' or '1.0.0'; zip uses a single leading v."""
    v = version.strip()
    return v[1:] if v.startswith("v") else v

def should_skip(path: Path) -> bool:
    parts = set(path.parts)
    if parts & EXCLUDE_DIRS:
        return True
    if path.name in EXCLUDE_FILES:
        return True
    name = path.name
    if ".bak_" in name or name.endswith((".bak", ".orig", ".rej", ".tmp", "~")):
        return True
    if name == "deploy.env":
        return True
    return False

def build_zip(version: str) -> Path:
    DIST_DIR.mkdir(exist_ok=True)
    label = zip_version_label(version)
    # Full bundle for Linux: unzip → atak-imagery/ → ./install_linux.sh (not minimal scripts-only zip).
    zip_path = DIST_DIR / f"atak-imagery-v{label}-linux-install.zip"

    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in ROOT.rglob("*"):
            if should_skip(item):
                continue
            if item == zip_path:
                continue
            if item.is_dir():
                continue

            rel_path = item.relative_to(ROOT)
            arcname = Path("atak-imagery") / rel_path
            zf.write(item, arcname.as_posix())

    return zip_path

def main() -> None:
    version = read_version()
    zip_path = build_zip(version)
    print(f"Created: {zip_path}")

if __name__ == "__main__":
    main()
