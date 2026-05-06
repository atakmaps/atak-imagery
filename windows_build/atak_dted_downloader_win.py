#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import queue
import re
import shutil
import subprocess
import sys
import os
import tempfile
import threading
import time
import traceback
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

import requests
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog

from tk_window_scaling import (
    apply_fixed_size_window,
    apply_resizable_window,
    ensure_window_stacking,
)

APP_TITLE = "ATAK DTED Downloader"
BASE_URL = "http://31.220.30.74/dted"
USER_AGENT = "ATAK-DTED-Downloader/1.0"

_DTED_W_DOWNLOAD = 0.55
_DTED_W_EXTRACT = 0.28
_DTED_W_BUILD = 0.12
_DTED_W_CLEANUP = 0.05
if getattr(sys, "frozen", False):
    RUNTIME_STATE_DIR = Path(sys.executable).resolve().parent
else:
    RUNTIME_STATE_DIR = Path(__file__).resolve().parent

LAST_IMAGERY_ROOT_FILE = RUNTIME_STATE_DIR / ".last_imagery_root.txt"
SKIP_STANDALONE_DTED_AFTER_SQLITE = RUNTIME_STATE_DIR / ".skip_standalone_dted_after_sqlite"


_DEVICE_DTED_MANIFEST_FILENAME = ".dted_states.json"


def _adb_executable() -> str:
    import shutil as _shutil
    return _shutil.which("adb") or "adb"


def _adb_base_cmd() -> List[str]:
    cmd = [_adb_executable()]
    serial = os.environ.get("ANDROID_SERIAL", "").strip()
    if serial:
        cmd.extend(["-s", serial])
    return cmd


def query_device_installed_dted_states(log_fn: Optional[Callable[[str], None]] = None) -> Optional[Set[str]]:
    """
    Read the DTED state manifest from the connected device via ``adb shell cat``.
    Returns installed state names, or ``None`` if not available.
    """
    _log = log_fn or log
    root = (os.environ.get("ATAK_DEVICE_FILES_ROOT") or "/sdcard/atak").strip() or "/sdcard/atak"
    remote_manifest = f"{root.rstrip('/')}/DTED/{_DEVICE_DTED_MANIFEST_FILENAME}"
    cmd = _adb_base_cmd() + ["shell", "cat", remote_manifest]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            _log("DTED device check: no manifest on device (first run or device not connected).")
            return None
        raw = proc.stdout.strip()
        if not raw:
            return None
        data = json.loads(raw)
        states: List[str] = data.get("states", [])
        result: Set[str] = {s for s in states if isinstance(s, str) and s.strip()}
        _log(f"DTED device check: manifest found — {len(result)} state(s) already installed: "
             f"{', '.join(sorted(result)) if result else '(none)'}")
        return result
    except Exception as exc:
        _log(f"DTED device check: could not query device ({exc}); will download all required states.")
        return None


def push_device_dted_manifest(all_installed_states: Set[str], log_fn: Optional[Callable[[str], None]] = None) -> None:
    """Push the merged DTED state manifest to the device."""
    _log = log_fn or log
    root = (os.environ.get("ATAK_DEVICE_FILES_ROOT") or "/sdcard/atak").strip() or "/sdcard/atak"
    remote_manifest = f"{root.rstrip('/')}/DTED/{_DEVICE_DTED_MANIFEST_FILENAME}"
    payload = {
        "schema_version": 1,
        "states": sorted(all_installed_states),
        "last_updated": datetime.now().isoformat(timespec="seconds"),
    }
    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix="atak_dted_manifest_",
            delete=False, encoding="utf-8",
        ) as tf:
            json.dump(payload, tf, indent=2)
            tmp_path = tf.name
        cmd = _adb_base_cmd() + ["push", tmp_path, remote_manifest]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            _log(f"DTED manifest push failed (non-fatal): {(proc.stderr or proc.stdout or '').strip()}")
        else:
            _log(f"DTED device manifest updated: {len(all_installed_states)} state(s) recorded.")
    except Exception as exc:
        _log(f"DTED manifest push error (non-fatal): {exc}")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def mark_standalone_dted_skip() -> None:
    SKIP_STANDALONE_DTED_AFTER_SQLITE.write_text(
        datetime.now().isoformat(timespec="seconds"), encoding="utf-8"
    )


def consume_standalone_dted_skip() -> bool:
    if not SKIP_STANDALONE_DTED_AFTER_SQLITE.is_file():
        return False
    try:
        SKIP_STANDALONE_DTED_AFTER_SQLITE.unlink()
    except OSError:
        pass
    return True


def bring_window_forward(win: tk.Misc, *, persistent_topmost: bool = False) -> None:
    ensure_window_stacking(win, persistent_topmost=persistent_topmost)


