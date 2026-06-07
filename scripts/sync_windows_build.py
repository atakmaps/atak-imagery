#!/usr/bin/env python3
"""Copy Linux scripts/ into windows_build/ with *_win.py names and Windows-specific patches."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
WIN = ROOT / "windows_build"

# Linux module → Windows module (content gets name substitutions)
RENAMED_MODULES = {
    "atak_downloader_finalbuild.py": "atak_downloader_finalbuild_win.py",
    "atak_downloader_from_installer.py": "atak_downloader_from_installer_win.py",
    "atak_imagery_sqlite_builder_finalbuild.py": "atak_imagery_sqlite_builder_finalbuild_win.py",
    "atak_dted_downloader.py": "atak_dted_downloader_win.py",
    "atak_adb_deploy.py": "atak_adb_deploy_win.py",
}

DIRECT_COPY = (
    "imagery_tile_selection.py",
    "git_update_check.py",
    "bundled_plugin_install.py",
    "usgs_throughput_probe.py",
)

SKIP_OVERWRITE = frozenset({"tk_window_scaling.py"})

SUBSTITUTIONS = (
    ("atak_downloader_finalbuild.py", "atak_downloader_finalbuild_win.py"),
    ("atak_imagery_sqlite_builder_finalbuild.py", "atak_imagery_sqlite_builder_finalbuild_win.py"),
    ("atak_dted_downloader.py", "atak_dted_downloader_win.py"),
    ("atak_downloader_from_installer.py", "atak_downloader_from_installer_win.py"),
    ("atak_adb_deploy.py", "atak_adb_deploy_win.py"),
    ("from atak_adb_deploy import", "from atak_adb_deploy_win import"),
    ("import atak_adb_deploy", "import atak_adb_deploy_win"),
    ("import atak_downloader_finalbuild as", "import atak_downloader_finalbuild_win as"),
    ("from atak_dted_downloader import", "from atak_dted_downloader_win import"),
    ("import atak_dted_downloader as", "import atak_dted_downloader_win as"),
    (
        "import atak_imagery_sqlite_builder_finalbuild as",
        "import atak_imagery_sqlite_builder_finalbuild_win as",
    ),
    (
        "from atak_imagery_sqlite_builder_finalbuild import",
        "from atak_imagery_sqlite_builder_finalbuild_win import",
    ),
)

UDEV_HINT = (
    'prompt. If you see “no permissions”, install udev rules for adb.\n\n'
)
WIN_ADB_HINT = (
    "prompt. Install Android platform-tools and ensure adb is on PATH.\n\n"
    "Download: https://developer.android.com/tools/releases/platform-tools\n\n"
)

MESHCORE_LINUX = 'DEFAULT_MESHCORE_PLUGIN_REPO = Path("/home/paul/Documents/ATAK/Plugins/MeshcoreAtak")'
MESHCORE_WIN = (
    "DEFAULT_MESHCORE_PLUGIN_REPO = Path.home() / \"Documents\" / \"ATAK\" / \"Plugins\" / \"MeshcoreAtak\""
)


def _apply_substitutions(text: str) -> str:
    for old, new in SUBSTITUTIONS:
        text = text.replace(old, new)
    text = text.replace(UDEV_HINT, WIN_ADB_HINT)
    return text


def _patch_adb_deploy_win(text: str) -> str:
    text = text.replace(MESHCORE_LINUX, MESHCORE_WIN)
    text = text.replace(
        "PROJECT_ROOT = SCRIPT_DIR.parent\nDEPLOY_ENV_PATH = PROJECT_ROOT / \"deploy.env\"",
        "if getattr(sys, \"frozen\", False):\n"
        "    PROJECT_ROOT = Path(sys.executable).resolve().parent\n"
        "else:\n"
        "    PROJECT_ROOT = SCRIPT_DIR.parent\n"
        "DEPLOY_ENV_PATH = PROJECT_ROOT / \"deploy.env\"",
    )
    text = text.replace(
        "def load_deploy_env_file() -> None:\n"
        "    if not DEPLOY_ENV_PATH.is_file():\n"
        "        return",
        "def load_deploy_env_file() -> None:\n"
        "    if not DEPLOY_ENV_PATH.is_file():\n"
        "        example = PROJECT_ROOT / \"deploy.env.example\"\n"
        "        if example.is_file():\n"
        "            try:\n"
        "                shutil.copy2(example, DEPLOY_ENV_PATH)\n"
        "            except OSError:\n"
        "                pass\n"
        "    if not DEPLOY_ENV_PATH.is_file():\n"
        "        return",
    )
    # Writable logs on Windows when frozen (AppData, not .local/share).
    old = """    if getattr(sys, "frozen", False):
        d = Path.home() / ".local" / "share" / "atak-pipeline" / "installer_logs"
    else:
        d = SCRIPT_DIR / "logs\""""
    new = """    if getattr(sys, "frozen", False):
        d = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "atak-pipeline" / "installer_logs"
    else:
        d = SCRIPT_DIR / "logs\""""
    if old in text:
        text = text.replace(old, new)
    alt_old = 'alt = Path.home() / ".local" / "share" / "atak-pipeline" / "installer_logs"'
    alt_new = 'alt = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "atak-pipeline" / "installer_logs"'
    text = text.replace(alt_old, alt_new)
    return text


