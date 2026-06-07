#!/usr/bin/env python3
"""PyInstaller entry: ATAK Device Installer (Windows)."""
from __future__ import annotations

import multiprocessing
import os
import sys
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent
if BASE.exists():
    sys.path.insert(0, str(BASE))


def _configure_frozen_tk() -> None:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        os.environ["TCL_LIBRARY"] = str(Path(sys._MEIPASS) / "_tcl_data")
        os.environ["TK_LIBRARY"] = str(Path(sys._MEIPASS) / "_tk_data")


def main() -> None:
    try:
        _configure_frozen_tk()
        import atak_adb_deploy_win as installer
        installer.main()
    except Exception as exc:
        tb = traceback.format_exc()
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.configure(cursor="arrow")
            root.withdraw()
            messagebox.showerror("ATAK Device Installer", f"{exc}\n\n{tb}")
            root.destroy()
        except Exception:
            pass
        raise


if __name__ == "__main__":
    multiprocessing.freeze_support()
    # Only the frozen EXE may start the GUI (see windows_launcher.py).
    if getattr(sys, "frozen", False):
        main()
    elif os.environ.get("ATAK_ALLOW_DEV_LAUNCH") == "1":
        main()