def _modal_grab(dlg: tk.Toplevel, parent: tk.Misc) -> None:
    """``grab_set`` only works after the dialog is viewable; parent must not be withdrawn."""
    try:
        parent.winfo_toplevel().deiconify()
    except tk.TclError:
        pass
    try:
        dlg.deiconify()
    except tk.TclError:
        pass
    try:
        parent.winfo_toplevel().lift()
    except tk.TclError:
        pass
    try:
        dlg.update_idletasks()
        dlg.lift()
        dlg.focus_force()
        dlg.update_idletasks()
        dlg.grab_set()
    except tk.TclError:
        pass


def peek_standalone_dted_skip_pending() -> bool:
    return SKIP_STANDALONE_DTED_AFTER_SQLITE.is_file()


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
    "WI": "Wisconsin", "WY": "Wyoming",
}

CONTIGUOUS_48 = sorted(
    [name for name in STATE_ABBR_TO_NAME.values() if name not in ("Alaska", "Hawaii")]
)


class Logger:
    def __init__(self) -> None:
        self.script_dir = RUNTIME_STATE_DIR
        self.log_dir = self.script_dir / "logs"
        self.log_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = self.log_dir / f"atak_dted_downloader_{ts}.log"
        self._fh = open(self.log_file, "a", encoding="utf-8", buffering=1)
        self.gui_queue: "queue.Queue[str]" = queue.Queue()

    def write(self, message: str) -> None:
        if not message.endswith("\n"):
            message += "\n"
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
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


def find_latest_dted_zip(upload_dir: Path) -> Optional[Path]:
    if not upload_dir.is_dir():
        return None
    zips = list(upload_dir.glob("dted2_*.zip"))
    if not zips:
        return None
    return max(zips, key=lambda p: p.stat().st_mtime)


def resolve_dted_zip_for_upload_dir(upload_dir: Path) -> Optional[Path]:
    z = find_latest_dted_zip(upload_dir)
    if z is not None:
        return z
    parent = upload_dir.parent
    if not parent.is_dir():
        return None
    all_z = list(parent.glob("ATAK_Upload_*/dted2_*.zip"))
    if not all_z:
        return None
    return max(all_z, key=lambda p: p.stat().st_mtime)


def ask_delete_raw_imagery_win(parent: tk.Tk, imagery_root: Path, *, dted_complete: bool) -> bool:
    result: Dict[str, bool] = {"delete": False}

    dlg = tk.Toplevel(parent)
    dlg.title(APP_TITLE)
    dlg.configure(cursor="arrow")
    dlg.transient(parent)
    dlg.resizable(False, False)

    if dted_complete:
        lead = (
            "Imagery SQLite cache and DTED package are ready.\n\n"
            "ATAK only needs the final SQLite and DTED outputs on your device.\n\n"
        )
    else:
        lead = (
            "The DTED step finished without packages for your selection.\n\n"
            "You can still delete raw downloaded imagery from this computer to free space.\n\n"
        )
    text = lead + f"Delete raw downloaded imagery now?\n\n{imagery_root}"
    tk.Label(dlg, text=text, justify="left", wraplength=520).pack(padx=16, pady=(16, 8))

    btn_row = tk.Frame(dlg)
    btn_row.pack(pady=(8, 16))

    def on_yes() -> None:
        result["delete"] = True
        dlg.destroy()

    def on_no() -> None:
        result["delete"] = False
        dlg.destroy()

    tk.Button(btn_row, text="Yes", width=10, command=on_yes).pack(side="left", padx=6)
    tk.Button(btn_row, text="No", width=10, command=on_no).pack(side="left", padx=6)

    dlg.update_idletasks()
    bring_window_forward(parent, persistent_topmost=False)
    bring_window_forward(dlg, persistent_topmost=True)
    _modal_grab(dlg, parent)
    parent.wait_window(dlg)
    return result["delete"]


def show_exit_ready_dialog_win(parent: tk.Tk) -> None:
    dlg = tk.Toplevel(parent)
    dlg.title(APP_TITLE)
    dlg.configure(cursor="arrow")
    dlg.transient(parent)
    dlg.resizable(False, False)
    tk.Label(
        dlg,
        text=(
            "ATAK build outputs are ready in the upload folder.\n"
            "Copy files to your Android device if needed, then relaunch ATAK.\n\n"
            "You may now exit the program."
        ),
        justify="center",
    ).pack(padx=24, pady=(20, 12))

    def on_exit() -> None:
        dlg.destroy()

    tk.Button(dlg, text="Exit", width=12, command=on_exit).pack(pady=(0, 20))
    dlg.update_idletasks()
    bring_window_forward(parent, persistent_topmost=False)
    bring_window_forward(dlg, persistent_topmost=True)
    _modal_grab(dlg, parent)
    parent.wait_window(dlg)


