#!/usr/bin/env python3
"""
Imagery pipeline entry **only** for ATAK Device Installer (``atak_adb_deploy``).

The standalone Imagery app is ``atak_downloader_finalbuild_win.py``. This wrapper sets
``ATAK_DOWNLOADER_LAUNCHED_FROM_DEVICE_INSTALLER`` so the shared core skips the
standalone USB/adb gate and session Exit dialog (Installer already handled device UX).
"""

from __future__ import annotations

import os


def main() -> None:
    os.environ["ATAK_DOWNLOADER_LAUNCHED_FROM_DEVICE_INSTALLER"] = "1"
    import atak_downloader_finalbuild_win as core

    core.main()


if __name__ == "__main__":
    main()
