"""Resolve local imagery tiles and DTED state ZIPs under a user-chosen folder tree."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

ImageryTileKey = Tuple[int, int, int]
ScanProgressCb = Callable[[int, int], None]


def _emit_scan_progress(
    progress_cb: Optional[ScanProgressCb],
    paths_seen: int,
    indexed: int,
    *,
    last_emit: int,
    emit_every: int,
) -> int:
    if progress_cb is None:
        return last_emit
    if paths_seen <= 0 or paths_seen - last_emit >= emit_every:
        progress_cb(paths_seen, indexed)
        return paths_seen
    return last_emit


def build_imagery_tile_index(
    raw_root: Path,
    progress_cb: Optional[ScanProgressCb] = None,
    *,
    emit_every: int = 200,
) -> Dict[ImageryTileKey, Path]:
    """
    Index ``…/z/x/y.jpg`` tiles anywhere under ``raw_root``.

    Keys are ``(z, x, y)`` only — region folder names (``Kansas``, ``Radius``, etc.)
    are not part of the key so radius downloads can reuse tiles stored under any
    subdirectory layout.

    ``progress_cb(paths_seen, tiles_indexed)`` is invoked periodically while walking.
    """
    index: Dict[ImageryTileKey, Path] = {}
    if not raw_root.is_dir():
        return index

    paths_seen = 0
    last_emit = 0
    if progress_cb is not None:
        progress_cb(0, 0)

    for dirpath, _dirnames, filenames in os.walk(raw_root, followlinks=False):
        for name in filenames:
            paths_seen += 1
            last_emit = _emit_scan_progress(
                progress_cb, paths_seen, len(index), last_emit=last_emit, emit_every=emit_every
            )
            if not name.lower().endswith(".jpg"):
                continue
            path = Path(dirpath) / name
            if not path.is_file():
                continue
            try:
                y = int(path.stem)
                x = int(path.parent.name)
                z = int(path.parent.parent.name)
            except (ValueError, AttributeError):
                continue
            key = (z, x, y)
            prev = index.get(key)
            if prev is None or len(path.parts) < len(prev.parts):
                index[key] = path

    if progress_cb is not None:
        progress_cb(paths_seen, len(index))
    return index


def find_local_imagery_tile(
    raw_root: Optional[Path],
    imagery_index: Optional[Dict[ImageryTileKey, Path]],
    state_label: str,
    z: int,
    x: int,
    y: int,
) -> Optional[Path]:
    """Return a local tile path if found directly or via a pre-built index."""
    if raw_root is None or not raw_root.is_dir():
        return None
    direct = raw_root / state_label / str(z) / str(x) / f"{y}.jpg"
    if direct.is_file():
        return direct
    if imagery_index is not None:
        hit = imagery_index.get((z, x, y))
        if hit is not None and hit.is_file():
            return hit
    return None


def build_dted_state_zip_index(
    dted_root: Path,
    progress_cb: Optional[ScanProgressCb] = None,
    *,
    emit_every: int = 100,
) -> Dict[str, Path]:
    """
    Index ``StateName/StateName.zip`` (or ``StateName.zip``) anywhere under ``dted_root``.

    ``progress_cb(paths_seen, zips_indexed)`` is invoked periodically while walking.
    """
    index: Dict[str, Path] = {}
    if not dted_root.is_dir():
        return index

    paths_seen = 0
    last_emit = 0
    if progress_cb is not None:
        progress_cb(0, 0)

    for dirpath, _dirnames, filenames in os.walk(dted_root, followlinks=False):
        for name in filenames:
            paths_seen += 1
            last_emit = _emit_scan_progress(
                progress_cb, paths_seen, len(index), last_emit=last_emit, emit_every=emit_every
            )
            if not name.lower().endswith(".zip"):
                continue
            path = Path(dirpath) / name
            if not path.is_file():
                continue
            state_name = path.stem
            preferred = path.parent.name == state_name
            prev = index.get(state_name)
            if prev is None:
                index[state_name] = path
                continue
            prev_preferred = prev.parent.name == state_name
            if preferred and not prev_preferred:
                index[state_name] = path
            elif preferred == prev_preferred and len(path.parts) < len(prev.parts):
                index[state_name] = path

    if progress_cb is not None:
        progress_cb(paths_seen, len(index))
    return index


def find_local_dted_state_zip(
    dted_root: Optional[Path],
    dted_index: Optional[Dict[str, Path]],
    state_name: str,
) -> Optional[Path]:
    """Return a local DTED state ZIP if found directly or anywhere under ``dted_root``."""
    if dted_root is None or not dted_root.is_dir():
        return None
    direct = dted_root / state_name / f"{state_name}.zip"
    if direct.is_file():
        return direct
    if dted_index is not None:
        hit = dted_index.get(state_name)
        if hit is not None and hit.is_file():
            return hit
    return None
