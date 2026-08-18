"""
Tile coverage for USGS orthophoto downloads vs state GeoJSON rings.

- Includes a tile when its Web Mercator center lies inside the state polygon, OR
  when that center is within ``boundary_buffer_m`` meters of any polygon edge
  (extra imagery past the nominal boundary so boundary lines stay visible).

Precomputed tile lists (per state, per zoom) live under ``data/tile_plans/v1/`` as
``.tiles.gz`` files; build them with ``scripts/build_tile_plan_cache.py``. At
runtime, ``build_tiles_for_state`` loads a cache when ``geojson_path`` and
``tile_plan_dir`` are set and the file matches the current boundary GeoJSON CRC
and buffer distance.

Large-state bbox scans (CPU-heavy) use multiple processes when the rectangle has
at least ``_TILE_PLAN_PARALLEL_MIN_CELLS`` cells. Override worker count with
``ATAK_TILE_PLAN_WORKERS`` (set to ``0`` to disable parallelism). Defaults to
``os.cpu_count()`` (capped).
"""
from __future__ import annotations

import gzip
import math
import multiprocessing
import os
import struct
import time
import zlib
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path
from typing import Callable, Dict, Iterator, List, NamedTuple, Optional, Tuple

# Miles beyond the GeoJSON boundary to keep imagery for (edge tiles + visibility).
STATE_BOUNDARY_BUFFER_MILES = 3.0
METERS_PER_MILE = 1609.344
DEFAULT_BOUNDARY_BUFFER_M = STATE_BOUNDARY_BUFFER_MILES * METERS_PER_MILE

# Subfolder under ``Imagery/`` for fixed-radius downloads (not a state name).
RADIUS_REGION_FOLDER = "Radius"

_SEGMENT_SAMPLES = 16

# CPU-heavy: scan every cell in the state's Web Mercator bbox. Above this size, split
# along X or Y (whichever is longer) so all cores stay busy even for tall-skinny rects.
_TILE_PLAN_PARALLEL_MIN_CELLS = 12_000

# Gzip tile list cache: magic, format, zoom, geojson_crc32, boundary_m (double), n, then n*(x,y) uint32 pairs.
_TILE_PLAN_MAGIC = b"ATKP"
_TILE_PLAN_FORMAT = 1
_TILE_PLAN_HEADER = struct.Struct("!4sIIIdI")  # 28 bytes


def crc32_file(path: Path) -> int:
    """CRC-32 of file bytes (unsigned)."""
    h = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h = zlib.crc32(chunk, h)
    return h & 0xFFFFFFFF


def _tile_plan_cache_path(tile_plan_dir: Path, state_name: str, zoom: int) -> Path:
    safe = state_name.replace("/", "_")
    return tile_plan_dir / f"{safe}_z{zoom}.tiles.gz"


def try_load_tile_plan_cache(
    cache_path: Path,
    zoom: int,
    boundary_buffer_m: float,
    geojson_crc32: int,
) -> Optional[List[Tuple[int, int]]]:
    if not cache_path.is_file():
        return None
    try:
        raw = gzip.decompress(cache_path.read_bytes())
    except (OSError, EOFError, gzip.BadGzipFile):
        return None
    if len(raw) < _TILE_PLAN_HEADER.size:
        return None
    magic, fmt, z, crc, buf_m, n = _TILE_PLAN_HEADER.unpack_from(raw, 0)
    if magic != _TILE_PLAN_MAGIC or fmt != _TILE_PLAN_FORMAT or z != zoom or crc != geojson_crc32:
        return None
    if abs(buf_m - boundary_buffer_m) > 1e-6:
        return None
    body = raw[_TILE_PLAN_HEADER.size :]
    if len(body) != n * 8:
        return None
    tiles: List[Tuple[int, int]] = []
    off = 0
    for _ in range(n):
        x, y = struct.unpack_from("!II", body, off)
        off += 8
        tiles.append((x, y))
    return tiles