def finalize_imagery_cleanup_and_exit_win(
    parent: tk.Tk,
    upload_dir: Optional[Path],
    imagery_root: Optional[Path],
    *,
    dted_complete: bool,
) -> None:
    """Delete/keep raw imagery, reveal upload folder, then Exit dialog (no adb on this build)."""
    if imagery_root and imagery_root.is_dir() and LAST_IMAGERY_ROOT_FILE.is_file():
        try:
            if hasattr(parent, "set_status"):
                parent.set_status("Choose whether to delete raw downloaded imagery on this computer.")
                parent.update_idletasks()
            bring_window_forward(parent, persistent_topmost=False)
            cleanup = ask_delete_raw_imagery_win(parent, imagery_root, dted_complete=dted_complete)
            if cleanup:
                shutil.rmtree(imagery_root)
                log(f"Deleted raw imagery folder: {imagery_root}")
                try:
                    LAST_IMAGERY_ROOT_FILE.unlink()
                    log(f"Deleted saved imagery path file: {LAST_IMAGERY_ROOT_FILE}")
                except OSError as cleanup_exc:
                    log(f"Warning: saved imagery path file removal failed: {cleanup_exc}")
            else:
                log(f"Raw imagery retained: {imagery_root}")
        except Exception as cleanup_exc:
            log(f"Warning: raw imagery cleanup failed: {cleanup_exc}")
            try:
                bring_window_forward(parent, persistent_topmost=False)
                messagebox.showwarning(APP_TITLE, f"Raw imagery cleanup failed:\n{cleanup_exc}", parent=parent)
            except Exception:
                pass

    if upload_dir and upload_dir.exists():
        try:
            os.startfile(str(upload_dir))
        except Exception as open_exc:
            log(f"WARNING: failed to open upload folder: {open_exc}")

    show_exit_ready_dialog_win(parent)


def clean_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned or "package"


def derive_package_folder_name(package_name: str) -> str:
    return f"atak_{clean_name(package_name)}"


def state_url(state_name: str) -> str:
    return f"{BASE_URL}/{state_name}/{state_name}.zip"


def ask_output_parent() -> str:
    root = tk.Tk()
    root.configure(cursor="arrow")
    root.withdraw()
    folder = filedialog.askdirectory(title="Select output parent folder")
    root.destroy()
    return folder or ""


def ask_package_name(default_name: str) -> str:
    root = tk.Tk()
    root.configure(cursor="arrow")
    root.withdraw()
    value = simpledialog.askstring(
        APP_TITLE,
        "Enter package name.\nThis creates an ATAK working folder named atak_<PackageName>.",
        initialvalue=default_name,
        parent=root,
    )
    root.destroy()
    return (value or "").strip()


class StateSelectionDialog(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_TITLE} - Select States")
        self.resizable(False, False)
        self.configure(cursor="arrow")

        self.result_mode = ""
        self.result_states: List[str] = []

        frame = tk.Frame(self, padx=12, pady=12)
        frame.pack(fill="both", expand=True)

        tk.Label(
            frame,
            text="Select DTED package(s) to download:",
            font=("Arial", 11, "bold")
        ).pack(anchor="w", pady=(0, 8))

        note = (
            "Choose one or more specific states, or use the Contiguous 48 shortcut.\n"
            "The downloader will fetch state ZIPs, extract them, and build one final dted2.zip."
        )
        tk.Label(frame, text=note, justify="left").pack(anchor="w", pady=(0, 10))

        top_btns = tk.Frame(frame)
        top_btns.pack(fill="x", pady=(0, 8))

        tk.Button(top_btns, text="Contiguous 48", width=16, command=self.select_contiguous_48).pack(side="left", padx=(0, 6))
        tk.Button(top_btns, text="Select All", width=12, command=self.select_all).pack(side="left", padx=(0, 6))
        tk.Button(top_btns, text="Clear All", width=12, command=self.clear_all).pack(side="left", padx=(0, 6))

        list_frame = tk.Frame(frame)
        list_frame.pack(fill="both", expand=True)

        canvas = tk.Canvas(list_frame, highlightthickness=1, highlightbackground="gray70")
        scrollbar = tk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas)

        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(cursor="arrow")
        scrollbar.pack(side="right", fill="y")

        self.vars: Dict[str, tk.BooleanVar] = {}
        for state_name in sorted(STATE_ABBR_TO_NAME.values()):
            var = tk.BooleanVar(value=False)
            self.vars[state_name] = var
            cb = tk.Checkbutton(inner, text=state_name, variable=var, anchor="w", justify="left")
            cb.pack(anchor="w")

        btns = tk.Frame(frame)
        btns.pack(fill="x", pady=(12, 0))
        tk.Button(btns, text="Cancel", width=12, command=self.cancel).pack(side="right", padx=(6, 0))
        tk.Button(btns, text="OK", width=12, command=self.submit).pack(side="right")

        apply_fixed_size_window(self, 620, 700)
        self.protocol("WM_DELETE_WINDOW", self.cancel)

    def select_all(self) -> None:
        self.result_mode = "all"
        for var in self.vars.values():
            var.set(True)

    def clear_all(self) -> None:
        self.result_mode = ""
        for var in self.vars.values():
            var.set(False)

    def select_contiguous_48(self) -> None:
        self.result_mode = "contiguous48"
        for state_name, var in self.vars.items():
            var.set(state_name in CONTIGUOUS_48)

    def submit(self) -> None:
        selected = sorted([state for state, var in self.vars.items() if var.get()])
        if not selected:
            messagebox.showwarning(APP_TITLE, "Select at least one state, or use Contiguous 48.")
            return
        self.result_states = selected
        if self.result_mode not in ("contiguous48", "all"):
            self.result_mode = "specific"
        self.destroy()

    def cancel(self) -> None:
        self.result_mode = ""
        self.result_states = []
        self.destroy()