def _sync_renamed_modules() -> list[str]:
    updated: list[str] = []
    for src_name, dst_name in RENAMED_MODULES.items():
        src = SCRIPTS / src_name
        dst = WIN / dst_name
        if not src.is_file():
            raise FileNotFoundError(src)
        text = src.read_text(encoding="utf-8")
        text = _apply_substitutions(text)
        if dst_name == "atak_adb_deploy_win.py":
            text = _patch_adb_deploy_win(text)
        dst.write_text(text, encoding="utf-8")
        updated.append(dst_name)
    return updated


def _sync_install_scripts() -> list[str]:
    updated: list[str] = []
    for name in ("install_windows.ps1", "setup_windows_pipeline.ps1"):
        src = SCRIPTS / name
        dst = WIN / name
        if src.is_file():
            shutil.copy2(src, dst)
            updated.append(name)
    return updated


def _sync_direct_copies() -> list[str]:
    updated: list[str] = []
    for name in DIRECT_COPY:
        if name in SKIP_OVERWRITE:
            continue
        src = SCRIPTS / name
        dst = WIN / name
        if not src.is_file():
            raise FileNotFoundError(src)
        shutil.copy2(src, dst)
        updated.append(name)
    return updated


def _sync_data_subdirs() -> list[str]:
    synced: list[str] = []
    for sub in ("mobile_xml", "bundled_plugins", "tile_plans"):
        src_dir = SCRIPTS / "data" / sub
        dst_dir = WIN / "data" / sub
        if not src_dir.is_dir():
            continue
        dst_dir.mkdir(parents=True, exist_ok=True)
        if sub == "tile_plans":
            src_v1 = src_dir / "v1"
            if src_v1.is_dir():
                dst_v1 = dst_dir / "v1"
                dst_v1.mkdir(parents=True, exist_ok=True)
                for gz in src_v1.glob("*.tiles.gz"):
                    shutil.copy2(gz, dst_v1 / gz.name)
                synced.append(f"data/tile_plans/v1 ({len(list(dst_v1.glob('*.tiles.gz')))} files)")
        else:
            count = 0
            for item in src_dir.rglob("*"):
                if item.is_file():
                    rel = item.relative_to(src_dir)
                    out = dst_dir / rel
                    out.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, out)
                    count += 1
            synced.append(f"data/{sub} ({count} files)")
    for leaf in ("us_states.geojson", "zoom_estimates_z10_z16.json"):
        src = SCRIPTS / "data" / leaf
        if src.is_file():
            shutil.copy2(src, WIN / "data" / leaf)
            synced.append(f"data/{leaf}")
    return synced


def main() -> None:
    WIN.mkdir(exist_ok=True)
    (WIN / "data").mkdir(exist_ok=True)
    renamed = _sync_renamed_modules()
    copied = _sync_direct_copies()
    install_scripts = _sync_install_scripts()
    data = _sync_data_subdirs()
    deploy_example = ROOT / "deploy.env.example"
    if deploy_example.is_file():
        shutil.copy2(deploy_example, WIN / "deploy.env.example")
        data.append("deploy.env.example")
    print("Synced windows_build from scripts/:")
    for line in renamed + copied + install_scripts + data:
        print(f"  - {line}")


if __name__ == "__main__":
    main()
