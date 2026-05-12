#!/usr/bin/env python3
"""
CIB01 to ATAK SQLite pipeline.

This script is designed to make CIB01 (RPF/NITF .i4x) usable for repeat ATAK SQL builds
without re-scanning millions of files each time.

Workflow:
1) index: one-time scan of CIB01 files -> spatial SQLite index with RTree
2) build: AOI query by MGRS or lat/lon + radius -> VRT -> clipped GeoTIFF -> MBTiles -> ATAK SQLite
"""

from __future__ import annotations

import argparse
import math
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from osgeo import gdal  # type: ignore


def _run(cmd: Sequence[str]) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def _compute_bbox_from_center(lat: float, lon: float, radius_miles: float) -> Tuple[float, float, float, float]:
    dlat = radius_miles / 69.0
    dlon = dlat / max(0.2, math.cos(math.radians(lat)))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


def _parse_center(args: argparse.Namespace) -> Tuple[float, float]:
    if args.mgrs:
        try:
            import mgrs as mgrs_mod
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("mgrs package is required when using --mgrs") from exc
        mgrs_clean = "".join(args.mgrs.split())
        lat, lon = mgrs_mod.MGRS().toLatLon(mgrs_clean)
        return float(lat), float(lon)

    if args.lat is None or args.lon is None:
        raise ValueError("Provide either --mgrs or both --lat and --lon")
    return float(args.lat), float(args.lon)


def _iter_cib01_files(root: Path) -> Iterable[Path]:
    exts = {".i41", ".i42", ".i43", ".i44", ".i45", ".i46"}
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            lower = name.lower()
            if Path(lower).suffix in exts:
                yield Path(dirpath) / name


def _frame_bounds(path: Path) -> Optional[Tuple[float, float, float, float]]:
    ds = gdal.Open(str(path), gdal.GA_ReadOnly)
    if ds is None:
        return None
    gt = ds.GetGeoTransform(can_return_null=True)
    if gt is None:
        ds = None
        return None

    width = ds.RasterXSize
    height = ds.RasterYSize
    x0, y0 = gt[0], gt[3]
    x1 = x0 + gt[1] * width
    y1 = y0 + gt[5] * height
    ds = None
    minx, maxx = sorted((x0, x1))
    miny, maxy = sorted((y0, y1))
    return minx, miny, maxx, maxy


