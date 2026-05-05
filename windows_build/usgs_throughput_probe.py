"""USGS orthophoto tile throughput sampling — no tkinter (safe in a multiprocessing spawn child)."""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional

import requests

_scripts = Path(__file__).resolve().parent
if str(_scripts) not in sys.path:
    sys.path.insert(0, str(_scripts))

from imagery_tile_selection import lonlat_to_tile  # noqa: E402

USGS_TILE_URL = (
    "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}"
)
USER_AGENT = "ATAK-Ortho-Downloader/1.1"
MAX_DOWNLOAD_WORKERS = 24


def measure_usgs_imagery_effective_bps() -> Optional[float]:
    """Sample aggregate bytes/sec with warm DNS/TLS and timed parallel bursts (matches worker count)."""
    z = 12
    lon0, lat0 = -98.35, 39.12
    x0, y0 = lonlat_to_tile(lon0, lat0, z)

    def urls_for_grid(ox: int, oy: int) -> List[str]:
        out: List[str] = []
        for i in range(MAX_DOWNLOAD_WORKERS):
            dx = i % 4
            dy = i // 4
            x, y = x0 + dx + ox, y0 + dy + oy
            out.append(USGS_TILE_URL.format(z=z, y=y, x=x))
        return out

    warm = requests.Session()
    warm.headers.update({"User-Agent": USER_AGENT})
    try:
        r0 = warm.get(USGS_TILE_URL.format(z=z, y=y0, x=x0), timeout=40)
        r0.raise_for_status()
        _ = r0.content
    except Exception:
        return None

    def fetch_bytes(url: str) -> int:
        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT})
        try:
            rr = s.get(url, timeout=45)
            rr.raise_for_status()
            return len(rr.content)
        except Exception:
            return 0

    def burst_bps(urls: List[str]) -> Optional[float]:
        t0 = time.perf_counter()
        try:
            with ThreadPoolExecutor(max_workers=MAX_DOWNLOAD_WORKERS) as pool:
                parts = list(pool.map(fetch_bytes, urls))
        except Exception:
            return None
        elapsed = time.perf_counter() - t0
        total_b = sum(parts)
        ok = sum(1 for p in parts if p >= 512)
        if ok < max(4, MAX_DOWNLOAD_WORKERS // 2) or elapsed < 0.06 or total_b < 4096:
            return None
        return total_b / elapsed

    b1 = burst_bps(urls_for_grid(0, 0))
    b2 = burst_bps(urls_for_grid(5, 5))
    candidates = [b for b in (b1, b2) if b is not None]
    if not candidates:
        return None
    return max(candidates)


def run_probe_process_entry(result_q: object) -> None:
    """Picklable entry for ``multiprocessing.Process`` (spawn); pushes Optional[float]."""
    try:
        bps = measure_usgs_imagery_effective_bps()
    except Exception:
        bps = None
    try:
        result_q.put(bps)
    except Exception:
        pass