def save_tile_plan_cache(
    cache_path: Path,
    zoom: int,
    boundary_buffer_m: float,
    geojson_crc32: int,
    tiles: List[Tuple[int, int]],
) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    header = _TILE_PLAN_HEADER.pack(
        _TILE_PLAN_MAGIC,
        _TILE_PLAN_FORMAT,
        zoom,
        geojson_crc32 & 0xFFFFFFFF,
        float(boundary_buffer_m),
        len(tiles),
    )
    body = b"".join(struct.pack("!II", int(x), int(y)) for x, y in tiles)
    blob = gzip.compress(header + body, compresslevel=6, mtime=0)
    cache_path.write_bytes(blob)


def lonlat_to_tile(lon: float, lat: float, zoom: int) -> Tuple[int, int]:
    lat = max(min(lat, 85.05112878), -85.05112878)
    n = 2.0 ** zoom
    xtile = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    xtile = max(0, min(int(n) - 1, xtile))
    ytile = max(0, min(int(n) - 1, ytile))
    return xtile, ytile


def tile_center_lonlat(x: int, y: int, z: int) -> Tuple[float, float]:
    n = 2.0 ** z
    lon_deg = (x + 0.5) / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * ((y + 0.5) / n))))
    lat_deg = math.degrees(lat_rad)
    return lon_deg, lat_deg


def tile_lonlat_bounds(x: int, y: int, z: int) -> Tuple[float, float, float, float]:
    """West, east, south, north edges in degrees (EPSG:4326), matching ``lonlat_to_tile`` grid."""
    n = 2.0 ** z
    lon_w = (x / n) * 360.0 - 180.0
    lon_e = ((x + 1) / n) * 360.0 - 180.0
    lat_n = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y / n)))))
    lat_s = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ((y + 1) / n)))))
    south = min(lat_s, lat_n)
    north = max(lat_s, lat_n)
    return lon_w, lon_e, south, north


def _point_in_lonlat_rect(
    lon: float, lat: float, lon_w: float, lon_e: float, south: float, north: float
) -> bool:
    if not (south <= lat <= north):
        return False
    if lon_w <= lon_e:
        return lon_w <= lon <= lon_e
    return lon >= lon_w or lon <= lon_e


def geodesic_disk_intersects_tile(
    center_lat: float, center_lon: float, radius_m: float, x: int, y: int, z: int
) -> bool:
    """
    True if a great-circle disk around ``(center_lat, center_lon)`` with radius ``radius_m``
    intersects the Web Mercator tile (any overlap). Uses tile corner/edge checks in WGS84.
    """
    if radius_m <= 0:
        return False
    lon_w, lon_e, south, north = tile_lonlat_bounds(x, y, z)
    corners = (
        (lon_w, north),
        (lon_e, north),
        (lon_e, south),
        (lon_w, south),
    )
    for clon, clat in corners:
        if haversine_m(center_lat, center_lon, clat, clon) <= radius_m:
            return True
    if _point_in_lonlat_rect(center_lon, center_lat, lon_w, lon_e, south, north):
        return True
    edges = (
        (lon_w, north, lon_e, north),
        (lon_e, north, lon_e, south),
        (lon_e, south, lon_w, south),
        (lon_w, south, lon_w, north),
    )
    for lon1, lat1, lon2, lat2 in edges:
        if min_dist_point_to_segment_m(center_lon, center_lat, lon1, lat1, lon2, lat2) <= radius_m:
            return True
    return False


def _radius_search_bbox(
    center_lat: float, center_lon: float, radius_m: float, zoom: int
) -> Tuple[int, int, int, int]:
    """Tile-index rectangle (x0, x1, y0, y1) guaranteed to contain the disk."""
    margin_m = max(200.0, radius_m * 0.01)
    r_search = radius_m + margin_m
    dlat = r_search / 111_320.0
    cos_lat = max(0.2, math.cos(math.radians(center_lat)))
    dlon = r_search / (111_320.0 * cos_lat)
    min_lat = max(-85.05112878, center_lat - dlat)
    max_lat = min(85.05112878, center_lat + dlat)
    min_lon = center_lon - dlon
    maxlon = center_lon + dlon

    min_x, max_y = lonlat_to_tile(min_lon, min_lat, zoom)
    max_x, min_y = lonlat_to_tile(maxlon, max_lat, zoom)
    x0, x1 = sorted((min_x, max_x))
    y0, y1 = sorted((min_y, max_y))
    return x0, x1, y0, y1


