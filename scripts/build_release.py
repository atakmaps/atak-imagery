#!/usr/bin/env python3
"""Build the Linux install zip (atak-imagery-v*-linux-install.zip).

Ships Linux runtime (scripts/, data/, install_linux.sh) plus maintainer-facing
docs (README, Windows Build Setup Instructions, tile-plan handoff). Excludes
Windows build trees, agent/Cursor handoffs, bundled plugin APKs, and tmp/logs.
"""
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
    "tmp",
    ".addon_plugin_asset_cache",
    ".protected_import_asset_cache",
}

# Add-on plugin APKs ship on GitHub *-plugin-assets releases (see deploy.env.example).
RELATIVE_EXCLUDE_PREFIXES: tuple[Path, ...] = (Path("scripts/data/bundled_plugins"),)

# Agent / IDE handoffs and internal checklists — never ship in end-user zips.
INTERNAL_DOC_BASENAMES = {
    "HANDOFF_AGENT.local.md",
    "RELEASE_CHECKLIST.md",
    "windows_agent.md",
}

# Windows-only repo files (Linux zip users run install_linux.sh, not Inno/PyInstaller).
RELATIVE_EXCLUDE_FILES: frozenset[Path] = frozenset(
    {
        Path("ATAK_Setup.iss"),
        Path("ATAKPipeline_Setup.iss"),
        Path("install_windows.cmd"),
        Path("requirements-windows-build.txt"),
        Path("windows_launcher.py"),
        Path("google_hybrid.xml"),
        Path("google_roadmap_no_poi.xml"),
        Path("scripts/audit_windows_bundle.py"),
        Path("scripts/sync_windows_build.py"),
        Path("scripts/windows_bundle_manifest.py"),
        Path("scripts/build_windows_exe.ps1"),
        Path("scripts/install_windows.ps1"),
        Path("scripts/setup_windows_pipeline.ps1"),
        Path("scripts/wipe_windows_install.ps1"),
    }
)

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

def _is_under(relative: Path, prefix: Path) -> bool:
    rel = relative.as_posix()
    base = prefix.as_posix()
    return rel == base or rel.startswith(base + "/")


def should_skip(path: Path) -> bool:
    parts = set(path.parts)
    if parts & EXCLUDE_DIRS:
        return True
    if path.name in EXCLUDE_FILES:
        return True
    name = path.name
    if name in INTERNAL_DOC_BASENAMES:
        return True
    if ".bak_" in name or name.endswith((".bak", ".orig", ".rej", ".tmp", "~")):
        return True
    if name == "deploy.env":
        return True
    try:
        rel = path.relative_to(ROOT)
    except ValueError:
        return True
    if rel in RELATIVE_EXCLUDE_FILES:
        return True
    for prefix in RELATIVE_EXCLUDE_PREFIXES:
        if _is_under(rel, prefix):
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
