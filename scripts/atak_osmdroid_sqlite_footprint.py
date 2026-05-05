#!/usr/bin/env python3
"""
Approximate ATAK 5.5.x **Mobile** OSMDroid SQLite footprint (green **Outline**).

ATAK's ``TilesetInfo.parseSQLiteDb`` uses ``OSMDroidInfo.get(db, BoundsDiscovery.Full)``
(decompiled ``abh.a(..., Full)``), then builds four WGS84 corners from min-zoom tile
columns/rows ``d,e,f,g``. That is an **axis-aligned lon/lat rectangle**. It is **not**
a geodesic circle, **not** guaranteed to be a "square" in meters, and **not** driven by
``ATAK_metadata`` ``bounds`` (that key is unused here for the four corners).

Usage::
    python3 atak_osmdroid_sqlite_footprint.py /path/to/ATAK_SQL_Radius.sqlite
    python3 atak_osmdroid_sqlite_footprint.py /path/to/file.sqlite --union
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from imagery_tile_selection import RADIUS_REGION_FOLDER, tile_lonlat_bounds


def _c_atak(i: int) -> int:
    if i < 0 or i > 28:
        return -1
    return i << (i * 2)


def max_key_upper_inclusive_for_zoom_class(z: int) -> int:
    """Matches ``abn.b(int)`` → ``c(z+1)-1``."""
    return _c_atak(z + 1) - 1


def zoom_from_key(j: int) -> int:
    for i in range(29):
        if j <= max_key_upper_inclusive_for_zoom_class(i):
            return i
    return -1


def tile_x_from_key(j: int, z: int) -> int:
    return int((j >> z) - (z << z))


def tile_y_from_key(j: int, z: int) -> int:
    return int(j & ((1 << z) - 1))


def osmdroid_key(z: int, x: int, y: int) -> int:
    return int(((z << z) + x << z) + y)


def _lat_north_edge(z: int, y_row: int) -> float:
    n = 1 << z
    return math.degrees(math.atan(math.sinh(math.pi * (1.0 - ((y_row * 2.0) / n)))))


def _lon_west_edge(z: int, x_col: int) -> float:
    n = 1 << z
    return (x_col / n) * 360.0 - 180.0


def atak_tileset_four_corners_wm(zmin: int, d: int, e: int, f: int, g: int) -> Tuple[float, float, float, float]:
    """
    Web Mercator branch (``TilesetInfo`` when SRID is map SRID / 3857-style).
    Returns west, south, east, north in WGS84 degrees.
    """
    lat_n = _lat_north_edge(zmin, g + 1)
    lat_s = _lat_north_edge(zmin, e)
    lon_w = _lon_west_edge(zmin, d)
    lon_e = _lon_west_edge(zmin, f + 1)
    south = min(lat_s, lat_n)
    north = max(lat_s, lat_n)
    west = min(lon_w, lon_e)
    east = max(lon_w, lon_e)
    return west, south, east, north


def atak_tileset_four_corners_4326(zmin: int, d: int, e: int, f: int, g: int) -> Tuple[float, float, float, float]:
    """Equirectangular grid branch when SRID is 4326 (matches decompiled TilesetInfo)."""
    min_lat = -85.05112878
    max_lat = 85.05112878
    min_lon = -180.0
    max_lon = 180.0
    tiles_across = 2 << zmin
    d5 = (max_lon - min_lon) / tiles_across
    d6 = (max_lat - min_lat) / (1 << zmin)
    lat_n = max_lat - ((g + 1) * d6)
    lat_s = max_lat - (e * d6)
    lon_w = (d * d5) + min_lon
    lon_e = ((f + 1) * d5) + min_lon
    south = min(lat_s, lat_n)
    north = max(lat_s, lat_n)
    west = min(lon_w, lon_e)
    east = max(lon_w, lon_e)
    return west, south, east, north


def _read_srid_and_content(conn: sqlite3.Connection) -> Tuple[int, Optional[str]]:
    srid = 3857
    content: Optional[str] = None
    try:
        cur = conn.execute(
            "SELECT key, value FROM ATAK_metadata WHERE key IN ('srid', 'content')"
        )
        for k, v in cur:
            if k == "srid" and v is not None:
                try:
                    srid = int(str(v))
                except ValueError:
                    pass
            if k == "content" and v is not None:
                content = str(v)
    except sqlite3.Error:
        pass
    if srid == 900913:
        srid = 3857
    return srid, content


def osmdroid_bounds_discovery_full(conn: sqlite3.Connection) -> Optional[Tuple[int, int, int, int, int, int]]:
    """
    Port of ``abh.a(db, BoundsDiscovery.Full)`` tile indices:
    ``z_min``, ``z_max`` (from min/max key), ``d``, ``e``, ``f``, ``g``.
    """
    row = conn.execute("SELECT min(key), max(key) FROM tiles").fetchone()
    if not row or row[0] is None or row[1] is None:
        return None
    k_min = int(row[0])
    k_max = int(row[1])
    z_min = zoom_from_key(k_min)
    z_max = zoom_from_key(k_max)
    if z_min < 0 or z_max < 0:
        return None

    hi = max_key_upper_inclusive_for_zoom_class(z_min)
    row = conn.execute("SELECT min(key) FROM tiles WHERE key <= ?", (hi,)).fetchone()
    if not row or row[0] is None:
        return None
    k0 = int(row[0])
    d = tile_x_from_key(k0, z_min)
    y0 = tile_y_from_key(k0, z_min)
    e = y0
    g = y0

    row = conn.execute("SELECT max(key) FROM tiles WHERE key <= ?", (hi,)).fetchone()
    if not row or row[0] is None:
        return None
    k1 = int(row[0])
    f = tile_x_from_key(k1, z_min)
    y1 = tile_y_from_key(k1, z_min)
    if y1 < e:
        e = y1
    if y1 > g:
        g = y1

    ymax = (1 << z_min) - 1
    for xcol in range(d, f + 1):
        k_lo = osmdroid_key(z_min, xcol, 0)
        k_hi_excl = osmdroid_key(z_min, xcol, e)
        row = conn.execute(
            "SELECT min(key) FROM tiles WHERE key >= ? AND key < ?", (k_lo, k_hi_excl)
        ).fetchone()
        if row and row[0] is not None:
            yy = tile_y_from_key(int(row[0]), z_min)
            if yy < e:
                e = yy
        k_lo2 = osmdroid_key(z_min, xcol, g)
        k_hi2 = osmdroid_key(z_min, xcol, ymax)
        row = conn.execute(
            "SELECT max(key) FROM tiles WHERE key > ? AND key <= ?", (k_lo2, k_hi2)
        ).fetchone()
        if row and row[0] is not None:
            yy = tile_y_from_key(int(row[0]), z_min)
            if yy > g:
                g = yy

    return z_min, z_max, d, e, f, g


def union_lonlat_all_tiles(conn: sqlite3.Connection) -> Optional[Tuple[float, float, float, float]]:
    west, east, south, north = 180.0, -180.0, 90.0, -90.0
    any_tiles = False
    for (k,) in conn.execute("SELECT key FROM tiles"):
        any_tiles = True
        z = zoom_from_key(int(k))
        if z < 0:
            continue
        x = tile_x_from_key(int(k), z)
        y = tile_y_from_key(int(k), z)
        lw, le, ss, nn = tile_lonlat_bounds(x, y, z)
        west = min(west, lw)
        east = max(east, le)
        south = min(south, ss)
        north = max(north, nn)
    if not any_tiles:
        return None
    return west, south, east, north


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def bbox_size_summary(w: float, s: float, e: float, n: float) -> str:
    mid_lat = (s + n) / 2.0
    mid_lon = (w + e) / 2.0
    ew_m = _haversine_m(mid_lat, w, mid_lat, e)
    ns_m = _haversine_m(s, mid_lon, n, mid_lon)
    return f"approx center ({mid_lat:.4f}°, {mid_lon:.4f}°) | E–W ~{ew_m/1000:.1f} km | N–S ~{ns_m/1000:.1f} km"


def report_lines(path: Path, *, include_union: bool = False) -> List[str]:
    """Return log lines (no printing). Raises on missing DB."""
    lines: List[str] = []
    if not path.is_file():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        try:
            tile_count = conn.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
        except sqlite3.Error as exc:
            return [f"Not a readable OSMDroid tiles DB: {exc}"]
        srid, content = _read_srid_and_content(conn)
        disc = osmdroid_bounds_discovery_full(conn)
        lines.append(f"ATAK mobile outline diagnostic | {path.name}")
        lines.append(f"Tiles: {tile_count:,} | srid={srid} | content={content!r}")
        if disc is None:
            lines.append("Could not run OSMDroidInfo.Full (missing keys?).")
            return lines
        z_min, z_max, d, e, f, g = disc
        lines.append(
            f"OSMDroidInfo.Full → min_zoom={z_min} max_zoom_in_db={z_max} "
            f"cols {d}..{f} rows {e}..{g}"
        )
        if srid == 4326:
            w, s, e2, nlat = atak_tileset_four_corners_4326(z_min, d, e, f, g)
        else:
            w, s, e2, nlat = atak_tileset_four_corners_wm(z_min, d, e, f, g)
        lines.append(
            f"Outline WGS84: west={w:.6f} south={s:.6f} east={e2:.6f} north={nlat:.6f}"
        )
        lines.append(f"  → {bbox_size_summary(w, s, e2, nlat)}")
        if include_union:
            u = union_lonlat_all_tiles(conn)
            if u:
                w, s, e2, nlat = u
                lines.append(
                    f"Union all tiles: west={w:.6f} south={s:.6f} east={e2:.6f} north={nlat:.6f}"
                )
                lines.append(f"  → {bbox_size_summary(w, s, e2, nlat)}")
    finally:
        conn.close()
    return lines


def report_sqlite(path: Path, *, include_union: bool) -> int:
    if not path.is_file():
        print(f"Not a file: {path}", file=sys.stderr)
        return 2
    try:
        lines = report_lines(path, include_union=include_union)
    except FileNotFoundError:
        print(f"Not a file: {path}", file=sys.stderr)
        return 2
    print(f"File: {path}")
    for line in lines:
        print(line)
    fp_candidates = [
        path.parent.parent / "Imagery" / RADIUS_REGION_FOLDER / ".radius_footprint.json",
        path.parent / RADIUS_REGION_FOLDER / ".radius_footprint.json",
    ]
    fp = next((p for p in fp_candidates if p.is_file()), None)
    if fp is not None:
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            fw = float(data["west"])
            fs = float(data["south"])
            fe = float(data["east"])
            fn = float(data["north"])
            print(
                "Downloader footprint (.radius_footprint.json): "
                f"west={fw:.6f} south={fs:.6f} east={fe:.6f} north={fn:.6f}"
            )
            print(f"  → {bbox_size_summary(fw, fs, fe, fn)}")
        except (KeyError, ValueError, OSError) as exc:
            print(f"(Could not read footprint JSON: {exc})")
    if lines and lines[0].startswith("Not a readable"):
        return 1
    if any("Could not run OSMDroidInfo" in ln for ln in lines):
        return 1
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Show ATAK Mobile OSMDroid outline extent for a SQLite tileset.")
    p.add_argument("sqlite", type=Path, help="Path to ATAK_SQL_*.sqlite or other OSMDroid DB")
    p.add_argument(
        "--union",
        action="store_true",
        help="Also scan every tile key and print geographic union (slower on large DBs)",
    )
    args = p.parse_args(list(argv) if argv is not None else None)
    return report_sqlite(args.sqlite, include_union=args.union)


if __name__ == "__main__":
    sys.exit(main())
