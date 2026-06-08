"""Imagery tile throughput sampling — no tkinter (safe in a multiprocessing spawn child)."""

from __future__ import annotations

import os
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
GOOGLE_HYBRID_TILE_URL = "https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}"
USER_AGENT = "ATAK-Ortho-Downloader/1.1"
DEFAULT_PROBE_WORKERS = min(48, max(8, (os.cpu_count() or 4) * 4))


def _probe_worker_count() -> int:
    raw = (os.environ.get("ATAK_IMAGERY_PROBE_WORKERS") or "").strip()
    if raw.isdigit():
        return max(1, min(64, int(raw)))
    return DEFAULT_PROBE_WORKERS


def _burst_bps(urls: List[str], *, workers: int) -> Optional[float]:
    def fetch_bytes(url: str) -> int:
        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT})
        try:
            rr = s.get(url, timeout=45)
            rr.raise_for_status()
            return len(rr.content)
        except Exception:
            return 0

    t0 = time.perf_counter()
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            parts = list(pool.map(fetch_bytes, urls))
    except Exception:
        return None
    elapsed = time.perf_counter() - t0
    total_b = sum(parts)
    ok = sum(1 for p in parts if p >= 512)
    if ok < max(4, workers // 3) or elapsed < 0.05 or total_b < 4096:
        return None
    return total_b / elapsed


def _urls_for_grid(
    template: str,
    *,
    z: int,
    lon0: float,
    lat0: float,
    workers: int,
    ox: int,
    oy: int,
) -> List[str]:
    x0, y0 = lonlat_to_tile(lon0, lat0, z)
    cols = max(4, int(round(workers**0.5)))
    out: List[str] = []
    for i in range(workers):
        dx = i % cols
        dy = i // cols
        x, y = x0 + dx + ox, y0 + dy + oy
        if "{z}/{y}/{x}" in template:
            out.append(template.format(z=z, y=y, x=x))
        else:
            out.append(template.format(z=z, x=x, y=y))
    return out


def _measure_source_bps(
    template: str,
    *,
    z: int,
    lon0: float,
    lat0: float,
    workers: int,
) -> Optional[float]:
    warm = requests.Session()
    warm.headers.update({"User-Agent": USER_AGENT})
    x0, y0 = lonlat_to_tile(lon0, lat0, z)
    try:
        if "{z}/{y}/{x}" in template:
            warm_url = template.format(z=z, y=y0, x=x0)
        else:
            warm_url = template.format(z=z, x=x0, y=y0)
        r0 = warm.get(warm_url, timeout=40)
        r0.raise_for_status()
        _ = r0.content
    except Exception:
        return None

    b1 = _burst_bps(_urls_for_grid(template, z=z, lon0=lon0, lat0=lat0, workers=workers, ox=0, oy=0), workers=workers)
    b2 = _burst_bps(_urls_for_grid(template, z=z, lon0=lon0, lat0=lat0, workers=workers, ox=5, oy=5), workers=workers)
    candidates = [b for b in (b1, b2) if b is not None]
    if not candidates:
        return None
    return max(candidates)


def measure_usgs_imagery_effective_bps() -> Optional[float]:
    """Sample aggregate USGS bytes/sec with warm DNS/TLS and timed parallel bursts."""
    workers = _probe_worker_count()
    return _measure_source_bps(
        USGS_TILE_URL,
        z=12,
        lon0=-98.35,
        lat0=39.12,
        workers=workers,
    )


def measure_google_hybrid_imagery_effective_bps() -> Optional[float]:
    """Sample aggregate Google Hybrid bytes/sec (radius z16+ jobs)."""
    workers = _probe_worker_count()
    return _measure_source_bps(
        GOOGLE_HYBRID_TILE_URL,
        z=16,
        lon0=-98.35,
        lat0=39.12,
        workers=workers,
    )


def measure_imagery_effective_bps(*, include_google: bool = False) -> Optional[float]:
    """Best observed throughput across sources relevant to the upcoming download."""
    candidates: List[float] = []
    usgs = measure_usgs_imagery_effective_bps()
    if usgs is not None and usgs > 0:
        candidates.append(usgs)
    if include_google:
        google = measure_google_hybrid_imagery_effective_bps()
        if google is not None and google > 0:
            candidates.append(google)
    if not candidates:
        return None
    return max(candidates)


def run_probe_process_entry(result_q: object, include_google: bool = False) -> None:
    """Picklable entry for ``multiprocessing.Process`` (spawn); pushes Optional[float]."""
    try:
        bps = measure_imagery_effective_bps(include_google=include_google)
    except Exception:
        bps = None
    try:
        result_q.put(bps)
    except Exception:
        pass
