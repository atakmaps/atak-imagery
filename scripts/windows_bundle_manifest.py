#!/usr/bin/env python3
"""
Single source of truth for Windows PyInstaller bundles (Device Installer + Imagery Downloader).

Used by:
  - scripts/audit_windows_bundle.py (pre-build validation)
  - windows_build/build_windows_exe.ps1 (hidden imports, collect-all, script bundles)
  - scripts/sync_windows_build.py (post-sync file checks)

Keep requirements-windows-build.txt in sync with PIP_PACKAGES below.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
WIN = ROOT / "windows_build"

# Must match requirements-windows-build.txt (one package name per line).
PIP_PACKAGES: tuple[str, ...] = (
    "requests",
    "pyinstaller",
    "mgrs",
    "packaging",
    "certifi",
    "urllib3",
    "charset-normalizer",
    "idna",
)

# Renamed runtime modules (sync from scripts/ → windows_build/*_win.py).
WIN_RUNTIME_MODULES: tuple[str, ...] = (
    "atak_adb_deploy_win",
    "atak_downloader_finalbuild_win",
    "atak_downloader_from_installer_win",
    "atak_imagery_sqlite_builder_finalbuild_win",
    "atak_dted_downloader_win",
)

# Shared helpers (direct copy or Windows-only).
HELPER_MODULES: tuple[str, ...] = (
    "imagery_tile_selection",
    "git_update_check",
    "bundled_plugin_install",
    "usgs_throughput_probe",
    "atak_osmdroid_sqlite_footprint",
    "tk_window_scaling",
    "win_subprocess",
)

# PyInstaller --hidden-import (includes lazy-import / spawn-child modules).
HIDDEN_IMPORTS: tuple[str, ...] = (
    *WIN_RUNTIME_MODULES,
    *HELPER_MODULES,
    # HTTP stack (requests + transitive deps; certifi CA bundle is critical).
    "requests",
    "urllib3",
    "certifi",
    "charset_normalizer",
    "idna",
    # MGRS radius entry (native libmgrs*.pyd bundled separately).
    "mgrs",
    "mgrs.core",
    "packaging",
    "packaging.tags",
    # Tk / frozen GUI.
    "tkinter",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "tkinter.simpledialog",
    "tkinter.scrolledtext",
    "tkinter.ttk",
    "_tkinter",
    # multiprocessing spawn workers (zoom probe, tile-plan bands).
    "multiprocessing",
    "multiprocessing.spawn",
    "multiprocessing.popen_spawn_win32",
    "multiprocessing.reduction",
    "concurrent.futures",
)

# PyInstaller --collect-all (data files + submodules).
COLLECT_ALL: tuple[str, ...] = (
    "mgrs",
    "certifi",
    "requests",
)

# Copied into bundle as scripts\*.py (fallback when import hooks miss a helper).
SCRIPT_BUNDLES: tuple[str, ...] = (
    "imagery_tile_selection.py",
    "git_update_check.py",
    "tk_window_scaling.py",
    "win_subprocess.py",
    "bundled_plugin_install.py",
    "usgs_throughput_probe.py",
    "atak_osmdroid_sqlite_footprint.py",
)

# PyInstaller entry scripts (Windows-only; not synced from Linux).
LAUNCHERS: tuple[str, ...] = (
    "windows_launcher.py",
    "windows_installer_launcher.py",
)

REQUIRED_DATA_FILES: tuple[str, ...] = (
    "data/us_states.geojson",
    "data/zoom_estimates_z10_z16.json",
)


def _resolve_helper_path(name: str) -> Path | None:
    """Helper .py may live in windows_build/ or scripts/."""
    for base in (WIN, SCRIPTS):
        path = base / name
        if path.is_file():
            return path
    return None


def validate_bundle_tree(root: Path | None = None) -> list[str]:
    """Return human-readable errors; empty list means OK."""
    root = root or ROOT
    win = root / "windows_build"
    errors: list[str] = []

    for launcher in LAUNCHERS:
        if not (win / launcher).is_file():
            errors.append(f"Missing launcher: windows_build/{launcher}")

    for mod in WIN_RUNTIME_MODULES:
        if not (win / f"{mod}.py").is_file():
            errors.append(f"Missing runtime module: windows_build/{mod}.py (run sync_windows_build.py)")

    for mod in HELPER_MODULES:
        if mod in ("tk_window_scaling", "win_subprocess"):
            if not (win / f"{mod}.py").is_file():
                errors.append(f"Missing Windows-only helper: windows_build/{mod}.py")
        elif not _resolve_helper_path(f"{mod}.py"):
            errors.append(f"Missing helper module: {mod}.py (sync or scripts/)")

    for rel in SCRIPT_BUNDLES:
        if not _resolve_helper_path(rel):
            errors.append(f"Missing script bundle source: {rel}")

    for rel in REQUIRED_DATA_FILES:
        if not (win / rel).is_file():
            errors.append(f"Missing bundled data: windows_build/{rel}")

    req_file = root / "requirements-windows-build.txt"
    if req_file.is_file():
        listed = {
            line.split("#", 1)[0].strip().lower()
            for line in req_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        for pkg in PIP_PACKAGES:
            if pkg.lower() not in listed:
                errors.append(
                    f"requirements-windows-build.txt missing {pkg!r} (see scripts/windows_bundle_manifest.py)"
                )
    else:
        errors.append("Missing requirements-windows-build.txt at repo root")

    return errors


def manifest_for_build() -> dict[str, Any]:
    return {
        "hidden_imports": list(HIDDEN_IMPORTS),
        "collect_all": list(COLLECT_ALL),
        "script_bundles": list(SCRIPT_BUNDLES),
        "pip_packages": list(PIP_PACKAGES),
    }


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Windows EXE bundle manifest")
    ap.add_argument("--json", action="store_true", help="Print JSON for build_windows_exe.ps1")
    ap.add_argument("--check", action="store_true", help="Validate tree only (exit 1 on error)")
    args = ap.parse_args()

    errors = validate_bundle_tree()
    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)

    if args.check and not args.json:
        print("Bundle tree OK")
        return

    if args.json:
        print(json.dumps(manifest_for_build()))
        return

    ap.print_help()


if __name__ == "__main__":
    main()