# Slack on the cheap planar reject below. The equirectangular approximation drifts from
# the great-circle distance by well under 1% at continental radii; 5% + 5 km keeps the
# fast path conservative so it can only ever skip the expensive test, never a real tile.
_FAST_REJECT_SLACK_FRAC = 1.05
_FAST_REJECT_SLACK_M = 5000.0


def _tile_certainly_outside_disk(
    center_lat: float,
    center_lon: float,
    radius_m: float,
    lon_w: float,
    lon_e: float,
    south: float,
    north: float,
) -> bool:
    """
    Cheap conservative reject for tiles far outside the disk.

    ``geodesic_disk_intersects_tile`` short-circuits on the first corner inside the
    radius, so tiles *inside* the disk are already cheap. The expensive case is a tile
    outside it: four corner haversines, then four edges sampled 16 times each. Those
    tiles are the ~21% of the bounding box that falls outside the inscribed circle, and
    at high zoom that is millions of cells. One planar distance-to-rectangle test
    removes almost all of them.
    """
    if lon_w > lon_e:  # antimeridian-crossing tile: leave it to the exact test
        return False
    lat_near = min(max(center_lat, south), north)
    lon_near = min(max(center_lon, lon_w), lon_e)
    dlat_m = (center_lat - lat_near) * 111_320.0
    dlon_m = (center_lon - lon_near) * 111_320.0 * math.cos(math.radians(lat_near))
    limit = radius_m * _FAST_REJECT_SLACK_FRAC + _FAST_REJECT_SLACK_M
    return (dlat_m * dlat_m + dlon_m * dlon_m) > limit * limit


def iter_tiles_for_radius(
    center_lat: float, center_lon: float, radius_miles: float, zoom: int
) -> Iterator[Tuple[int, int]]:
    """
    Yield tiles at ``zoom`` whose Web Mercator footprint intersects a geodesic circle
    of ``radius_miles`` around ``(center_lat, center_lon)``. The full tile is included
    whenever the circle intersects it (not only the centroid).

    Streaming matters at high zoom: a 130-mile radius is ~17.8M tiles at z18 and ~71M at
    z19, and materialising those as a list of tuples costs gigabytes.
    """
    radius_m = max(0.0, float(radius_miles)) * METERS_PER_MILE
    if radius_m <= 0:
        return

    x0, x1, y0, y1 = _radius_search_bbox(center_lat, center_lon, radius_m, zoom)
    for xx in range(x0, x1 + 1):
        for yy in range(y0, y1 + 1):
            lon_w, lon_e, south, north = tile_lonlat_bounds(xx, yy, zoom)
            if _tile_certainly_outside_disk(
                center_lat, center_lon, radius_m, lon_w, lon_e, south, north
            ):
                continue
            if geodesic_disk_intersects_tile(center_lat, center_lon, radius_m, xx, yy, zoom):
                yield (xx, yy)


def compute_tiles_for_radius(
    center_lat: float, center_lon: float, radius_miles: float, zoom: int
) -> List[Tuple[int, int]]:
    """Materialised :func:`iter_tiles_for_radius`. Prefer the iterator above z16."""
    return list(iter_tiles_for_radius(center_lat, center_lon, radius_miles, zoom))


def count_tiles_for_radius(
    center_lat: float, center_lon: float, radius_miles: float, zoom: int
) -> int:
    """Tile count for the disk in constant memory (no tile list is built)."""
    return sum(1 for _ in iter_tiles_for_radius(center_lat, center_lon, radius_miles, zoom))