class ProgressWindow(tk.Tk):
    def __init__(self, log_path: Path):
        super().__init__()
        self.title(f"{APP_TITLE} - Progress")
        self.configure(cursor="arrow")

        top = tk.Frame(self, padx=10, pady=10)
        top.pack(fill="x")

        self.status_var = tk.StringVar(value="Initializing...")
        self.counter_var = tk.StringVar(value="0 / 0")
        self.detail_var = tk.StringVar(value=f"Log: {log_path}")

        tk.Label(top, textvariable=self.status_var, font=("Arial", 11, "bold")).pack(anchor="w")
        tk.Label(top, textvariable=self.counter_var).pack(anchor="w", pady=(4, 0))
        tk.Label(top, textvariable=self.detail_var, fg="gray30").pack(anchor="w", pady=(4, 8))

        self.canvas = tk.Canvas(top, height=24, bg="white", highlightthickness=1, highlightbackground="gray70")
        self.canvas.configure(cursor="arrow")
        self.canvas.pack(fill="x")
        self.bar = self.canvas.create_rectangle(0, 0, 0, 24, fill="#4a90e2", width=0)
        self.bar_text = self.canvas.create_text(5, 12, anchor="w", text="0%")

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
        self.error_message = None
        self.upload_dir: Optional[Path] = None
        self.final_dted_zip: Optional[Path] = None

    def append_log(self, line: str) -> None:
        self.text.insert("end", line)
        self.text.see("end")
        self.update_idletasks()

    def set_progress(self, completed: int, total: int) -> None:
        total = max(total, 1)
        pct = int((completed / total) * 100)
        self.counter_var.set(f"{completed} / {total}")
        width = max(self.canvas.winfo_width(), 1)
        fill_w = int(width * (completed / total))
        self.canvas.coords(self.bar, 0, 0, fill_w, 24)
        self.canvas.coords(self.bar_text, 8, 12)
        self.canvas.itemconfig(self.bar_text, text=f"{pct}%")

    def set_progress_fraction(self, frac: float, counter_detail: Optional[str] = None) -> None:
        frac = max(0.0, min(1.0, float(frac)))
        pct = int(frac * 100)
        if counter_detail is not None:
            self.counter_var.set(counter_detail)
        else:
            self.counter_var.set(f"{pct}%")
        self.update_idletasks()
        width = max(int(self.canvas.winfo_width()), 1)
        fill_w = int(width * frac)
        self.canvas.coords(self.bar, 0, 0, fill_w, 24)
        self.canvas.coords(self.bar_text, 8, 12)
        self.canvas.itemconfig(self.bar_text, text=f"{pct}%")

    def set_status(self, text: str) -> None:
        self.status_var.set(text)
        self.update_idletasks()

    def set_stat(self, key: str, value: int) -> None:
        label = key.capitalize()
        self.stats_vars[key].set(f"{label}: {value}")
        self.update_idletasks()

    def on_close(self) -> None:
        status = self.status_var.get().strip().lower()
        is_done = status in {"complete", "completed", "done", "finished"}
        if is_done:
            self.closed = True
            self.destroy()
            return
        if messagebox.askyesno(APP_TITLE, "Close the progress window? The process will keep running in the background."):
            self.closed = True
            self.destroy()


def _dted_download_slice_progress(
    overall: Callable[[float], None],
    state_index: int,
    n_states: int,
    bytes_read: int,
    total_bytes: int,
) -> None:
    n = max(n_states, 1)
    base = _DTED_W_DOWNLOAD * (state_index / n)
    span = _DTED_W_DOWNLOAD / n
    if total_bytes > 0:
        overall(base + span * (bytes_read / total_bytes))
    else:
        sub = 1.0 - math.exp(-bytes_read / (8 * 1024 * 1024))
        overall(base + span * min(sub, 0.97))


