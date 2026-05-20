#!/usr/bin/env python3
"""
ATAK + plugin install over ADB, then launch the imagery pipeline via
``atak_downloader_from_installer.py`` (not the standalone Imagery Downloader entry).

Configuration (environment variables — in ``deploy.env`` at the project root):

  **Easiest (no JSON file):** set both of these:

  ATAK_CIV_APK_URL — full ``https://...`` link to the **ATAK CIv .apk** file your team hosts.
  ATAK_CIV_VERSION — version label for reporting (e.g. ``5.2.0``). Can match Play Store / release notes.

  **Advanced (one “setup” JSON file on your server):** instead of the two lines above, set

  ATAK_DEPLOY_MANIFEST_URL — HTTPS URL to a small JSON file listing ``atak_apk_url`` and
    ``atak_version`` (and optionally ``plugin_apk_url``). Your server admin creates this once.

    Example JSON:
      {
        "atak_version": "5.2.0",
        "atak_apk_url": "/releases/atak.apk",
        "plugin_apk_url": "/releases/plugin.apk"
      }
    Relative paths are resolved against the manifest URL. You may omit ``plugin_apk_url`` when
    using ATAK_PLUGIN_GITHUB_REPO or other env sources.

  If both ATAK_DEPLOY_MANIFEST_URL and ATAK_CIV_* are set, the manifest URL wins.

  ATAK_DEPLOY_REPORT_URL — optional. Receives POST JSON when installs progress:
      After ATAK install (phase "atak_installed"):
        atak_version, android_serial, plugin_source (empty string), phase.
      After plugin install (phase "complete"):
        atak_version, plugin_source, android_serial, phase.
    If a POST fails, the error is logged and the wizard continues unless
    ATAK_DEPLOY_REPORT_STRICT=1 is set (then the step aborts with an error dialog).

  ATAK_DEPLOY_API_TOKEN — optional. Sent as Authorization: Bearer when posting reports.

  ATAK_PLUGIN_APK — optional explicit local path (overrides all other plugin sources).

  ATAK_PLUGIN_GITHUB_REPO — **recommended**: ``owner/repo``. The installer downloads one
    ``.apk`` from that repository’s **Releases** page — specifically GitHub’s **Latest**
    release (``/releases/latest``), from the files attached to that release — not from git
    branches or Sources zip.

  ATAK_PLUGIN_REPO — optional root directory; the newest *.apk under it (may be a
    debug build—prefer GitHub for installable release APKs).

  ATAK_PLUGIN_APK_URL — optional HTTP(S) URL or path relative to the manifest URL.

  Alternatively, add optional plugin_apk_url to the manifest JSON (lowest priority
  among network/manifest sources after the above).

  **Bundled add-on plugins:** ``scripts/data/bundled_plugins/`` may contain additional
  ``.apk`` files (shipped inside the release zip / PyInstaller bundle). They are installed
  over adb after the appropriate step. **TAK-UV-PRO** is never taken from this folder —
  it always comes from GitHub Releases / manifest / env so you always get the latest.
  Sync ``bundled_plugins`` from ``…/Plugins/Add Ons for Build/`` before building (see
  project handoff / Cursor rule).

  ATAK_PACKAGE_NAME — ATAK applicationId to install/launch (default
    com.atakmap.app.civ).

Server operators can host the manifest next to the ATAK APK and update
atak_version / atak_apk_url whenever you publish a new build; the POST to
ATAK_DEPLOY_REPORT_URL records what was installed on each device.

If adb reports INSTALL_FAILED_VERSION_DOWNGRADE (APK versionCode lower than the
  installed app), the installer retries with ``adb install --allow-downgrade -r``.
  If the phone's package manager does not support that flag (IllegalArgumentException /
  Unknown option), it retries again with the legacy ``-d`` flag (``adb install -d -r``).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from bundled_plugin_install import install_bundled_addon_apks as _install_bundled_addon_apks_core

try:
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
    run_startup_release_update_check = None  # type: ignore[assignment]

APP_TITLE = "ATAK Device Installer"
DEFAULT_ATAK_PACKAGE = "com.atakmap.app.civ"
DEFAULT_PLUGIN_PACKAGE = "com.uvpro.plugin"

# After ATAK APK is installed: show this while the user completes first-run on device.
ATAK_POST_INSTALL_SETUP_INSTRUCTIONS = (
    "Follow setup prompts\n\n"
    "1. Agree to the EULA\n\n"
    "2. Follow the prompts. For each question select “Allow”, “Allow while using the app”, "
    "and select “Allow All” if it is displayed.\n\n"
    "3. Select “I understand” when it asks for background location\n\n"
    "4. Select “Ok” for Android 11+ Warning\n\n"
    "5. Select “I Understand” for required missing permissions\n\n"
    "6. Settings window: Select “Permissions”, then “Location”, then “Allow all the time”\n\n"
    "7. Select the back arrow until you return to ATAK\n\n"
    "8. Select “I understand” for file system access\n\n"
    "9. Settings window: Turn on “Allow access to manage all files”\n\n"
    "10. Select the back arrow\n\n"
    "11. Select “Done” on the TAK Device Setup screen\n\n"
    "12. Select “Do not show this hint again” and OK\n\n"
    "13. Select “Allow” to allow to run in background\n\n"
    "14. Select “Continue” on this window. Allow ATAK to install the plugin.\n\n"
    "Leave ATAK open on the main map.\n\n"
    "When setup is complete, select Continue."
)

# After plugin APK is installed: confirm the in-app prompt on the device.
ATAK_POST_PLUGIN_SETUP_INSTRUCTIONS = (
    "Plugin install is complete.\n\n"
    "Please select OK on your device to install the plugin.\n\n"
    "When the plugin is installed on the device, click Continue."
)

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    SCRIPT_DIR = Path(sys._MEIPASS) / "scripts"
else:
    SCRIPT_DIR = Path(__file__).resolve().parent

DOWNLOADER = SCRIPT_DIR / "atak_downloader_from_installer.py"
MOBILE_XML_DIR = SCRIPT_DIR / "data" / "mobile_xml"
MOBILE_XML_DEVICE_PATH = "/sdcard/atak/imagery/mobile/mapsources"
MOBILE_IMPORT_DEVICE_PATH = "/sdcard/atak/tools/import"
USER_AGENT = "ATAK-Pipeline-Deploy/1.0"
PROJECT_ROOT = SCRIPT_DIR.parent
DEPLOY_ENV_PATH = PROJECT_ROOT / "deploy.env"

_INSTALLER_LOG: Optional[Path] = None

# Env keys we never log verbatim (presence only).
_SENSITIVE_ENV_SUBSTR = ("TOKEN", "SECRET", "PASSWORD", "KEY", "CREDENTIAL")


def installer_log_dir() -> Path:
    """Writable log directory (PyInstaller bundle dir is often read-only)."""
    if getattr(sys, "frozen", False):
        d = Path.home() / ".local" / "share" / "atak-pipeline" / "installer_logs"
    else:
        d = SCRIPT_DIR / "logs"
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        alt = Path.home() / ".local" / "share" / "atak-pipeline" / "installer_logs"
        alt.mkdir(parents=True, exist_ok=True)
        return alt


def setup_installer_logging() -> Path:
    """File + stderr logging for support; call once at process start."""
    global _INSTALLER_LOG
    log_dir = installer_log_dir()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    _INSTALLER_LOG = log_dir / f"atak_installer_{ts}.log"

    logger = logging.getLogger("atak_installer")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fh = logging.FileHandler(_INSTALLER_LOG, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    fh.setFormatter(fmt)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)

    logger.info("Log file: %s", _INSTALLER_LOG)
    try:
        (log_dir / "LATEST_LOG.txt").write_text(str(_INSTALLER_LOG.resolve()) + "\n", encoding="utf-8")
    except OSError:
        pass
    return _INSTALLER_LOG


def _install_exception_hooks() -> None:
    logger = logging.getLogger("atak_installer")

    def _main_hook(
        exc_type: type[BaseException], exc_value: BaseException, exc_tb: Any
    ) -> None:
        if logger.handlers:
            logger.critical("Uncaught exception (main thread)", exc_info=(exc_type, exc_value, exc_tb))
        else:
            traceback.print_exception(exc_type, exc_value, exc_tb, file=sys.stderr)
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _main_hook

    if hasattr(threading, "excepthook"):
        _default_thread_hook = threading.excepthook

        def _thread_hook(args: threading.ExceptHookArgs) -> None:
            if logger.handlers:
                logger.critical(
                    "Uncaught exception in thread %r",
                    args.thread.name,
                    exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
                )
            _default_thread_hook(args)

        threading.excepthook = _thread_hook


def log_startup_context() -> None:
    logger = logging.getLogger("atak_installer")
    ver_path = PROJECT_ROOT / "VERSION"
    ver = ver_path.read_text(encoding="utf-8").strip() if ver_path.is_file() else "(unknown)"
    logger.info("VERSION (%s): %s", ver_path, ver)
    logger.info("Python: %s", sys.version.replace("\n", " "))
    logger.info("Platform: %s", sys.platform)
    logger.info("Frozen bundle: %s", getattr(sys, "frozen", False))
    logger.info("CWD: %s", os.getcwd())
    logger.info("Argv: %s", sys.argv)
    logger.info("Script path: %s", Path(__file__).resolve())
    logger.info("adb on PATH: %s", shutil.which("adb") or "(not found)")

    env_keys = sorted(
        {
            "ATAK_DEPLOY_MANIFEST_URL",
            "ATAK_CIV_APK_URL",
            "ATAK_CIV_VERSION",
            "ATAK_PLUGIN_GITHUB_REPO",
            "ATAK_PLUGIN_APK",
            "ATAK_PLUGIN_APK_URL",
            "ATAK_PACKAGE_NAME",
            "ANDROID_SERIAL",
            "ATAK_DEPLOY_REPORT_URL",
            "ATAK_DEPLOY_API_TOKEN",
        }
        | {k for k in os.environ if k.startswith("ATAK_")}
    )
    for k in env_keys:
        raw = os.environ.get(k, "")
        if any(s in k.upper() for s in _SENSITIVE_ENV_SUBSTR):
            logger.info("Env %s: %s", k, "(set)" if raw.strip() else "(empty)")
        elif len(raw) > 200:
            logger.info("Env %s: %s… (%d chars)", k, raw[:200], len(raw))
        else:
            logger.info("Env %s: %s", k, raw if raw else "(empty)")


def load_deploy_env_file() -> None:
    if not DEPLOY_ENV_PATH.is_file():
        return
    try:
        raw = DEPLOY_ENV_PATH.read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if not key or not val:
            continue
        if not os.environ.get(key, "").strip():
            os.environ[key] = val


def ensure_gui_path_for_adb() -> None:
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
    os.environ["PATH"] = merged


def log(msg: str) -> None:
    text = msg.rstrip("\n")
    lg = logging.getLogger("atak_installer")
    if lg.handlers:
        for part in text.splitlines() or [""]:
            lg.info("%s", part)
    else:
        line = msg if msg.endswith("\n") else msg + "\n"
        try:
            sys.stderr.write(line)
            sys.stderr.flush()
        except Exception:
            pass


def env_optional(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def atak_package_name() -> str:
    return env_optional("ATAK_PACKAGE_NAME", DEFAULT_ATAK_PACKAGE)


def adb_executable() -> str:
    return shutil.which("adb") or "adb"


def run_adb(args: List[str], serial: Optional[str] = None, timeout: int = 120) -> subprocess.CompletedProcess:
    cmd = [adb_executable()]
    if serial:
        cmd += ["-s", serial]
    cmd.extend(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def adb_available() -> bool:
    try:
        r = subprocess.run(
            [adb_executable(), "version"], capture_output=True, text=True, timeout=10
        )
        return r.returncode == 0
    except Exception:
        return False


def adb_devices_raw() -> subprocess.CompletedProcess:
    """Run plain ``adb devices`` (no ``-l``) for stable, whitespace-tolerant parsing."""
    run_adb(["start-server"], serial=None, timeout=30)
    return run_adb(["devices"], serial=None, timeout=30)


def parse_adb_devices_lines(stdout: str) -> Tuple[List[str], List[str]]:
    """Return (serials in *device* state, diagnostic lines for any other row)."""
    ready: List[str] = []
    diag: List[str] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        if line.startswith("*"):  # e.g. daemon messages
            diag.append(line)
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        if state == "device":
            ready.append(serial)
        else:
            diag.append(line)
    return ready, diag


def list_usb_devices() -> List[str]:
    r = adb_devices_raw()
    if r.returncode != 0:
        log(f"adb devices failed: {r.stderr or r.stdout}")
        return []
    ready, _diag = parse_adb_devices_lines(r.stdout)
    return ready


def adb_devices_human_summary() -> str:
    """For error dialogs: adb path + full ``adb devices`` output."""
    exe = adb_executable()
    r = adb_devices_raw()
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    lines = [f"adb binary: {exe}", "", "$ adb devices", out or "(no stdout)"]
    if err:
        lines.extend(["", "stderr:", err])
    return "\n".join(lines)


def resolve_url(manifest_url: str, maybe_relative: str) -> str:
    return urllib.parse.urljoin(manifest_url, maybe_relative)


def fetch_manifest(url: str) -> Dict[str, Any]:
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(url, headers=headers, timeout=60)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise ValueError("Manifest must be a JSON object")
    return data


def parse_inline_atak_from_env() -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Simple deploy: ATAK_CIV_APK_URL (full URL to .apk) + ATAK_CIV_VERSION in deploy.env.
    Returns (manifest-shaped dict, base URL for resolve_url) or (None, '') if not configured.
    """
    apk = env_optional("ATAK_CIV_APK_URL")
    ver = env_optional("ATAK_CIV_VERSION")
    if not apk or not ver:
        return None, ""
    p = urllib.parse.urlparse(apk)
    if p.scheme in ("http", "https") and p.netloc:
        base = f"{p.scheme}://{p.netloc}/"
    else:
        base = "https://local.invalid/"
    return {"atak_version": ver, "atak_apk_url": apk}, base