# Above this zoom, radius coverage counts are modelled rather than enumerated. Exact work
# quadruples per zoom, so z16+ costs minutes of CPU and gigabytes of RAM for a number that
# is only ever read off an estimate screen.
RADIUS_COUNT_EXACT_MAX_ZOOM = 15


def estimate_tile_counts_for_radius(
    center_lat: float,
    center_lon: float,
    radius_miles: float,
    zoom_min: int,
    zoom_max: int,
    *,
    exact_max_zoom: int = RADIUS_COUNT_EXACT_MAX_ZOOM,
    on_zoom: Optional[Callable[[int, int, bool], None]] = None,
) -> Tuple[Dict[int, int], int]:
    """
    Tile counts per zoom for a radius download: exact through ``exact_max_zoom``,
    modelled above it. Returns ``(counts_by_zoom, highest_exact_zoom)``.

    The count over a fixed disk is ``N(z) = a * 4^z + b * 2^z``. The ``4^z`` term is
    disk-area over tile-area; the ``2^z`` term is the ring of partially covered tiles
    around the perimeter, which grows with the circumference. Fitting ``a`` and ``b`` on
    the two highest exact zooms reproduces measured counts to within 0.03% — far inside
    the error of the average-tile-size figure the byte estimate already uses.

    ``on_zoom(zoom, count, is_exact)`` is called as each zoom resolves, for progress.
    """
    counts: Dict[int, int] = {}
    exact_through = min(exact_max_zoom, zoom_max)

    for z in range(zoom_min, exact_through + 1):
        counts[z] = count_tiles_for_radius(center_lat, center_lon, radius_miles, z)
        if on_zoom is not None:
            on_zoom(z, counts[z], True)

    if zoom_max <= exact_through:
        return counts, exact_through

    fit_zooms = [z for z in (exact_through - 1, exact_through) if z in counts]
    a = b = None
    if len(fit_zooms) == 2:
        z1, z2 = fit_zooms
        n1, n2 = float(counts[z1]), float(counts[z2])
        a1, b1 = 4.0 ** z1, 2.0 ** z1
        a2, b2 = 4.0 ** z2, 2.0 ** z2
        det = a1 * b2 - a2 * b1
        if det != 0.0:
            a = (n1 * b2 - n2 * b1) / det
            b = (a1 * n2 - a2 * n1) / det

    last_z = exact_through
    last_n = counts.get(last_z, 0)
    for z in range(exact_through + 1, zoom_max + 1):
        if a is not None and a > 0.0:
            n = int(round(a * (4.0 ** z) + b * (2.0 ** z)))
        else:
            # Degenerate fit (tiny radius, or too few exact zooms): fall back to the
            # area term alone, which is the dominant one anyway.
            n = int(round(last_n * (4.0 ** (z - last_z))))
        # Coverage can only grow with zoom; never let a fit artifact invert that.
        counts[z] = max(n, counts.get(z - 1, 0))
        if on_zoom is not None:
            on_zoom(z, counts[z], False)

    return counts, exact_through


def bbox_for_rings(rings: List[List[Tuple[float, float]]]) -> Tuple[float, float, float, float]:
    xs: List[float] = []
    ys: List[float] = []
    for ring in rings:
        for x, y in ring:
            xs.append(x)
            ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def point_in_ring(lon: float, lat: float, ring: List[Tuple[float, float]]) -> bool:
    inside = False
    n = len(ring)
    if n < 3:
        return False
    x1, y1 = ring[0]
    for i in range(1, n + 1):
        x2, y2 = ring[i % n]
        if ((y1 > lat) != (y2 > lat)):
            xinters = (x2 - x1) * (lat - y1) / ((y2 - y1) or 1e-12) + x1
            if lon < xinters:
                inside = not inside
        x1, y1 = x2, y2
    return inside