def remote_file_size(session: requests.Session, url: str) -> int:
    try:
        resp = session.head(url, timeout=30, allow_redirects=True)
        if resp.status_code == 404:
            return -1
        resp.raise_for_status()
        value = resp.headers.get("Content-Length", "").strip()
        return int(value) if value.isdigit() else 0
    except requests.RequestException:
        return 0


def download_file(
    session: requests.Session,
    url: str,
    out_path: Path,
    log_fn: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> str:
    _log = log_fn or log
    size_hint = remote_file_size(session, url)
    if size_hint < 0:
        return "missing"

    if out_path.exists() and size_hint > 0 and out_path.stat().st_size == size_hint:
        return "existing"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".part")

    last_emit = 0.0

    def _emit_progress(read: int, total_known: int, *, force: bool = False) -> None:
        nonlocal last_emit
        if not progress_cb:
            return
        now = time.monotonic()
        complete = total_known > 0 and read >= total_known
        if force or complete or now - last_emit >= 0.12:
            last_emit = now
            progress_cb(read, total_known)

    try:
        with session.get(url, timeout=300, stream=True) as r:
            if r.status_code == 404:
                return "missing"
            r.raise_for_status()
            cl = r.headers.get("Content-Length", "").strip()
            total_bytes = int(cl) if cl.isdigit() else 0
            if total_bytes <= 0:
                total_bytes = size_hint if size_hint > 0 else -1
            read = 0
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
                        read += len(chunk)
                        _emit_progress(read, total_bytes)
            _emit_progress(read, total_bytes if total_bytes > 0 else read, force=True)
        tmp_path.replace(out_path)
        return "downloaded"
    except Exception as exc:
        _log(f"ERROR downloading {url}: {exc}")
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        return "failed"


def extract_state_zip(
    zip_path: Path,
    extract_root: Path,
    log_fn: Optional[Callable[[str], None]] = None,
) -> None:
    _log = log_fn or log
    state_name = zip_path.stem
    state_extract_dir = extract_root / state_name
    if state_extract_dir.exists():
        shutil.rmtree(state_extract_dir)
    state_extract_dir.mkdir(parents=True, exist_ok=True)

    _log(f"Extracting {zip_path.name} -> {state_extract_dir}")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(state_extract_dir)


def build_final_dted_zip(
    extract_root: Path,
    final_zip_path: Path,
    log_fn: Optional[Callable[[str], None]] = None,
    on_packed: Optional[Callable[[int, int], None]] = None,
) -> None:
    _log = log_fn or log
    if final_zip_path.exists():
        final_zip_path.unlink()

    entries: List[Tuple[Path, str]] = []
    for state_dir in sorted(extract_root.iterdir(), key=lambda p: p.name.lower()):
        if not state_dir.is_dir():
            continue
        for item in sorted(state_dir.rglob("*")):
            if not item.is_file():
                continue
            arcname = item.relative_to(state_dir)
            entries.append((item, arcname.as_posix()))

    _log(f"Building final ATAK zip: {final_zip_path} ({len(entries)} files)")

    nf = len(entries)
    with zipfile.ZipFile(final_zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=5) as zf:
        for idx, (item, arc_posix) in enumerate(entries, start=1):
            zf.write(item, arc_posix)
            _log(f"ADD {arc_posix}")
            if on_packed:
                on_packed(idx, nf)

    _log(f"Final zip ready: {final_zip_path}")


