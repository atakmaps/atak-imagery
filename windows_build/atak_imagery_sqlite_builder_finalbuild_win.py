#!/usr/bin/env python3
"""
ATAK imagery SQLite builder (imagery-only)

Builds or updates an ATAK/osmdroid-style SQLite tile cache from a folder tree:

    State/
      12/
        x/
          y.jpg
      13/
        x/
          y.jpg

Behavior:
- Imports ALL numeric zoom folders present under the selected source folder
- When multiple state folders are present under the imagery root, writes one
  ATAK_SQL_<State>.sqlite per state under ATAK_Upload_<date>/ (no time in filenames)
- If .last_imagery_session_states.txt exists (written by the imagery downloader), only
  those state folders are built so old Imagery/<State>/ trees do not create extra SQL files.
- Safe to re-run: duplicate tiles are replaced, not duplicated
- GUI-first workflow with folder dialogs and a live log window
- GUI auto-creates an ATAK export folder named atak_<StateName>
- SQLite file inside that folder is named <StateName>.sqlite
- Persistent timestamped log file for debugging

Based on the user's ATAK-generated sample DB and osmdroid SQL key formula.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import queue
import re
import sqlite3
import shutil
import sys
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk

    from tk_window_scaling import apply_resizable_window, ensure_window_stacking
except Exception:  # pragma: no cover
    tk = None
    filedialog = None
    messagebox = None
    simpledialog = None
    ttk = None
    apply_resizable_window = None  # type: ignore[assignment]
    ensure_window_stacking = None  # type: ignore[assignment]

VALID_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
DEFAULT_PROVIDER = "USGSImageryOnly"
DEFAULT_SRID = "3857"
LOG_DIR = Path.home() / ".atak_pipeline_logs"
BATCH_SIZE = 1000

if getattr(sys, "frozen", False):
    RUNTIME_STATE_DIR = Path(sys.executable).resolve().parent
else:
    RUNTIME_STATE_DIR = Path(__file__).resolve().parent

LAST_IMAGERY_ROOT_FILE = RUNTIME_STATE_DIR / ".last_imagery_root.txt"
LAST_IMAGERY_SESSION_STATES_FILE = RUNTIME_STATE_DIR / ".last_imagery_session_states.txt"


def _bring_window_forward(win, *, persistent_topmost: bool = False) -> None:
    if ensure_window_stacking is None:
        return
    ensure_window_stacking(win, persistent_topmost=persistent_topmost)


@dataclass
class BuildConfig:
    source_dir: Path
    output_dir: Path
    sqlite_path: Path
    provider: str = DEFAULT_PROVIDER
    srid: str = DEFAULT_SRID


class QueueLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue[str]):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self.q.put(msg)
        except Exception:
            self.handleError(record)


class LoggerManager:
    def __init__(self, log_file: Path, gui_queue: Optional[queue.Queue[str]] = None):
        self.logger = logging.getLogger(f"atak_pipeline_{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False

        formatter = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")

        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(formatter)
        self.logger.addHandler(sh)

        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(formatter)
        self.logger.addHandler(fh)

        if gui_queue is not None:
            qh = QueueLogHandler(gui_queue)
            qh.setFormatter(formatter)
            self.logger.addHandler(qh)

        self.log_file = log_file

    def get(self) -> logging.Logger:
        return self.logger


def compute_sqlite_key(x: int, y: int, z: int) -> int:
    """
    osmdroid SqlTileWriter SQL primary-key formula.
    Equivalent to: ((z << z) + x << z) + y
    """
    return (((z << z) + x) << z) + y


TILE_RE = re.compile(r"^(\d+)(?:\.[A-Za-z0-9]+)?$")


def detect_zoom_dirs(source_dir: Path) -> List[int]:
    zooms: List[int] = []
    for child in source_dir.iterdir():
        if child.is_dir() and child.name.isdigit():
            zooms.append(int(child.name))
    return sorted(zooms)


def iter_tiles(source_dir: Path, zooms: Iterable[int], logger: logging.Logger):
    total_seen = 0
    for z in zooms:
        z_dir = source_dir / str(z)
        if not z_dir.is_dir():
            logger.warning("Skipping missing zoom directory: %s", z_dir)
            continue

        zoom_seen = 0
        for x_dir in sorted(z_dir.iterdir(), key=lambda p: p.name):
            if not x_dir.is_dir() or not x_dir.name.isdigit():
                continue
            x = int(x_dir.name)
            for tile_file in sorted(x_dir.iterdir(), key=lambda p: p.name):
                if not tile_file.is_file():
                    continue
                if tile_file.suffix.lower() not in VALID_EXTS:
                    continue
                m = TILE_RE.match(tile_file.name)
                if not m:
                    logger.warning("Skipping tile with unexpected filename: %s", tile_file)
                    continue
                y = int(m.group(1))
                zoom_seen += 1
                total_seen += 1
                yield z, x, y, tile_file
        logger.info("Detected %s tiles at zoom %s", f"{zoom_seen:,}", z)
    logger.info("Detected %s total tiles across all zooms", f"{total_seen:,}")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tiles (
    key INTEGER PRIMARY KEY,
    provider TEXT,
    tile BLOB
);

CREATE TABLE IF NOT EXISTS ATAK_catalog (
    key INTEGER PRIMARY KEY,
    access INTEGER,
    expiration INTEGER,
    size INTEGER
);

CREATE TABLE IF NOT EXISTS ATAK_metadata (
    key TEXT,
    value TEXT
);

CREATE INDEX IF NOT EXISTS tiles_provider_idx ON tiles (provider);
CREATE INDEX IF NOT EXISTS atak_catalog_exp_idx ON ATAK_catalog (expiration);
"""