def _ensure_index_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS frames (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            minx REAL NOT NULL,
            miny REAL NOT NULL,
            maxx REAL NOT NULL,
            maxy REAL NOT NULL,
            mtime_ns INTEGER NOT NULL,
            size_bytes INTEGER NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS frames_rtree USING rtree(
            id, minx, maxx, miny, maxy
        );

        CREATE INDEX IF NOT EXISTS frames_path_idx ON frames(path);
        """
    )
    conn.commit()


def _upsert_frame(
    conn: sqlite3.Connection,
    path: Path,
    bounds: Tuple[float, float, float, float],
    mtime_ns: int,
    size_bytes: int,
) -> None:
    minx, miny, maxx, maxy = bounds
    conn.execute(
        """
        INSERT INTO frames(path, minx, miny, maxx, maxy, mtime_ns, size_bytes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            minx=excluded.minx,
            miny=excluded.miny,
            maxx=excluded.maxx,
            maxy=excluded.maxy,
            mtime_ns=excluded.mtime_ns,
            size_bytes=excluded.size_bytes
        """,
        (str(path), minx, miny, maxx, maxy, mtime_ns, size_bytes),
    )
    row_id = conn.execute("SELECT id FROM frames WHERE path = ?", (str(path),)).fetchone()[0]
    conn.execute(
        """
        INSERT INTO frames_rtree(id, minx, maxx, miny, maxy)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            minx=excluded.minx,
            maxx=excluded.maxx,
            miny=excluded.miny,
            maxy=excluded.maxy
        """,
        (row_id, minx, maxx, miny, maxy),
    )


def run_index(args: argparse.Namespace) -> int:
    root = Path(args.cib01_root).expanduser().resolve()
    db_path = Path(args.index_db).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    _ensure_index_schema(conn)

    batch = 0
    scanned = 0
    indexed = 0
    start = time.time()

    print(f"Indexing CIB01 from: {root}")
    print(f"Index DB: {db_path}")

    for frame in _iter_cib01_files(root):
        scanned += 1
        try:
            stat = frame.stat()
        except OSError:
            continue

        existing = conn.execute(
            "SELECT mtime_ns, size_bytes FROM frames WHERE path = ?",
            (str(frame),),
        ).fetchone()
        if existing and existing[0] == stat.st_mtime_ns and existing[1] == stat.st_size:
            continue

        bounds = _frame_bounds(frame)
        if not bounds:
            continue

        _upsert_frame(conn, frame, bounds, stat.st_mtime_ns, stat.st_size)
        indexed += 1
        batch += 1

        if batch >= 1000:
            conn.commit()
            batch = 0

        if scanned % 10000 == 0:
            elapsed = max(1.0, time.time() - start)
            rate = scanned / elapsed
            print(f"scanned={scanned:,} indexed={indexed:,} rate={rate:,.1f}/s")

    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
    conn.close()
    print(f"Done. scanned={scanned:,} updated={indexed:,} total_indexed={total:,}")
    return 0


def _query_frames(conn: sqlite3.Connection, bbox: Tuple[float, float, float, float]) -> List[str]:
    minx, miny, maxx, maxy = bbox
    rows = conn.execute(
        """
        SELECT f.path
        FROM frames_rtree r
        JOIN frames f ON f.id = r.id
        WHERE r.maxx >= ? AND r.minx <= ? AND r.maxy >= ? AND r.miny <= ?
        ORDER BY f.path
        """,
        (minx, maxx, miny, maxy),
    ).fetchall()
    return [r[0] for r in rows]


def _compute_sqlite_key(x: int, y: int, z: int) -> int:
    return (((z << z) + x) << z) + y


def _mbtiles_to_atak_sqlite(mbtiles_path: Path, out_sqlite: Path, provider: str) -> None:
    out_sqlite.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(mbtiles_path)
    dst = sqlite3.connect(out_sqlite)
    cur = dst.cursor()
    cur.executescript(
        """
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
        CREATE INDEX IF NOT EXISTS tiles_provider_idx ON tiles(provider);
        CREATE INDEX IF NOT EXISTS atak_catalog_exp_idx ON ATAK_catalog(expiration);
        """
    )
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA synchronous=NORMAL;")
    cur.execute("PRAGMA temp_store=MEMORY;")
    cur.execute("PRAGMA cache_size=-200000;")

    meta_rows = src.execute("SELECT name, value FROM metadata").fetchall()
    meta = {k: v for k, v in meta_rows}
    zmin, zmax = src.execute("SELECT MIN(zoom_level), MAX(zoom_level) FROM tiles").fetchone()
    cur.execute("INSERT OR REPLACE INTO ATAK_metadata(key, value) VALUES (?, ?)", ("srid", "3857"))
    if zmin is not None:
        cur.execute("INSERT OR REPLACE INTO ATAK_metadata(key, value) VALUES (?, ?)", ("minzoom", str(zmin)))
    if zmax is not None:
        cur.execute("INSERT OR REPLACE INTO ATAK_metadata(key, value) VALUES (?, ?)", ("maxzoom", str(zmax)))
    if "bounds" in meta:
        cur.execute("INSERT OR REPLACE INTO ATAK_metadata(key, value) VALUES (?, ?)", ("bounds", meta["bounds"]))

    now_ms = int(time.time() * 1000)
    far_future = 9223372036854775807
    batch_tiles = []
    batch_cat = []
    count = 0
    for z, x, y_tms, tile_data in src.execute("SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles"):
        y_xyz = ((1 << z) - 1) - y_tms
        key = _compute_sqlite_key(x, y_xyz, z)
        batch_tiles.append((key, provider, tile_data))
        batch_cat.append((key, now_ms, far_future, len(tile_data)))
        if len(batch_tiles) >= 2000:
            cur.executemany("INSERT OR REPLACE INTO tiles(key, provider, tile) VALUES (?, ?, ?)", batch_tiles)
            cur.executemany(
                "INSERT OR REPLACE INTO ATAK_catalog(key, access, expiration, size) VALUES (?, ?, ?, ?)",
                batch_cat,
            )
            dst.commit()
            count += len(batch_tiles)
            batch_tiles.clear()
            batch_cat.clear()

    if batch_tiles:
        cur.executemany("INSERT OR REPLACE INTO tiles(key, provider, tile) VALUES (?, ?, ?)", batch_tiles)
        cur.executemany(
            "INSERT OR REPLACE INTO ATAK_catalog(key, access, expiration, size) VALUES (?, ?, ?, ?)",
            batch_cat,
        )
        dst.commit()
        count += len(batch_tiles)

    src.close()
    dst.close()
    print(f"ATAK SQLite tiles written: {count:,}")


@dataclass
class BuildPaths:
    run_dir: Path
    file_list: Path
    vrt: Path
    tif_3857: Path
    mbtiles: Path
    atak_sqlite: Path


def _build_paths(output_dir: Path, name: str) -> BuildPaths:
    run_dir = output_dir / name
    run_dir.mkdir(parents=True, exist_ok=True)
    return BuildPaths(
        run_dir=run_dir,
        file_list=run_dir / f"{name}_frames.txt",
        vrt=run_dir / f"{name}.vrt",
        tif_3857=run_dir / f"{name}_3857.tif",
        mbtiles=run_dir / f"{name}.mbtiles",
        atak_sqlite=run_dir / f"{name}.sqlite",
    )


def run_build(args: argparse.Namespace) -> int:
    index_db = Path(args.index_db).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    name = args.name
    lat, lon = _parse_center(args)
    bbox = _compute_bbox_from_center(lat, lon, args.radius_miles)
    minx, miny, maxx, maxy = bbox

    conn = sqlite3.connect(index_db)
    frames = _query_frames(conn, bbox)
    conn.close()

    if not frames:
        print("No CIB01 frames intersect AOI.")
        return 2

    paths = _build_paths(output_dir, name)
    with paths.file_list.open("w", encoding="utf-8") as fh:
        for frame in frames:
            fh.write(frame + "\n")
    print(f"Intersecting frames: {len(frames):,}")
    print(f"Frame list: {paths.file_list}")

    _run(["gdalbuildvrt", "-input_file_list", str(paths.file_list), str(paths.vrt)])
    _run(
        [
            "gdalwarp",
            "-te_srs",
            "EPSG:4326",
            "-te",
            f"{minx}",
            f"{miny}",
            f"{maxx}",
            f"{maxy}",
            "-t_srs",
            "EPSG:3857",
            "-r",
            "near",
            "-dstalpha",
            str(paths.vrt),
            str(paths.tif_3857),
        ]
    )
    _run(
        [
            "gdal_translate",
            "-of",
            "MBTILES",
            "-co",
            "TILE_FORMAT=JPEG",
            "-co",
            f"QUALITY={args.jpeg_quality}",
            str(paths.tif_3857),
            str(paths.mbtiles),
        ]
    )
    _run(["gdaladdo", "-r", "average", str(paths.mbtiles), "2", "4", "8", "16"])
    _mbtiles_to_atak_sqlite(paths.mbtiles, paths.atak_sqlite, args.provider)
    print(f"Done: {paths.atak_sqlite}")

    if args.adb_push:
        _run(["adb", "shell", "mkdir", "-p", "/sdcard/atak/imagery"])
        _run(["adb", "push", str(paths.atak_sqlite), f"/sdcard/atak/imagery/{paths.atak_sqlite.name}"])
        print(f"Pushed to device: /sdcard/atak/imagery/{paths.atak_sqlite.name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare and build CIB01 AOI packages for ATAK SQLite.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_index = sub.add_parser("index", help="Build/update one-time spatial index for CIB01 frames")
    p_index.add_argument("--cib01-root", required=True, help="Root folder containing CIB01 .i41-.i46 files")
    p_index.add_argument(
        "--index-db",
        default="scripts/data/cib01_index.sqlite",
        help="SQLite index file path (default: scripts/data/cib01_index.sqlite)",
    )
    p_index.set_defaults(func=run_index)

    p_build = sub.add_parser("build", help="Build AOI MBTiles + ATAK SQLite from indexed CIB01")
    p_build.add_argument("--index-db", required=True, help="Path to CIB01 index SQLite")
    p_build.add_argument("--name", required=True, help="Output package base name")
    p_build.add_argument("--output-dir", default="tmp/cib01_builds", help="Output directory for build artifacts")
    p_build.add_argument("--radius-miles", type=float, required=True, help="AOI radius in miles")
    p_build.add_argument("--mgrs", help="AOI center in MGRS (example: 12SVA9656489470)")
    p_build.add_argument("--lat", type=float, help="AOI center latitude if not using --mgrs")
    p_build.add_argument("--lon", type=float, help="AOI center longitude if not using --mgrs")
    p_build.add_argument("--provider", default="CIB01", help="Provider value written into ATAK SQLite")
    p_build.add_argument("--jpeg-quality", type=int, default=85, help="MBTiles JPEG quality (default: 85)")
    p_build.add_argument("--adb-push", action="store_true", help="Push resulting ATAK SQLite to connected device")
    p_build.set_defaults(func=run_build)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except subprocess.CalledProcessError as exc:
        print(f"Command failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