def run_dted_inline_for_states(
    selected_states: List[str],
    upload_dir: Path,
    *,
    log_sink: Callable[[str], None],
    progress: object,
    local_state_zip_root: Optional[Path] = None,
) -> Optional[Path]:
    import tempfile

    if not selected_states:
        log_sink("DTED: no states in selection; skipping.")
        return None

    stats = {"downloaded": 0, "existing": 0, "failed": 0, "missing": 0}
    for key in stats:
        progress.set_stat(key, 0)

    ts = datetime.now().strftime("%H%M%S")
    temp_root = Path(tempfile.mkdtemp(prefix="atak_dted_inline_"))
    downloads_dir = temp_root / "_state_zips"
    extract_root = temp_root / "_extracted_states"
    final_zip_path = upload_dir / f"dted2_{ts}.zip"
    upload_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir.mkdir(parents=True, exist_ok=True)
    extract_root.mkdir(parents=True, exist_ok=True)

    log_sink(f"DTED: using upload folder {upload_dir}")
    log_sink(f"DTED: temp work folder {temp_root}")
    if local_state_zip_root is not None:
        log_sink(f"DTED: local state ZIP tree (copy first): {local_state_zip_root}")
    log_sink(f"DTED: server base URL {BASE_URL}")

    plan: List[Tuple[str, str, Path]] = []
    for state_name in selected_states:
        url = state_url(state_name)
        out_path = downloads_dir / state_name / f"{state_name}.zip"
        plan.append((state_name, url, out_path))

    total = len(plan)
    n_states = max(total, 1)

    def set_overall(frac: float, detail: Optional[str] = None) -> None:
        fn = getattr(progress, "set_progress_fraction", None)
        if callable(fn):
            fn(frac, counter_detail=detail)
        else:
            progress.set_progress(int(frac * 100), 100)

    set_overall(0.0, f"0 / {total} states")
    progress.set_status("DTED: downloading state ZIPs…")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    completed = 0
    try:
        for i, (state_name, url, out_path) in enumerate(plan):
            progress.wait_if_paused()

            idx = i

            def dl_progress(read: int, tot: int, ix: int = idx) -> None:
                def apply_fr(fr: float) -> None:
                    set_overall(fr, f"{int(fr * 100)}% · state {ix + 1} / {total}")
                _dted_download_slice_progress(apply_fr, ix, total, read, tot)

            local_zip: Optional[Path] = None
            if local_state_zip_root is not None:
                cand = local_state_zip_root / state_name / f"{state_name}.zip"
                if cand.is_file():
                    local_zip = cand

            if local_zip is not None:
                progress.set_status(f"DTED: copying {state_name} from local tree…")
                log_sink(f"DTED: local copy {local_zip}")
                try:
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(local_zip, out_path)
                    result = "downloaded"
                except Exception as exc:
                    log_sink(f"DTED: local copy failed ({exc}); trying network")
                    progress.set_status(f"DTED: downloading {state_name}…")
                    log_sink(f"DTED: GET {url}")
                    result = download_file(session, url, out_path, log_fn=log_sink, progress_cb=dl_progress)
            else:
                progress.set_status(f"DTED: downloading {state_name}…")
                log_sink(f"DTED: GET {url}")
                result = download_file(session, url, out_path, log_fn=log_sink, progress_cb=dl_progress)
            stats[result] += 1
            completed += 1
            set_overall(_DTED_W_DOWNLOAD * ((idx + 1) / n_states), f"{int(_DTED_W_DOWNLOAD * (idx + 1) / n_states * 100)}% · state {idx + 1} / {total}")
            for key, value in stats.items():
                progress.set_stat(key, value)
            log_sink(f"DTED: {state_name}: {result} ({completed}/{total})")

        if stats["failed"] > 0:
            raise RuntimeError(
                f"DTED download incomplete. failed={stats['failed']} missing={stats['missing']}."
            )

        if stats["missing"] > 0:
            log_sink(f"DTED: WARNING missing ZIPs: {stats['missing']}")

        extract_plan = [(sn, op) for sn, _, op in plan if op.exists()]
        m_ext = max(len(extract_plan), 1)

        progress.set_status("DTED: extracting state ZIPs…")
        extracted_any = False
        for j, (state_name, out_path) in enumerate(extract_plan):
            progress.wait_if_paused()
            progress.set_status(f"DTED: extracting {state_name}…")
            extract_state_zip(out_path, extract_root, log_fn=log_sink)
            extracted_any = True
            fr = _DTED_W_DOWNLOAD + _DTED_W_EXTRACT * ((j + 1) / m_ext)
            set_overall(fr, f"{int(fr * 100)}% · extract {j + 1} / {len(extract_plan)}")

        if not extracted_any:
            log_sink("DTED: no packages extracted; skipping final zip.")
            return None

        progress.wait_if_paused()
        base_b = _DTED_W_DOWNLOAD + _DTED_W_EXTRACT

        def on_packed(cur: int, tot: int) -> None:
            fr = base_b + _DTED_W_BUILD * (cur / max(tot, 1))
            set_overall(fr, f"{int(fr * 100)}% · packing {cur} / {tot}")

        progress.set_status("DTED: building dted2.zip…")
        build_final_dted_zip(extract_root, final_zip_path, log_fn=log_sink, on_packed=on_packed)
        set_overall(base_b + _DTED_W_BUILD, f"{int((base_b + _DTED_W_BUILD) * 100)}% · finishing…")

        # Write a sidecar listing which states are in this zip so the
        # push step can update the device manifest.
        sidecar_path = final_zip_path.with_suffix(".states.json")
        try:
            sidecar_path.write_text(
                json.dumps({"states": sorted(selected_states)}, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            log_sink(f"DTED: could not write states sidecar ({exc}); device manifest may be incomplete.")

        log_sink(f"DTED: complete -> {final_zip_path}")
        return final_zip_path
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def run_download(selected_states: List[str], mode: str, output_parent: Path, package_name: str, progress: ProgressWindow) -> None:
    stats = {"downloaded": 0, "existing": 0, "failed": 0, "missing": 0}

    try:
        from datetime import datetime
        import tempfile

        date_str = datetime.now().strftime("%Y%m%d")
        ts = datetime.now().strftime("%H%M%S")

        upload_dir = output_parent / f"ATAK_Upload_{date_str}"
        upload_dir.mkdir(parents=True, exist_ok=True)

        temp_root = Path(tempfile.mkdtemp(prefix="atak_dted_"))
        downloads_dir = temp_root / "_state_zips"
        extract_root = temp_root / "_extracted_states"
        final_zip_path = upload_dir / f"dted2_{ts}.zip"

        downloads_dir.mkdir(parents=True, exist_ok=True)
        extract_root.mkdir(parents=True, exist_ok=True)

        log(f"Using upload folder: {upload_dir}")
        log(f"Using temp work folder: {temp_root}")
        log(f"Server base URL: {BASE_URL}")

        plan = []
        for state_name in selected_states:
            url = state_url(state_name)
            out_path = downloads_dir / state_name / f"{state_name}.zip"
            plan.append((state_name, url, out_path))

        total = len(plan)
        n_states = max(total, 1)

        def set_overall(frac: float, detail: Optional[str] = None) -> None:
            progress.set_progress_fraction(frac, counter_detail=detail)

        set_overall(0.0, f"0 / {total} states")
        progress.set_status("Downloading state ZIPs…")

        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})

        completed = 0
        for i, (state_name, url, out_path) in enumerate(plan):
            progress.set_status(f"Downloading {state_name}…")
            log(f"GET {url}")

            idx = i

            def dl_progress(read: int, tot: int, ix: int = idx) -> None:
                def apply_fr(fr: float) -> None:
                    set_overall(fr, f"{int(fr * 100)}% · state {ix + 1} / {total}")
                _dted_download_slice_progress(apply_fr, ix, total, read, tot)

            result = download_file(session, url, out_path, progress_cb=dl_progress)
            stats[result] += 1
            completed += 1

            set_overall(_DTED_W_DOWNLOAD * ((idx + 1) / n_states), f"{int(_DTED_W_DOWNLOAD * (idx + 1) / n_states * 100)}% · state {idx + 1} / {total}")
            for key, value in stats.items():
                progress.set_stat(key, value)

            log(f"{state_name}: {result} -> {out_path} | progress {completed}/{total}")

        if stats["failed"] > 0:
            raise RuntimeError(
                f"Download incomplete. failed={stats['failed']} missing={stats['missing']}."
            )

        if stats["missing"] > 0:
            log(f"WARNING: continuing with missing tiles: {stats['missing']}")

        extract_plan = [(sn, op) for sn, _, op in plan if op.exists()]
        m_ext = max(len(extract_plan), 1)

        progress.set_status("Extracting state ZIPs…")
        extracted_any = False
        for j, (state_name, out_path) in enumerate(extract_plan):
            progress.set_status(f"Extracting {state_name}…")
            extract_state_zip(out_path, extract_root)
            extracted_any = True
            fr = _DTED_W_DOWNLOAD + _DTED_W_EXTRACT * ((j + 1) / m_ext)
            set_overall(fr, f"{int(fr * 100)}% · extract {j + 1} / {len(extract_plan)}")

        if not extracted_any:
            progress.completion_message = "No DTED packages were available for the selected state(s)."
            return

        base_b = _DTED_W_DOWNLOAD + _DTED_W_EXTRACT

        def on_packed(cur: int, tot: int) -> None:
            fr = base_b + _DTED_W_BUILD * (cur / max(tot, 1))
            set_overall(fr, f"{int(fr * 100)}% · packing {cur} / {tot}")

        progress.set_status("Building final dted2.zip…")
        build_final_dted_zip(extract_root, final_zip_path, on_packed=on_packed)
        set_overall(base_b + _DTED_W_BUILD, f"{int((base_b + _DTED_W_BUILD) * 100)}% · finishing…")

        progress.set_status("Cleaning temporary files…")
        set_overall(_DTED_W_DOWNLOAD + _DTED_W_EXTRACT + _DTED_W_BUILD + _DTED_W_CLEANUP * 0.45)
        shutil.rmtree(downloads_dir, ignore_errors=True)
        shutil.rmtree(extract_root, ignore_errors=True)
        set_overall(1.0, "100%")

        progress.set_status("Complete")
        log("DTED download complete")
        log(f"Mode: {mode}")
        log(f"Selected states: {', '.join(selected_states)}")
        log(f"Upload folder: {upload_dir}")
        log(f"Final ATAK zip: {final_zip_path}")
        log(f"Downloaded: {stats['downloaded']}")
        log(f"Existing: {stats['existing']}")
        log(f"Missing: {stats['missing']}")
        log(f"Failed: {stats['failed']}")

        progress.upload_dir = upload_dir
        progress.final_dted_zip = final_zip_path
        progress.completion_message = "DTED build complete."
        try:
            LOGGER.close()
        except Exception:
            pass
        return

    except Exception as exc:
        tb = traceback.format_exc()
        log(f"ERROR: {exc}")
        log(tb)
        progress.error_message = f"Error:\n{exc}\n\nLog file:\n{LOGGER.log_file}"