def apply_radius_footprint_metadata(
    conn: sqlite3.Connection,
    source_dir: Path,
    logger: logging.Logger,
    zooms: List[int],
) -> None:
    """If the downloader left ``.radius_footprint.json``, store MBTiles-style bounds in ATAK_metadata."""
    path = source_dir / ".radius_footprint.json"
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        w = float(data["west"])
        s = float(data["south"])
        e = float(data["east"])
        n = float(data["north"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning("Skipping radius footprint metadata (%s): %s", path, exc)
        return
    bounds = f"{w},{s},{e},{n}"
    rows = [
        ("bounds", bounds),
        ("minzoom", str(min(zooms))),
        ("maxzoom", str(max(zooms))),
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO ATAK_metadata(key, value) VALUES (?, ?)",
        rows,
    )
    conn.commit()
    logger.info(
        "ATAK_metadata footprint from %s: bounds=%s minzoom=%s maxzoom=%s",
        path.name,
        bounds,
        min(zooms),
        max(zooms),
    )


def initialize_db(conn: sqlite3.Connection, logger: logging.Logger, provider: str, srid: str) -> None:
    logger.info("Initializing SQLite schema")
    conn.executescript(SCHEMA_SQL)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA cache_size=-200000;")
    conn.execute("INSERT OR REPLACE INTO ATAK_metadata(key, value) VALUES (?, ?)", ("srid", srid))
    conn.commit()

    existing_providers = [row[0] for row in conn.execute("SELECT DISTINCT provider FROM tiles WHERE provider IS NOT NULL")]
    if existing_providers and provider not in existing_providers:
        logger.warning(
            "Existing DB contains provider(s) %s, but current run is using provider '%s'.",
            existing_providers,
            provider,
        )


class Builder:
    def __init__(self, config: BuildConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.stats: Dict[str, int] = {
            "inserted_or_replaced": 0,
            "bytes_written": 0,
            "skipped": 0,
            "zooms": 0,
        }

    def run(self) -> None:
        source_dir = self.config.source_dir
        output_dir = self.config.output_dir
        sqlite_path = self.config.sqlite_path

        if not source_dir.is_dir():
            raise FileNotFoundError(f"Source directory not found: {source_dir}")
        output_dir.mkdir(parents=True, exist_ok=True)

        zooms = detect_zoom_dirs(source_dir)
        if not zooms:
            raise RuntimeError(f"No numeric zoom folders found under: {source_dir}")
        self.stats["zooms"] = len(zooms)

        self.logger.info("Source directory: %s", source_dir)
        self.logger.info("Output SQLite: %s", sqlite_path)
        self.logger.info("Provider: %s", self.config.provider)
        self.logger.info("SRID: %s", self.config.srid)
        self.logger.info("Detected zoom levels: %s", ", ".join(map(str, zooms)))
        for z in zooms:
            sample_res = 156543.03392804097 / (2 ** z)
            self.logger.info("Zoom %d nominal Web Mercator resolution: %.12f m/px", z, sample_res)

        conn = sqlite3.connect(sqlite_path)
        try:
            initialize_db(conn, self.logger, self.config.provider, self.config.srid)
            self._import_tiles(conn, zooms)
            apply_radius_footprint_metadata(conn, source_dir, self.logger, zooms)
            conn.commit()
            self._report_counts(conn)
        finally:
            conn.close()

        try:
            from atak_osmdroid_sqlite_footprint import report_lines

            for line in report_lines(sqlite_path, include_union=False):
                self.logger.info("%s", line)
        except Exception as exc:
            self.logger.warning("Mobile outline diagnostic skipped: %s", exc)

    def _import_tiles(self, conn: sqlite3.Connection, zooms: List[int]) -> None:
        start = time.time()
        batch_tiles: List[Tuple[int, str, bytes]] = []
        batch_catalog: List[Tuple[int, int, int, int]] = []
        progress_counter = 0
        now_ms = int(time.time() * 1000)
        expiration_ms = now_ms + (365 * 24 * 60 * 60 * 1000)  # 1 year placeholder

        self.logger.info("Beginning tile import")
        for z, x, y, tile_file in iter_tiles(self.config.source_dir, zooms, self.logger):
            key = compute_sqlite_key(x, y, z)
            try:
                tile_bytes = tile_file.read_bytes()
            except Exception as exc:
                self.stats["skipped"] += 1
                self.logger.error("Failed reading %s: %s", tile_file, exc)
                continue

            batch_tiles.append((key, self.config.provider, sqlite3.Binary(tile_bytes)))
            batch_catalog.append((key, now_ms, expiration_ms, len(tile_bytes)))
            self.stats["bytes_written"] += len(tile_bytes)
            self.stats["inserted_or_replaced"] += 1
            progress_counter += 1

            if progress_counter <= 10:
                self.logger.info(
                    "Sample key z=%d x=%d y=%d -> key=%d (%s)",
                    z, x, y, key, tile_file.name
                )

            if len(batch_tiles) >= BATCH_SIZE:
                self._flush_batch(conn, batch_tiles, batch_catalog)
                batch_tiles.clear()
                batch_catalog.clear()
                elapsed = time.time() - start
                rate = self.stats["inserted_or_replaced"] / elapsed if elapsed > 0 else 0
                self.logger.info(
                    "Progress: %s tiles processed, %.1f tiles/sec",
                    f"{self.stats['inserted_or_replaced']:,}",
                    rate,
                )

        if batch_tiles:
            self._flush_batch(conn, batch_tiles, batch_catalog)

        elapsed = time.time() - start
        self.logger.info(
            "Finished import in %.1f sec | tiles processed=%s | skipped=%s | bytes=%s",
            elapsed,
            f"{self.stats['inserted_or_replaced']:,}",
            f"{self.stats['skipped']:,}",
            f"{self.stats['bytes_written']:,}",
        )

    def _flush_batch(self, conn: sqlite3.Connection, batch_tiles, batch_catalog) -> None:
        conn.executemany(
            "INSERT OR REPLACE INTO tiles(key, provider, tile) VALUES (?, ?, ?)",
            batch_tiles,
        )
        conn.executemany(
            "INSERT OR REPLACE INTO ATAK_catalog(key, access, expiration, size) VALUES (?, ?, ?, ?)",
            batch_catalog,
        )
        conn.commit()

    def _report_counts(self, conn: sqlite3.Connection) -> None:
        tile_count = conn.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
        catalog_count = conn.execute("SELECT COUNT(*) FROM ATAK_catalog").fetchone()[0]
        providers = [r[0] for r in conn.execute("SELECT DISTINCT provider FROM tiles ORDER BY provider")]
        self.logger.info("Final DB counts: tiles=%s ATAK_catalog=%s", f"{tile_count:,}", f"{catalog_count:,}")
        self.logger.info("Providers in DB: %s", providers)
        self.logger.info("SQLite file size: %s bytes", f"{self.config.sqlite_path.stat().st_size:,}")


def derive_output_name(source_dir: Path) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", source_dir.name.strip()) or "imagery"
    return f"{cleaned}.sqlite"


def atak_upload_sqlite_path(upload_dir: Path, state_folder_name: str) -> Path:
    """Per-state DB in the upload folder: ATAK_SQL_<sanitized_name>.sqlite (no timestamp)."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", state_folder_name.strip()) or "state"
    return upload_dir / f"ATAK_SQL_{stem}.sqlite"


def derive_atak_folder_name(source_dir: Path) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", source_dir.name.strip()) or "imagery"
    return f"atak_{cleaned}"



def find_state_imagery_dirs(imagery_root: Path) -> List[Path]:
    results: List[Path] = []
    if not imagery_root.is_dir():
        return results
    for child in sorted(imagery_root.iterdir(), key=lambda p: p.name.lower()):
        if child.is_dir() and detect_zoom_dirs(child):
            results.append(child)
    return results


def filter_state_dirs_for_last_imagery_session(state_dirs: List[Path]) -> Tuple[List[Path], Optional[str]]:
    """
    After a downloader run, .last_imagery_session_states.txt lists state folder names
    that were selected. If present, build SQLite only for those names so leftover
    Imagery/<OtherState>/ trees from older tests do not produce extra ATAK_SQL_*.sqlite files.
    Delete the file next to this script to build every folder under Imagery/ again.
    """
    path = LAST_IMAGERY_SESSION_STATES_FILE
    if not path.is_file():
        return state_dirs, None
    try:
        wanted = {ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()}
    except OSError:
        return state_dirs, None
    if not wanted:
        return state_dirs, None
    filtered = [p for p in state_dirs if p.name in wanted]
    if not filtered:
        return state_dirs, (
            f"{path.name} lists states that do not match any folder here; building all detected folders."
        )
    skipped = [p.name for p in state_dirs if p.name not in wanted]
    note = (
        f"Using {path.name} from the last imagery download — building only: {', '.join(sorted(wanted))}. "
        f"Skipped folders: {', '.join(sorted(skipped))}. "
        f"Delete {path.name} next to this script to include every folder under Imagery/."
    )
    return filtered, note


def ask_directory_linux_native(title: str) -> Optional[Path]:
    try:
        if shutil.which("zenity"):
            result = subprocess.run(
                [
                    "zenity",
                    "--file-selection",
                    "--directory",
                    f"--title={title}",
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                value = result.stdout.strip()
                return Path(value) if value else None
            return None
    except Exception:
        pass

    selected = filedialog.askdirectory(title=title)
    return Path(selected) if selected else None

class App:
    def __init__(self):
        if tk is None:
            raise RuntimeError("Tkinter is not available on this system.")
        self.root = tk.Tk()
        self.root.title("ATAK Imagery SQLite Builder")
        self.root.configure(cursor="arrow")
        apply_resizable_window(self.root, 980, 700, (820, 560))

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.logger: Optional[logging.Logger] = None
        self.log_file: Optional[Path] = None
        self.config: Optional[BuildConfig] = None
        self.worker: Optional[threading.Thread] = None
        self.completion_message: Optional[str] = None
        self.error_message: Optional[str] = None
        self.imagery_root: Optional[Path] = None
        self._last_upload_dir: Optional[Path] = None

        self.status_var = tk.StringVar(value="Preparing to build…")
        self.summary_var = tk.StringVar(value="")

        self._build_ui()
        self.root.after(100, self._poll_logs)
        self.root.after(250, self.start_wizard)

    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="ATAK Imagery SQLite Builder", font=("TkDefaultFont", 12, "bold")).pack(anchor="w")
        ttk.Label(
            top,
            text="Imports all detected zoom folders under one state/source folder into a single ATAK-style SQLite cache.",
        ).pack(anchor="w", pady=(2, 8))

        ttk.Label(top, textvariable=self.status_var).pack(anchor="w")
        ttk.Label(top, textvariable=self.summary_var, foreground="#444").pack(anchor="w", pady=(4, 0))

        btns = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        btns.pack(fill="x")
        ttk.Button(btns, text="Open Log Folder", command=self.open_log_folder).pack(side="left")
        ttk.Button(btns, text="Exit", command=self.root.destroy).pack(side="right")

        log_frame = ttk.Frame(self.root, padding=10)
        log_frame.pack(fill="both", expand=True)
        ttk.Label(log_frame, text="Live Log").pack(anchor="w")

        self.text = tk.Text(log_frame, wrap="word", height=30)
        self.text.pack(side="left", fill="both", expand=True)
        self.text.configure(state="disabled")
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.text.yview)
        scroll.pack(side="right", fill="y")
        self.text.configure(yscrollcommand=scroll.set)

    def append_log(self, msg: str) -> None:
        self.text.configure(state="normal")
        self.text.insert("end", msg + "\n")
        self.text.see("end")
        self.text.configure(state="disabled")

    def _poll_logs(self) -> None:
        # Show completion/error as soon as it is set; do not drain a huge backlog first
        # (that blocked the UI for a long time after the status already said "Build complete").
        if self.completion_message:
            msg = self.completion_message
            self.completion_message = None
            try:
                _bring_window_forward(self.root, persistent_topmost=False)
                skip_inline_dted = False
                try:
                    from atak_dted_downloader_win import (
                        complete_device_deploy_and_imagery_cleanup,
                        consume_standalone_dted_skip,
                        peek_standalone_dted_skip_pending,
                    )

                    skip_inline_dted = peek_standalone_dted_skip_pending()
                except ImportError:
                    pass

                if skip_inline_dted and self.imagery_root and self._last_upload_dir:
                    consume_standalone_dted_skip()

                    def _finish_after_device_deploy() -> None:
                        try:
                            self.root.quit()
                            self.root.destroy()
                        except Exception:
                            pass
                        sys.exit(0)

                    complete_device_deploy_and_imagery_cleanup(
                        self.root,
                        self._last_upload_dir,
                        None,
                        self.imagery_root,
                        dted_complete=True,
                        on_finished=_finish_after_device_deploy,
                    )
                    return

                if skip_inline_dted:
                    consume_standalone_dted_skip()
                    _bring_window_forward(self.root, persistent_topmost=False)
                    messagebox.showwarning(
                        "Build complete",
                        "Inline DTED was used earlier, but the upload folder could not be determined.\n\n"
                        "Push SQLite and DTED from the ATAK_Upload_* folder manually, then exit.",
                        parent=self.root,
                    )
                    try:
                        self.root.quit()
                        self.root.destroy()
                    except Exception:
                        pass
                    sys.exit(0)

                _bring_window_forward(self.root, persistent_topmost=False)
                messagebox.showinfo("Build complete", msg, parent=self.root)
                self.root.destroy()

                if getattr(sys, "frozen", False):
                    if hasattr(sys, "_MEIPASS"):
                        os.environ["TCL_LIBRARY"] = str(Path(sys._MEIPASS) / "_tcl_data")
                        os.environ["TK_LIBRARY"] = str(Path(sys._MEIPASS) / "_tk_data")
                    import atak_dted_downloader_win as dted
                    dted.main()
                    os._exit(0)
                else:
                    next_script = Path(__file__).resolve().parent / "atak_dted_downloader_win.py"
                    subprocess.Popen([sys.executable, str(next_script)])
                    sys.exit(0)
            except Exception as exc:
                _bring_window_forward(self.root, persistent_topmost=False)
                messagebox.showerror("Build complete", f"Failed to launch DTED downloader:\n{exc}", parent=self.root)
                sys.exit(1)

        if self.error_message:
            msg = self.error_message
            self.error_message = None
            _bring_window_forward(self.root, persistent_topmost=False)
            messagebox.showerror("Build failed", msg, parent=self.root)
            self.root.destroy()
            return

        _max_batch = 400
        for _ in range(_max_batch):
            try:
                msg = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.append_log(msg)

        self.root.after(100, self._poll_logs)

    def open_log_folder(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.append_log(f"Log folder: {LOG_DIR}")

    def start_wizard(self) -> None:
        try:
            self._run_wizard()
        except Exception as exc:
            traceback.print_exc()
            messagebox.showerror("Error", str(exc))

    def _run_wizard(self) -> None:
        if not LAST_IMAGERY_ROOT_FILE.exists():
            self.append_log("No imagery path found. Skipping SQLite builder.")
            self.status_var.set("No imagery to process")
            return

        imagery_root = Path(LAST_IMAGERY_ROOT_FILE.read_text(encoding="utf-8").strip())
        self.imagery_root = imagery_root
        if not imagery_root.is_dir():
            messagebox.showerror("Missing imagery folder", f"Saved imagery folder not found:\n{imagery_root}")
            return

        state_dirs = find_state_imagery_dirs(imagery_root)
        state_dirs, filter_note = filter_state_dirs_for_last_imagery_session(state_dirs)
        if filter_note:
            self.append_log(filter_note)
        if not state_dirs:
            messagebox.showerror("No imagery found", f"No state folders with numeric zoom levels were found under:\n{imagery_root}")
            return

        out_parent = imagery_root.parent

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = LOG_DIR / f"atak_sqlite_builder_{timestamp}.log"
        self.log_file = log_file
        self.logger = LoggerManager(log_file, self.log_queue).get()

        self.summary_var.set(
            f"Imagery root: {imagery_root} | States detected: {len(state_dirs)} | Output parent: {out_parent} | Log: {log_file}"
        )
        self.status_var.set("Running...")
        self.append_log("=" * 80)
        self.append_log(f"Starting build at {datetime.now().isoformat(timespec='seconds')}")
        self.append_log(f"Imagery root: {imagery_root}")
        self.append_log(f"Output parent: {out_parent}")
        self.append_log(f"States detected: {', '.join(p.name for p in state_dirs)}")
        self.append_log(f"Log file: {log_file}")
        self.append_log("=" * 80)

        self.worker = threading.Thread(target=self._worker_run_all, args=(state_dirs, out_parent), daemon=True)
        self.worker.start()

    def _worker_run_all(self, state_dirs: List[Path], out_parent: Path) -> None:
        assert self.logger is not None
        try:
            total = len(state_dirs)
            built = 0

            from datetime import datetime
            date_str = datetime.now().strftime("%Y%m%d")

            upload_dir = out_parent / f"ATAK_Upload_{date_str}"
            upload_dir.mkdir(parents=True, exist_ok=True)

            self.append_log(f"Upload folder: {upload_dir}")

            for idx, source_dir in enumerate(state_dirs, start=1):
                self.status_var.set(f"Running {idx}/{total}: {source_dir.name}")
                self.append_log("")
                sqlite_path = atak_upload_sqlite_path(upload_dir, source_dir.name)
                self.append_log(f"[{idx}/{total}] State: {source_dir.name} -> {sqlite_path.name}")

                config = BuildConfig(
                    source_dir=source_dir,
                    output_dir=out_parent,
                    sqlite_path=sqlite_path,
                    provider=source_dir.name,
                )
                Builder(config, self.logger).run()
                built += 1

            self.status_var.set("Build complete")
            self.append_log("")
            self.append_log(f"All state SQLite builds complete: {built} file(s) in {upload_dir}")
            self._last_upload_dir = upload_dir
            self.completion_message = "SQLite build complete."
        except Exception as exc:
            self.status_var.set("Build failed")
            tb = traceback.format_exc()
            self.logger.error("Unhandled exception: %s", exc)
            self.logger.error(tb)
            self.append_log("")
            self.append_log(f"FAILED: {exc}")
            self.append_log(f"See log: {self.log_file}")
            self.error_message = f"Build failed.\n\nError:\n{exc}\n\nSee log:\n{self.log_file}"

    def _worker_run(self) -> None:
        assert self.config is not None
        assert self.logger is not None
        try:
            builder = Builder(self.config, self.logger)
            builder.run()
            self.status_var.set("Build complete")
            self.append_log("")
            self.append_log(f"SUCCESS: {self.config.sqlite_path}")
            self.append_log(f"Log saved to: {self.log_file}")
            self.completion_message = "SQLite build complete."
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(0)
            try:
                self.root.after(0, lambda: (self.root.quit(), self.root.destroy()))
            except Exception:
                pass
        except Exception as exc:
            self.status_var.set("Build failed")
            tb = traceback.format_exc()
            self.logger.error("Unhandled exception: %s", exc)
            self.logger.error(tb)
            self.append_log("")
            self.append_log(f"FAILED: {exc}")
            self.append_log(f"See log: {self.log_file}")
            self.error_message = f"Build failed.\n\nError:\n{exc}\n\nSee log:\n{self.log_file}"

    def run(self) -> None:
        self.root.mainloop()


def cli_main(args: argparse.Namespace) -> int:
    source_dir = Path(args.source).expanduser().resolve()
    output_parent = Path(args.output).expanduser().resolve()
    output_dir = output_parent / derive_atak_folder_name(source_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    sqlite_name = args.name or derive_output_name(source_dir)
    if not sqlite_name.lower().endswith(".sqlite"):
        sqlite_name += ".sqlite"
    sqlite_path = output_dir / sqlite_name

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"atak_sqlite_builder_{timestamp}.log"
    logger = LoggerManager(log_file).get()

    config = BuildConfig(
        source_dir=source_dir,
        output_dir=output_dir,
        sqlite_path=sqlite_path,
        provider=args.provider,
    )

    logger.info("CLI mode")
    logger.info("Log file: %s", log_file)
    try:
        Builder(config, logger).run()
        logger.info("SUCCESS: %s", sqlite_path)
        print(f"\nSuccess: {sqlite_path}")
        print(f"Log: {log_file}")
        return 0
    except Exception as exc:
        logger.error("FAILED: %s", exc)
        logger.error(traceback.format_exc())
        print(f"\nFailed: {exc}")
        print(f"Log: {log_file}")
        return 1


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build/update an ATAK-style imagery SQLite cache from XYZ tile folders.")
    parser.add_argument("source", nargs="?", help="State/source imagery folder containing numeric zoom folders")
    parser.add_argument("output", nargs="?", help="Output parent folder")
    parser.add_argument("--gui", action="store_true", help="Launch GUI wizard")
    parser.add_argument("--name", help="Output SQLite filename")
    parser.add_argument("--provider", default=DEFAULT_PROVIDER, help=f"Tile provider name (default: {DEFAULT_PROVIDER})")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.gui or (not args.source and not args.output):
        app = App()
        app.run()
        return 0
    if not args.source or not args.output:
        print("CLI mode requires: source and output")
        return 2
    return cli_main(args)


if __name__ == "__main__":
    sys.exit(main())
