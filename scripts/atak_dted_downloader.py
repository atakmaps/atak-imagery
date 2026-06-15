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

if getattr(sys, "frozen", False):
    RUNTIME_STATE_DIR = Path(sys.executable).resolve().parent
else:
    RUNTIME_STATE_DIR = Path(__file__).resolve().parent

# Overall progress bar: downloads, extract, build dted2.zip, temp cleanup (must sum to 1.0).
_DTED_W_DOWNLOAD = 0.55
_DTED_W_EXTRACT = 0.28
_DTED_W_BUILD = 0.12
_DTED_W_CLEANUP = 0.05
LAST_IMAGERY_ROOT_FILE = RUNTIME_STATE_DIR / ".last_imagery_root.txt"
# Same file written by the imagery downloader; used to avoid pushing old ATAK_SQL_*.sqlite
# left in the same-day ATAK_Upload_* folder from earlier runs.
LAST_IMAGERY_SESSION_STATES_FILE = RUNTIME_STATE_DIR / ".last_imagery_session_states.txt"
SKIP_STANDALONE_DTED_AFTER_SQLITE = RUNTIME_STATE_DIR / ".skip_standalone_dted_after_sqlite"


def mark_standalone_dted_skip() -> None:
    """SQLite builder consumes this to avoid launching the standalone DTED app after inline DTED."""
    SKIP_STANDALONE_DTED_AFTER_SQLITE.write_text(
        datetime.now().isoformat(timespec="seconds"), encoding="utf-8"
    )


def peek_standalone_dted_skip_pending() -> bool:
    """True if the downloader asked the SQLite builder to skip launching standalone DTED."""
    return SKIP_STANDALONE_DTED_AFTER_SQLITE.is_file()


def consume_standalone_dted_skip() -> bool:
    """Return True if the skip flag was present (and remove it)."""
    if not SKIP_STANDALONE_DTED_AFTER_SQLITE.is_file():
        return False
    try:
        SKIP_STANDALONE_DTED_AFTER_SQLITE.unlink()
    except OSError:
        pass
    return True


def bring_window_forward(win: tk.Misc, *, persistent_topmost: bool = False) -> None:
    """Raise a window above others; on some WMs ``-topmost`` is needed briefly (or kept for modals)."""
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


def _session_allowed_atak_sql_filenames() -> Optional[set[str]]:
    """Basenames ATAK_SQL_<State>.sqlite for states in the last imagery download, or None if no session file."""
    path = LAST_IMAGERY_SESSION_STATES_FILE
    if not path.is_file():
        return None
    try:
        names = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return None
    if not names:
        return None
    out: set[str] = set()
    for state_name in names:
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", state_name.strip()) or "state"
        out.add(f"ATAK_SQL_{stem}.sqlite")
    return out


_DEVICE_DTED_MANIFEST_FILENAME = ".dted_states.json"


def _adb_executable() -> str:
    return shutil.which("adb") or "adb"


def _adb_base_cmd() -> List[str]:
    cmd = [_adb_executable()]
    serial = os.environ.get("ANDROID_SERIAL", "").strip()
    if serial:
        cmd.extend(["-s", serial])
    return cmd


