#!/usr/bin/env python3
"""
Copy Linux scripts/ into windows_build/ — never modify Linux sources for Windows.

Workflow:
  1. Copy scripts/*.py → windows_build/*_win.py (renames + string substitutions)
  2. Apply Windows-only patches defined in THIS file
  3. Copy shared helpers; preserve Windows-only files (tk_window_scaling.py, launchers, win_subprocess.py)

Run before every Windows EXE build:
  python3 scripts/sync_windows_build.py

Maintainer checklist (every patch documented):
  windows_build/LINUX_TO_WINDOWS_CONVERSION.md
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
WIN = ROOT / "windows_build"

# If these appear in Linux scripts/*.py, someone edited Linux for Windows — stop immediately.
_FORBIDDEN_LINUX_MARKERS = (
    "win32",
    "run_hidden",
    "win_subprocess",
    "AppData/Local/Programs/ATAK Pipeline",
    "AppData\\\\Local",
    "CREATE_NO_WINDOW",
)

_LINUX_RUNTIME_SOURCES = (
    "atak_adb_deploy.py",
    "atak_downloader_finalbuild.py",
    "atak_downloader_from_installer.py",
    "atak_imagery_sqlite_builder_finalbuild.py",
    "atak_dted_downloader.py",
)


def _assert_linux_sources_untouched() -> None:
    """Linux scripts/ are read-only inputs. Windows changes belong in patches below."""
    violations: list[str] = []
    for name in _LINUX_RUNTIME_SOURCES:
        path = SCRIPTS / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for marker in _FORBIDDEN_LINUX_MARKERS:
            if marker in text:
                violations.append(f"{name}: contains Windows-only marker {marker!r}")
    if violations:
        raise RuntimeError(
            "Linux scripts/ must not be modified for Windows. Fix windows_build via sync patches only:\n"
            + "\n".join(f"  - {v}" for v in violations)
        )

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

# Never overwritten — Windows-specific behavior lives here.
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

_LINUX_ENSURE_GUI_PATH = '''def ensure_gui_path_for_adb() -> None:
    """Desktop .desktop launches often have a short PATH; match common dev locations."""
    home = Path.home()
    extras = [
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        str(home / "Android/Sdk/platform-tools"),
        str(home / "Android/Sdk/cmdline-tools/latest/bin"),
    ]
    path = os.environ.get("PATH", "")
    parts = [p for p in path.split(os.pathsep) if p]
    merged = path
    for e in reversed(extras):
        if e not in parts and Path(e).is_dir():
            merged = e + os.pathsep + merged
            parts.insert(0, e)
    os.environ["PATH"] = merged'''

_WIN_ENSURE_GUI_PATH = '''def ensure_gui_path_for_adb() -> None:
    """Desktop/EXE launches often have a short PATH; match common dev locations."""
    home = Path.home()
    extras = [
        str(home / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools"),
        str(home / "AppData" / "Local" / "atak-pipeline" / "platform-tools"),
        str(home / "AppData" / "Local" / "Programs" / "platform-tools"),
        r"C:\\platform-tools",
    ]
    for base in (
        Path.cwd(),
        SCRIPT_DIR.parent,
        Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else None,
    ):
        if base is None:
            continue
        extras.append(str(base / "tools" / "platform-tools"))
    path = os.environ.get("PATH", "")
    parts = [p for p in path.split(os.pathsep) if p]
    merged = path
    for e in reversed(extras):
        if e not in parts and Path(e).is_dir():
            merged = e + os.pathsep + merged
            parts.insert(0, e)
    os.environ["PATH"] = merged'''

_LINUX_RUN_ADB_RETURN = "    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)"

_WIN_RUN_ADB_RETURN = """    try:
        return run_hidden(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(
            args=cmd, returncode=127, stdout="", stderr="adb executable not found on PATH"
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=cmd, returncode=124, stdout="", stderr=f"adb timed out after {timeout}s"
        )"""

_LINUX_TK_IMPORT_BLOCK = """try:
    import tkinter as tk
    from tkinter import messagebox, scrolledtext, ttk

    from git_update_check import run_startup_git_update_check, run_startup_release_update_check
    from tk_window_scaling import apply_resizable_window, ensure_window_stacking, scaled_int
except Exception:  # pragma: no cover
    tk = None
    messagebox = None
    scrolledtext = None
    ttk = None
    apply_resizable_window = None  # type: ignore[assignment]
    scaled_int = None  # type: ignore[assignment]
    ensure_window_stacking = None  # type: ignore[assignment]
    run_startup_git_update_check = None  # type: ignore[assignment]
    run_startup_release_update_check = None  # type: ignore[assignment]"""

_WIN_TK_IMPORT_BLOCK = """try:
    import tkinter as tk
    from tkinter import messagebox, scrolledtext, ttk
except Exception:  # pragma: no cover
    tk = None
    messagebox = None
    scrolledtext = None
    ttk = None

try:
    from git_update_check import run_startup_git_update_check, run_startup_release_update_check
except Exception:  # pragma: no cover
    run_startup_git_update_check = None  # type: ignore[assignment]
    run_startup_release_update_check = None  # type: ignore[assignment]

try:
    from tk_window_scaling import apply_resizable_window, ensure_window_stacking, scaled_int
except Exception:  # pragma: no cover
    apply_resizable_window = None  # type: ignore[assignment]
    scaled_int = None  # type: ignore[assignment]
    ensure_window_stacking = None  # type: ignore[assignment]"""


def _apply_substitutions(text: str) -> str:
    for old, new in SUBSTITUTIONS:
        text = text.replace(old, new)
    text = text.replace(UDEV_HINT, WIN_ADB_HINT)
    return text


def _inject_win_subprocess(text: str) -> str:
    if "from win_subprocess import run_hidden" in text:
        return text
    if "import requests\n" in text:
        return text.replace(
            "import requests\n",
            "import requests\n\nfrom win_subprocess import run_hidden\n",
            1,
        )
    return "from win_subprocess import run_hidden\n\n" + text


def _patch_subprocess_calls(text: str) -> str:
    text = text.replace(_LINUX_RUN_ADB_RETURN, _WIN_RUN_ADB_RETURN)
    text = text.replace(
        "        r = subprocess.run(\n            [adb_executable(), \"version\"], capture_output=True, text=True, timeout=10\n        )",
        "        r = run_hidden(\n            [adb_executable(), \"version\"], capture_output=True, text=True, timeout=10\n        )",
    )
    text = text.replace(
        "        r = subprocess.run(\n            [_adb_executable(), \"version\"], capture_output=True, text=True, timeout=10\n        )",
        "        r = run_hidden(\n            [_adb_executable(), \"version\"], capture_output=True, text=True, timeout=10\n        )",
    )
    for old in (
        "proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)",
        "proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)",
        "proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)",
        "proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)",
    ):
        text = text.replace(old, old.replace("subprocess.run", "run_hidden"))
    return text


def _patch_adb_deploy_win(text: str) -> str:
    text = text.replace(MESHCORE_LINUX, MESHCORE_WIN)
    text = text.replace(_LINUX_TK_IMPORT_BLOCK, _WIN_TK_IMPORT_BLOCK)
    text = text.replace(_LINUX_ENSURE_GUI_PATH, _WIN_ENSURE_GUI_PATH)
    text = _inject_win_subprocess(text)
    text = _patch_subprocess_calls(text)
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


def _patch_downloader_win(text: str) -> str:
    text = _inject_win_subprocess(text)
    text = _patch_subprocess_calls(text)
    text = text.replace(
        "from atak_adb_deploy_win import install_apk  # noqa: E402",
        "from atak_adb_deploy_win import install_apk, ensure_gui_path_for_adb  # noqa: E402",
    )
    text = text.replace(
        "def _resolve_adb_serial_for_push() -> Tuple[str, List[str]]:\n    devs = list_usb_devices()",
        "def _resolve_adb_serial_for_push() -> Tuple[str, List[str]]:\n"
        "    if not adb_available():\n"
        "        return \"\", []\n"
        "    devs = list_usb_devices()",
    )
    text = text.replace(
        "def main() -> None:\n    log(\"Startup: beginning git update check...\")",
        "def main() -> None:\n"
        "    try:\n"
        "        ensure_gui_path_for_adb()\n"
        "    except Exception as exc:\n"
        "        log(f\"Startup warning: adb PATH setup failed: {exc}\")\n"
        "    log(\"Startup: beginning git update check...\")",
    )
    text = text.replace(
        "    ensure_window_stacking(parent)\n    text = body if body is not None else DOWNLOADER_NEXT_SQLITE_DIALOG_TEXT",
        "    text = body if body is not None else DOWNLOADER_NEXT_SQLITE_DIALOG_TEXT",
    )
    text = text.replace(
        "def parse_mgrs_to_latlon(mgrs_str: str) -> Tuple[float, float]:\n    import mgrs as _mgrs",
        "def parse_mgrs_to_latlon(mgrs_str: str) -> Tuple[float, float]:\n"
        "    if getattr(sys, \"frozen\", False) and hasattr(sys, \"_MEIPASS\"):\n"
        "        _mgrs_base = Path(sys._MEIPASS)\n"
        "        if hasattr(os, \"add_dll_directory\"):\n"
        "            os.add_dll_directory(str(_mgrs_base))\n"
        "        _path = os.environ.get(\"PATH\", \"\")\n"
        "        if str(_mgrs_base) not in _path.split(os.pathsep):\n"
        "            os.environ[\"PATH\"] = str(_mgrs_base) + os.pathsep + _path\n"
        "    import mgrs as _mgrs",
    )
    return text


def _patch_dted_win(text: str) -> str:
    text = _inject_win_subprocess(text)
    return _patch_subprocess_calls(text)


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
        elif dst_name == "atak_downloader_finalbuild_win.py":
            text = _patch_downloader_win(text)
        elif dst_name == "atak_dted_downloader_win.py":
            text = _patch_dted_win(text)
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
    _assert_linux_sources_untouched()
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
    print("Synced windows_build from scripts/ (Linux sources unchanged):")
    for line in renamed + copied + install_scripts + data:
        print(f"  - {line}")
    print("Windows-only files preserved: tk_window_scaling.py, win_subprocess.py, windows_launcher.py, ...")


if __name__ == "__main__":
    main()