def github_release_api_headers() -> Dict[str, str]:
    h = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = env_optional("GITHUB_TOKEN") or env_optional("ATAK_GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def github_latest_release_plugin_apk(owner_repo: str) -> Tuple[str, str, str]:
    """Pick the plugin ``.apk`` from the repo's **published** GitHub Release marked *latest*.

    Uses the GitHub API: ``GET /repos/{{owner}}/{{repo}}/releases/latest`` — i.e. the
    release GitHub shows as "Latest" on the Releases page, **not** a branch or raw files.

    Returns ``(browser_download_url, tag_name, asset_file_name)``.
    """
    slug = owner_repo.strip().strip("/")
    parts = [p for p in slug.split("/") if p]
    if len(parts) != 2:
        raise ValueError(
            f"ATAK_PLUGIN_GITHUB_REPO must be owner/repo (e.g. atakmaps/BTECH-Relay), got {owner_repo!r}"
        )
    owner, repo = parts[0], parts[1]
    api = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    r = requests.get(api, headers=github_release_api_headers(), timeout=60)
    r.raise_for_status()
    data = r.json()
    tag = str(data.get("tag_name") or "?")
    assets = data.get("assets") or []
    apk_assets = [a for a in assets if str(a.get("name", "")).lower().endswith(".apk")]
    if not apk_assets:
        raise RuntimeError(
            f"GitHub Releases latest for {owner}/{repo} ({tag}) has no .apk assets attached. "
            "Upload a release .apk on that release."
        )

    def name_lower(i: int) -> str:
        return str(apk_assets[i].get("name", "")).lower()

    non_debug_idx = [i for i in range(len(apk_assets)) if "debug" not in name_lower(i)]
    pool_idx = non_debug_idx if non_debug_idx else list(range(len(apk_assets)))

    def prefer() -> int:
        for i in pool_idx:
            n = name_lower(i)
            if "plugin" in n and "release" in n:
                return i
        for i in pool_idx:
            n = name_lower(i)
            if "release" in n:
                return i
        return pool_idx[0]

    chosen = apk_assets[prefer()]
    filename = str(chosen.get("name", ""))
    url = str(chosen["browser_download_url"])
    return url, tag, filename


def download_file(url: str, dest: Path, status_cb=None, timeout: int = 600) -> None:
    headers = {"User-Agent": USER_AGENT}
    with requests.get(url, headers=headers, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        downloaded = 0
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                if status_cb and total:
                    status_cb(downloaded, total)


def install_apk(serial: str, apk_path: Path, status_cb=None, *, package_name: Optional[str] = None) -> None:
    """Install APK with downgrade and signature-mismatch retries.

    If the device already has the package signed with a different certificate
    (INSTALL_FAILED_UPDATE_INCOMPATIBLE), the old package is uninstalled and the
    install is retried automatically.  Pass ``package_name`` so the uninstall
    targets the correct package; if omitted the recovery step is skipped.
    """
    name = apk_path.name
    if status_cb:
        status_cb(f"Installing {name}…")
    r = run_adb(["install", "-r", str(apk_path)], serial=serial, timeout=600)
    combined = (r.stderr or "") + (r.stdout or "")

    if r.returncode != 0 and "INSTALL_FAILED_VERSION_DOWNGRADE" in combined:
        log("adb install: INSTALL_FAILED_VERSION_DOWNGRADE; retrying with --allow-downgrade")
        if status_cb:
            status_cb(f"Installing {name} (allow downgrade)…")
        r = run_adb(
            ["install", "--allow-downgrade", "-r", str(apk_path)],
            serial=serial,
            timeout=600,
        )
        combined = (r.stderr or "") + (r.stdout or "")
        if r.returncode != 0 and _device_rejects_allow_downgrade_flag(combined):
            log("adb install: device pm has no --allow-downgrade; retrying with -d")
            if status_cb:
                status_cb(f"Installing {name} (allow downgrade, -d)…")
            r = run_adb(["install", "-d", "-r", str(apk_path)], serial=serial, timeout=600)
            combined = (r.stderr or "") + (r.stdout or "")
        # Some devices still reject downgrade flags. In that case, fully uninstall
        # the existing package and retry once if the caller provided a package name.
        if r.returncode != 0 and "INSTALL_FAILED_VERSION_DOWNGRADE" in combined and package_name:
            log(
                f"adb install: downgrade still rejected for {package_name}; "
                "performing full uninstall and retrying install"
            )
            if status_cb:
                status_cb(f"Removing previous {package_name} (downgrade blocked)…")
            uninstall_package(serial, package_name, status_cb, require_absent=True)
            if status_cb:
                status_cb(f"Installing {name}…")
            r = run_adb(["install", "-r", str(apk_path)], serial=serial, timeout=600)
            combined = (r.stderr or "") + (r.stdout or "")

    # Signature mismatch: uninstall the old package and retry once.
    if r.returncode != 0 and "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in combined:
        if package_name:
            # -k cannot clear the stored signature — only a full uninstall does.
            log(f"adb install: signature mismatch for {package_name}; performing full uninstall (clears signature) and retrying")
            if status_cb:
                status_cb(f"Removing old {name} (signature changed — full uninstall required)…")
            run_adb(["uninstall", package_name], serial=serial, timeout=120)
            if status_cb:
                status_cb(f"Installing {name}…")
            r = run_adb(["install", "-r", str(apk_path)], serial=serial, timeout=600)
            combined = (r.stderr or "") + (r.stdout or "")
        else:
            log("adb install: INSTALL_FAILED_UPDATE_INCOMPATIBLE but no package_name provided for uninstall")

    if r.returncode != 0:
        raise RuntimeError(f"adb install failed:\n{(r.stderr or r.stdout).strip()}")


def install_bundled_addon_apks(serial: str, log_fn, status_cb=None) -> None:
    """Install add-on plugin APKs bundled under ``scripts/data/bundled_plugins/`` (non-fatal per file)."""
    _install_bundled_addon_apks_core(
        serial,
        log_fn,
        status_cb,
        install_apk,
        plugin_root=SCRIPT_DIR / "data" / "bundled_plugins",
    )


def _device_rejects_allow_downgrade_flag(combined: str) -> bool:
    """True if device's ``pm install`` failed because ``--allow-downgrade`` is unsupported."""
    if not combined:
        return False
    lower = combined.lower()
    if "unknown option" in lower and "allow-downgrade" in lower:
        return True
    if "illegalargumentexception" in lower and "allow-downgrade" in lower:
        return True
    return False



def push_mobile_xml(serial: str, log_fn=None) -> None:
    """Push bundled mobile map/waypoint files to the device.

    Routing by extension (bundled: scripts/data/mobile_xml/ — rsync from
    /home/paul/Documents/ATAK/Plugins/Add Ons for Build/ before every build; see HANDOFF):
      .xml          → MOBILE_XML_DEVICE_PATH  (/sdcard/atak/imagery/mobile/mapsources)
      .kmz / .zip   → MOBILE_IMPORT_DEVICE_PATH (/sdcard/atak/tools/import)
    """
    if not MOBILE_XML_DIR.is_dir():
        if log_fn:
            log_fn("No mobile asset directory found; skipping.")
        return

    xml_files = sorted(MOBILE_XML_DIR.rglob("*.xml"))
    import_files = sorted(MOBILE_XML_DIR.rglob("*.kmz")) + sorted(MOBILE_XML_DIR.rglob("*.zip"))
    dest_map: dict[str, list] = {
        MOBILE_XML_DEVICE_PATH: xml_files,
        MOBILE_IMPORT_DEVICE_PATH: import_files,
    }

    total = sum(len(v) for v in dest_map.values())
    if total == 0:
        if log_fn:
            log_fn("No mobile assets found to push.")
        return

    for device_path, files in dest_map.items():
        if not files:
            continue
        run_adb(["shell", "mkdir", "-p", device_path], serial=serial, timeout=30)
        for f in files:
            rel = f.relative_to(MOBILE_XML_DIR)
            if log_fn:
                log_fn(f"Pushing {rel} …")
            r = run_adb(["push", str(f), f"{device_path}/{f.name}"], serial=serial, timeout=120)
            if r.returncode != 0 and log_fn:
                log_fn(f"Warning: failed to push {f.name}: {r.stderr}")

    if log_fn:
        log_fn(f"Mobile assets installed ({total} files).")


def launch_atak(serial: str) -> None:
    pkg = atak_package_name()
    r = run_adb(
        ["shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"],
        serial=serial,
        timeout=60,
    )
    if r.returncode != 0:
        log(f"monkey launch returned {r.returncode}: {r.stderr}")


def launch_atak_reliable(serial: Optional[str] = None) -> bool:
    """Best-effort ATAK launch with fallback strategies."""
    ser = (serial or env_optional("ANDROID_SERIAL")).strip()
    pkg = atak_package_name()

    # Clear stale background state first.
    run_adb(["shell", "am", "force-stop", pkg], serial=(ser or None), timeout=30)
    time.sleep(0.8)

    # Resolve launcher activity dynamically (varies by ATAK flavor/build).
    resolved_activity = ""
    rr = run_adb(
        ["shell", "cmd", "package", "resolve-activity", "--brief", pkg],
        serial=(ser or None),
        timeout=30,
    )
    if rr.returncode == 0:
        for line in (rr.stdout or "").splitlines():
            txt = line.strip()
            if txt.startswith(pkg + "/"):
                resolved_activity = txt
                break

    # Prefer explicit am start and wait for launch completion.
    am_target = resolved_activity or f"{pkg}/com.atakmap.app.ATAKActivityCiv"
    r2 = run_adb(
        [
            "shell",
            "am",
            "start",
            "-W",
            "-a",
            "android.intent.action.MAIN",
            "-c",
            "android.intent.category.LAUNCHER",
            "-n",
            am_target,
        ],
        serial=(ser or None),
        timeout=60,
    )
    out2 = ((r2.stdout or "") + "\n" + (r2.stderr or "")).strip()
    if r2.returncode == 0 and "Error:" not in out2:
        log(f"ATAK restart via am start succeeded: {am_target}")
        return True

    # Fallback to monkey launcher.
    r = run_adb(
        ["shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"],
        serial=(ser or None),
        timeout=60,
    )
    out1 = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    if r.returncode == 0:
        log("ATAK restart via monkey succeeded.")
        return True

    log(
        "Warning: ATAK restart failed. "
        f"am start={r2.returncode} ({out2}), "
        f"monkey={r.returncode} ({out1})"
    )
    return False


def is_package_installed(serial: str, package_name: str) -> bool:
    """True if package is installed on the target device."""
    r = run_adb(["shell", "pm", "list", "packages", package_name], serial=serial, timeout=30)
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    if r.returncode != 0:
        return False
    needle = f"package:{package_name}"
    return any(line.strip() == needle for line in out.splitlines())


def uninstall_package(serial: str, package_name: str, status_cb=None, *, require_absent: bool = False) -> None:
    """Full uninstall for a package.

    By default this is non-fatal if the package is not installed. When
    ``require_absent`` is True, raise if the package is still present after
    uninstall attempt(s).
    """
    if status_cb:
        status_cb(f"Removing previous {package_name}…")
    installed_before = is_package_installed(serial, package_name)
    if not installed_before:
        log(f"Package not installed (nothing to remove): {package_name}")
        return

    r = run_adb(["uninstall", package_name], serial=serial, timeout=120)
    out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    lower = out.lower()
    if r.returncode == 0 and "success" in lower:
        if not require_absent or not is_package_installed(serial, package_name):
            log(f"Uninstalled package: {package_name}")
            return

    still_installed = is_package_installed(serial, package_name)
    if not still_installed:
        log(f"Uninstalled package (verified absent): {package_name}")
        return

    msg = f"uninstall of {package_name} failed; package is still installed. adb={r.returncode}: {out}"
    if require_absent:
        raise RuntimeError(msg)
    # Keep going but log details so operators can diagnose odd device states.
    log(f"Warning: {msg}")


def post_report(
    report_url: str,
    token: Optional[str],
    atak_version: str,
    plugin_source: str,
    android_serial: str,
    phase: str,
) -> None:
    payload = {
        "atak_version": atak_version,
        "plugin_source": plugin_source,
        "android_serial": android_serial,
        "phase": phase,
    }
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.post(report_url, json=payload, headers=headers, timeout=60)
    r.raise_for_status()


def safe_post_report(
    report_url: str,
    token: Optional[str],
    atak_version: str,
    plugin_source: str,
    android_serial: str,
    phase: str,
) -> None:
    strict = env_optional("ATAK_DEPLOY_REPORT_STRICT") == "1"
    try:
        post_report(report_url, token, atak_version, plugin_source, android_serial, phase)
    except requests.RequestException as exc:
        log(f"ATAK_DEPLOY_REPORT_URL POST failed ({phase}): {exc}")
        if strict:
            raise


def resolve_plugin_apk(manifest: Dict[str, Any], manifest_url: str) -> Tuple[Path, str, bool]:
    """
    Returns (path to apk, description for report, whether temp file should be deleted).

    Resolution order:
      1. ATAK_PLUGIN_APK — explicit file
      2. ATAK_PLUGIN_GITHUB_REPO — **GitHub Releases**: downloads the chosen ``.apk``
         attached to the repository's *Latest* published release (API ``.../releases/latest``).
      3. ATAK_PLUGIN_REPO — newest .apk under directory
      4. ATAK_PLUGIN_APK_URL — download
      5. plugin_apk_url from manifest
    """
    env_apk = env_optional("ATAK_PLUGIN_APK")
    if env_apk:
        p = Path(env_apk).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"ATAK_PLUGIN_APK is not a file: {p}")
        return p, str(p), False

    gh = env_optional("ATAK_PLUGIN_GITHUB_REPO")
    if gh:
        dl_url, rel_tag, asset_name = github_latest_release_plugin_apk(gh)
        log(
            f"Plugin APK from GitHub Releases (latest): {gh} @ {rel_tag} — asset {asset_name!r}"
        )
        fd, tmp = tempfile.mkstemp(suffix=".apk")
        os.close(fd)
        tmp_path = Path(tmp)
        download_file(dl_url, tmp_path)
        return tmp_path, f"github-releases:{gh}@{rel_tag}:{asset_name}", True

    repo = env_optional("ATAK_PLUGIN_REPO")
    if repo:
        root = Path(repo).expanduser()
        apks = [x for x in root.rglob("*.apk") if x.is_file()]
        if not apks:
            raise FileNotFoundError(f"No .apk files under ATAK_PLUGIN_REPO: {root}")
        newest = max(apks, key=lambda x: x.stat().st_mtime)
        return newest, str(newest), False

    plugin_env_url = env_optional("ATAK_PLUGIN_APK_URL")
    if plugin_env_url:
        if plugin_env_url.startswith("http://") or plugin_env_url.startswith("https://"):
            full = plugin_env_url
        else:
            full = resolve_url(manifest_url, plugin_env_url)
        fd, tmp = tempfile.mkstemp(suffix=".apk")
        os.close(fd)
        tmp_path = Path(tmp)
        download_file(full, tmp_path)
        return tmp_path, full, True

    url = manifest.get("plugin_apk_url")
    if not url:
        raise RuntimeError(
            "No plugin APK source: set ATAK_PLUGIN_GITHUB_REPO (recommended), ATAK_PLUGIN_APK_URL, "
            "ATAK_PLUGIN_APK, ATAK_PLUGIN_REPO, or plugin_apk_url in the manifest."
        )
    full = resolve_url(manifest_url, str(url))
    fd, tmp = tempfile.mkstemp(suffix=".apk")
    os.close(fd)
    tmp_path = Path(tmp)
    download_file(full, tmp_path)
    return tmp_path, full, True


def resolve_atak_apk(manifest: Dict[str, Any], manifest_url: str) -> Tuple[Path, str, bool]:
    ver = manifest.get("atak_version")
    url = manifest.get("atak_apk_url")
    if not ver or not url:
        raise RuntimeError("Manifest must include atak_version and atak_apk_url")
    full = resolve_url(manifest_url, str(url))
    fd, tmp = tempfile.mkstemp(suffix=".apk")
    os.close(fd)
    tmp_path = Path(tmp)
    download_file(full, tmp_path)
    return tmp_path, str(ver), True


def pick_serial(devices: List[str]) -> Optional[str]:
    if not devices:
        return None
    if len(devices) == 1:
        return devices[0]
    pref = env_optional("ANDROID_SERIAL")
    if pref and pref in devices:
        return pref
    return None


class DeployWizard(tk.Tk):
    def report_callback_exception(self, exc: BaseException, val: Optional[BaseException], tb: Any) -> None:
        logging.getLogger("atak_installer").error(
            "Tkinter callback exception",
            exc_info=(exc, val, tb),
        )
        super().report_callback_exception(exc, val, tb)

    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.configure(cursor="arrow")
        self.selected_serial: Optional[str] = None
        self.manifest_url = env_optional("ATAK_DEPLOY_MANIFEST_URL")
        self._inline_atak_manifest, self._inline_resolve_base = parse_inline_atak_from_env()
        if self.manifest_url:
            self._inline_atak_manifest = None
            self._inline_resolve_base = ""
        self.report_url = env_optional("ATAK_DEPLOY_REPORT_URL")
        self._atak_apk_temp: Optional[Path] = None
        self._plugin_apk_temp: Optional[Path] = None
        self._manifest_cache: Optional[Dict[str, Any]] = None
        self._atak_version_value = ""
        self._plugin_report_label = ""
        # "atak", "plugin", or "both" — set on step 1
        self._install_choice: str = "both"

        outer = tk.Frame(self, padx=16, pady=16)
        outer.configure(cursor="arrow")
        outer.pack(fill="both", expand=True)

        self.step_label = tk.Label(outer, text="", font=("Arial", 12, "bold"), anchor="w", justify="left")
        self.step_label.pack(fill="x", pady=(0, 8))

        self.body = tk.Label(outer, text="", justify="left", anchor="w", wraplength=500)

        self._instructions_outer = tk.Frame(outer)
        self._setup_scroll = scrolledtext.ScrolledText(
            self._instructions_outer,
            height=15,
            wrap=tk.WORD,
            font=("Arial", 10),
        )
        self._setup_scroll.pack(fill="both", expand=True)
        self._setup_scroll.configure(cursor="arrow")

        # Bottom strip: buttons directly above progress (same for ATAK install, plugin install, etc.)
        self.footer = tk.Frame(outer)
        self.btn_row = tk.Frame(self.footer)
        self.btn_primary = tk.Button(self.btn_row, text="Continue", width=14, command=self._on_primary)
        self.btn_primary.pack(side="right", padx=(8, 0))
        self.btn_secondary = tk.Button(self.btn_row, text="Quit", width=10, command=self.destroy)
        self.btn_secondary.pack(side="right")

        self.progress = ttk.Progressbar(self.footer, mode="indeterminate")
        try:
            self.progress.configure(cursor="arrow")
        except tk.TclError:
            pass

        self.status = tk.Label(self.footer, text="", anchor="w", justify="left", fg="gray25")

        self.body.pack(fill="both", expand=True, pady=(0, 12))
        _dw_scale = apply_resizable_window(self, 700, 560, (600, 460))
        self.body.configure(wraplength=scaled_int(640, _dw_scale))

        # Note label below scroll box — built after scale is known so wraplength is correct
        self._instructions_note = tk.Label(
            self._instructions_outer,
            text="",
            anchor="w",
            justify="left",
            font=("Arial", 10, "bold"),
            wraplength=scaled_int(620, _dw_scale),
        )

        # Step 1 — install selection panel (built after scale is known)
        self._selection_outer = tk.Frame(outer)
        self._choice_var = tk.StringVar(value="both")
        _rb_wrap = scaled_int(620, _dw_scale)
        for val, label in (
            ("both", "ATAK + TAK-UV-PRO plugin (recommended for first-time install)"),
            ("atak", "ATAK only"),
            ("plugin", "TAK-UV-PRO plugin only"),
        ):
            rb = tk.Radiobutton(
                self._selection_outer,
                text=label,
                variable=self._choice_var,
                value=val,
                anchor="w",
                justify="left",
                wraplength=_rb_wrap,
                font=("Arial", 11),
            )
            rb.pack(anchor="w", fill="x", pady=4)
        self.btn_row.pack(fill="x")
        self.progress.pack(fill="x", pady=(8, 0))
        self.status.pack(fill="x", pady=(4, 0))
        self.footer.pack(fill="x", pady=(12, 0))

        self._step = 0
        self._render_step()
        logging.getLogger("atak_installer").info("DeployWizard ready, initial step=%s", self._step)

    def _focus_for_dialog(self) -> None:
        if ensure_window_stacking is not None:
            ensure_window_stacking(self)
            self.update_idletasks()

    def _atak_install_ready(self) -> bool:
        return bool(self.manifest_url) or bool(self._inline_atak_manifest)

    def _resolve_url_base(self) -> str:
        if self.manifest_url:
            return self.manifest_url
        return self._inline_resolve_base or ""

    def _set_busy(self, busy: bool) -> None:
        self.btn_primary.configure(state=("disabled" if busy else "normal"))

    def _set_secondary_visible(self, visible: bool) -> None:
        if visible:
            if not self.btn_secondary.winfo_manager():
                self.btn_secondary.pack(side="right")
        else:
            if self.btn_secondary.winfo_manager():
                self.btn_secondary.pack_forget()

    def _restart_atak_then_finish(self) -> None:
        """Step-6 continue action: restart ATAK, wait, then show final exit screen."""
        self.btn_primary.configure(state="disabled")
        self.btn_secondary.configure(state="disabled")
        self.status.configure(text="Restarting ATAK on device…")

        def work() -> None:
            try:
                log("Step 6 continue: attempting ATAK restart (pass 1)")
                ok = launch_atak_reliable(self.selected_serial)
                if not ok:
                    log("Step 6 continue: first ATAK restart attempt failed; retrying once")
                    time.sleep(1.0)
                    launch_atak_reliable(self.selected_serial)
            except Exception:
                log("launch_atak before final screen failed")
            # Keep installer alive briefly so restart command settles while app is running.
            time.sleep(5.0)
            self.after(0, lambda: self._advance(7))

        threading.Thread(target=work, daemon=True).start()

    def _show_body_label(self) -> None:
        self._instructions_outer.pack_forget()
        self._selection_outer.pack_forget()
        self.body.pack(fill="both", expand=True, pady=(0, 12), before=self.footer)

    def _show_instructions_panel(self, body: str, *, footer_note: str = "") -> None:
        self.body.pack_forget()
        self._selection_outer.pack_forget()
        self._instructions_outer.pack(fill="both", expand=True, pady=(0, 12), before=self.footer)
        self._setup_scroll.configure(state="normal")
        self._setup_scroll.delete("1.0", tk.END)
        self._setup_scroll.insert("1.0", body)
        self._setup_scroll.configure(state="disabled")
        if footer_note:
            self._instructions_note.configure(text=footer_note)
            self._instructions_note.pack(fill="x", pady=(8, 0))
        else:
            self._instructions_note.pack_forget()
            self._instructions_note.configure(text="")

    def _show_selection_panel(self) -> None:
        self.body.pack_forget()
        self._instructions_outer.pack_forget()
        self._selection_outer.pack(fill="both", expand=True, pady=(0, 12), before=self.footer)

    def _show_setup_instructions_panel(self) -> None:
        self._show_instructions_panel(
            ATAK_POST_INSTALL_SETUP_INSTRUCTIONS,
            footer_note=(
                "NOTE: Make sure you have accomplished the ATAK setup BEFORE you select Continue."
            ),
        )

    def _render_step(self) -> None:
        logging.getLogger("atak_installer").info("_render_step step=%s choice=%s", self._step, self._install_choice)
        self.progress.stop()
        self.progress.pack_forget()

        # Step 0 — welcome
        if self._step == 0:
            self._set_secondary_visible(True)
            self._show_body_label()
            self.step_label.configure(text="")
            self.body.configure(
                text=(
                    "This program will install the ATAK software and/or the TAK-UV-PRO plugin.\n\n"
                    "The installer will guide you through the process.\n\n"
                    "Upon completion, run the ATAK Imagery Downloader from your start menu or desktop to download any required imagery."
                )
            )
            self.btn_primary.configure(text="Continue", command=lambda: self._advance(1))
            self.status.configure(text="")

        # Step 1 — choose what to install
        elif self._step == 1:
            self._set_secondary_visible(True)
            self.body.configure(text="")
            self._show_selection_panel()
            self.step_label.configure(text="What would you like to install?")
            self.btn_primary.configure(state="normal", text="Continue", command=self._step_confirm_choice)
            self.status.configure(text="")

        # Step 2 — connect device
        elif self._step == 2:
            self._set_secondary_visible(True)
            self._show_body_label()
            self.step_label.configure(text="Connect your Android device")
            self.body.configure(
                text=(
                    "1. On the phone, enable Developer options and USB debugging.\n"
                    "2. Connect USB\n"
                    "3. Select USB Mode, File Transfer\n\n"
                    "Click Continue to verify that adb sees your device."
                )
            )
            self.btn_primary.configure(state="normal", text="Continue", command=self._step_connect_check)
            self.status.configure(text="")

        # Step 3 — install ATAK  (skipped when plugin-only)
        elif self._step == 3:
            self._set_secondary_visible(True)
            self._show_body_label()
            self.step_label.configure(text="Installing ATAK")
            self.body.configure(
                text="Downloading the ATAK build from your server and installing it with adb."
            )
            self.progress.pack(fill="x", pady=(8, 0), before=self.status)
            self.btn_primary.configure(state="disabled", text="Working…")
            self._begin_install_atak()

        # Step 4 — ATAK first-run setup  (skipped when plugin-only)
        elif self._step == 4:
            self._set_secondary_visible(True)
            self._show_setup_instructions_panel()
            self.step_label.configure(text="Complete ATAK setup on device")
            # After ATAK setup: go to plugin install if needed, else skip to completion
            next_step = 5 if self._install_choice == "both" else 7
            self.btn_primary.configure(
                state="normal", text="Continue", command=lambda n=next_step: self._advance(n)
            )
            self.status.configure(text="")

        # Step 5 — install plugin  (skipped when atak-only)
        elif self._step == 5:
            self._set_secondary_visible(True)
            self._show_body_label()
            self.step_label.configure(text="Installing plugin")
            self.body.configure(
                text=(
                    "Installing the TAK-UV-PRO plugin from your configured download source."
                )
            )
            self.progress.pack(fill="x", pady=(8, 0), before=self.status)
            self.btn_primary.configure(state="disabled", text="Working…")
            self._begin_install_plugin()

        # Step 6 — post-plugin instructions  (skipped when atak-only)
        elif self._step == 6:
            self._set_secondary_visible(True)
            self._show_instructions_panel(ATAK_POST_PLUGIN_SETUP_INSTRUCTIONS)
            self.step_label.configure(text="Almost done")
            self.btn_primary.configure(
                state="normal", text="Continue", command=self._restart_atak_then_finish
            )
            self.status.configure(text="")

        # Step 7 — completion
        elif self._step == 7:
            self._set_secondary_visible(False)
            self._show_body_label()
            self.step_label.configure(text="Installation complete")
            self.body.configure(
                text=(
                    "Your device install is complete.\n\n"
                    "You may now exit the program. Restart ATAK upon completion.\n\n"
                    "Please run the ATAK Imagery Downloader to install imagery on your device."
                )
            )
            self.btn_primary.configure(state="normal", text="Exit", command=self.destroy)
            self.protocol("WM_DELETE_WINDOW", self.destroy)
            self.status.configure(text="")

    def _on_primary(self) -> None:
        """Placeholder; _render_step sets the real Continue handler each step."""
        pass

    def _advance(self, n: int) -> None:
        logging.getLogger("atak_installer").info("_advance -> step %s", n)
        self._step = n
        self._render_step()

    def _step_confirm_choice(self) -> None:
        self._install_choice = self._choice_var.get()
        self._advance(2)

    def _step_connect_check(self) -> None:
        _lg = logging.getLogger("atak_installer")
        need_atak = self._install_choice in ("atak", "both")
        _lg.info(
            "_step_connect_check: choice=%s need_atak=%s atak_configured=%s",
            self._install_choice,
            need_atak,
            self._atak_install_ready(),
        )
        if need_atak and not self._atak_install_ready():
            self._focus_for_dialog()
            messagebox.showerror(
                APP_TITLE,
                "ATAK install links are not configured yet.\n\n"
                f"Edit this file with a text editor (see deploy.env.example in the same folder):\n"
                f"{DEPLOY_ENV_PATH}\n\n"
                "Set either ATAK_CIV_APK_URL + ATAK_CIV_VERSION, or ATAK_DEPLOY_MANIFEST_URL.\n"
                "Then start this installer again.",
                parent=self,
            )
            return

        if not adb_available():
            self._focus_for_dialog()
            messagebox.showerror(
                APP_TITLE,
                "adb was not found. Install Android platform tools (adb) and ensure it is on PATH.",
                parent=self,
            )
            return

        devices = list_usb_devices()
        serial = pick_serial(devices)
        if serial is None and len(devices) > 1:
            serial = self._ask_serial_choice(devices)
        if not serial:
            detail = adb_devices_human_summary()
            if len(detail) > 2400:
                detail = detail[:2400] + "\n…"
            self._focus_for_dialog()
            messagebox.showwarning(
                APP_TITLE,
                "No Android device in the *device* state (ready for adb).\n\n"
                "If the phone shows “unauthorized”, unlock it and accept the USB debugging "
                "prompt. If you see “no permissions”, install udev rules for adb.\n\n"
                f"{detail}",
                parent=self,
            )
            return

        self.selected_serial = serial
        os.environ["ANDROID_SERIAL"] = serial
        _lg.info("Device selected serial=%s (count=%s)", serial, len(devices))
        # Skip ATAK install steps when plugin-only
        if self._install_choice == "plugin":
            self._advance(5)
        else:
            self._advance(3)

    def _ask_serial_choice(self, devices: List[str]) -> Optional[str]:
        top = tk.Toplevel(self)
        top.title("Select device")
        top.configure(cursor="arrow")
        top.transient(self)
        top.grab_set()
        choice: List[Optional[str]] = [None]

        tk.Label(top, text="Multiple devices connected. Pick one:").pack(padx=12, pady=(12, 6))
        lb = tk.Listbox(top, height=min(len(devices), 8), width=40)
        for d in devices:
            lb.insert("end", d)
        lb.pack(padx=12, pady=6)
        lb.selection_set(0)

        def ok() -> None:
            sel = lb.curselection()
            if sel:
                choice[0] = devices[int(sel[0])]
            top.destroy()

        def cancel() -> None:
            top.destroy()

        bf = tk.Frame(top)
        bf.pack(pady=12)
        tk.Button(bf, text="OK", command=ok).pack(side="left", padx=6)
        tk.Button(bf, text="Cancel", command=cancel).pack(side="left", padx=6)
        top.update_idletasks()
        if ensure_window_stacking is not None:
            ensure_window_stacking(top, above=self)
        self.wait_window(top)
        return choice[0]

    def _cleanup_temp_apks(self) -> None:
        for p in (self._atak_apk_temp, self._plugin_apk_temp):
            if p and p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass

    def _begin_install_atak(self) -> None:
        err: Optional[Exception] = None
        try:
            base = self._resolve_url_base()
            if self._inline_atak_manifest:
                manifest = self._inline_atak_manifest
            else:
                manifest = fetch_manifest(self.manifest_url)
            self._manifest_cache = manifest
            apk_path, version, is_temp = resolve_atak_apk(manifest, base)
            self._atak_apk_temp = apk_path if is_temp else None
            self._atak_version_value = str(version)

            def ui_install(msg: str) -> None:
                self.status.configure(text=msg)
                self.update_idletasks()

            self.progress.start(8)
            self.update_idletasks()
            install_apk(self.selected_serial, apk_path, ui_install)
            push_mobile_xml(self.selected_serial or "", log)
            if self._install_choice == "atak":
                install_bundled_addon_apks(self.selected_serial or "", log, ui_install)

            if is_temp:
                try:
                    apk_path.unlink(missing_ok=True)
                except Exception:
                    pass
            self._atak_apk_temp = None
            if self.report_url:
                safe_post_report(
                    self.report_url,
                    env_optional("ATAK_DEPLOY_API_TOKEN") or None,
                    self._atak_version_value,
                    "",
                    self.selected_serial or "",
                    "atak_installed",
                )
        except Exception as e:
            err = e
            log(traceback.format_exc())
        finally:
            try:
                self.progress.stop()
            except Exception:
                pass
            self._after_install_atak(err)

    def _after_install_atak(self, err: Optional[Exception]) -> None:
        if err:
            self.progress.stop()
            self._step = 2
            self._cleanup_temp_apks()
            self._focus_for_dialog()
            messagebox.showerror(APP_TITLE, f"Could not install ATAK:\n{err}", parent=self)
            self._render_step()
            return
        try:
            launch_atak(self.selected_serial or "")
        except Exception:
            log("launch_atak after ATAK install failed (user can open ATAK manually)")
        self._advance(4)

    def _begin_install_plugin(self) -> None:
        ser = self.selected_serial or ""
        err: Optional[Exception] = None
        try:
            def ui_install(msg: str) -> None:
                self.status.configure(text=msg)
                self.update_idletasks()

            self.progress.start(8)
            self.update_idletasks()

            manifest = self._manifest_cache
            if manifest is None:
                if self.manifest_url:
                    manifest = fetch_manifest(self.manifest_url)
                elif self._inline_atak_manifest:
                    manifest = dict(self._inline_atak_manifest)
                else:
                    raise RuntimeError("No ATAK deploy configuration")
            apk_path, report_label, is_temp = resolve_plugin_apk(manifest, self._resolve_url_base())
            self._plugin_apk_temp = apk_path if is_temp else None
            self._plugin_report_label = report_label

            if self._install_choice == "both":
                # Keep ATAK closed while installing bundled add-ons so only UV-PRO
                # triggers the in-app "load plugin" notification.
                try:
                    run_adb(["shell", "am", "force-stop", atak_package_name()], serial=ser or None, timeout=30)
                except Exception:
                    log("force-stop ATAK before bundled add-on install failed")
                install_bundled_addon_apks(ser, log, ui_install)

            if self._install_choice == "plugin":
                # Plugin-only flow: force a clean slate before install to avoid
                # version/signature mismatch from prior UV-PRO installs.
                uninstall_package(ser, DEFAULT_PLUGIN_PACKAGE, ui_install, require_absent=True)

            try:
                launch_atak(ser)
                time.sleep(15.0)
            except Exception:
                log("launch_atak before UV-PRO install failed")

            install_apk(
                self.selected_serial,
                apk_path,
                ui_install,
                package_name=DEFAULT_PLUGIN_PACKAGE,
            )

            if self.report_url:
                safe_post_report(
                    self.report_url,
                    env_optional("ATAK_DEPLOY_API_TOKEN") or None,
                    self._atak_version_value,
                    self._plugin_report_label,
                    self.selected_serial or "",
                    "complete",
                )
        except Exception as e:
            err = e
            log(traceback.format_exc())
        finally:
            try:
                self.progress.stop()
            except Exception:
                pass
            self._after_install_plugin(err)

    def _after_install_plugin(self, err: Optional[Exception]) -> None:
        self.progress.stop()
        if err:
            self._cleanup_temp_apks()
            self._focus_for_dialog()
            messagebox.showerror(APP_TITLE, f"Could not install plugin:\n{err}", parent=self)
            # Go back to ATAK setup step if we did both, or connect step if plugin-only
            self._step = 4 if self._install_choice == "both" else 2
            self._render_step()
            return
        if self._install_choice == "plugin":
            try:
                launch_atak(self.selected_serial or "")
            except Exception:
                log("launch_atak after plugin-only install failed")
        self._cleanup_temp_apks()
        self._advance(6)

def main() -> None:
    if tk is None or scrolledtext is None:
        print("tkinter is required for this wizard.", file=sys.stderr)
        sys.exit(1)
    log_path = setup_installer_logging()
    _install_exception_hooks()
    try:
        print(f"Installer log: {log_path}", file=sys.stderr)
        sys.stderr.flush()
    except Exception:
        pass
    if run_startup_git_update_check is not None:
        run_startup_git_update_check(app_title=APP_TITLE, script_path=Path(__file__).resolve())
    if run_startup_release_update_check is not None:
        run_startup_release_update_check(app_title=APP_TITLE, script_path=Path(__file__).resolve())
    ensure_gui_path_for_adb()
    load_deploy_env_file()
    log_startup_context()
    w = DeployWizard()
    logging.getLogger("atak_installer").info("Starting Tk mainloop")
    w.mainloop()
    logging.getLogger("atak_installer").info("Tk mainloop ended normally")


if __name__ == "__main__":
    main()