def query_device_installed_dted_states(log_fn: Optional[Callable[[str], None]] = None) -> Optional[Set[str]]:
    """
    Read the DTED state manifest from the connected device via ``adb shell cat``.

    Returns a ``set`` of state names already installed, or ``None`` if the device
    is not connected, the manifest doesn't exist yet, or it can't be parsed.
    When ``None`` is returned the caller should assume all required states need
    to be downloaded.
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
    except subprocess.TimeoutExpired:
        _log("DTED device check: adb timed out querying manifest; will download all required states.")
        return None
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        _log(f"DTED device check: manifest unreadable ({exc}); will download all required states.")
        return None
    except Exception as exc:
        _log(f"DTED device check: could not query device ({exc}); will download all required states.")
        return None


def push_device_dted_manifest(all_installed_states: Set[str], log_fn: Optional[Callable[[str], None]] = None) -> None:
    """
    Write the merged DTED state manifest to the device via ``adb push``.
    ``all_installed_states`` must be the *complete* set of states now on the device
    (previously installed + newly pushed).  Errors are logged and silently swallowed.
    """
    _log = log_fn or log
    root = (os.environ.get("ATAK_DEVICE_FILES_ROOT") or "/sdcard/atak").strip() or "/sdcard/atak"
    remote_manifest = f"{root.rstrip('/')}/DTED/{_DEVICE_DTED_MANIFEST_FILENAME}"
    payload = {
        "schema_version": 1,
        "states": sorted(all_installed_states),
        "last_updated": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            prefix="atak_dted_manifest_",
            delete=False,
            encoding="utf-8",
        ) as tf:
            json.dump(payload, tf, indent=2)
            tmp_path = tf.name
        cmd = _adb_base_cmd() + ["push", tmp_path, remote_manifest]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            _log(f"DTED manifest push failed (non-fatal): {(proc.stderr or proc.stdout or '').strip()}")
        else:
            _log(f"DTED device manifest updated: {len(all_installed_states)} state(s) recorded on device.")
    except Exception as exc:
        _log(f"DTED manifest push error (non-fatal): {exc}")
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def _find_atak_sqlite_files(upload_dir: Path) -> List[Path]:
    """All per-state ATAK_SQL*.sqlite under upload_dir, or from the best-matching sibling ATAK_Upload_* folder."""

    def sorted_in_dir(dirpath: Path) -> List[Path]:
        if not dirpath.is_dir():
            return []
        return sorted(dirpath.glob("ATAK_SQL*.sqlite"), key=lambda p: p.name.lower())

    direct = sorted_in_dir(upload_dir)
    if direct:
        return direct

    parent = upload_dir.parent
    if not parent.is_dir():
        return []

    all_sql = list(parent.glob("ATAK_Upload_*/ATAK_SQL*.sqlite"))
    if not all_sql:
        return []

    by_folder: Dict[Path, List[Path]] = {}
    for p in all_sql:
        by_folder.setdefault(p.parent, []).append(p)

    def folder_peak_mtime(paths: List[Path]) -> float:
        return max(x.stat().st_mtime for x in paths)

    best_folder = max(by_folder.items(), key=lambda item: folder_peak_mtime(item[1]))[0]
    return sorted(by_folder[best_folder], key=lambda p: p.name.lower())


def adb_push_pipeline_outputs(
    upload_dir: Path,
    final_dted_zip: Path,
    *,
    states_in_zip: Optional[List[str]] = None,
) -> Tuple[bool, str]:
    """Push imagery SQLite DB file(s) and DTED zip to the device (default /sdcard/atak/imagery and .../DTED).

    ``states_in_zip`` lists the DTED states contained in *this* push.  When provided,
    the existing device manifest is read, merged with ``states_in_zip``, and the
    updated manifest is pushed to the device after a successful transfer.
    """
    sqlite_paths = _find_atak_sqlite_files(upload_dir)
    allowed_names = _session_allowed_atak_sql_filenames()
    if allowed_names is not None and sqlite_paths:
        filtered = [p for p in sqlite_paths if p.name in allowed_names]
        skipped = [p.name for p in sqlite_paths if p.name not in allowed_names]
        if skipped:
            log(
                "Skipping SQLite file(s) not in the last imagery download session "
                f"(delete them from your ATAK_Upload_* folder or remove "
                f"{LAST_IMAGERY_SESSION_STATES_FILE.name} to push everything): "
                + ", ".join(sorted(skipped))
            )
        if not filtered:
            return False, (
                "No ATAK_SQL*.sqlite matched the last imagery session.\n\n"
                f"Session list: {LAST_IMAGERY_SESSION_STATES_FILE}\n"
                f"SQLite files found: {', '.join(p.name for p in sqlite_paths)}"
            )
        sqlite_paths = filtered
    if not sqlite_paths:
        return False, f"No ATAK_SQL*.sqlite found (looked in {upload_dir} and sibling upload folders)."
    if not final_dted_zip.is_file():
        return False, f"DTED zip not found: {final_dted_zip}"

    root = (os.environ.get("ATAK_DEVICE_FILES_ROOT") or "/sdcard/atak").strip() or "/sdcard/atak"
    remote_imagery = f"{root.rstrip('/')}/imagery"
    remote_dted = f"{root.rstrip('/')}/DTED"

    steps: List[List[str]] = [_adb_base_cmd() + ["shell", "mkdir", "-p", remote_imagery, remote_dted]]
    for p in sqlite_paths:
        steps.append(_adb_base_cmd() + ["push", str(p), f"{remote_imagery}/"])
    steps.append(_adb_base_cmd() + ["push", str(final_dted_zip), f"{remote_dted}/"])

    log_lines: List[str] = []
    for cmd in steps:
        log_lines.append(" ".join(cmd))
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        except subprocess.TimeoutExpired:
            return False, "adb timed out during push.\n\n" + "\n".join(log_lines)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            return False, f"{err}\n\n" + "\n".join(log_lines)

    names = ", ".join(p.name for p in sqlite_paths)

    # Update device manifest: combine states_in_zip (explicit) with sidecar file next to zip.
    effective_states: List[str] = list(states_in_zip or [])
    sidecar = final_dted_zip.with_suffix(".states.json")
    if sidecar.is_file():
        try:
            sidecar_data = json.loads(sidecar.read_text(encoding="utf-8"))
            sidecar_states: List[str] = sidecar_data.get("states", [])
            for s in sidecar_states:
                if s not in effective_states:
                    effective_states.append(s)
        except Exception:
            pass
    if effective_states:
        existing = query_device_installed_dted_states() or set()
        merged = existing | set(effective_states)
        push_device_dted_manifest(merged)

    return True, (
        f"Pushed imagery DB file(s) to {remote_imagery}/: {names}\n"
        f"Pushed DTED archive to {remote_dted}/{final_dted_zip.name}"
    )


ATAK_CIV_PACKAGE = "com.atakmap.app.civ"


def adb_restart_atak_civ() -> None:
    """Force-stop and relaunch ATAK CIv via adb monkey (best-effort)."""
    steps = [
        _adb_base_cmd() + ["shell", "am", "force-stop", ATAK_CIV_PACKAGE],
        _adb_base_cmd() + ["shell", "monkey", "-p", ATAK_CIV_PACKAGE, "1"],
    ]
    for cmd in steps:
        log_line = " ".join(cmd)
        log(f"Running: {log_line}")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()
                log(f"adb exit {proc.returncode}: {err}")
        except subprocess.TimeoutExpired:
            log("adb command timed out")
        except Exception as exc:
            log(f"adb command failed: {exc}")


def ask_delete_raw_imagery(parent: tk.Tk, imagery_root: Path, *, dted_complete: bool) -> bool:
    """True = delete raw imagery, False = keep. Buttons labeled Yes / No."""
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
        try:
            dlg.grab_release()
        except tk.TclError:
            pass
        dlg.destroy()

    def on_no() -> None:
        result["delete"] = False
        try:
            dlg.grab_release()
        except tk.TclError:
            pass
        dlg.destroy()

    tk.Button(btn_row, text="Yes", width=10, command=on_yes).pack(side="left", padx=6)
    tk.Button(btn_row, text="No", width=10, command=on_no).pack(side="left", padx=6)

    dlg.update_idletasks()
    bring_window_forward(parent, persistent_topmost=False)
    bring_window_forward(dlg, persistent_topmost=True)
    _modal_grab(dlg, parent)
    parent.wait_window(dlg)
    return result["delete"]


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
        self.script_dir = Path(__file__).resolve().parent
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
    """Newest dted2_*.zip in upload_dir, or under any sibling ATAK_Upload_* next to it."""
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


def show_imagery_loaded_exit_dialog(parent: tk.Tk) -> None:
    dlg = tk.Toplevel(parent)
    dlg.title(APP_TITLE)
    dlg.configure(cursor="arrow")
    dlg.transient(parent)
    dlg.resizable(False, False)
    tk.Label(
        dlg,
        text="Imagery has been loaded on your device.\n\nYou may now exit the program.",
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


def _set_parent_status(parent: tk.Misc, msg: str) -> None:
    if hasattr(parent, "set_status"):
        parent.set_status(msg)
    elif hasattr(parent, "status_var"):
        try:
            parent.status_var.set(msg)
        except Exception:
            pass
    try:
        parent.update_idletasks()
    except tk.TclError:
        pass


def _show_working_message_dialog(parent: tk.Misc, body: str, *, title: Optional[str] = None) -> Callable[[], None]:
    """
    Small modal notice while work runs on a background thread.
    Call the returned closer on the Tk main thread when the work finishes.
    """
    dlg_title = title or APP_TITLE
    busy = tk.Toplevel(parent)
    busy.title(dlg_title)
    busy.configure(cursor="watch")
    busy.transient(parent)
    busy.resizable(False, False)
    tk.Label(busy, text=body, justify="center", wraplength=440).pack(padx=28, pady=28)
    busy.protocol("WM_DELETE_WINDOW", lambda: None)
    busy.update_idletasks()
    bring_window_forward(parent, persistent_topmost=False)
    bring_window_forward(busy, persistent_topmost=False)
    _modal_grab(busy, parent)

    def close_working_message() -> None:
        try:
            busy.grab_release()
        except tk.TclError:
            pass
        try:
            busy.destroy()
        except tk.TclError:
            pass

    return close_working_message


def _open_upload_output_folder(upload_dir: Optional[Path]) -> None:
    if upload_dir and upload_dir.exists():
        try:
            if sys.platform.startswith("linux"):
                subprocess.Popen(["xdg-open", str(upload_dir)])
            elif sys.platform.startswith("win"):
                os.startfile(str(upload_dir))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(upload_dir)])
        except Exception as open_exc:
            log(f"WARNING: failed to open upload folder: {open_exc}")


def complete_device_deploy_and_imagery_cleanup(
    parent: tk.Tk,
    upload_dir: Optional[Path],
    final_dted_zip: Optional[Path],
    imagery_root: Optional[Path],
    *,
    dted_complete: bool,
    on_finished: Optional[Callable[[], None]] = None,
) -> None:
    """
    Push SQLite + DTED via adb, prompt to delete raw imagery, open the upload folder,
    restart ATAK on device, then show the final Exit dialog.

    ``adb push`` and deleting raw imagery run off the Tk thread so the UI stays responsive.
    ``on_finished`` runs after the final Exit dialog is dismissed (used to destroy host windows).
    """
    fz = final_dted_zip if (final_dted_zip and final_dted_zip.is_file()) else None
    if fz is None and upload_dir and upload_dir.is_dir():
        fz = resolve_dted_zip_for_upload_dir(upload_dir)

    needs_push = bool(dted_complete and upload_dir and upload_dir.is_dir() and fz is not None)

    def run_finalize_phase() -> None:
        _open_upload_output_folder(upload_dir)
        if dted_complete:
            adb_restart_atak_civ()
        show_imagery_loaded_exit_dialog(parent)
        if on_finished is not None:
            on_finished()

    def run_after_delete_choice(wants_delete: bool, root_for_delete: Optional[Path]) -> None:
        if not wants_delete:
            if root_for_delete is not None:
                log(f"Raw imagery retained: {root_for_delete}")
            run_finalize_phase()
            return
        assert root_for_delete is not None
        close_cleanup_dialog = _show_working_message_dialog(
            parent,
            "Cleaning up extra files…\n\n"
            "Removing downloaded imagery from this computer. Large folders can take a minute.",
        )
        _set_parent_status(parent, "Cleaning up extra files…")
        bring_window_forward(parent, persistent_topmost=False)
        err_box: List[Optional[BaseException]] = [None]

        def delete_worker() -> None:
            try:
                shutil.rmtree(root_for_delete)
                log(f"Deleted raw imagery folder: {root_for_delete}")
                try:
                    LAST_IMAGERY_ROOT_FILE.unlink()
                    log(f"Deleted saved imagery path file: {LAST_IMAGERY_ROOT_FILE}")
                except OSError as cleanup_exc:
                    log(f"Warning: saved imagery path file removal failed: {cleanup_exc}")
            except Exception as exc:
                err_box[0] = exc
                log(f"Warning: raw imagery cleanup failed: {exc}")

        def on_delete_done() -> None:
            close_cleanup_dialog()
            if err_box[0] is not None:
                try:
                    bring_window_forward(parent, persistent_topmost=False)
                    messagebox.showwarning(APP_TITLE, f"Raw imagery cleanup failed:\n{err_box[0]}", parent=parent)
                except Exception:
                    pass
            run_finalize_phase()

        def delete_thread_main() -> None:
            delete_worker()
            parent.after(0, on_delete_done)

        threading.Thread(target=delete_thread_main, daemon=True).start()

    def run_imagery_prompt() -> None:
        if imagery_root and imagery_root.is_dir() and LAST_IMAGERY_ROOT_FILE.is_file():
            try:
                _set_parent_status(parent, "Choose whether to delete raw imagery on this computer.")
                bring_window_forward(parent, persistent_topmost=False)
                cleanup = ask_delete_raw_imagery(parent, imagery_root, dted_complete=dted_complete)
                run_after_delete_choice(cleanup, imagery_root)
            except Exception as cleanup_exc:
                log(f"Warning: raw imagery cleanup failed: {cleanup_exc}")
                try:
                    bring_window_forward(parent, persistent_topmost=False)
                    messagebox.showwarning(APP_TITLE, f"Raw imagery cleanup failed:\n{cleanup_exc}", parent=parent)
                except Exception:
                    pass
                run_finalize_phase()
        else:
            run_finalize_phase()

    close_adb_work_dialog: List[Callable[[], None]] = [lambda: None]

    def continue_after_adb(ok: bool, detail: str) -> None:
        close_adb_work_dialog[0]()
        close_adb_work_dialog[0] = lambda: None
        if needs_push:
            if ok:
                log("adb push completed successfully")
            else:
                log(f"adb push failed: {detail}")
                try:
                    bring_window_forward(parent, persistent_topmost=False)
                    messagebox.showwarning(
                        APP_TITLE,
                        "Could not push files to the device via adb:\n\n"
                        f"{detail}\n\n"
                        "You can copy files from the upload folder manually.",
                        parent=parent,
                    )
                except Exception:
                    pass
        elif dted_complete:
            log("Skipping adb push: missing upload folder or DTED zip.")
        run_imagery_prompt()

    if needs_push:
        close_adb_work_dialog[0] = _show_working_message_dialog(
            parent,
            "Copying to your device…\n\n"
            "Pushing SQLite and DTED over USB. This may take several minutes.",
        )
        _set_parent_status(
            parent,
            "Pushing SQLite and DTED to your device via adb… (this can take several minutes)",
        )
        bring_window_forward(parent, persistent_topmost=False)

        def adb_worker() -> None:
            push_ok, push_detail = adb_push_pipeline_outputs(upload_dir, fz)
            parent.after(0, lambda o=push_ok, d=push_detail: continue_after_adb(o, d))

        threading.Thread(target=adb_worker, daemon=True).start()
    else:
        continue_after_adb(True, "")


def clean_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned or "package"


def derive_package_folder_name(package_name: str) -> str:
    return f"atak_{clean_name(package_name)}"


def server_state_slug(state_name: str) -> str:
    """Map human state name to DTED server folder/file naming."""
    return re.sub(r"\s+", "_", state_name.strip())


def state_url(state_name: str) -> str:
    slug = server_state_slug(state_name)
    return f"{BASE_URL}/{slug}/{slug}.zip"


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
    cancel_check: Optional[Callable[[], None]] = None,
) -> str:
    """Download url to out_path.  cancel_check() is called each chunk; if it
    raises, the exception propagates immediately (not caught as a failure)."""
    _log = log_fn or log
    size_hint = remote_file_size(session, url)
    if size_hint < 0:
        return "missing"

    if out_path.exists() and size_hint > 0 and out_path.stat().st_size == size_hint:
        return "existing"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".part")

    last_emit = 0.0
    _cancel_exc: Optional[BaseException] = None

    def _emit_progress(read: int, total_known: int, *, force: bool = False) -> None:
        nonlocal last_emit, _cancel_exc
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
                    if cancel_check:
                        try:
                            cancel_check()
                        except BaseException as ce:
                            _cancel_exc = ce
                            raise
                    if chunk:
                        f.write(chunk)
                        read += len(chunk)
                        _emit_progress(read, total_bytes)
            _emit_progress(read, total_bytes if total_bytes > 0 else read, force=True)
        tmp_path.replace(out_path)
        return "downloaded"
    except BaseException as exc:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        if _cancel_exc is not None:
            raise _cancel_exc
        if isinstance(exc, Exception):
            _log(f"ERROR downloading {url}: {exc}")
            return "failed"
        raise


def extract_state_zip(
    zip_path: Path,
    extract_root: Path,
    log_fn: Optional[Callable[[str], None]] = None,
    radius_bbox_lonlat: Optional[Tuple[float, float, float, float]] = None,
) -> None:
    _log = log_fn or log
    state_name = zip_path.stem
    state_extract_dir = extract_root / state_name
    if state_extract_dir.exists():
        shutil.rmtree(state_extract_dir)
    state_extract_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        if radius_bbox_lonlat is None:
            _log(f"Extracting {zip_path.name} -> {state_extract_dir}")
            zf.extractall(state_extract_dir)
            return

        rw, rs, re_, rn = radius_bbox_lonlat
        kept = 0
        skipped = 0
        _log(
            f"Extracting {zip_path.name} -> {state_extract_dir} "
            f"(radius clip: only DTED cells overlapping bbox)"
        )
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            cell = _dted_cell_bounds_from_arcname(name)
            if cell is None:
                skipped += 1
                continue
            cw, cs, ce, cn = cell
            if not _bbox_overlaps(cw, cs, ce, cn, rw, rs, re_, rn):
                skipped += 1
                continue
            dest = state_extract_dir / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            kept += 1
        _log(f"DTED: radius-selective extract kept {kept} file(s), skipped {skipped}")


_LON_DIR_RE = re.compile(r"^([ew])(\d{3})$", re.IGNORECASE)
_LAT_FILE_RE = re.compile(r"^([ns])(\d{2})\.dt[012]$", re.IGNORECASE)


def _dted_cell_bounds_from_arcname(arc_posix: str) -> Optional[Tuple[float, float, float, float]]:
    """
    Parse DTED cell bounds from archive path like ``w111/n32.dt2``.

    Returns (west, south, east, north) in lon/lat degrees, or None when
    the path is not a DTED cell file.
    """
    parts = Path(arc_posix).parts
    if len(parts) < 2:
        return None
    lon_part = parts[-2]
    lat_part = parts[-1]
    m_lon = _LON_DIR_RE.match(lon_part)
    m_lat = _LAT_FILE_RE.match(lat_part)
    if not m_lon or not m_lat:
        return None

    lon_hemi, lon_deg_s = m_lon.groups()
    lat_hemi, lat_deg_s = m_lat.groups()
    lon_deg = int(lon_deg_s)
    lat_deg = int(lat_deg_s)

    west = -lon_deg if lon_hemi.lower() == "w" else lon_deg
    south = -lat_deg if lat_hemi.lower() == "s" else lat_deg
    east = west + 1.0
    north = south + 1.0
    return west, south, east, north


def _bbox_overlaps(
    a_w: float, a_s: float, a_e: float, a_n: float,
    b_w: float, b_s: float, b_e: float, b_n: float,
) -> bool:
    return not (a_e < b_w or b_e < a_w or a_n < b_s or b_n < a_s)


def build_final_dted_zip(
    extract_root: Path,
    final_zip_path: Path,
    log_fn: Optional[Callable[[str], None]] = None,
    on_packed: Optional[Callable[[int, int], None]] = None,
    radius_bbox_lonlat: Optional[Tuple[float, float, float, float]] = None,
) -> None:
    _log = log_fn or log
    if final_zip_path.exists():
        final_zip_path.unlink()

    entries: List[Tuple[Path, str]] = []
    filtered_out = 0
    radius_filter_enabled = radius_bbox_lonlat is not None
    if radius_filter_enabled:
        rw, rs, re_, rn = radius_bbox_lonlat  # type: ignore[misc]
    for state_dir in sorted(extract_root.iterdir(), key=lambda p: p.name.lower()):
        if not state_dir.is_dir():
            continue
        for item in sorted(state_dir.rglob("*")):
            if not item.is_file():
                continue
            arcname = item.relative_to(state_dir)
            arc_posix = arcname.as_posix()
            if radius_filter_enabled:
                cell = _dted_cell_bounds_from_arcname(arc_posix)
                if cell is None:
                    filtered_out += 1
                    continue
                cw, cs, ce, cn = cell
                if not _bbox_overlaps(cw, cs, ce, cn, rw, rs, re_, rn):
                    filtered_out += 1
                    continue
            entries.append((item, arc_posix))

    _log(f"Building final ATAK zip: {final_zip_path} ({len(entries)} files)")
    if radius_filter_enabled:
        _log(
            "DTED radius clip applied: "
            f"kept {len(entries)} file(s), filtered {filtered_out} outside radius bbox."
        )

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
    local_dted_index: Optional[Dict[str, Path]] = None,
    radius_bbox_lonlat: Optional[Tuple[float, float, float, float]] = None,
) -> Optional[Path]:
    """
    Download DTED state ZIPs for the given states into upload_dir, same server/layout as the DTED app.
    Uses progress.wait_if_paused / set_status / set_progress_fraction / set_stat; optional set_speed_eta cleared when present.
    Returns path to dted2_*.zip or None if nothing could be produced.

    When ``local_state_zip_root`` is set, looks for ``StateName/StateName.zip`` under that folder
    (searched recursively) before using HTTP.
    """
    from local_asset_lookup import build_dted_state_zip_index, find_local_dted_state_zip

    import tempfile

    if not selected_states:
        log_sink("DTED: no states in selection; skipping.")
        return None

    stats = {"downloaded": 0, "existing": 0, "failed": 0, "missing": 0}
    for key in stats:
        progress.set_stat(key, 0)
    if hasattr(progress, "set_speed_eta"):
        progress.set_speed_eta(0.0, None)

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
        if local_dted_index is None:
            local_dted_index = build_dted_state_zip_index(local_state_zip_root)
            log_sink(
                f"DTED: indexed {len(local_dted_index):,} local state ZIP(s) under {local_state_zip_root}"
            )
    log_sink(f"DTED: server base URL {BASE_URL}")
    if radius_bbox_lonlat is not None:
        rw, rs, re_, rn = radius_bbox_lonlat
        log_sink(
            "DTED: radius mode — the server provides one ZIP per state, so each overlapping "
            "state ZIP is downloaded in full; extract and the final dted2 archive are clipped "
            f"to cells inside ({rw:.5f}, {rs:.5f})–({re_:.5f}, {rn:.5f})."
        )

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
                local_zip = find_local_dted_state_zip(
                    local_state_zip_root, local_dted_index, state_name
                )

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
                    result = download_file(
                        session, url, out_path,
                        log_fn=log_sink, progress_cb=dl_progress,
                        cancel_check=progress.wait_if_paused,
                    )
            else:
                progress.set_status(f"DTED: downloading {state_name}…")
                log_sink(f"DTED: GET {url}")
                result = download_file(
                    session, url, out_path,
                    log_fn=log_sink, progress_cb=dl_progress,
                    cancel_check=progress.wait_if_paused,
                )
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
            extract_state_zip(
                out_path,
                extract_root,
                log_fn=log_sink,
                radius_bbox_lonlat=radius_bbox_lonlat,
            )
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
        build_final_dted_zip(
            extract_root,
            final_zip_path,
            log_fn=log_sink,
            on_packed=on_packed,
            radius_bbox_lonlat=radius_bbox_lonlat,
        )
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
                ud = getattr(window, "upload_dir", None)
                fz = getattr(window, "final_dted_zip", None)
                log(msg)
                if hasattr(window, "set_status"):
                    window.set_status("DTED build finished. Next: push to device, then optional cleanup.")
                    window.update_idletasks()

                def _close_dted_progress_window() -> None:
                    window.closed = True
                    try:
                        window.quit()
                        window.destroy()
                    except Exception:
                        pass

                complete_device_deploy_and_imagery_cleanup(
                    window,
                    ud,
                    fz,
                    imagery_root,
                    dted_complete=True,
                    on_finished=_close_dted_progress_window,
                )
                return
            else:
                if hasattr(window, "set_status"):
                    window.set_status("Showing summary — next: optional raw imagery cleanup.")
                    window.update_idletasks()
                bring_window_forward(window, persistent_topmost=False)
                messagebox.showinfo(APP_TITLE, msg, parent=window)
                if imagery_root is not None:
                    try:
                        cleanup = ask_delete_raw_imagery(window, imagery_root, dted_complete=False)
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
                            bring_window_forward(window, persistent_topmost=False)
                            messagebox.showwarning(APP_TITLE, f"Raw imagery cleanup failed:\n{cleanup_exc}", parent=window)
                        except Exception:
                            pass

                upload_dir = getattr(window, "upload_dir", None)
                if upload_dir and upload_dir.exists():
                    try:
                        if sys.platform.startswith("linux"):
                            subprocess.Popen(["xdg-open", str(upload_dir)])
                        elif sys.platform.startswith("win"):
                            os.startfile(str(upload_dir))
                        elif sys.platform == "darwin":
                            subprocess.Popen(["open", str(upload_dir)])
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