def point_in_state(lon: float, lat: float, rings: List[List[Tuple[float, float]]]) -> bool:
    return any(point_in_ring(lon, lat, ring) for ring in rings)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters (WGS84 sphere)."""
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlamb = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlamb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, h)))


def min_dist_point_to_segment_m(
    lon: float, lat: float, lon1: float, lat1: float, lon2: float, lat2: float
) -> float:
    """Minimum meters from (lon,lat) to segment endpoints and linearly interpolated samples."""
    best = min(
        haversine_m(lat, lon, lat1, lon1),
        haversine_m(lat, lon, lat2, lon2),
    )
    for i in range(1, _SEGMENT_SAMPLES):
        t = i / _SEGMENT_SAMPLES
        lt = lat1 + t * (lat2 - lat1)
        ln = lon1 + t * (lon2 - lon1)
        best = min(best, haversine_m(lat, lon, lt, ln))
    return best


def min_distance_point_to_rings_m(lon: float, lat: float, rings: List[List[Tuple[float, float]]]) -> float:
    best = float("inf")
    for ring in rings:
        n = len(ring)
        if n < 2:
            continue
        for i in range(n):
            lon1, lat1 = ring[i]
            lon2, lat2 = ring[(i + 1) % n]
            best = min(best, min_dist_point_to_segment_m(lon, lat, lon1, lat1, lon2, lat2))
    return best


def _circle_lonlat_bbox(center_lat: float, center_lon: float, radius_m: float) -> Tuple[float, float, float, float]:
    """West, south, east, north: axis-aligned box containing the circle (spherical approx)."""
    dlat = radius_m / 111_320.0
    cos_lat = max(0.2, math.cos(math.radians(center_lat)))
    dlon = radius_m / (111_320.0 * cos_lat)
    return (
        center_lon - dlon,
        center_lat - dlat,
        center_lon + dlon,
        center_lat + dlat,
    )


def square_lonlat_footprint_for_radius_miles(
    center_lat: float, center_lon: float, radius_miles: float
) -> Tuple[float, float, float, float]:
    """
    West, south, east, north for an axis-aligned box matching the circle's diameter on the ground:
    ~``radius_miles`` statute miles from the center along both north–south and east–west
    (spherical approximation, same as ``_circle_lonlat_bbox``).
    """
    r_m = max(0.0, float(radius_miles)) * METERS_PER_MILE
    if r_m <= 0:
        raise ValueError("radius_miles must be positive")
    return _circle_lonlat_bbox(center_lat, center_lon, r_m)


def _lonlat_boxes_overlap(
    west1: float,
    south1: float,
    east1: float,
    north1: float,
    west2: float,
    south2: float,
    east2: float,
    north2: float,
) -> bool:
    return not (east1 < west2 or east2 < west1 or north1 < south2 or north2 < south1)


def state_names_intersecting_geodesic_circle(
    center_lat: float,
    center_lon: float,
    radius_miles: float,
    states: Dict[str, List[List[Tuple[float, float]]]],
) -> List[str]:
    """
    States to pull **full** DTED packages for after a radius imagery download.

    Uses a simple lat/lon bounding-box overlap between each state's footprint and the
    circle's bounding box (slightly conservative — may include an extra neighbor).
    """
    r_m = max(0.0, float(radius_miles)) * METERS_PER_MILE
    if r_m <= 0:
        return []
    cw, cs, ce, cn = _circle_lonlat_bbox(center_lat, center_lon, r_m)
    names: List[str] = []
    for name, rings in states.items():
        sw, ss, se, sn = bbox_for_rings(rings)
        if _lonlat_boxes_overlap(cw, cs, ce, cn, sw, ss, se, sn):
            names.append(name)
    return sorted(names)


def expand_bbox_by_buffer_m(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float, buffer_m: float
) -> Tuple[float, float, float, float]:
    if buffer_m <= 0:
        return min_lon, min_lat, max_lon, max_lat
    mid_lat = (min_lat + max_lat) / 2.0
    cos_lat = max(0.2, math.cos(math.radians(mid_lat)))
    dlat = buffer_m / 111_320.0
    dlon = buffer_m / (111_320.0 * cos_lat)
    return min_lon - dlon, min_lat - dlat, max_lon + dlon, max_lat + dlat


def tile_qualifies(
    lon: float, lat: float, rings: List[List[Tuple[float, float]]], boundary_buffer_m: float
) -> bool:
    if point_in_state(lon, lat, rings):
        return True
    if boundary_buffer_m <= 0:
        return False
    return min_distance_point_to_rings_m(lon, lat, rings) <= boundary_buffer_m


class TilePlanBuildResult(NamedTuple):
    """tiles plus whether they came from a precomputed ``.tiles.gz`` cache."""

    tiles: List[Tuple[int, int]]
    from_cache: bool


def _available_cpu_count() -> int:
    """
    CPUs this process can actually run on.

    On Linux, honor cpuset/cgroup affinity (common on VMs/containers) instead of
    raw os.cpu_count(), which may include unavailable cores.
    """
    try:
        affinity = os.sched_getaffinity(0)  # type: ignore[attr-defined]
        if affinity:
            return len(affinity)
    except (AttributeError, OSError):
        pass
    return os.cpu_count() or 4


def _tile_plan_worker_count() -> int:
    """Processes for bbox scan. Set ATAK_TILE_PLAN_WORKERS=0 to force single-process."""
    raw = os.environ.get("ATAK_TILE_PLAN_WORKERS", "").strip()
    if raw == "0":
        return 0
    if raw.isdigit() and int(raw) > 0:
        return min(64, int(raw))
    n = _available_cpu_count()
    return max(2, min(32, n))


def _split_axis_ranges(axis_start: int, axis_end: int, workers: int) -> List[Tuple[int, int]]:
    span = axis_end - axis_start + 1
    workers = max(1, min(workers, span))
    out: List[Tuple[int, int]] = []
    base, rem = divmod(span, workers)
    pos = axis_start
    for i in range(workers):
        w = base + (1 if i < rem else 0)
        if w <= 0:
            break
        lo, hi = pos, pos + w - 1
        out.append((lo, hi))
        pos = hi + 1
    return out


def _split_x_ranges(x_start: int, x_end: int, workers: int) -> List[Tuple[int, int]]:
    return _split_axis_ranges(x_start, x_end, workers)


def _split_y_ranges(y_start: int, y_end: int, workers: int) -> List[Tuple[int, int]]:
    return _split_axis_ranges(y_start, y_end, workers)


def _compute_tiles_rect_band(
    args: Tuple[
        int,
        int,
        int,
        int,
        int,
        float,
        List[List[Tuple[float, float]]],
        int,
    ],
) -> List[Tuple[int, int]]:
    """Picklable worker: qualifying (x, y) inside inclusive [x_lo,x_hi]×[y_lo,y_hi]."""
    (
        x_lo,
        x_hi,
        y_lo,
        y_hi,
        zoom,
        boundary_buffer_m,
        rings,
        band_idx,
    ) = args
    tiles: List[Tuple[int, int]] = []
    for x in range(x_lo, x_hi + 1):
        for y in range(y_lo, y_hi + 1):
            lon, lat = tile_center_lonlat(x, y, zoom)
            if tile_qualifies(lon, lat, rings, boundary_buffer_m):
                tiles.append((x, y))
    return tiles


def _compute_tiles_for_state(
    rings: List[List[Tuple[float, float]]],
    zoom: int,
    boundary_buffer_m: float,
    *,
    progress_interval_s: float = 0.0,
    progress_log: Optional[Callable[[int, int, int, float], None]] = None,
    parallel_band_done: Optional[Callable[[int, int, int, float], None]] = None,
    scan_progress_interval_s: float = 0.0,
    scan_progress_callback: Optional[Callable[[int, int, int, float], None]] = None,
) -> List[Tuple[int, int]]:
    """
    parallel_band_done(bands_done, bands_total, tiles_so_far, elapsed_s): called from the
    main process after each worker band finishes, enabling ETA updates during parallel scans.

    scan_progress_callback(cells_done, rect_total, qual_tiles, job_elapsed_s): optional
    heartbeat while scanning the Mercator bbox (multi-process or single-process). When
    ``scan_progress_interval_s > 0`` and parallel workers are used, the parent polls
    shared per-band counters on that interval.
    """
    min_lon, min_lat, max_lon, max_lat = bbox_for_rings(rings)
    min_lon, min_lat, max_lon, max_lat = expand_bbox_by_buffer_m(
        min_lon, min_lat, max_lon, max_lat, boundary_buffer_m
    )

    min_x, max_y = lonlat_to_tile(min_lon, min_lat, zoom)
    max_x, min_y = lonlat_to_tile(max_lon, max_lat, zoom)

    x_start, x_end = sorted((min_x, max_x))
    y_start, y_end = sorted((min_y, max_y))

    nx = x_end - x_start + 1
    ny = y_end - y_start + 1
    rect_total = max(nx * ny, 1)

    use_progress = progress_interval_s > 0 and progress_log is not None
    workers = _tile_plan_worker_count()
    if (
        not use_progress
        and workers > 0
        and rect_total >= _TILE_PLAN_PARALLEL_MIN_CELLS
    ):
        if nx >= ny:
            n_chunks = min(workers, nx)
            bands = _split_x_ranges(x_start, x_end, n_chunks)
            args_list = [
                (xa, xb, y_start, y_end, zoom, boundary_buffer_m, rings, bi, None, None)
                for bi, (xa, xb) in enumerate(bands)
            ]
        else:
            n_chunks = min(workers, ny)
            bands = _split_y_ranges(y_start, y_end, n_chunks)
            args_list = [
                (x_start, x_end, ya, yb, zoom, boundary_buffer_m, rings, bi, None, None)
                for bi, (ya, yb) in enumerate(bands)
            ]
        n_bands = len(args_list)
        band_cells = [(a[1] - a[0] + 1) * (a[3] - a[2] + 1) for a in args_list]
        job_t0 = time.perf_counter()
        ctx = multiprocessing.get_context("spawn")
        tiles_acc: List[Tuple[int, int]] = []
        results: List[Optional[List[Tuple[int, int]]]] = [None] * n_bands
        completed_cells = 0
        last_scan_cb_t = job_t0
        wait_timeout = 30.0
        if scan_progress_callback is not None and scan_progress_interval_s > 0:
            wait_timeout = max(0.2, float(scan_progress_interval_s))
        try:
            with ProcessPoolExecutor(max_workers=n_bands, mp_context=ctx) as ex:
                futures = {ex.submit(_compute_tiles_rect_band, a): i for i, a in enumerate(args_list)}
                bands_done = 0
                pending = set(futures)
                while pending:
                    done, pending = wait(pending, timeout=wait_timeout, return_when=FIRST_COMPLETED)
                    for fut in done:
                        idx = futures[fut]
                        results[idx] = fut.result()
                        completed_cells += band_cells[idx]
                        bands_done += 1
                    if parallel_band_done is not None:
                        tiles_so_far = sum(len(r) for r in results if r is not None)
                        parallel_band_done(
                            bands_done, n_bands, tiles_so_far, time.perf_counter() - job_t0
                        )
                    if scan_progress_callback is not None and scan_progress_interval_s > 0:
                        now = time.perf_counter()
                        if now - last_scan_cb_t >= scan_progress_interval_s:
                            tiles_so_far = sum(len(r) for r in results if r is not None)
                            scan_progress_callback(
                                completed_cells,
                                rect_total,
                                tiles_so_far,
                                now - job_t0,
                            )
                            last_scan_cb_t = now
        finally:
            pass
        if scan_progress_callback is not None and scan_progress_interval_s > 0:
            scan_progress_callback(
                rect_total,
                rect_total,
                sum(len(r) for r in results if r is not None),
                time.perf_counter() - job_t0,
            )
        for part in results:
            if part:
                tiles_acc.extend(part)
        return tiles_acc

    tiles: List[Tuple[int, int]] = []
    rect_done = 0
    job_t0 = time.perf_counter()
    last_log_t = job_t0
    last_scan_cb_t = job_t0
    scan_cell_stride = 65_536

    for x in range(x_start, x_end + 1):
        for y in range(y_start, y_end + 1):
            rect_done += 1
            lon, lat = tile_center_lonlat(x, y, zoom)
            if tile_qualifies(lon, lat, rings, boundary_buffer_m):
                tiles.append((x, y))
            if use_progress:
                now = time.perf_counter()
                if now - last_log_t >= progress_interval_s:
                    progress_log(len(tiles), rect_done, rect_total, now - job_t0)
                    last_log_t = now
            if scan_progress_callback is not None and scan_progress_interval_s > 0:
                now = time.perf_counter()
                if now - last_scan_cb_t >= scan_progress_interval_s or (
                    rect_done % scan_cell_stride == 0
                ):
                    scan_progress_callback(
                        rect_done, rect_total, len(tiles), now - job_t0
                    )
                    last_scan_cb_t = now
    if scan_progress_callback is not None and scan_progress_interval_s > 0:
        now = time.perf_counter()
        scan_progress_callback(rect_done, rect_total, len(tiles), now - job_t0)
    return tiles


def build_tiles_for_state_result(
    state_name: str,
    rings: List[List[Tuple[float, float]]],
    zoom: int,
    boundary_buffer_m: Optional[float] = None,
    *,
    geojson_path: Optional[Path] = None,
    tile_plan_dir: Optional[Path] = None,
    cancel_check: Optional[Callable[[], None]] = None,
) -> TilePlanBuildResult:
    """
    Like ``build_tiles_for_state`` but reports whether the list came from disk cache.
    """
    buf = DEFAULT_BOUNDARY_BUFFER_M if boundary_buffer_m is None else float(boundary_buffer_m)

    if geojson_path is not None and tile_plan_dir is not None and geojson_path.is_file():
        crc = crc32_file(geojson_path)
        cache_path = _tile_plan_cache_path(tile_plan_dir, state_name, zoom)
        cached = try_load_tile_plan_cache(cache_path, zoom, buf, crc)
        if cached is not None:
            return TilePlanBuildResult(cached, True)

    parallel_done_cb = None
    scan_cb = None
    scan_interval_s = 0.0
    if cancel_check is not None:
        def _parallel_done_cb(_bands_done: int, _bands_total: int, _tiles_so_far: int, _elapsed_s: float) -> None:
            cancel_check()

        def _scan_cb(_rect_done: int, _rect_total: int, _qual_tiles: int, _elapsed_s: float) -> None:
            cancel_check()

        parallel_done_cb = _parallel_done_cb
        scan_cb = _scan_cb
        scan_interval_s = 0.25

    tiles = _compute_tiles_for_state(
        rings,
        zoom,
        buf,
        parallel_band_done=parallel_done_cb,
        scan_progress_interval_s=scan_interval_s,
        scan_progress_callback=scan_cb,
    )
    return TilePlanBuildResult(tiles, False)


def build_tiles_for_state(
    state_name: str,
    rings: List[List[Tuple[float, float]]],
    zoom: int,
    boundary_buffer_m: Optional[float] = None,
    *,
    geojson_path: Optional[Path] = None,
    tile_plan_dir: Optional[Path] = None,
) -> List[Tuple[int, int]]:
    """
    Return (x, y) tile indices whose centers lie inside the state polygon or within
    ``boundary_buffer_m`` meters of its boundary (Web Mercator tile grid at ``zoom``).

    When ``geojson_path`` and ``tile_plan_dir`` are set and a matching ``.tiles.gz``
    cache exists (same GeoJSON CRC-32 and buffer), returns the cached list immediately.
    Otherwise computes the list (slow for large states at high zoom).
    """
    return build_tiles_for_state_result(
        state_name,
        rings,
        zoom,
        boundary_buffer_m,
        geojson_path=geojson_path,
        tile_plan_dir=tile_plan_dir,
    ).tiles