def pump_gui_logs(window: ProgressWindow) -> None:
    try:
        while True:
            line = LOGGER.gui_queue.get_nowait()
            if not getattr(window, "closed", False):
                window.append_log(line)
    except queue.Empty:
        pass

    if not getattr(window, "closed", False):
        if getattr(window, "completion_message", None):
            msg = window.completion_message
            window.completion_message = None
            is_full_dted = msg == "DTED build complete."

            imagery_root: Optional[Path] = None
            if LAST_IMAGERY_ROOT_FILE.exists():
                ir = Path(LAST_IMAGERY_ROOT_FILE.read_text(encoding="utf-8").strip())
                if ir.is_dir():
                    imagery_root = ir

            if is_full_dted:
                log(msg)
                ud = getattr(window, "upload_dir", None)
                if hasattr(window, "set_status"):
                    window.set_status("Build finished. Next: optional cleanup and upload folder.")
                    window.update_idletasks()
                bring_window_forward(window, persistent_topmost=False)
                finalize_imagery_cleanup_and_exit_win(window, ud, imagery_root, dted_complete=True)
            else:
                if hasattr(window, "set_status"):
                    window.set_status("Showing summary — next: optional raw imagery cleanup.")
                    window.update_idletasks()
                bring_window_forward(window, persistent_topmost=False)
                messagebox.showinfo(APP_TITLE, msg, parent=window)
                if imagery_root is not None:
                    try:
                        cleanup = ask_delete_raw_imagery_win(window, imagery_root, dted_complete=False)
                        if cleanup:
                            shutil.rmtree(imagery_root)
                            log(f"Deleted raw imagery folder: {imagery_root}")
                            try:
                                LAST_IMAGERY_ROOT_FILE.unlink()
                                log(f"Deleted saved imagery path file: {LAST_IMAGERY_ROOT_FILE}")
                            except Exception as cleanup_exc:
                                log(f"Warning: saved imagery path file removal failed: {cleanup_exc}")
                        else:
                            log(f"Raw imagery retained: {imagery_root}")
                    except Exception as cleanup_exc:
                        log(f"Warning: raw imagery cleanup failed: {cleanup_exc}")
                        try:
                            bring_window_forward(window, persistent_topmost=False)
                            messagebox.showwarning(APP_TITLE, f"Raw imagery cleanup failed:\n{cleanup_exc}", parent=window)
                        except Exception:
                            pass

                upload_dir = getattr(window, "upload_dir", None)
                if upload_dir and upload_dir.exists():
                    try:
                        os.startfile(str(upload_dir))
                    except Exception as open_exc:
                        log(f"WARNING: failed to open upload folder: {open_exc}")

            window.closed = True
            try:
                window.quit()
                window.destroy()
            except Exception:
                pass
            return

        if getattr(window, "error_message", None):
            msg = window.error_message
            window.error_message = None
            bring_window_forward(window, persistent_topmost=False)
            messagebox.showerror(APP_TITLE, msg, parent=window)
            window.closed = True
            window.destroy()
            return

        window.after(150, pump_gui_logs, window)


