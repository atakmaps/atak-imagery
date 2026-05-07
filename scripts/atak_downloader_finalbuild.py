#!/usr/bin/env python3
"""
ATAK USGS Orthophoto Downloader (shared core)

**Two entry points** (do not fold into one script — different first/last UI):

- **Standalone Imagery app:** run ``atak_downloader_finalbuild.py`` / desktop launcher.
  Full intro (ATAK + USB/adb), session Exit dialog, then SQLite builder.
- **After Device Installer:** run ``atak_downloader_from_installer.py`` only (set by
  ``atak_adb_deploy``). Skips the standalone USB/adb intro; same download, SQLite handoff dialog, and builder chain.

- Download scope first (entire state(s) vs fixed radius) and optional raw tile tree
- State selection or radius center second
- Zoom selection third
- Zoom screen: storage estimates, background USGS throughput probe, ETA vs selection
- Summary confirmation before output folder picker
- Zenity folder picker on Linux with Tk fallback
- Progress bar during download
- Safe re-run: skips tiles that already exist
- After download: pushes bundled map/import files (`data/mobile_xml/`) and installs bundled add-on plugin APKs (`data/bundled_plugins/`) when adb serial is set (same payloads as the Device Installer).

Output structure:
    <selected parent>/Imagery/<state or radius name>/zoom/x/y.jpg
    Radius name is chosen on the download-scope screen; each name gets its own folder and SQLite file.
"""

import json
import math
import multiprocessing
import re
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import warnings

# Suppress the benign "leaked semaphore" warning from Python's resource_tracker
# subprocess on hard exit (os._exit). The env var propagates to child processes;
# warnings.filterwarnings covers the main process.
import os as _os
_os.environ.setdefault("PYTHONWARNINGS", "ignore::UserWarning:multiprocessing.resource_tracker")
warnings.filterwarnings(
    "ignore",
    message="resource_tracker.*leaked semaphore",
    category=UserWarning,
)
import traceback
from collections import deque
from concurrent.futures import FIRST_COMPLETED, CancelledError, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Set, Tuple, Optional

import requests
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from git_update_check import run_startup_git_update_check, run_startup_release_update_check
from tk_window_scaling import (
    apply_fixed_size_window,
    apply_resizable_window,
    ensure_window_stacking,
    pack_vertical_scroll_area,
    pack_vertical_scroll_area_when_needed,
    refit_toplevel_geometry,
    scale_factor,
    scaled_gap_px,
    scaled_int,
)

APP_TITLE = "ATAK Imagery Downloader"

# Set to "1" by scripts/atak_downloader_from_installer.py (Device Installer post-step only).
LAUNCHED_FROM_DEVICE_INSTALLER_ENV = "ATAK_DOWNLOADER_LAUNCHED_FROM_DEVICE_INSTALLER"


def is_launched_from_device_installer() -> bool:
    return os.environ.get(LAUNCHED_FROM_DEVICE_INSTALLER_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


class DownloadCancelled(Exception):
    """Raised when the user stops the download from the progress window."""


USGS_TILE_URL = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}"
USER_AGENT = "ATAK-Ortho-Downloader/1.1"
USGS_MAPSERVER_BASE_URL = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer"
DTED_SERVER_BASE_URL = "http://31.220.30.74/dted"
OFFLINE_MISSING_DATA_MSG = "Internet unreachable to download missing data."
# Parallel HTTP tile fetches (thread pool). Network-bound — not the same as CPU cores;
# imagery_tile_selection uses processes for the heavy *tile plan* bbox scan on large states.
MAX_DOWNLOAD_WORKERS = min(48, max(8, (os.cpu_count() or 4) * 4))
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    SCRIPT_DIR = Path(sys._MEIPASS) / "scripts"
    # Writable, persistent location for .last_imagery_root.txt (MEIPASS itself is temporary).
    RUNTIME_STATE_DIR = Path(sys.executable).resolve().parent
else:
    SCRIPT_DIR = Path(__file__).resolve().parent
    RUNTIME_STATE_DIR = SCRIPT_DIR

DATA_DIR = SCRIPT_DIR / "data"
ZOOM_ESTIMATE_PATH = DATA_DIR / "zoom_estimates_z10_z16.json"
STATE_GEOJSON_PATH = DATA_DIR / "us_states.geojson"
TILE_PLAN_DIR = DATA_DIR / "tile_plans" / "v1"
MOBILE_ASSET_DIR = DATA_DIR / "mobile_xml"
BUNDLED_PLUGIN_DIR = DATA_DIR / "bundled_plugins"
MOBILE_XML_DEVICE_PATH = "/sdcard/atak/imagery/mobile/mapsources"
MOBILE_IMPORT_DEVICE_PATH = "/sdcard/atak/tools/import"
LAST_IMAGERY_ROOT_FILE = RUNTIME_STATE_DIR / ".last_imagery_root.txt"
# Written after each successful imagery download: state folder names (for SQLite builder filter).
LAST_IMAGERY_SESSION_STATES_FILE = RUNTIME_STATE_DIR / ".last_imagery_session_states.txt"

from imagery_tile_selection import (  # noqa: E402
    STATE_BOUNDARY_BUFFER_MILES,
    RADIUS_REGION_FOLDER,
    build_tiles_for_state_result,
    compute_tiles_for_radius,
    lonlat_to_tile,
    square_lonlat_footprint_for_radius_miles,
    state_names_intersecting_geodesic_circle,
)


