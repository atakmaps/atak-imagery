"""Discover and install bundled add-on ATAK plugin APKs (installer + imagery downloader).

TAK-UV-PRO is never installed from this path — it comes from GitHub / manifest only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Optional

from atak_version_policy import is_blocked_legacy_55_apk_filename

InstallApkFn = Callable[..., None]


def _is_uvpro_apk_filename(name: str) -> bool:
    compact = name.lower().replace("_", "").replace("-", "").replace(" ", "")
    return "uvpro" in compact or "takuvpro" in compact


def iter_bundled_addon_apks(plugin_root: Path) -> List[Path]:
    """APKs under *plugin_root*, excluding UV-PRO filenames; one path per basename."""
    if not plugin_root.is_dir():
        return []
    candidates = sorted(
        (
            p
            for p in plugin_root.rglob("*.apk")
            if p.is_file()
            and not _is_uvpro_apk_filename(p.name)
            and not is_blocked_legacy_55_apk_filename(p.name)
        ),
        key=lambda p: str(p).lower(),
    )
    seen: set[str] = set()
    out: List[Path] = []
    for p in candidates:
        if p.name in seen:
            continue
        seen.add(p.name)
        out.append(p)
    return out


def install_bundled_addon_apks(
    serial: str,
    log_fn,
    status_cb,
    install_apk: InstallApkFn,
    *,
    plugin_root: Path,
) -> None:
    """Install each bundled add-on APK (non-fatal per file)."""
    apks = iter_bundled_addon_apks(plugin_root)
    if not apks:
        log_fn("No bundled add-on plugins to install.")
        return
    log_fn(f"Installing {len(apks)} bundled add-on plugin(s)…")
    for apk in apks:
        rel = apk.relative_to(plugin_root)
        try:
            if status_cb:
                status_cb(f"Add-on: {apk.name}…")
            log_fn(f"Installing bundled add-on: {rel}")
            install_apk(serial, apk, status_cb, package_name=None)
        except Exception as exc:
            log_fn(f"Warning: bundled add-on failed ({apk.name}): {exc}")