def main() -> None:
    log(f"Log file: {LOGGER.log_file}")
    log(f"Server base URL: {BASE_URL}")

    selector = StateSelectionDialog()
    selector.mainloop()

    if not selector.result_states:
        log("Cancelled at state selection.")
        return

    if selector.result_mode == "contiguous48":
        default_package_name = "Contiguous_48"
    elif selector.result_mode == "all":
        default_package_name = "All_States"
    elif len(selector.result_states) == 1:
        default_package_name = selector.result_states[0]
    else:
        default_package_name = "Selected_States"

    if not LAST_IMAGERY_ROOT_FILE.exists():
        log(f"Missing saved imagery path file: {LAST_IMAGERY_ROOT_FILE}")
        return

    imagery_root = Path(LAST_IMAGERY_ROOT_FILE.read_text(encoding="utf-8").strip())
    if not imagery_root.is_dir():
        log(f"Saved imagery folder not found: {imagery_root}")
        return

    output_folder = str(imagery_root.parent)

    package_name = "DTED"
    log(f"Using imagery parent folder for DTED output: {output_folder}")

    progress = ProgressWindow(LOGGER.log_file)
    pump_gui_logs(progress)

    worker = threading.Thread(
        target=run_download,
        args=(selector.result_states, selector.result_mode, Path(output_folder), package_name, progress),
        daemon=True,
    )
    worker.start()
    progress.mainloop()


if __name__ == "__main__":
    log("Starting ATAK DTED Downloader")
    log(f"Python: {sys.version}")
    log(f"Working directory: {Path.cwd()}")
    log(f"Script directory: {Path(__file__).resolve().parent}")
    main()