def sanitize_radius_imagery_folder_name(raw: str) -> str:
    """
    Sanitize the user-entered radius name for Imagery/<name>/ tiles and for
    ATAK_SQL_<name>.sqlite (same character rules as atak_imagery_sqlite_builder_finalbuild).
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", raw.strip())
    if not cleaned or cleaned in (".", ".."):
        raise ValueError(
            "Enter a non-empty name using letters, numbers, spaces, or . _ -\n"
            "(other characters become underscores)."
        )
    return cleaned


def _shutdown_executor_pool(executor: ThreadPoolExecutor) -> None:
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except TypeError:
        executor.shutdown(wait=False)


# -----------------------------
# Logging
# -----------------------------

class Logger:
    def __init__(self) -> None:
        self.script_dir = Path(__file__).resolve().parent
        self.log_dir = self.script_dir / "logs"
        self.log_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = self.log_dir / f"atak_downloader_{ts}.log"
        self._fh = open(self.log_file, "a", encoding="utf-8", buffering=1)
        self.gui_queue: "queue.Queue[str]" = queue.Queue()

    def write(self, message: str) -> None:
        if not message.endswith("\n"):
            message += "\n"
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {message}"
        try:
            sys.__stdout__.write(line)
            sys.__stdout__.flush()
        except Exception:
            pass
        try:
            self._fh.write(line)
            self._fh.flush()
        except Exception:
            pass
        try:
            self.gui_queue.put_nowait(line)
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


LOGGER = Logger()


def log(msg: str) -> None:
    LOGGER.write(msg)


def install_excepthook() -> None:
    def handle_exception(exc_type, exc_value, exc_tb):
        log("FATAL: Unhandled exception")
        tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log(tb)
        try:
            messagebox.showerror(APP_TITLE, f"Unhandled exception.\n\nLog file:\n{LOGGER.log_file}")
        except Exception:
            pass
    sys.excepthook = handle_exception


install_excepthook()

# -----------------------------
# Android / adb (aligned with atak_adb_deploy device step)
# -----------------------------


def _adb_executable() -> str:
    return shutil.which("adb") or "adb"


def _run_adb(args: List[str], serial: Optional[str] = None, timeout: int = 120) -> subprocess.CompletedProcess:
    cmd = [_adb_executable()]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def adb_available() -> bool:
    try:
        r = subprocess.run(
            [_adb_executable(), "version"], capture_output=True, text=True, timeout=10
        )
        return r.returncode == 0
    except Exception:
        return False


def adb_devices_raw() -> subprocess.CompletedProcess:
    _run_adb(["start-server"], serial=None, timeout=30)
    return _run_adb(["devices"], serial=None, timeout=30)


def parse_adb_devices_lines(stdout: str) -> Tuple[List[str], List[str]]:
    ready: List[str] = []
    diag: List[str] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        if line.startswith("*"):
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
    exe = _adb_executable()
    r = adb_devices_raw()
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    lines = [f"adb binary: {exe}", "", "$ adb devices", out or "(no stdout)"]
    if err:
        lines.extend(["", "stderr:", err])
    return "\n".join(lines)


def pick_adb_serial(devices: List[str]) -> Optional[str]:
    if not devices:
        return None
    if len(devices) == 1:
        return devices[0]
    pref = os.environ.get("ANDROID_SERIAL", "").strip()
    if pref and pref in devices:
        return pref
    return None


def ask_adb_serial_choice(parent: tk.Tk, devices: List[str]) -> Optional[str]:
    top = tk.Toplevel(parent)
    top.title("Select device")
    top.configure(cursor="arrow")
    top.transient(parent)
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
    ensure_window_stacking(top, above=parent)
    parent.wait_window(top)
    return choice[0]


def check_device_ready_and_unlocked(serial: Optional[str]) -> Tuple[bool, str]:
    r = _run_adb(["shell", "getprop", "sys.boot_completed"], serial=serial, timeout=25)
    if r.returncode != 0:
        return False, (
            "Could not communicate with the device over adb.\n\n"
            "Check the USB cable, enable USB debugging, and accept the prompt on the phone."
        )
    if (r.stdout or "").strip() != "1":
        return False, "Wait until the device has finished booting to the home screen, then try again."

    r2 = _run_adb(["shell", "dumpsys", "window"], serial=serial, timeout=45)
    out = r2.stdout or ""
    if "mDreamingLockscreen=true" in out:
        return False, "Unlock your phone (dismiss the lock screen) and try again."
    return True, ""


def push_mobile_assets(log_fn=None) -> None:
    """Push bundled mobile map/waypoint files to the device (additive — never deletes device files).

    Routing by extension (scripts/data/mobile_xml/ — rsync from
    /home/paul/Documents/ATAK/Plugins/Add Ons for Build/ before every build; see HANDOFF):
      .xml        → MOBILE_XML_DEVICE_PATH  (/sdcard/atak/imagery/mobile/mapsources)
      .kmz / .zip → MOBILE_IMPORT_DEVICE_PATH (/sdcard/atak/tools/import)
    """
    if not MOBILE_ASSET_DIR.is_dir():
        return

    serial: Optional[str] = os.environ.get("ANDROID_SERIAL") or None

    xml_files = sorted(MOBILE_ASSET_DIR.rglob("*.xml"))
    import_files = sorted(MOBILE_ASSET_DIR.rglob("*.kmz")) + sorted(MOBILE_ASSET_DIR.rglob("*.zip"))
    dest_map: dict = {
        MOBILE_XML_DEVICE_PATH: xml_files,
        MOBILE_IMPORT_DEVICE_PATH: import_files,
    }

    total = sum(len(v) for v in dest_map.values())
    if total == 0:
        return

    for device_path, files in dest_map.items():
        if not files:
            continue
        _run_adb(["shell", "mkdir", "-p", device_path], serial=serial, timeout=30)
        for f in files:
            rel = f.relative_to(MOBILE_ASSET_DIR)
            if log_fn:
                log_fn(f"Pushing {rel} to device…")
            r = _run_adb(["push", str(f), f"{device_path}/{f.name}"], serial=serial, timeout=120)
            if r.returncode != 0 and log_fn:
                log_fn(f"Warning: failed to push {f.name}: {r.stderr}")

    if log_fn:
        log_fn(f"Mobile assets pushed to device ({total} files).")


def verify_adb_device_for_imagery_downloader(parent: tk.Tk) -> Tuple[bool, Optional[str], str]:
    if not adb_available():
        return False, None, (
            "adb was not found. Install Android platform tools (adb) and ensure it is on PATH."
        )

    devices = list_usb_devices()
    if not devices:
        detail = adb_devices_human_summary()
        if len(detail) > 2400:
            detail = detail[:2400] + "\n…"
        return False, None, (
            "No Android device in the *device* state (ready for adb).\n\n"
            "If the phone shows “unauthorized”, unlock it and accept the USB debugging "
            "prompt. If you see “no permissions”, install udev rules for adb.\n\n"
            f"{detail}"
        )

    serial = pick_adb_serial(devices)
    if serial is None and len(devices) > 1:
        serial = ask_adb_serial_choice(parent, devices)
    if not serial:
        return False, None, "No device was selected."

    ready, msg = check_device_ready_and_unlocked(serial)
    if not ready:
        return False, None, msg

    os.environ["ANDROID_SERIAL"] = serial
    return True, serial, ""


DOWNLOADER_INTRO_TEXT = (
    "This program will download imagery to your device. "
    "You must have ATAK installed on your device. "
    "If you do not have ATAK installed, exit this program and run the "
    "ATAK Device Installer application.\n\n"
    "\n\n"
    "1. On the phone, enable Developer options and USB debugging.\n"
    "2. Connect USB\n"
    "3. Select USB Mode, File Transfer\n\n"
    "Select Continue when your device is connected."
)


def show_downloader_intro_and_verify_device() -> bool:
    """ATAK + USB prerequisites, then adb device / unlock check. Return True to proceed."""
    root = tk.Tk()
    root.title(APP_TITLE)
    root.configure(cursor="arrow")
    root.update_idletasks()
    s = scale_factor(root)
    proceed = {"ok": False}

    tk.Label(root, text="Before you begin", font=("TkDefaultFont", 12, "bold")).pack(anchor="w", padx=16, pady=(16, 6))

    intro_lbl = tk.Label(
        root,
        text=DOWNLOADER_INTRO_TEXT,
        justify="left",
        wraplength=scaled_int(600, s),
    )
    intro_lbl.pack(anchor="w", padx=16, pady=(0, 8))

    status_var = tk.StringVar(value="")
    tk.Label(root, textvariable=status_var, fg="#333").pack(anchor="w", padx=16, pady=(4, 8))

    btn_row = tk.Frame(root)
    btn_row.pack(pady=12)

    def on_quit() -> None:
        proceed["ok"] = False
        root.destroy()

    btn_cont = tk.Button(btn_row, text="Continue", width=12)

    def on_continue() -> None:
        btn_cont.configure(state="disabled")
        status_var.set("Verifying device via adb…")
        root.update_idletasks()
        ok, _serial, err = verify_adb_device_for_imagery_downloader(root)
        btn_cont.configure(state="normal")
        status_var.set("")
        if not ok:
            ensure_window_stacking(root)
            messagebox.showwarning(APP_TITLE, err, parent=root)
            return
        proceed["ok"] = True
        root.destroy()

    btn_cont.configure(command=on_continue)
    btn_cont.pack(side="left", padx=6)
    tk.Button(btn_row, text="Quit", width=12, command=on_quit).pack(side="left", padx=6)

    apply_resizable_window(root, 640, 520, (480, 300))

    def _sync_intro_wrap(_evt: Optional[object] = None) -> None:
        root.update_idletasks()
        try:
            rw = int(root.winfo_width())
        except tk.TclError:
            return
        if rw < 64:
            return
        intro_lbl.configure(wraplength=max(120, min(scaled_int(600, s), rw - 48)))

    root.bind("<Configure>", lambda e: _sync_intro_wrap())
    refit_toplevel_geometry(root, 640, 520)
    _sync_intro_wrap()

    root.protocol("WM_DELETE_WINDOW", on_quit)
    root.mainloop()
    return bool(proceed["ok"])


DOWNLOADER_NEXT_SQLITE_DIALOG_TEXT = (
    "Imagery successfully downloaded.\n\n"
    "Next will be to build the data for install on your device.\n\n"
    "Click Next to continue."
)

CONNECT_DEVICE_BEFORE_DTED_TEXT = (
    "Imagery Download Complete.\n\n"
    "Please connect your device now and ensure you have USB debugging on. "
    "Then select USB for File Transfer.\n\n"
    "If you do not get this notification: Swipe down from top, expand the Android "
    "System Notification, tap 'Charging this device via USB' before you select USB "
    "File Transfer.\n\n"
    "Click OK when your device is connected and ready."
)


def show_downloader_session_exit_dialog(parent: tk.Tk, body: Optional[str] = None) -> None:
    """After imagery (and optional inline DTED), prompt user before launching the SQLite builder."""
    ensure_window_stacking(parent)
    text = body if body is not None else DOWNLOADER_NEXT_SQLITE_DIALOG_TEXT
    dlg = tk.Toplevel(parent)
    dlg.title(APP_TITLE)
    dlg.configure(cursor="arrow")
    dlg.transient(parent)
    dlg.grab_set()
    dlg.resizable(False, False)
    _exit_scale = apply_fixed_size_window(dlg, 520, 280)
    tk.Label(
        dlg,
        text=text,
        justify="center",
        wraplength=scaled_int(460, _exit_scale),
    ).pack(padx=24, pady=(20, 12))

    def on_next() -> None:
        dlg.destroy()

    dlg.protocol("WM_DELETE_WINDOW", on_next)
    tk.Button(dlg, text="Next", width=12, command=on_next).pack(pady=(0, 20))
    parent.wait_window(dlg)

# -----------------------------
# Helpers
# -----------------------------

def human_bytes(num_bytes: int) -> str:
    value = float(max(num_bytes, 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{int(num_bytes)} B"


def human_throughput(bps: float) -> str:
    if bps <= 0:
        return "--"
    mb = bps / (1024 * 1024)
    if mb >= 0.05:
        return f"{mb:.1f} MB/s"
    kb = bps / 1024
    return f"{kb:.0f} KB/s"


# Raw estimates size the full-resolution download; on-device ATAK imagery SQLite is typically
# much smaller than that working-set figure (single packaged DB vs loose tiles + padding).
DEVICE_INSTALL_BYTES_VS_RAW_ESTIMATE = 0.22


def estimate_device_sqlite_bytes(raw_tile_bytes_sum: int) -> int:
    """Approximate on-device imagery DB size vs bundled raw-download estimate (see ratio above)."""
    if raw_tile_bytes_sum <= 0:
        return 0
    return max(1, int(raw_tile_bytes_sum * DEVICE_INSTALL_BYTES_VS_RAW_ESTIMATE))


def read_zoom_estimates_file(path: Path = ZOOM_ESTIMATE_PATH) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"Missing zoom estimate file:\n{path}\n\n"
            f"Copy scripts/data/ into this fresh repo or generate it first."
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_zoom_estimates() -> Dict[str, Dict[str, Dict[str, int]]]:
    return read_zoom_estimates_file()["states"]


def avg_tile_bytes_by_zoom(payload: Dict[str, Any]) -> Dict[int, int]:
    raw = payload.get("avg_tile_size_bytes", {})
    out: Dict[int, int] = {}
    for k, v in raw.items():
        try:
            out[int(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def parse_mgrs_to_latlon(mgrs_str: str) -> Tuple[float, float]:
    import mgrs as _mgrs

    s = "".join(mgrs_str.strip().split())
    if len(s) < 3:
        raise ValueError("MGRS string is too short.")
    lat, lon = _mgrs.MGRS().toLatLon(s)
    return float(lat), float(lon)

# -----------------------------
# Tile math
# -----------------------------

def zoom_resolution_labels(z: int, latitude_deg: float = 39.0) -> str:
    equator_mpp = 156543.03392804097 / (2 ** z)
    local_mpp = equator_mpp * math.cos(math.radians(latitude_deg))
    local_ft = local_mpp * 3.28084
    return f"Zoom {z}  (~{equator_mpp:.2f} m/px equator, ~{local_ft:.1f} ft/px mid-US)"


def format_download_eta(seconds: float) -> str:
    if seconds <= 0 or math.isnan(seconds) or math.isinf(seconds):
        return "unknown"
    if seconds < 120:
        return f"about {max(1, int(seconds))} seconds"
    if seconds < 7200:
        return f"about {seconds / 60:.0f} minutes"
    hours = seconds / 3600.0
    if hours >= 72:
        return f"about {hours / 24:.1f} days"
    return f"about {hours:.1f} hours"


# -----------------------------
# State boundaries
# -----------------------------

STATE_ABBR_TO_NAME = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}


def bundled_state_geojson_path() -> Path:
    """Census 2010 state boundaries shipped under data/ (no network fetch)."""
    if not STATE_GEOJSON_PATH.is_file():
        raise FileNotFoundError(
            f"Missing state boundaries file:\n{STATE_GEOJSON_PATH}\n\n"
            f"Ensure scripts/data/us_states.geojson is present (same folder as zoom_estimates)."
        )
    log(f"Using bundled state boundaries: {STATE_GEOJSON_PATH}")
    return STATE_GEOJSON_PATH


def load_states(geojson_path: Path) -> Dict[str, List[List[Tuple[float, float]]]]:
    with open(geojson_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    states = {}
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        name = props.get("NAME") or props.get("NAME10") or props.get("STATE_NAME")
        geom = feature.get("geometry", {})
        gtype = geom.get("type")
        coords = geom.get("coordinates", [])

        rings: List[List[Tuple[float, float]]] = []
        if gtype == "Polygon":
            if coords:
                rings.append([(float(x), float(y)) for x, y in coords[0]])
        elif gtype == "MultiPolygon":
            for poly in coords:
                if poly:
                    rings.append([(float(x), float(y)) for x, y in poly[0]])

        if name and rings:
            states[name] = rings
    return states


# -----------------------------
# UI
# -----------------------------


class DownloadScopeDialog(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_TITLE} - Download scope")
        self.resizable(True, True)
        self.configure(cursor="arrow")

        self.accepted = False
        self.download_scope = "state"
        self.raw_imagery_path = ""
        self.local_dted_path = ""
        self.radius_region_folder = ""

        self.update_idletasks()
        _scope_scale = scale_factor(self)
        _scope_wrap = scaled_int(640, _scope_scale)
        _fmt_indent = scaled_gap_px(16, _scope_scale, lo=8, hi=36)
        _section_gap = scaled_gap_px(10, _scope_scale, lo=4, hi=20)
        _g2 = scaled_gap_px(12, _scope_scale, lo=8, hi=28)  # ~double line between sections

        outer = tk.Frame(self, padx=14, pady=14)
        outer.pack(fill="both", expand=True)

        btns = tk.Frame(outer)
        btns.pack(side="bottom", fill="x", pady=(_section_gap, 0))
        tk.Button(btns, text="Cancel", width=12, command=self.cancel).pack(side="right", padx=(6, 0))
        tk.Button(btns, text="OK", width=12, command=self.submit).pack(side="right")

        body = tk.Frame(outer)
        body.pack(fill="both", expand=True)

        form_top = tk.Frame(body)
        form_top.pack(fill="x")

        tk.Label(
            form_top,
            text="How should imagery be chosen?",
            font=("Arial", 11, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        self.scope_var = tk.StringVar(value="state")
        tk.Radiobutton(
            form_top,
            text="An entire state (or several states)",
            variable=self.scope_var,
            value="state",
            anchor="w",
            justify="left",
        ).pack(anchor="w")
        tk.Radiobutton(
            form_top,
            text="Fixed radius from a point (not limited to one state; good near borders)",
            variable=self.scope_var,
            value="radius",
            anchor="w",
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        radius_name_frame = tk.Frame(form_top)
        radius_name_frame.pack(fill="x", pady=(6, _g2))
        tk.Label(
            radius_name_frame,
            text=(
                "Radius download name — folder under Imagery/ and base name for ATAK_SQL_<name>.sqlite "
                "(required for radius; use a new name for each area so downloads do not overwrite):"
            ),
            font=("Arial", 10),
            justify="left",
            wraplength=_scope_wrap,
            anchor="w",
        ).pack(anchor="w")
        self.radius_name_var = tk.StringVar(value="")
        self.radius_name_entry = tk.Entry(radius_name_frame, textvariable=self.radius_name_var)
        self.radius_name_entry.pack(anchor="w", fill="x", pady=(4, 0))
        try:
            self.radius_name_entry.configure(disabledforeground="gray55")
        except tk.TclError:
            pass

        def _sync_radius_name_field(*_a: object) -> None:
            if self.scope_var.get() == "radius":
                self.radius_name_entry.configure(state="normal")
            else:
                self.radius_name_entry.configure(state="disabled")

        self.scope_var.trace_add("write", _sync_radius_name_field)
        _sync_radius_name_field()

        tk.Label(
            form_top,
            text=(
                "Advanced settings: Leave blank if you don't have locally installed imagery or elevation."
            ),
            font=("Arial", 11, "bold"),
            justify="left",
            wraplength=_scope_wrap,
            anchor="w",
        ).pack(anchor="w", pady=(0, _g2))

        scroll_host = tk.Frame(body)
        scroll_host.pack(fill="both", expand=True)
        wrapped_scroll_labels: List[tk.Label] = []

        def _add_wrapped(parent: tk.Misc, **kw: object) -> tk.Label:
            lb = tk.Label(parent, **kw)
            wrapped_scroll_labels.append(lb)
            return lb

        def _sync_scroll_wrap(_evt: Optional[object] = None) -> None:
            scroll_inner.update_idletasks()
            try:
                iw = int(scroll_inner.winfo_width())
            except tk.TclError:
                return
            if iw < 64:
                return
            wl = max(120, min(scaled_int(640, _scope_scale), iw - 16))
            for lb in wrapped_scroll_labels:
                try:
                    lb.configure(wraplength=wl)
                except tk.TclError:
                    pass

        scroll_inner = pack_vertical_scroll_area_when_needed(
            scroll_host, on_inner_layout=_sync_scroll_wrap
        )

        tk.Label(
            scroll_inner,
            text="Local Imagery Location",
            font=("Arial", 11, "bold"),
        ).pack(anchor="w", pady=(0, 4))
        _add_wrapped(
            scroll_inner,
            text="If you have USGS-style imagery on a local disk, choose the path below.",
            justify="left",
            wraplength=_scope_wrap,
            anchor="w",
        ).pack(anchor="w", pady=(0, 4))
        _add_wrapped(
            scroll_inner,
            text="NOTE: This imagery must be stored in this format:",
            justify="left",
            wraplength=_scope_wrap,
            anchor="w",
        ).pack(anchor="w", pady=(0, 2))
        tk.Label(
            scroll_inner,
            text="Region name/zoom/x/y.jpg",
            justify="left",
            anchor="w",
            font=("Courier", 10),
        ).pack(anchor="w", padx=(_fmt_indent, 0), pady=(0, 6))
        _add_wrapped(
            scroll_inner,
            text=(
                "Any required imagery not present on the local disk will be downloaded from USGS. "
                "Leave blank if you do not have the imagery required to build your selection."
            ),
            justify="left",
            wraplength=_scope_wrap,
            anchor="w",
        ).pack(anchor="w", pady=(0, 8))

        raw_row = tk.Frame(scroll_inner)
        raw_row.pack(fill="x", pady=(0, 0))
        raw_row.grid_columnconfigure(0, weight=1)
        self.raw_var = tk.StringVar(value="")
        raw_entry = tk.Entry(raw_row, textvariable=self.raw_var)
        raw_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        def browse_raw() -> None:
            initial = Path(self.raw_var.get().strip() or str(Path.home()))
            folder = pick_directory("Raw imagery root (optional)", initial, self)
            if folder:
                self.raw_var.set(folder)

        tk.Button(raw_row, text="Browse…", command=browse_raw).grid(row=0, column=1, sticky="e")

        tk.Label(
            scroll_inner,
            text="Local Elevation Location",
            font=("Arial", 11, "bold"),
        ).pack(anchor="w", pady=(_g2, 4))
        _add_wrapped(
            scroll_inner,
            text="If you have elevation data (DTED) on a local disk, choose the path below.",
            justify="left",
            wraplength=_scope_wrap,
            anchor="w",
        ).pack(anchor="w", pady=(0, 4))
        _add_wrapped(
            scroll_inner,
            text="Note: This data must be stored in this format: State name/State name.zip",
            justify="left",
            wraplength=_scope_wrap,
            anchor="w",
        ).pack(anchor="w", pady=(0, 6))
        _add_wrapped(
            scroll_inner,
            text=(
                "Any required elevation not present on the local disk will be downloaded. "
                "Leave blank if you do not have the elevation data required to build your selection."
            ),
            justify="left",
            wraplength=_scope_wrap,
            anchor="w",
        ).pack(anchor="w", pady=(0, 12))

        dted_row = tk.Frame(scroll_inner)
        dted_row.pack(fill="x", pady=(0, 0))
        dted_row.grid_columnconfigure(0, weight=1)
        self.dted_var = tk.StringVar(value="")
        tk.Entry(dted_row, textvariable=self.dted_var).grid(row=0, column=0, sticky="ew", padx=(0, 8))

        def browse_dted() -> None:
            initial = Path(self.dted_var.get().strip() or str(Path.home()))
            folder = pick_directory("Local DTED state zips (optional)", initial, self)
            if folder:
                self.dted_var.set(folder)

        tk.Button(dted_row, text="Browse…", command=browse_dted).grid(row=0, column=1, sticky="e")

        apply_resizable_window(self, 740, 780, (560, 300))

        self.bind("<Configure>", lambda e: _sync_scroll_wrap())
        refit_toplevel_geometry(self, 740, 780)
        _sync_scroll_wrap()

        self.protocol("WM_DELETE_WINDOW", self.cancel)

    def submit(self) -> None:
        self.download_scope = self.scope_var.get()
        if self.download_scope == "radius":
            raw_name = self.radius_name_var.get().strip()
            if not raw_name:
                ensure_window_stacking(self)
                messagebox.showwarning(
                    APP_TITLE,
                    "You must enter a name for your radius.",
                    parent=self,
                )
                return
            try:
                self.radius_region_folder = sanitize_radius_imagery_folder_name(raw_name)
            except ValueError as exc:
                ensure_window_stacking(self)
                messagebox.showwarning(APP_TITLE, str(exc), parent=self)
                return
        else:
            self.radius_region_folder = ""
        self.raw_imagery_path = self.raw_var.get().strip()
        self.local_dted_path = self.dted_var.get().strip()
        self.accepted = True
        self.destroy()

    def cancel(self) -> None:
        self.accepted = False
        self.destroy()


class RadiusCenterDialog(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_TITLE} - Radius center")
        self.resizable(False, False)
        self.configure(cursor="arrow")

        self.accepted = False
        self.center_lat = 0.0
        self.center_lon = 0.0
        self.radius_miles = 0.0

        frame = tk.Frame(self, padx=14, pady=14)
        frame.pack(fill="both", expand=True)
        _rc_scale = apply_fixed_size_window(self, 580, 560)
        _rc_wrap = scaled_int(520, _rc_scale)

        instr = (
            "Center point — use MGRS from ATAK (default), or decimal latitude / longitude.\n\n"
            "In ATAK: open your waypoint, tap to select it, then read MGRS from the upper-right readout.\n"
            "Or enter decimal degrees (e.g. lat 39.8283, lon -98.5795). "
            "Radius is always in statute miles."
        )
        tk.Label(frame, text=instr, justify="left", wraplength=_rc_wrap).pack(anchor="w", pady=(0, 10))

        self.coord_var = tk.StringVar(value="mgrs")

        modes = tk.Frame(frame)
        modes.pack(anchor="w")
        tk.Radiobutton(modes, text="MGRS", variable=self.coord_var, value="mgrs").pack(side="left")
        tk.Radiobutton(modes, text="Decimal lat / lon", variable=self.coord_var, value="latlon").pack(
            side="left", padx=(12, 0)
        )

        self.mgrs_var = tk.StringVar(value="")
        self.lat_var = tk.StringVar(value="")
        self.lon_var = tk.StringVar(value="")

        tk.Label(frame, text="MGRS").pack(anchor="w", pady=(10, 2))
        self.e_mgrs = tk.Entry(frame, textvariable=self.mgrs_var, width=48)
        self.e_mgrs.pack(anchor="w")

        tk.Label(frame, text="Latitude (decimal degrees, north positive)").pack(anchor="w", pady=(8, 2))
        self.e_lat = tk.Entry(frame, textvariable=self.lat_var, width=24)
        self.e_lat.pack(anchor="w")

        tk.Label(frame, text="Longitude (decimal degrees, east positive; use negative for west)").pack(
            anchor="w", pady=(8, 2)
        )
        self.e_lon = tk.Entry(frame, textvariable=self.lon_var, width=24)
        self.e_lon.pack(anchor="w")

        tk.Label(frame, text="Radius (miles)").pack(anchor="w", pady=(10, 2))
        self.radius_var = tk.StringVar(value="25")
        tk.Entry(frame, textvariable=self.radius_var, width=12).pack(anchor="w")

        def refresh_coord_state(*_a: object) -> None:
            use_mgrs = self.coord_var.get() == "mgrs"
            for w in (self.e_mgrs,):
                w.configure(state="normal" if use_mgrs else "disabled")
            for w in (self.e_lat, self.e_lon):
                w.configure(state="disabled" if use_mgrs else "normal")

        self.coord_var.trace_add("write", refresh_coord_state)
        refresh_coord_state()

        btns = tk.Frame(frame)
        btns.pack(fill="x", pady=(16, 0))
        tk.Button(btns, text="Cancel", width=12, command=self.cancel).pack(side="right", padx=(6, 0))
        tk.Button(btns, text="OK", width=12, command=self.submit).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self.cancel)

    def submit(self) -> None:
        try:
            miles = float(self.radius_var.get().strip())
        except ValueError:
            ensure_window_stacking(self)
            messagebox.showwarning(APP_TITLE, "Enter a numeric radius in miles.", parent=self)
            return
        if miles <= 0:
            ensure_window_stacking(self)
            messagebox.showwarning(APP_TITLE, "Radius must be greater than zero.", parent=self)
            return

        if self.coord_var.get() == "mgrs":
            mgrs_s = self.mgrs_var.get().strip()
            if not mgrs_s:
                ensure_window_stacking(self)
                messagebox.showwarning(APP_TITLE, "Enter an MGRS coordinate.", parent=self)
                return
            try:
                lat, lon = parse_mgrs_to_latlon(mgrs_s)
            except Exception as exc:
                ensure_window_stacking(self)
                messagebox.showwarning(APP_TITLE, f"Could not parse MGRS:\n{exc}", parent=self)
                return
        else:
            try:
                lat = float(self.lat_var.get().strip())
                lon = float(self.lon_var.get().strip())
            except ValueError:
                ensure_window_stacking(self)
                messagebox.showwarning(
                    APP_TITLE, "Enter numeric latitude and longitude in decimal degrees.", parent=self
                )
                return
            if lat < -90 or lat > 90 or lon < -180 or lon > 180:
                ensure_window_stacking(self)
                messagebox.showwarning(APP_TITLE, "Latitude or longitude is out of range.", parent=self)
                return

        self.center_lat = lat
        self.center_lon = lon
        self.radius_miles = miles
        self.accepted = True
        self.destroy()

    def cancel(self) -> None:
        self.accepted = False
        self.destroy()


class StateSelectionDialog(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_TITLE} - Select States")
        self.resizable(True, True)
        self.configure(cursor="arrow")
        self.update_idletasks()
        _st_scale = scale_factor(self)

        self.result_mode = ""
        self.result_states: List[str] = []

        frame = tk.Frame(self, padx=12, pady=12)
        frame.pack(fill="both", expand=True)

        tk.Label(
            frame,
            text="Select imagery state(s) to download:",
            font=("Arial", 11, "bold")
        ).pack(anchor="w", pady=(0, 8))

        note = (
            "Choose one or more specific states, or use Select All.\n"
            "The downloader will fetch imagery for every selected state."
        )
        note_lbl = tk.Label(
            frame,
            text=note,
            justify="left",
            wraplength=scaled_int(560, _st_scale),
        )
        note_lbl.pack(anchor="w", pady=(0, 10))

        list_frame = tk.Frame(frame)
        list_frame.pack(fill="both", expand=True)

        inner = pack_vertical_scroll_area(list_frame)

        self.vars: Dict[str, tk.BooleanVar] = {}
        for state_name in sorted(STATE_ABBR_TO_NAME.values()):
            var = tk.BooleanVar(value=False)
            self.vars[state_name] = var
            cb = tk.Checkbutton(inner, text=state_name, variable=var, anchor="w", justify="left")
            cb.pack(anchor="w")

        btns = tk.Frame(frame)
        btns.pack(fill="x", pady=(12, 0))
        tk.Button(btns, text="Select All", width=12, command=self.select_all).pack(side="left")
        tk.Button(btns, text="Cancel", width=12, command=self.cancel).pack(side="right", padx=(6, 0))
        tk.Button(btns, text="OK", width=12, command=self.submit).pack(side="right")

        apply_resizable_window(self, 620, 700, (400, 280))

        def _sync_note_wrap(_evt: Optional[object] = None) -> None:
            self.update_idletasks()
            try:
                fw = int(frame.winfo_width())
            except tk.TclError:
                return
            if fw < 64:
                return
            note_lbl.configure(wraplength=max(120, min(scaled_int(560, _st_scale), fw - 24)))

        self.bind("<Configure>", lambda e: _sync_note_wrap())
        refit_toplevel_geometry(self, 620, 700)
        _sync_note_wrap()

        self.protocol("WM_DELETE_WINDOW", self.cancel)

    def select_all(self) -> None:
        self.result_mode = "all"
        for var in self.vars.values():
            var.set(True)

    def submit(self) -> None:
        selected = sorted([state for state, var in self.vars.items() if var.get()])
        if not selected:
            ensure_window_stacking(self)
            messagebox.showwarning(APP_TITLE, "Select at least one state.", parent=self)
            return
        with_imagery = [s for s in selected if s != "District of Columbia"]
        if not with_imagery:
            ensure_window_stacking(self)
            messagebox.showwarning(
                APP_TITLE,
                "District of Columbia cannot be used alone for this download.\n\n"
                "USGS imagery here follows full state boundaries; Washington D.C. is omitted from that set.\n"
                "Select at least one state (e.g. Maryland or Delaware). Note: “District of Columbia” is "
                "listed right under Delaware.",
                parent=self,
            )
            return
        self.result_states = selected
        if self.result_mode != "all":
            self.result_mode = "specific"
        self.destroy()

    def cancel(self) -> None:
        self.result_mode = ""
        self.result_states = []
        self.destroy()


class ZoomDialog(tk.Tk):
    def __init__(
        self,
        selected_states: List[str],
        zoom_estimates: Dict[str, Dict[str, Dict[str, int]]],
        *,
        download_scope: str = "state",
        radius_center: Optional[Tuple[float, float]] = None,
        radius_miles: Optional[float] = None,
        radius_imagery_folder: Optional[str] = None,
        avg_tile_bytes_by_zoom: Optional[Dict[int, int]] = None,
        precomputed_radius_tiles: Optional[Dict[int, int]] = None,
    ) -> None:
        super().__init__()
        # Hide until layout + radius tile counts finish; otherwise a blank Tk flashes for seconds
        # (radius mode runs compute_tiles_for_radius synchronously for each zoom).
        self.withdraw()
        self.title(f"{APP_TITLE} - Select Zoom Levels")
        self.resizable(True, True)
        self.configure(cursor="arrow")
        self.update_idletasks()
        _zs = scale_factor(self)

        self.download_scope = download_scope

        self.result: List[int] = []
        self.go_back = False
        self.zoom_total_bytes: Dict[int, int] = {}
        self.zoom_total_tiles: Dict[int, int] = {}
        self.vars: Dict[int, tk.BooleanVar] = {}
        self._probe_finished = False
        self._download_throughput_bps: Optional[float] = None

        frame = tk.Frame(self, padx=28, pady=20)
        frame.pack(fill="both", expand=True)

        btns = tk.Frame(frame)
        btns.pack(side="bottom", fill="x", pady=(16, 4), padx=(4, 4))
        tk.Button(btns, text="Back", width=12, command=self.back).pack(side="left", padx=(0, 6))
        tk.Button(btns, text="Select All", width=12, command=self.select_all).pack(side="left", padx=(0, 6))
        tk.Button(btns, text="Clear All", width=12, command=self.clear_all).pack(side="left", padx=(0, 6))
        tk.Button(btns, text="Cancel", width=12, command=self.cancel).pack(side="right", padx=(6, 0))
        tk.Button(btns, text="OK", width=12, command=self.submit).pack(side="right")

        body = tk.Frame(frame)
        body.pack(fill="both", expand=True)

        note_wrap = scaled_int(940, _zs)

        if download_scope == "radius" and radius_center is not None and radius_miles is not None:
            rfolder = radius_imagery_folder or RADIUS_REGION_FOLDER
            header = "Radius download — full tiles that intersect the circle:"
            sub = (
                f"Center ({radius_center[0]:.5f}, {radius_center[1]:.5f}) decimal deg | "
                f"radius {radius_miles:g} mi | folder Imagery/{rfolder}/…"
            )
        else:
            header = "Selected states to be installed:"
            sub = ", ".join(sorted(selected_states))

        tk.Label(
            body,
            text=header,
            font=("Arial", 11, "bold"),
            anchor="w",
        ).pack(anchor="w", pady=(0, 4))

        tk.Label(
            body,
            text=sub,
            justify="left",
            wraplength=note_wrap,
            anchor="w",
            font=("Arial", 10),
            fg="gray30",
        ).pack(anchor="w", fill="x", pady=(0, 10))

        bg = body.cget("bg")
        intro_lines = max(4, min(9, int(round(6 * _zs))))
        intro = tk.Text(
            body,
            height=intro_lines,
            width=max(42, int(round(102 * _zs))),
            wrap="word",
            font=("Arial", 12),
            relief="flat",
            padx=0,
            pady=0,
            highlightthickness=0,
            borderwidth=0,
            bg=bg,
            cursor="arrow",
        )
        intro.tag_configure("title", font=("Arial", 11, "bold"))
        intro.tag_configure("note_label", font=("Arial", 12, "bold"))
        intro.tag_configure("note_body", font=("Arial", 12))
        intro.insert("end", "Select the zoom levels (resolution) to download.\n\n", "title")
        if download_scope == "radius":
            intro.insert(
                "end",
                "Radius mode: checking a zoom also selects every coarser zoom (same pyramid as state mode). "
                "Very low zoom tiles span huge areas, so the full pyramid can make the ATAK offline outline "
                "look wider than your circle even when the high-zoom data is tight — uncheck coarser zooms if you want a tighter footprint.\n\n",
                "note_body",
            )
        intro.insert("end", "NOTE:", "note_label")
        intro.insert(
            "end",
            " This is the RAW image size only, it will not take up this much space on your Android device. "
            "Ensure you have enough hard drive space to contain this imagery. "
            "You will be able to remove the raw imagery later once compiled and installed on your device.",
            "note_body",
        )
        intro.configure(state="disabled")
        intro.pack(anchor="w", fill="x", pady=(0, 8))

        self.temp_space_var = tk.StringVar(
            value="Estimated temporary space needed for selected zooms: select at least one zoom"
        )
        tk.Label(
            body,
            textvariable=self.temp_space_var,
            font=("Arial", 11, "bold"),
            justify="left",
            wraplength=note_wrap,
            anchor="w",
        ).pack(anchor="w", fill="x", pady=(0, 4))

        self.device_var = tk.StringVar(
            value="Estimated space to be installed on device: select at least one zoom"
        )
        tk.Label(
            body,
            textvariable=self.device_var,
            font=("Arial", 11, "bold"),
            justify="left",
            wraplength=note_wrap,
            anchor="w",
        ).pack(anchor="w", fill="x", pady=(0, 4))

        self.download_time_var = tk.StringVar(
            value="Estimated time for download with your internet connection: measuring…"
        )
        tk.Label(
            body,
            textvariable=self.download_time_var,
            font=("Arial", 11, "bold"),
            justify="left",
            wraplength=note_wrap,
            anchor="w",
        ).pack(anchor="w", fill="x", pady=(0, 16))

        mid = tk.Frame(body)
        mid.pack(fill="both", expand=True)
        checks = pack_vertical_scroll_area(mid)

        default_avg: Dict[int, int] = {zz: 25000 for zz in range(10, 17)}
        avgs = avg_tile_bytes_by_zoom if avg_tile_bytes_by_zoom else default_avg

        for z in range(10, 17):
            total_tiles = 0
            total_bytes = 0
            if download_scope == "radius":
                if precomputed_radius_tiles is not None:
                    n = precomputed_radius_tiles.get(z, 0)
                    total_tiles = n
                    total_bytes = n * int(avgs.get(z, 25000))
                elif radius_center is not None and radius_miles is not None:
                    tiles = compute_tiles_for_radius(radius_center[0], radius_center[1], radius_miles, z)
                    n = len(tiles)
                    avg_b = int(avgs.get(z, 25000))
                    total_tiles = n
                    total_bytes = n * avg_b
            else:
                for state_name in selected_states:
                    state_info = zoom_estimates.get(state_name, {})
                    zoom_info = state_info.get(str(z), {})
                    total_tiles += int(zoom_info.get("estimated_tiles", 0))
                    total_bytes += int(zoom_info.get("estimated_bytes", 0))

            self.zoom_total_tiles[z] = total_tiles
            self.zoom_total_bytes[z] = total_bytes

            var = tk.BooleanVar(value=False)
            self.vars[z] = var
            cb = tk.Checkbutton(
                checks,
                text=(
                    f"{zoom_resolution_labels(z)}   |   "
                    f"estimated tiles: {total_tiles:,}   |   "
                    f"estimated size: {human_bytes(total_bytes)}"
                ),
                variable=var,
                anchor="w",
                justify="left",
                wraplength=note_wrap,
                command=lambda zz=z: self._on_zoom_toggle(zz),
            )
            cb.pack(anchor="w", fill="x")

        self.protocol("WM_DELETE_WINDOW", self.cancel)
        apply_resizable_window(self, 1040, 820, (520, 440))
        refit_toplevel_geometry(self, 1040, 820)

        self._probe_poll_after_id: Optional[str] = None
        self._probe_proc: Optional["multiprocessing.Process"] = None
        self._probe_mp_q: Optional["multiprocessing.Queue"] = None
        self._probe_poll_after_id = self.after(50, self._start_usgs_probe_process)
        self.update_size_label()

        try:
            self.deiconify()
            ensure_window_stacking(self)
            self.update_idletasks()
        except tk.TclError:
            pass

    def destroy(self) -> None:  # type: ignore[override]
        aid = getattr(self, "_probe_poll_after_id", None)
        if aid is not None:
            try:
                self.after_cancel(aid)
            except tk.TclError:
                pass
            self._probe_poll_after_id = None
        proc = getattr(self, "_probe_proc", None)
        if proc is not None and proc.exitcode is None:
            try:
                proc.terminate()
                proc.join(timeout=3)
            except Exception:
                pass
            self._probe_proc = None
        super().destroy()

    def _start_usgs_probe_process(self) -> None:
        self._probe_poll_after_id = None
        try:
            if not int(self.winfo_exists()):
                return
        except tk.TclError:
            return
        try:
            from usgs_throughput_probe import run_probe_process_entry

            ctx = multiprocessing.get_context("spawn")
            self._probe_mp_q = ctx.Queue()
            self._probe_proc = ctx.Process(
                target=run_probe_process_entry,
                args=(self._probe_mp_q,),
            )
            self._probe_proc.start()
            self._probe_poll_after_id = self.after(100, self._poll_usgs_probe_process)
        except Exception as e:
            log(f"Imagery throughput probe failed to start: {e}")
            self._probe_proc = None
            self._probe_mp_q = None
            self._apply_probe_result(None)

    def _poll_usgs_probe_process(self) -> None:
        self._probe_poll_after_id = None
        try:
            if not int(self.winfo_exists()):
                proc = getattr(self, "_probe_proc", None)
                if proc is not None and proc.exitcode is None:
                    try:
                        proc.terminate()
                        proc.join(timeout=2)
                    except Exception:
                        pass
                return
        except tk.TclError:
            return
        proc = self._probe_proc
        if proc is None:
            self._apply_probe_result(None)
            return
        if proc.exitcode is None:
            self._probe_poll_after_id = self.after(100, self._poll_usgs_probe_process)
            return
        bps: Optional[float] = None
        q = self._probe_mp_q
        if q is not None:
            try:
                bps = q.get_nowait()
            except queue.Empty:
                bps = None
        try:
            self._apply_probe_result(bps)
        except tk.TclError:
            pass

    def _apply_probe_result(self, bps: Optional[float]) -> None:
        self._probe_finished = True
        self._download_throughput_bps = bps
        if bps is not None and bps > 0:
            log(f"Imagery throughput probe: sampled aggregate {human_throughput(bps)}")
        try:
            self.update_size_label()
        except tk.TclError:
            pass

    def select_all(self) -> None:
        for v in self.vars.values():
            v.set(True)
        self.update_size_label()

    def clear_all(self) -> None:
        for v in self.vars.values():
            v.set(False)
        self.update_size_label()

    def _on_zoom_toggle(self, z: int) -> None:
        """Checking a zoom also checks every coarser zoom (10…z) for state and radius."""
        if self.vars[z].get():
            for zz in range(10, z + 1):
                self.vars[zz].set(True)
        self.update_size_label()

    def update_size_label(self) -> None:
        selected = [z for z, var in self.vars.items() if var.get()]
        time_prefix = "Estimated time for download with your internet connection:"
        if not selected:
            self.temp_space_var.set(
                "Estimated temporary space needed for selected zooms: select at least one zoom"
            )
            self.device_var.set(
                "Estimated space to be installed on device: select at least one zoom"
            )
            if not self._probe_finished:
                self.download_time_var.set(f"{time_prefix} measuring connection to imagery server…")
            elif self._download_throughput_bps is None:
                self.download_time_var.set(
                    f"{time_prefix} could not measure speed (server unreachable or blocked)"
                )
            else:
                self.download_time_var.set(
                    f"{time_prefix} select zoom levels for an estimate."
                )
            return
        total_bytes = sum(self.zoom_total_bytes[z] for z in selected)
        total_tiles = sum(self.zoom_total_tiles[z] for z in selected)
        device_bytes = estimate_device_sqlite_bytes(total_bytes)
        self.temp_space_var.set(
            f"Estimated temporary space needed for selected zooms: {human_bytes(total_bytes)}   |   "
            f"estimated tiles: {total_tiles:,}"
        )
        self.device_var.set(
            f"Estimated space to be installed on device: {human_bytes(device_bytes)}"
        )
        if not self._probe_finished:
            self.download_time_var.set(f"{time_prefix} measuring connection to imagery server…")
        elif self._download_throughput_bps is None or self._download_throughput_bps <= 0:
            self.download_time_var.set(
                f"{time_prefix} could not measure speed (server unreachable or blocked)"
            )
        else:
            eta_sec = total_bytes / self._download_throughput_bps
            self.download_time_var.set(
                f"{time_prefix} {format_download_eta(eta_sec)}"
            )

    def back(self) -> None:
        self.go_back = True
        self.result = []
        self.destroy()

    def submit(self) -> None:
        self.result = sorted([z for z, var in self.vars.items() if var.get()])
        if not self.result:
            ensure_window_stacking(self)
            messagebox.showwarning(APP_TITLE, "Select at least one zoom level.", parent=self)
            return
        self.destroy()

    def cancel(self) -> None:
        self.go_back = False
        self.result = []
        self.destroy()


class ProgressWindow(tk.Tk):
    _PAUSED_STATUS_TEXT = "Paused — click Resume to continue"

    def __init__(self, log_path: Path):
        super().__init__()
        self.title(f"{APP_TITLE} - Progress")
        self.configure(cursor="arrow")

        top = tk.Frame(self, padx=10, pady=10)
        top.pack(fill="x")

        self.status_var = tk.StringVar(value="Initializing...")
        self.counter_var = tk.StringVar(value="0 / 0")
        self.detail_var = tk.StringVar(value=f"Log: {log_path}")
        self.speed_var = tk.StringVar(value="Speed: --")
        self.eta_var = tk.StringVar(value="ETA: --")

        tk.Label(top, textvariable=self.status_var, font=("Arial", 11, "bold")).pack(anchor="w")
        tk.Label(top, textvariable=self.counter_var).pack(anchor="w", pady=(4, 0))
        tk.Label(top, textvariable=self.detail_var, fg="gray30").pack(anchor="w", pady=(4, 4))
        tk.Label(top, textvariable=self.speed_var, font=("Arial", 10, "bold"), fg="darkgreen").pack(anchor="w")
        tk.Label(top, textvariable=self.eta_var, font=("Arial", 10, "bold"), fg="darkblue").pack(anchor="w", pady=(0, 8))

        self.canvas = tk.Canvas(top, height=24, bg="white", highlightthickness=1, highlightbackground="gray70")
        self.canvas.configure(cursor="arrow")
        self.canvas.pack(fill="x")
        self.bar = self.canvas.create_rectangle(0, 0, 0, 24, fill="#4a90e2", width=0)
        self.bar_text = self.canvas.create_text(5, 12, anchor="w", text="0%")

        self._ui_lock = threading.Lock()
        self._pending_progress: Optional[Tuple[int, int]] = None
        self._pending_status: Optional[str] = None
        self._pending_stats: Optional[Dict[str, int]] = None
        self._pending_speed: Optional[Tuple[float, Optional[float]]] = None
        self._pending_progress_fraction: Optional[Tuple[float, Optional[str]]] = None
        self._progress_canvas_mode = "none"
        self._progress_canvas_count: Tuple[int, int] = (0, 1)
        self._progress_canvas_frac: float = 0.0
        self._progress_canvas_bar_lbl: str = "0%"
        self.canvas.bind("<Configure>", self._on_progress_canvas_configure)

        stats = tk.Frame(self, padx=10)
        stats.pack(fill="x", pady=(6, 6))

        self.stats_vars = {
            "downloaded": tk.StringVar(value="Downloaded: 0"),
            "existing": tk.StringVar(value="Existing: 0"),
            "failed": tk.StringVar(value="Failed: 0"),
            "missing": tk.StringVar(value="Missing: 0"),
        }
        for i, key in enumerate(("downloaded", "existing", "failed", "missing")):
            tk.Label(stats, textvariable=self.stats_vars[key], width=18, anchor="w").grid(row=0, column=i, sticky="w")

        ctrl = tk.Frame(self, padx=10)
        ctrl.pack(fill="x", pady=(0, 4))
        self._ctl_lock = threading.Lock()
        self._paused = False
        self._cancel_requested = False
        self.user_cancelled = False
        self._last_activity_status = "Initializing..."
        self.btn_pause = tk.Button(ctrl, text="Pause", width=12, command=self._on_pause_toggle)
        self.btn_pause.pack(side="left", padx=(0, 8))
        tk.Button(ctrl, text="Cancel", width=12, command=self._on_cancel_download).pack(side="left")

        log_frame = tk.Frame(self, padx=10, pady=10)
        log_frame.pack(fill="both", expand=True)

        self.text = tk.Text(log_frame, wrap="word")
        self.text.pack(side="left", fill="both", expand=True)

        scroll = tk.Scrollbar(log_frame, command=self.text.yview)
        scroll.pack(side="right", fill="y")
        self.text.config(yscrollcommand=scroll.set)

        apply_resizable_window(self, 860, 560, (680, 400))
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.closed = False
        self.completion_message = None
        self.completion_log_summary = None
        self.error_message = None
        self.device_ready_prompt: Optional[str] = None
        self.device_ready_event: Optional[threading.Event] = None

    def append_log(self, line: str) -> None:
        self.text.insert("end", line)
        self.text.see("end")
        self.update_idletasks()

    def _on_gui_thread(self) -> bool:
        return threading.current_thread() is threading.main_thread()

    def _flush_pending_ui(self) -> None:
        """Apply progress/status updates posted from the download worker (Tk is main-thread only)."""
        if getattr(self, "closed", False):
            return
        with self._ui_lock:
            pp = self._pending_progress
            ps = self._pending_status
            pst = self._pending_stats.copy() if self._pending_stats else None
            sp = self._pending_speed
            pf = self._pending_progress_fraction
        if pp is not None:
            self._set_progress_ui(*pp)
        if ps is not None:
            self._set_status_ui(ps)
        if pst is not None:
            for k, v in pst.items():
                self._set_stat_ui(k, v)
        if sp is not None:
            self._set_speed_eta_ui(*sp)
        if pf is not None:
            self._set_progress_fraction_ui(*pf)

    def _draw_progress_bar(self, frac: float, bar_label: str) -> None:
        frac = max(0.0, min(1.0, float(frac)))
        width = max(int(self.canvas.winfo_width()), 1)
        fill_w = int(width * frac)
        if frac > 0 and fill_w < 2:
            fill_w = min(2, width)
        self.canvas.coords(self.bar, 0, 0, fill_w, 24)
        self.canvas.coords(self.bar_text, 8, 12)
        self.canvas.itemconfig(self.bar_text, text=bar_label)
        self.update_idletasks()

    def _on_progress_canvas_configure(self, event: tk.Event) -> None:
        if event.widget != self.canvas:
            return
        mode = self._progress_canvas_mode
        if mode == "count":
            c, t = self._progress_canvas_count
            total = max(t, 1)
            frac = c / total
            pct_f = 100.0 * frac
            if pct_f >= 10:
                bar_lbl = f"{pct_f:.1f}%"
            elif pct_f >= 1:
                bar_lbl = f"{pct_f:.2f}%"
            else:
                bar_lbl = f"{pct_f:.3f}%"
            self._draw_progress_bar(frac, bar_lbl)
        elif mode == "fraction":
            self._draw_progress_bar(self._progress_canvas_frac, self._progress_canvas_bar_lbl)

    def _format_tile_counter(self, completed: int, total: int) -> str:
        total = max(total, 1)
        pct_f = 100.0 * completed / total
        if total >= 100_000:
            dec = 4
        elif total >= 10_000:
            dec = 3
        else:
            dec = 2
        return f"{completed:,} / {total:,} tiles ({pct_f:.{dec}f}%)"

    def _set_progress_ui(self, completed: int, total: int) -> None:
        total = max(total, 1)
        frac = completed / total
        self._progress_canvas_mode = "count"
        self._progress_canvas_count = (completed, total)
        pct_f = 100.0 * frac
        if pct_f >= 10:
            bar_lbl = f"{pct_f:.1f}%"
        elif pct_f >= 1:
            bar_lbl = f"{pct_f:.2f}%"
        else:
            bar_lbl = f"{pct_f:.3f}%"
        self.counter_var.set(self._format_tile_counter(completed, total))
        self._draw_progress_bar(frac, bar_lbl)

    def _set_progress_fraction_ui(self, frac: float, counter_detail: Optional[str] = None) -> None:
        frac = max(0.0, min(1.0, float(frac)))
        self._progress_canvas_mode = "fraction"
        self._progress_canvas_frac = frac
        pct_f = frac * 100
        bar_lbl = f"{pct_f:.3f}%" if pct_f < 1 else f"{pct_f:.2f}%"
        self._progress_canvas_bar_lbl = bar_lbl
        if counter_detail is not None:
            self.counter_var.set(counter_detail)
        else:
            dec = 4 if pct_f < 1 else 2
            self.counter_var.set(f"{pct_f:.{dec}f}%")
        self._draw_progress_bar(frac, bar_lbl)

    def _set_status_ui(self, text: str) -> None:
        if text != self._PAUSED_STATUS_TEXT:
            self._last_activity_status = text
        self.status_var.set(text)
        self.update_idletasks()

    def _set_speed_eta_ui(self, speed_bps: float, eta_seconds: Optional[float]) -> None:
        if speed_bps and speed_bps > 0:
            self.speed_var.set(f"Speed: {human_bytes(int(speed_bps))}/s")
        else:
            self.speed_var.set("Speed: --")

        if eta_seconds is None or eta_seconds < 0 or not math.isfinite(eta_seconds):
            self.eta_var.set("ETA: --")
        else:
            total_seconds = int(max(0, eta_seconds))
            hours, rem = divmod(total_seconds, 3600)
            minutes, seconds = divmod(rem, 60)
            if hours > 0:
                self.eta_var.set(f"ETA: {hours}h {minutes}m {seconds}s")
            elif minutes > 0:
                self.eta_var.set(f"ETA: {minutes}m {seconds}s")
            else:
                self.eta_var.set(f"ETA: {seconds}s")

        self.update_idletasks()

    def _set_stat_ui(self, key: str, value: int) -> None:
        label = key.capitalize()
        self.stats_vars[key].set(f"{label}: {value}")
        self.update_idletasks()

    def set_progress(self, completed: int, total: int) -> None:
        if not self._on_gui_thread():
            with self._ui_lock:
                self._pending_progress = (completed, total)
            return
        self._set_progress_ui(completed, total)

    def set_progress_fraction(self, frac: float, counter_detail: Optional[str] = None) -> None:
        if not self._on_gui_thread():
            with self._ui_lock:
                self._pending_progress_fraction = (frac, counter_detail)
            return
        self._set_progress_fraction_ui(frac, counter_detail)

    def set_status(self, text: str) -> None:
        if not self._on_gui_thread():
            with self._ui_lock:
                self._pending_status = text
            return
        self._set_status_ui(text)

    def set_speed_eta(self, speed_bps: float, eta_seconds: Optional[float]) -> None:
        if not self._on_gui_thread():
            with self._ui_lock:
                self._pending_speed = (speed_bps, eta_seconds)
            return
        self._set_speed_eta_ui(speed_bps, eta_seconds)

    def set_stat(self, key: str, value: int) -> None:
        if not self._on_gui_thread():
            with self._ui_lock:
                if self._pending_stats is None:
                    self._pending_stats = {}
                self._pending_stats[key] = value
            return
        self._set_stat_ui(key, value)

    def _on_pause_toggle(self) -> None:
        with self._ctl_lock:
            self._paused = not self._paused
            now_paused = self._paused
            label = "Resume" if now_paused else "Pause"
        try:
            self.btn_pause.configure(text=label)
            if now_paused:
                self.set_status(self._PAUSED_STATUS_TEXT)
            else:
                self.set_status(self._last_activity_status)
        except tk.TclError:
            pass

    def _on_cancel_download(self) -> None:
        ensure_window_stacking(self)
        if not messagebox.askyesno(
            APP_TITLE,
            "Stop downloading and exit the program?",
            parent=self,
        ):
            return
        with self._ctl_lock:
            self._cancel_requested = True

    def wait_if_paused(self) -> None:
        while True:
            with self._ctl_lock:
                if self._cancel_requested:
                    raise DownloadCancelled()
                if not self._paused:
                    return
            time.sleep(0.05)

    def on_close(self) -> None:
        status = self.status_var.get().strip().lower()
        if status in {"complete", "completed", "done", "finished"}:
            self.closed = True
            self.destroy()
            return
        if status == "cancelled":
            self.closed = True
            self.destroy()
            return
        ensure_window_stacking(self)
        if messagebox.askyesno(
            APP_TITLE,
            "Stop downloading and exit the program?",
            parent=self,
        ):
            with self._ctl_lock:
                self._cancel_requested = True

# -----------------------------
# Workflow helpers
# -----------------------------

def run_with_busy_dialog(message: str, fn: Callable[[], None]) -> None:
    """Show a pulsing zenity progress bar while ``fn()`` runs, then close it.

    Uses zenity (already a dependency on Linux) so no extra Tk root is created.
    Falls back to running ``fn()`` silently if zenity is unavailable.
    ``fn()`` is called on the main thread; zenity runs in a subprocess.
    """
    if not shutil.which("zenity"):
        fn()
        return

    proc = subprocess.Popen(
        [
            "zenity",
            "--progress",
            "--pulsate",
            "--no-cancel",
            "--auto-close",
            "--width=400",
            "--height=120",
            f"--title={APP_TITLE}",
            f"--text={message}",
        ],
        stdin=subprocess.PIPE,
    )
    try:
        fn()
    finally:
        try:
            proc.stdin.write(b"100\n")
            proc.stdin.flush()
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()


def show_summary_confirm(
    selected_states: List[str],
    selected_zooms: List[int],
    total_bytes: int,
    total_tiles: int,
    *,
    download_scope: str = "state",
    radius_summary: Optional[str] = None,
) -> bool:
    if download_scope == "radius" and radius_summary:
        msg = (
            f"{radius_summary}\n\n"
            f"Zooms:\n{', '.join(map(str, selected_zooms))}\n\n"
            f"Estimated size:\n{human_bytes(total_bytes)}\n\n"
            f"Estimated tiles:\n{total_tiles:,}\n\n"
            "Select a temporary folder for install on the next screen (defaults to your "
            "Downloads folder if you have not chosen one before).\n\n"
            "Press OK to continue."
        )
    else:
        state_summary = ", ".join(selected_states[:6])
        if len(selected_states) > 6:
            state_summary += f", ... ({len(selected_states)} total)"
        msg = (
            f"States:\n{state_summary}\n\n"
            f"Zooms:\n{', '.join(map(str, selected_zooms))}\n\n"
            f"Estimated size:\n{human_bytes(total_bytes)}\n\n"
            f"Estimated tiles:\n{total_tiles:,}\n\n"
            "Select a temporary folder for install on the next screen (defaults to your "
            "Downloads folder if you have not chosen one before).\n\n"
            "Press OK to continue."
        )
    if shutil.which("zenity"):
        try:
            subprocess.run(
                ["zenity", "--info", "--no-wrap", f"--title={APP_TITLE}", f"--text={msg}"],
                check=False,
            )
            return True
        except OSError:
            pass

    root = tk.Tk()
    try:
        root.option_add("*cursor", "arrow")
    except tk.TclError:
        pass
    root.configure(cursor="arrow")
    # Position at center but keep visible so topmost works on GNOME/Mutter.
    root.geometry("1x1")
    root.update_idletasks()
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    root.geometry(f"1x1+{sw // 2}+{sh // 2}")
    root.attributes("-topmost", True)
    root.lift()
    root.focus_force()
    root.update_idletasks()
    messagebox.showinfo(APP_TITLE, msg, parent=root)
    try:
        root.destroy()
    except tk.TclError:
        pass
    return True


PIPELINE_OUTPUT_PARENT_FILE = RUNTIME_STATE_DIR / ".last_pipeline_output_parent.txt"


def default_output_parent() -> Path:
    if PIPELINE_OUTPUT_PARENT_FILE.is_file():
        try:
            p = Path(PIPELINE_OUTPUT_PARENT_FILE.read_text(encoding="utf-8").strip())
            if p.is_dir():
                return p
        except OSError:
            pass
    downloads = Path.home() / "Downloads"
    if downloads.is_dir():
        return downloads
    return Path.home()


def save_output_parent(parent: Path) -> None:
    try:
        PIPELINE_OUTPUT_PARENT_FILE.write_text(str(parent.resolve()), encoding="utf-8")
    except OSError:
        pass


def pick_directory(title: str, initial: Path, parent: tk.Misc) -> str:
    """Linux: same Zenity folder picker as output selection; else Tk ``askdirectory`` parented to ``parent``."""
    try:
        if shutil.which("zenity"):
            start_uri = str(initial.resolve()) + "/"
            result = subprocess.run(
                [
                    "zenity",
                    "--file-selection",
                    "--directory",
                    f"--title={title}",
                    f"--filename={start_uri}",
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                folder = result.stdout.strip()
                if folder:
                    return str(Path(folder))
            return ""
    except Exception:
        pass
    ensure_window_stacking(parent)
    folder = filedialog.askdirectory(title=title, initialdir=str(initial), parent=parent)
    return folder or ""


def ask_output_parent() -> str:
    initial = default_output_parent()
    pick_title = "Select a temporary folder for install"
    try:
        if shutil.which("zenity"):
            # Start in Downloads or last-used folder
            start_uri = str(initial.resolve()) + "/"
            result = subprocess.run(
                [
                    "zenity",
                    "--file-selection",
                    "--directory",
                    f"--title={pick_title}",
                    f"--filename={start_uri}",
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                folder = result.stdout.strip()
                if folder:
                    p = Path(folder)
                    save_output_parent(p)
                    return str(p)
            return ""
    except Exception:
        pass

    root = tk.Tk()
    root.configure(cursor="arrow")
    root.geometry("1x1")
    root.update_idletasks()
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    root.geometry(f"1x1+{sw // 2}+{sh // 2}")
    root.attributes("-topmost", True)
    root.lift()
    root.focus_force()
    root.update_idletasks()
    try:
        folder = filedialog.askdirectory(
            title=pick_title,
            initialdir=str(initial),
            parent=root,
        )
    finally:
        try:
            root.attributes("-topmost", False)
        except tk.TclError:
            pass
        root.destroy()
    if folder:
        save_output_parent(Path(folder))
        return folder
    return ""




DOWNLOAD_SESSION_LOCAL = threading.local()


def get_download_session() -> requests.Session:
    session = getattr(DOWNLOAD_SESSION_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        DOWNLOAD_SESSION_LOCAL.session = session
    return session


def plan_requires_network_for_imagery(
    plan: List[Tuple[str, int, int, int, Path]],
    raw_imagery_root: Optional[Path],
) -> bool:
    """True if any planned tile is not already present and not available under raw_imagery_root."""
    raw = raw_imagery_root if raw_imagery_root is not None and raw_imagery_root.is_dir() else None
    for state_label, z, x, y, out_path in plan:
        if out_path.is_file():
            continue
        if raw is not None:
            local = raw / state_label / str(z) / str(x) / f"{y}.jpg"
            if local.is_file():
                continue
        return True
    return False


def dted_requires_network_fetch(
    dted_state_list: List[str],
    local_dted_root: Optional[Path],
) -> bool:
    """True if inline DTED would need to download at least one state zip over the network."""
    if not dted_state_list:
        return False
    root = local_dted_root if local_dted_root is not None and local_dted_root.is_dir() else None
    if root is None:
        return True
    for state_name in dted_state_list:
        cand = root / state_name / f"{state_name}.zip"
        if not cand.is_file():
            return True
    return False


def http_host_reachable(url: str, timeout: float = 6.0) -> bool:
    try:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        r = session.head(url, timeout=timeout, allow_redirects=True)
        if r.status_code < 400:
            return True
        if r.status_code == 405:
            rr = session.get(url, timeout=timeout, stream=True)
            try:
                return rr.status_code < 400
            finally:
                rr.close()
        return False
    except Exception:
        return False


def fetch_tile(
    z: int,
    x: int,
    y: int,
    out_path: Path,
    *,
    state_label: str,
    raw_imagery_root: Optional[Path] = None,
) -> Tuple[str, int]:
    if out_path.exists():
        return "existing", 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if raw_imagery_root is not None and raw_imagery_root.is_dir():
        local = raw_imagery_root / state_label / str(z) / str(x) / f"{y}.jpg"
        if local.is_file():
            try:
                shutil.copy2(local, out_path)
                return "downloaded", local.stat().st_size
            except Exception as e:
                log(f"ERROR copying local tile {local}: {e}")

    url = USGS_TILE_URL.format(z=z, x=x, y=y)
    bytes_written = 0
    try:
        session = get_download_session()
        with session.get(url, timeout=30, stream=True) as r:
            if r.status_code == 404:
                return "missing", 0
            r.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 64):
                    if chunk:
                        f.write(chunk)
                        bytes_written += len(chunk)
        return "downloaded", bytes_written
    except Exception as e:
        log(f"ERROR downloading z{z}/{x}/{y}: {e}")
        try:
            if out_path.exists():
                out_path.unlink()
        except Exception:
            pass
        return "failed", 0


def run_download(
    selected_zooms: List[int],
    selected_states: List[str],
    mode: str,
    output_parent: Path,
    progress: ProgressWindow,
    *,
    raw_imagery_root: Optional[Path] = None,
    local_dted_root: Optional[Path] = None,
    radius_center: Optional[Tuple[float, float]] = None,
    radius_miles: Optional[float] = None,
    radius_region_folder: Optional[str] = None,
    preloaded_states: Optional[Dict[str, List[List[Tuple[float, float]]]]] = None,
) -> None:
    stats = {"downloaded": 0, "existing": 0, "failed": 0, "missing": 0}
    executor: Optional[ThreadPoolExecutor] = None

    if raw_imagery_root is not None:
        raw_imagery_root = raw_imagery_root.expanduser()
        if not raw_imagery_root.is_dir():
            log(f"Raw imagery root not found or not a directory (ignored): {raw_imagery_root}")
            raw_imagery_root = None
        else:
            log(f"Raw imagery tree (try local copy before HTTP): {raw_imagery_root}")

    if local_dted_root is not None:
        local_dted_root = local_dted_root.expanduser()
        if not local_dted_root.is_dir():
            log(f"Local DTED root not found or not a directory (ignored): {local_dted_root}")
            local_dted_root = None
        else:
            log(f"Local DTED state zip tree (copy before network): {local_dted_root}")

    radius_mode = radius_center is not None and radius_miles is not None
    radius_folder = (radius_region_folder or RADIUS_REGION_FOLDER) if radius_mode else ""

    try:
        log(
            f"run_download: states={selected_states!r} zooms={selected_zooms!r} mode={mode!r} "
            f"radius_mode={radius_mode} radius_folder={radius_folder!r}"
        )
        progress.wait_if_paused()

        output_root = output_parent / "Imagery"
        output_root.mkdir(parents=True, exist_ok=True)

        date_str = datetime.now().strftime("%Y%m%d")
        upload_dir = output_parent / f"ATAK_Upload_{date_str}"
        upload_dir.mkdir(parents=True, exist_ok=True)

        LAST_IMAGERY_ROOT_FILE.write_text(str(output_root), encoding="utf-8")
        log(f"Using output root: {output_root}")
        log(f"Using upload folder: {upload_dir}")

        plan: List[Tuple[str, int, int, int, Path]] = []

        if radius_mode:
            lat, lon = radius_center  # type: ignore[assignment]
            miles = float(radius_miles)  # type: ignore[arg-type]
            log(
                f"Tile coverage: geodesic circle {miles:g} mi from ({lat:.5f}, {lon:.5f}); "
                "include any tile the circle intersects"
            )
            state_names = [radius_folder]
            try:
                w, s, e, n = square_lonlat_footprint_for_radius_miles(lat, lon, miles)
                fp_dir = output_root / radius_folder
                fp_dir.mkdir(parents=True, exist_ok=True)
                fp_path = fp_dir / ".radius_footprint.json"
                fp_path.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "center_lat": lat,
                            "center_lon": lon,
                            "radius_miles": miles,
                            "west": w,
                            "south": s,
                            "east": e,
                            "north": n,
                            "note": "axis_aligned_box_side_eq_diameter_m_apx",
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                log(f"Wrote radius footprint for SQLite metadata: {fp_path}")
            except (OSError, ValueError) as exc:
                log(f"WARNING: could not write radius footprint file: {exc}")
            progress.set_status("Building radius tile download plan…")
            for z in selected_zooms:
                progress.wait_if_paused()
                progress.set_status(f"Tile plan: radius zoom {z}…")
                tiles = compute_tiles_for_radius(lat, lon, miles, z)
                log(f"Tile plan (radius): {len(tiles):,} tiles for zoom {z}")
                for i, (x, y) in enumerate(tiles, start=1):
                    if i % 2048 == 0:
                        progress.wait_if_paused()
                    out_path = output_root / radius_folder / str(z) / str(x) / f"{y}.jpg"
                    plan.append((radius_folder, z, x, y, out_path))
        else:
            # State boundaries are pre-loaded on the main thread before this
            # thread starts, avoiding a large allocation burst from the
            # background thread that can trigger the cyclic GC at the wrong time.
            if preloaded_states is not None:
                states = preloaded_states
                geojson_path = STATE_GEOJSON_PATH
                log(f"Using pre-loaded state boundaries ({len(states)} states)")
            else:
                progress.set_status("Loading state boundaries...")
                geojson_path = bundled_state_geojson_path()
                states = load_states(geojson_path)

            state_names = []
            for state_name in selected_states:
                if state_name not in states:
                    raise RuntimeError(f"State not found in boundary file: {state_name}")
                if state_name == "District of Columbia":
                    continue
                state_names.append(state_name)

            if not state_names:
                if not selected_states:
                    raise RuntimeError(
                        "No valid states selected (empty list). "
                        "Try running the downloader again; if this repeats, keep the log file for support."
                    )
                raise RuntimeError(
                    "No states to download imagery for.\n\n"
                    f"You selected: {', '.join(selected_states)}\n\n"
                    "This tool skips District of Columbia: USGS state imagery uses full state shapes, "
                    "and D.C. is not included as its own download region. "
                    "Choose at least one state (for example Delaware or Maryland). "
                    "D.C. is listed directly under Delaware in the list — easy to select by mistake."
                )

            log(
                f"Tile coverage: GeoJSON boundaries + {STATE_BOUNDARY_BUFFER_MILES:g} mi edge buffer "
                "(tile center inside polygon or within buffer of boundary)"
            )
            log(f"Selected states: {', '.join(state_names)}")

            progress.set_status("Building tile download plan…")
            for state_name in state_names:
                progress.wait_if_paused()
                rings = states[state_name]
                for z in selected_zooms:
                    progress.wait_if_paused()
                    progress.set_status(f"Tile plan: {state_name} zoom {z}…")
                    tpr = build_tiles_for_state_result(
                        state_name,
                        rings,
                        z,
                        geojson_path=STATE_GEOJSON_PATH,
                        tile_plan_dir=TILE_PLAN_DIR,
                    )
                    tiles = tpr.tiles
                    if tpr.from_cache:
                        log(
                            f"Tile plan (cache): {len(tiles):,} tiles for {state_name}, zoom {z} "
                            f"— {state_name.replace('/', '_')}_z{z}.tiles.gz"
                        )
                    else:
                        log(f"Tile plan (computed): {len(tiles):,} tiles for {state_name}, zoom {z}")
                    for i, (x, y) in enumerate(tiles, start=1):
                        if i % 2048 == 0:
                            progress.wait_if_paused()
                        out_path = output_root / state_name / str(z) / str(x) / f"{y}.jpg"
                        plan.append((state_name, z, x, y, out_path))

        if radius_mode:
            progress.set_status("Resolving states for DTED…")
            dted_state_list = state_names_intersecting_geodesic_circle(
                lat, lon, miles, load_states(bundled_state_geojson_path())
            )
            log(
                "DTED: full state package(s) for states overlapping the radius "
                f"(bounding-box match): {', '.join(dted_state_list) if dted_state_list else '(none)'}"
            )
        else:
            dted_state_list = list(state_names)

        need_img_net = plan_requires_network_for_imagery(plan, raw_imagery_root)
        need_dted_net = dted_requires_network_fetch(dted_state_list, local_dted_root)
        if need_img_net or need_dted_net:
            log(
                f"Pre-download check: need network for imagery={need_img_net}, "
                f"for DTED={need_dted_net}"
            )
            if need_img_net and not http_host_reachable(USGS_MAPSERVER_BASE_URL):
                log("USGS MapServer unreachable; cannot fetch missing imagery tiles.")
                progress.error_message = OFFLINE_MISSING_DATA_MSG
                progress.set_status("Incomplete")
                return
            if need_dted_net and not http_host_reachable(DTED_SERVER_BASE_URL):
                log("DTED server unreachable; cannot fetch missing elevation packages.")
                progress.error_message = OFFLINE_MISSING_DATA_MSG
                progress.set_status("Incomplete")
                return

        total = len(plan)
        log(f"Total tile candidates: {total}")
        progress.set_progress(0, total)
        progress.set_status("Starting download...")

        completed = 0
        downloaded_bytes = 0
        started_at = time.time()
        speed_samples = deque(maxlen=10)
        last_sample_time = started_at
        last_sample_bytes = 0
        smoothed_speed_bps = 0.0
        eta_smoothed = None
        last_eta_update_time = time.time()
        tile_time_samples = deque(maxlen=20)
        last_tile_complete_time = started_at
        progress.set_speed_eta(0.0, None)

        def download_one(tile: Tuple[str, int, int, int, Path]) -> Tuple[str, int, int, int, str, int]:
            state_name, z, x, y, out_path = tile
            result, bytes_written = fetch_tile(
                z,
                x,
                y,
                out_path,
                state_label=state_name,
                raw_imagery_root=raw_imagery_root,
            )
            return state_name, z, x, y, result, bytes_written

        max_workers = max(1, min(MAX_DOWNLOAD_WORKERS, total if total > 0 else 1))
        progress.set_status(f"Starting download with {max_workers} workers...")

        future_to_tile = {}
        plan_iter = iter(plan)

        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            for _ in range(max_workers):
                try:
                    tile = next(plan_iter)
                except StopIteration:
                    break
                future = executor.submit(download_one, tile)
                future_to_tile[future] = tile

            while future_to_tile:
                progress.wait_if_paused()
                done, _ = wait(list(future_to_tile.keys()), timeout=0.2, return_when=FIRST_COMPLETED)
                progress.wait_if_paused()
                if not done:
                    continue

                for future in done:
                    progress.wait_if_paused()
                    tile = future_to_tile.pop(future)
                    state_name, z, x, y, out_path = tile
                    progress.set_status(f"Downloading {state_name} | zoom {z} | x={x} y={y}")

                    try:
                        _, _, _, _, result, bytes_written = future.result()
                    except CancelledError:
                        raise DownloadCancelled()
                    except DownloadCancelled:
                        raise
                    except Exception as e:
                        log(f"ERROR downloading z{z}/{x}/{y}: {e}")
                        result, bytes_written = "failed", 0

                    stats[result] += 1
                    downloaded_bytes += bytes_written
                    completed += 1

                    tile_now = time.time()
                    tile_elapsed = max(tile_now - last_tile_complete_time, 0.001)
                    last_tile_complete_time = tile_now
                    if result in ("downloaded", "existing", "missing", "failed"):
                        tile_time_samples.append(tile_elapsed)
                    progress.set_progress(completed, total)
                    for key, value in stats.items():
                        progress.set_stat(key, value)

                    now = time.time()
                    interval = now - last_sample_time
                    bytes_since_last = downloaded_bytes - last_sample_bytes

                    if interval >= 0.75 and bytes_since_last >= 0:
                        inst_speed = bytes_since_last / max(interval, 0.001)
                        speed_samples.append(inst_speed)
                        last_sample_time = now
                        last_sample_bytes = downloaded_bytes

                    if speed_samples:
                        smoothed_speed_bps = sum(speed_samples) / len(speed_samples)
                    else:
                        elapsed = max(now - started_at, 0.001)
                        smoothed_speed_bps = downloaded_bytes / elapsed

                    now_t = time.time()

                    if completed >= 8 and total > completed and tile_time_samples:
                        seconds_per_tile = sum(tile_time_samples) / len(tile_time_samples)
                        raw_eta = (seconds_per_tile * (total - completed)) * 1.08
                    elif total > completed:
                        raw_eta = None
                    else:
                        raw_eta = 0

                    if raw_eta is None:
                        eta_to_show = None
                    else:
                        if eta_smoothed is None:
                            eta_smoothed = raw_eta
                            last_eta_update_time = now_t
                        elif (now_t - last_eta_update_time) >= 2.0:
                            alpha = 0.12
                            proposed_eta = (alpha * raw_eta) + ((1 - alpha) * eta_smoothed)

                            delta_t = now_t - last_eta_update_time
                            max_drop = delta_t * 1.10
                            max_rise = delta_t * 6.0

                            lower_bound = max(0, eta_smoothed - max_drop)
                            upper_bound = eta_smoothed + max_rise
                            eta_smoothed = min(max(proposed_eta, lower_bound), upper_bound)

                            last_eta_update_time = now_t

                        eta_to_show = max(0, eta_smoothed)

                    progress.set_speed_eta(smoothed_speed_bps, eta_to_show)

                    if completed % 25 == 0 or result in ("failed", "missing"):
                        log(
                            f"Progress {completed}/{total} | "
                            f"downloaded={stats['downloaded']} existing={stats['existing']} "
                            f"missing={stats['missing']} failed={stats['failed']} "
                            f"bytes={downloaded_bytes}"
                        )

                    try:
                        next_tile = next(plan_iter)
                        next_future = executor.submit(download_one, next_tile)
                        future_to_tile[next_future] = next_tile
                    except StopIteration:
                        pass
        finally:
            if executor is not None:
                _shutdown_executor_pool(executor)
                executor = None

        progress.set_speed_eta(smoothed_speed_bps if "smoothed_speed_bps" in locals() else 0.0, 0)
        log("Imagery tile download complete")
        log(f"Downloaded: {stats['downloaded']}")
        log(f"Existing: {stats['existing']}")
        log(f"Missing: {stats['missing']}")
        log(f"Failed: {stats['failed']}")

        try:
            LAST_IMAGERY_SESSION_STATES_FILE.write_text(
                "\n".join(sorted(state_names)) + "\n",
                encoding="utf-8",
            )
            log(
                f"Recorded states for next SQLite build: {', '.join(state_names)} "
                f"({LAST_IMAGERY_SESSION_STATES_FILE.name}). "
                "Older folders under Imagery/ will be skipped unless you delete that file."
            )
        except OSError as exc:
            log(f"WARNING: could not write session state list: {exc}")

        dted_note = ""
        try:
            from atak_dted_downloader import (
                mark_standalone_dted_skip,
                query_device_installed_dted_states,
                run_dted_inline_for_states,
            )

            if not dted_state_list:
                log("DTED: no state packages match this download region; skipping.")
            else:
                # Mark that DTED is handled inline so the SQLite builder won't
                # launch the standalone DTED downloader afterward.
                mark_standalone_dted_skip()
                # Download-first workflow: imagery is already complete. Now prompt
                # for device connection before deciding whether DTED is needed.
                progress.set_status("Imagery complete — waiting for device connection for DTED check…")
                progress.device_ready_event = threading.Event()
                progress.device_ready_prompt = CONNECT_DEVICE_BEFORE_DTED_TEXT
                while True:
                    progress.wait_if_paused()
                    evt = progress.device_ready_event
                    if evt is None or evt.is_set():
                        break
                    time.sleep(0.1)

                progress.set_status("DTED: checking what is already installed on device…")
                device_dted_states: Set[str] = query_device_installed_dted_states(log) or set()
                states_needed = [s for s in dted_state_list if s not in device_dted_states]
                states_skipped = [s for s in dted_state_list if s in device_dted_states]
                if states_skipped:
                    log(
                        f"DTED: already on device — skipping: {', '.join(sorted(states_skipped))}"
                    )
                if not states_needed:
                    log("DTED: all required states already on device; skipping download.")
                    dted_note = "\n\nDTED: all required elevation data already on device — skipped."
                else:
                    if states_skipped:
                        log(
                            f"DTED: downloading only missing states: {', '.join(sorted(states_needed))}"
                        )
                    dted_zip = run_dted_inline_for_states(
                        states_needed,
                        upload_dir,
                        log_sink=log,
                        progress=progress,
                        local_state_zip_root=local_dted_root,
                    )
                    if dted_zip is not None:
                        dted_note = f"\n\nDTED archive ready:\n{dted_zip.name}"
        except ImportError as exc:
            log(f"DTED: skipped (module not loadable: {exc}).")
        except DownloadCancelled:
            raise
        except Exception as exc:
            log(f"DTED: failed — {exc}")

        devs = list_usb_devices()
        env_ser = (os.environ.get("ANDROID_SERIAL") or "").strip()
        if env_ser:
            ser_resolved = env_ser
        elif len(devs) == 1:
            ser_resolved = devs[0]
            os.environ["ANDROID_SERIAL"] = ser_resolved
        else:
            ser_resolved = ""
            if len(devs) > 1:
                log(
                    "Multiple adb devices connected — set ANDROID_SERIAL (or use the USB intro) "
                    "so map files and bundled plugins go to the correct device."
                )

        try:
            if ser_resolved:
                progress.set_status("Pushing map sources and import files to device…")
                push_mobile_assets(log_fn=log)
            elif not devs:
                log("No adb device — skipping map/import push and bundled plugin install.")
        except Exception as exc:
            log(f"Warning: mobile asset push failed — {exc}")

        try:
            if ser_resolved:
                from atak_adb_deploy import install_apk
                from bundled_plugin_install import install_bundled_addon_apks as install_bundled_addon_plugins

                def ui_addon(msg: str) -> None:
                    progress.set_status(msg)

                progress.set_status("Installing bundled add-on plugins…")
                install_bundled_addon_plugins(
                    ser_resolved, log, ui_addon, install_apk, plugin_root=BUNDLED_PLUGIN_DIR
                )
        except Exception as exc:
            log(f"Warning: bundled add-on plugin install failed — {exc}")

        progress.set_status("Complete")
        progress.completion_log_summary = "Download complete." + dted_note
        progress.completion_message = DOWNLOADER_NEXT_SQLITE_DIALOG_TEXT + dted_note

    except DownloadCancelled:
        log("Download cancelled by user.")
        progress.user_cancelled = True
        progress.set_speed_eta(0.0, None)
        progress.set_status("Cancelled")
    except Exception as e:
        tb = traceback.format_exc()
        log(f"ERROR: {e}")
        log(tb)
        progress.error_message = f"Error:\n{e}\n\nLog file:\n{LOGGER.log_file}"


def pump_gui_logs(window: ProgressWindow) -> None:
    try:
        while True:
            line = LOGGER.gui_queue.get_nowait()
            if not getattr(window, "closed", False):
                window.append_log(line)
    except queue.Empty:
        pass

    window._flush_pending_ui()

    if not getattr(window, "closed", False):
        if getattr(window, "user_cancelled", False):
            window.closed = True
            try:
                window.destroy()
            except Exception:
                pass
            os._exit(0)

        if getattr(window, "device_ready_prompt", None):
            prompt_text = window.device_ready_prompt
            evt = getattr(window, "device_ready_event", None)
            window.device_ready_prompt = None
            ensure_window_stacking(window)
            messagebox.showinfo(APP_TITLE, prompt_text, parent=window)
            if evt is not None:
                evt.set()

        if getattr(window, "completion_message", None):
            msg = window.completion_message
            window.completion_message = None
            summary = getattr(window, "completion_log_summary", None)
            if summary is not None:
                window.completion_log_summary = None
                log(summary.replace("\n\n", " | "))
            show_downloader_session_exit_dialog(window, body=msg)
            try:
                window.closed = True
                window.destroy()

                if getattr(sys, "frozen", False):
                    if hasattr(sys, "_MEIPASS"):
                        os.environ["TCL_LIBRARY"] = str(Path(sys._MEIPASS) / "_tcl_data")
                        os.environ["TK_LIBRARY"] = str(Path(sys._MEIPASS) / "_tk_data")
                    import atak_imagery_sqlite_builder_finalbuild as sqlite_builder
                    sqlite_builder.main([])
                    os._exit(0)
                else:
                    next_script = Path(__file__).resolve().parent / "atak_imagery_sqlite_builder_finalbuild.py"
                    subprocess.Popen([sys.executable, str(next_script)])
                    os._exit(0)
            except Exception as exc:
                ensure_window_stacking(window)
                messagebox.showerror(
                    APP_TITLE, f"Failed to launch SQLite builder:\n{exc}", parent=window
                )
                sys.exit(1)

        if getattr(window, "error_message", None):
            msg = window.error_message
            window.error_message = None
            ensure_window_stacking(window)
            messagebox.showerror(APP_TITLE, msg, parent=window)
            window.closed = True
            window.destroy()
            return

        window.after(150, pump_gui_logs, window)


def main() -> None:
    run_startup_git_update_check(app_title=APP_TITLE, script_path=Path(__file__).resolve())
    run_startup_release_update_check(app_title=APP_TITLE, script_path=Path(__file__).resolve())
    log(f"Log file: {LOGGER.log_file}")
    zoom_payload = read_zoom_estimates_file()
    zoom_estimates = zoom_payload["states"]
    avg_tile_bytes_map = avg_tile_bytes_by_zoom(zoom_payload)

    if is_launched_from_device_installer():
        log("Launched from Device Installer — skipping standalone USB/adb intro.")
    else:
        log("Standalone launch — skipping upfront device check (device connected after download).")

    while True:
        scope_dlg = DownloadScopeDialog()
        scope_dlg.mainloop()
        if not scope_dlg.accepted:
            log("Cancelled at download scope.")
            return

        raw_path: Optional[Path] = None
        if scope_dlg.raw_imagery_path:
            candidate = Path(scope_dlg.raw_imagery_path).expanduser()
            if not candidate.is_dir():
                root = tk.Tk()
                try:
                    root.option_add("*cursor", "arrow")
                except tk.TclError:
                    pass
                root.withdraw()
                root.update_idletasks()
                ensure_window_stacking(root)
                messagebox.showwarning(
                    APP_TITLE,
                    f"Raw imagery folder is not a directory:\n{candidate}",
                    parent=root,
                )
                try:
                    root.destroy()
                except tk.TclError:
                    pass
                continue
            raw_path = candidate

        dted_path: Optional[Path] = None
        if scope_dlg.local_dted_path:
            dc = Path(scope_dlg.local_dted_path).expanduser()
            if not dc.is_dir():
                root = tk.Tk()
                try:
                    root.option_add("*cursor", "arrow")
                except tk.TclError:
                    pass
                root.withdraw()
                root.update_idletasks()
                ensure_window_stacking(root)
                messagebox.showwarning(
                    APP_TITLE,
                    f"Local DTED folder is not a directory:\n{dc}",
                    parent=root,
                )
                try:
                    root.destroy()
                except tk.TclError:
                    pass
                continue
            dted_path = dc

        if scope_dlg.download_scope == "state":
            while True:
                selector = StateSelectionDialog()
                selector.mainloop()
                if not selector.result_states:
                    log("Cancelled at state selection.")
                    return

                while True:
                    zoom_dialog = ZoomDialog(
                        selector.result_states,
                        zoom_estimates,
                        download_scope="state",
                    )
                    zoom_dialog.mainloop()
                    if zoom_dialog.go_back:
                        log("Back from zoom to state selection.")
                        break

                    selected_zooms = zoom_dialog.result
                    if not selected_zooms:
                        log("Cancelled at zoom selection.")
                        return

                    est_total_bytes = 0
                    est_total_tiles = 0
                    for z in selected_zooms:
                        for state_name in selector.result_states:
                            info = zoom_estimates.get(state_name, {}).get(str(z), {})
                            est_total_bytes += int(info.get("estimated_bytes", 0))
                            est_total_tiles += int(info.get("estimated_tiles", 0))

                    if not show_summary_confirm(
                        selector.result_states,
                        selected_zooms,
                        est_total_bytes,
                        est_total_tiles,
                        download_scope="state",
                    ):
                        log("Summary declined. Returning to state selection.")
                        continue

                    output_folder = ask_output_parent()
                    if not output_folder:
                        log("Cancelled at output folder prompt.")
                        return

                    # Pre-load state boundaries on the main thread to avoid a
                    # large GC-triggering allocation burst in the download thread.
                    _geo = bundled_state_geojson_path()
                    _preloaded = load_states(_geo)

                    progress = ProgressWindow(LOGGER.log_file)
                    pump_gui_logs(progress)
                    worker = threading.Thread(
                        target=run_download,
                        args=(
                            list(selected_zooms),
                            list(selector.result_states),
                            selector.result_mode,
                            Path(output_folder),
                            progress,
                        ),
                        kwargs={
                            "raw_imagery_root": raw_path,
                            "local_dted_root": dted_path,
                            "radius_center": None,
                            "radius_miles": None,
                            "preloaded_states": _preloaded,
                        },
                        daemon=True,
                    )
                    worker.start()
                    progress.mainloop()
                    return
        else:
            while True:
                rd = RadiusCenterDialog()
                rd.mainloop()
                if not rd.accepted:
                    log("Returned to download scope from radius / center dialog.")
                    break

                while True:
                    _radius_tiles: Dict[int, int] = {}

                    def _precompute_radius_tiles() -> None:
                        for z in range(10, 17):
                            tiles = compute_tiles_for_radius(rd.center_lat, rd.center_lon, rd.radius_miles, z)
                            _radius_tiles[z] = len(tiles)

                    run_with_busy_dialog("Calculating coverage…", _precompute_radius_tiles)

                    zoom_dialog = ZoomDialog(
                        [],
                        zoom_estimates,
                        download_scope="radius",
                        radius_center=(rd.center_lat, rd.center_lon),
                        radius_miles=rd.radius_miles,
                        radius_imagery_folder=scope_dlg.radius_region_folder,
                        avg_tile_bytes_by_zoom=avg_tile_bytes_map,
                        precomputed_radius_tiles=_radius_tiles,
                    )
                    zoom_dialog.mainloop()
                    if zoom_dialog.go_back:
                        log("Back from zoom to radius center.")
                        break

                    selected_zooms = zoom_dialog.result
                    if not selected_zooms:
                        log("Cancelled at zoom selection.")
                        return

                    est_total_bytes = 0
                    est_total_tiles = 0
                    for z in selected_zooms:
                        n = _radius_tiles.get(z, 0)
                        est_total_bytes += n * int(avg_tile_bytes_map.get(z, 25000))
                        est_total_tiles += n

                    radius_summary = (
                        f"Fixed-radius download:\n"
                        f"Center ({rd.center_lat:.5f}, {rd.center_lon:.5f}) decimal deg, "
                        f"radius {rd.radius_miles:g} mi\n"
                        f"Imagery folder: {scope_dlg.radius_region_folder}"
                    )
                    if not show_summary_confirm(
                        [],
                        selected_zooms,
                        est_total_bytes,
                        est_total_tiles,
                        download_scope="radius",
                        radius_summary=radius_summary,
                    ):
                        log("Summary declined. Returning to radius center.")
                        continue

                    output_folder = ask_output_parent()
                    if not output_folder:
                        log("Cancelled at output folder prompt.")
                        return

                    progress = ProgressWindow(LOGGER.log_file)
                    pump_gui_logs(progress)
                    worker = threading.Thread(
                        target=run_download,
                        args=(list(selected_zooms), [], "radius", Path(output_folder), progress),
                        kwargs={
                            "raw_imagery_root": raw_path,
                            "local_dted_root": dted_path,
                            "radius_center": (rd.center_lat, rd.center_lon),
                            "radius_miles": rd.radius_miles,
                            "radius_region_folder": scope_dlg.radius_region_folder,
                        },
                        daemon=True,
                    )
                    worker.start()
                    progress.mainloop()
                    return


if __name__ == "__main__":
    multiprocessing.freeze_support()
    log("Starting ATAK Imagery Downloader")
    log(f"Python: {sys.version}")
    log(f"Working directory: {Path.cwd()}")
    log(f"Script directory: {Path(__file__).resolve().parent}")
    main()
