#!/usr/bin/env python3
"""
Precompute Web Mercator tile (x, y) lists per US state and zoom for the imagery downloader.

Writes gzip binary caches under scripts/data/tile_plans/v1/*.tiles.gz (see imagery_tile_selection).
Run once before a release (or after changing us_states.geojson / boundary buffer); the downloader
loads these instantly instead of scanning huge tile rectangles at runtime.

Examples:
  python3 scripts/build_tile_plan_cache.py
  python3 scripts/build_tile_plan_cache.py --states Arkansas,Texas
  python3 scripts/build_tile_plan_cache.py --zooms 14,15,16
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from imagery_tile_selection import (  # noqa: E402
    DEFAULT_BOUNDARY_BUFFER_M,
    STATE_BOUNDARY_BUFFER_MILES,
    crc32_file,
    save_tile_plan_cache,
    _compute_tiles_for_state,
)

DATA_DIR = SCRIPT_DIR / "data"
STATE_GEOJSON_PATH = DATA_DIR / "us_states.geojson"
TILE_PLAN_DIR = DATA_DIR / "tile_plans" / "v1"
ZOOM_ESTIMATE_PATH = DATA_DIR / "zoom_estimates_z10_z16.json"


def load_zoom_tile_estimates(path: Path) -> Dict[str, Dict[int, int]]:
    """state_name -> zoom -> estimated_tiles (from build_imagery_zoom_estimates)."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: Dict[str, Dict[int, int]] = {}
    for state_name, per_zoom in (data.get("states") or {}).items():
        inner: Dict[int, int] = {}
        for zk, info in per_zoom.items():
            try:
                z = int(zk)
                inner[z] = int(info.get("estimated_tiles", 0))
            except (TypeError, ValueError):
                continue
        if inner:
            out[str(state_name)] = inner
    return out


def sum_estimated_tiles_remaining(
    jobs: List[Tuple[str, int]],
    start_index: int,
    estimates: Dict[str, Dict[int, int]],
) -> int:
    """Sum estimated_tiles for jobs[start_index:] (0-based)."""
    total = 0
    for state_name, z in jobs[start_index:]:
        total += estimates.get(state_name, {}).get(z, 0)
    return total


def load_states(geojson_path: Path) -> Dict[str, List[List[Tuple[float, float]]]]:
    with geojson_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    states: Dict[str, List[List[Tuple[float, float]]]] = {}
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        name = props.get("NAME") or props.get("NAME10") or props.get("STATE_NAME")
        geom = feature.get("geometry", {})
        gtype = geom.get("type")
        coords = geom.get("coordinates", [])

        rings: List[List[Tuple[float, float]]] = []
        if gtype == "Polygon":
            if coords:
                rings.append([(float(x), float(y)) for x, y in coords[0]])
        elif gtype == "MultiPolygon":
            for poly in coords:
                if poly:
                    rings.append([(float(x), float(y)) for x, y in poly[0]])

        if name and rings:
            states[name] = rings
    return states


def _fmt_duration(seconds: float) -> str:
    if seconds < 0 or not (seconds == seconds):  # NaN
        return "—"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m {s:02d}s"
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m:02d}m {s:02d}s"


def _eta_part_scan_remaining(
    *,
    rem_est: int,
    sec_per_tile: float,
    total_elapsed_s: float,
    jobs_remaining: int,
    eta_scale: float,
) -> str:
    """Same wording as the per-job completion line (batch scan ETA)."""
    if rem_est <= 0:
        return "no est. output tiles remaining (by schedule file)"
    eta_linear_s = sec_per_tile * rem_est
    eta_s = max(0.0, eta_linear_s * eta_scale)
    total_scan_est_s = total_elapsed_s + eta_s
    return (
        f"scan time left ~{_fmt_duration(eta_s)} "
        f"(batch scan total ~{_fmt_duration(total_scan_est_s)} incl. done so far; "
        f"~{rem_est:,} est. output tiles left in {jobs_remaining} job(s); "
        f"linear×{eta_scale:g} vs raw ~{_fmt_duration(eta_linear_s)})"
    )


def _eta_part_job_average_fallback(
    *,
    sum_compute_s: float,
    job_done_idx: int,
    total_jobs: int,
) -> str:
    if job_done_idx <= 0 or job_done_idx >= total_jobs:
        return "time left: … (need completed jobs for fallback ETA)"
    eta_s = (sum_compute_s / job_done_idx) * (total_jobs - job_done_idx)
    return (
        f"time left for full batch (job-average fallback; ~{total_jobs - job_done_idx} job(s)): "
        f"~{_fmt_duration(eta_s)}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Build tile plan .tiles.gz caches for imagery downloader.")
    ap.add_argument(
        "--states",
        type=str,
        default="",
        help="Comma-separated state names (default: all in GeoJSON). Example: Arkansas,Texas",
    )
    ap.add_argument(
        "--zooms",
        type=str,
        default="10,11,12,13,14,15,16",
        help="Comma-separated zoom levels (default: 10-16).",
    )
    ap.add_argument(
        "--geojson",
        type=Path,
        default=STATE_GEOJSON_PATH,
        help=f"State boundaries GeoJSON (default: {STATE_GEOJSON_PATH})",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=TILE_PLAN_DIR,
        help=f"Output directory (default: {TILE_PLAN_DIR})",
    )
    ap.add_argument(
        "--eta-scale",
        type=float,
        default=0.58,
        metavar="F",
        help=(
            "Scale for tile-linear scan ETA (default: 0.58). Uncorrected "
            "(elapsed/output-tiles)×remaining-est-tiles over-predicts because low zooms finish "
            "fast per tile; use 1.0 for raw linear extrapolation."
        ),
    )
    ap.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip a job when its output .tiles.gz already exists (resume after moving output dir).",
    )
    ap.add_argument(
        "--progress-interval",
        type=float,
        default=30.0,
        metavar="SEC",
        help=(
            "While scanning tiles for one state×zoom, print a progress line every SEC seconds "
            "(ETA for this job from zoom estimates). Use 0 to disable. Default: 30."
        ),
    )
    args = ap.parse_args()

    if not args.geojson.is_file():
        print(f"Missing GeoJSON: {args.geojson}", file=sys.stderr)
        return 1

    zooms = [int(z.strip()) for z in args.zooms.split(",") if z.strip()]
    if not zooms:
        print("No zoom levels.", file=sys.stderr)
        return 1

    states = load_states(args.geojson)
    if args.states.strip():
        wanted = {s.strip() for s in args.states.split(",") if s.strip()}
        missing = wanted - set(states.keys())
        if missing:
            print(f"Unknown state names: {sorted(missing)}", file=sys.stderr)
            return 1
        states = {k: v for k, v in states.items() if k in wanted}

    crc = crc32_file(args.geojson)
    buf = DEFAULT_BOUNDARY_BUFFER_M
    state_list = sorted(states.keys())
    jobs: List[Tuple[str, int]] = [(sn, z) for sn in state_list for z in zooms]
    total_jobs = len(jobs)
    zoom_estimates = load_zoom_tile_estimates(ZOOM_ESTIMATE_PATH)
    total_est_tiles_all = sum_estimated_tiles_remaining(jobs, 0, zoom_estimates)
    if zoom_estimates and total_est_tiles_all > 0:
        est_note = f"~{total_est_tiles_all:,} output tiles (from {ZOOM_ESTIMATE_PATH.name}; used for ETA)"
    else:
        est_note = f"no tile estimates at {ZOOM_ESTIMATE_PATH.name} — ETA falls back to average time per job"

    print(
        "Note: No USGS download — only tile-index math (same as the downloader’s "
        "“Scanning tile coverage” phase). Downloader wall time also includes network I/O.\n",
        flush=True,
    )
    print(
        f"GeoJSON: {args.geojson}\n"
        f"CRC-32: {crc:#010x}\n"
        f"Buffer: {STATE_BOUNDARY_BUFFER_MILES:g} mi ({buf:.1f} m)\n"
        f"States: {len(states)}  Zooms: {zooms}  Jobs: {total_jobs}\n"
        f"Batch scope: {est_note}\n"
        f"ETA scale (tile-linear overshoot): {args.eta_scale:g}\n"
        f"Output: {args.out_dir}\n",
        flush=True,
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)

    run_t0 = time.perf_counter()
    sum_compute_s = 0.0
    sum_out_tiles = 0

    for k, (state_name, z) in enumerate(jobs):
        out = args.out_dir / f"{state_name.replace('/', '_')}_z{z}.tiles.gz"
        if args.skip_existing and out.is_file():
            job_done_idx = k + 1
            pct = 100.0 * job_done_idx / total_jobs
            print(
                f"[{job_done_idx}/{total_jobs}] ({pct:.1f}%) {state_name} z{z} | "
                f"CACHE SKIP — file already on disk (no work): {out.name}",
                flush=True,
            )
            continue

        rings = states[state_name]
        t0 = time.perf_counter()
        def progress_log(qual_count: int, rect_done: int, rect_total: int, job_elapsed: float) -> None:
            total_elapsed_s = time.perf_counter() - run_t0
            est_j = zoom_estimates.get(state_name, {}).get(z, 0)
            rem_after = sum_estimated_tiles_remaining(jobs, k + 1, zoom_estimates)
            rem_current = max(0, est_j - qual_count) if est_j else 0
            rem_est = rem_after + rem_current
            jobs_remaining = total_jobs - k

            sec_per_tile: Optional[float] = None
            if sum_out_tiles > 0 and sum_compute_s > 0:
                sec_per_tile = sum_compute_s / sum_out_tiles
            elif qual_count > 0 and job_elapsed >= 0.5:
                sec_per_tile = job_elapsed / qual_count

            if sec_per_tile is not None and rem_est > 0:
                eta_part = _eta_part_scan_remaining(
                    rem_est=rem_est,
                    sec_per_tile=sec_per_tile,
                    total_elapsed_s=total_elapsed_s,
                    jobs_remaining=jobs_remaining,
                    eta_scale=args.eta_scale,
                )
            elif k > 0 and sum_compute_s > 0:
                eta_part = _eta_part_job_average_fallback(
                    sum_compute_s=sum_compute_s,
                    job_done_idx=k,
                    total_jobs=total_jobs,
                )
            else:
                eta_part = (
                    "scan time left: … (establishing rate — need a few more qualifying tiles on this job)"
                )

            rect_pct = 100.0 * rect_done / max(rect_total, 1)
            tail = (
                f"{qual_count:,} tiles in {job_elapsed:.1f}s (scanning {rect_pct:.1f}% of tile grid) "
                f"→ {out.name}"
            )
            print(
                f"[{k + 1}/{total_jobs}] ({100.0 * (k + 1) / total_jobs:.1f}%) {state_name} z{z} | "
                f"elapsed {_fmt_duration(total_elapsed_s)} | {eta_part} | {tail}",
                flush=True,
            )

        if args.progress_interval > 0:
            tiles = _compute_tiles_for_state(
                rings,
                z,
                buf,
                progress_interval_s=args.progress_interval,
                progress_log=progress_log,
            )
        else:
            tiles = _compute_tiles_for_state(rings, z, buf)
        elapsed_task = time.perf_counter() - t0
        sum_compute_s += elapsed_task
        n_out = len(tiles)
        sum_out_tiles += n_out

        save_tile_plan_cache(out, z, buf, crc, tiles)

        total_elapsed_s = time.perf_counter() - run_t0
        job_done_idx = k + 1
        pct = 100.0 * job_done_idx / total_jobs

        if job_done_idx < total_jobs and sum_out_tiles > 0 and sum_compute_s > 0:
            rem_est = sum_estimated_tiles_remaining(jobs, job_done_idx, zoom_estimates)
            if rem_est > 0:
                sec_per_tile = sum_compute_s / sum_out_tiles
                eta_part = _eta_part_scan_remaining(
                    rem_est=rem_est,
                    sec_per_tile=sec_per_tile,
                    total_elapsed_s=total_elapsed_s,
                    jobs_remaining=total_jobs - job_done_idx,
                    eta_scale=args.eta_scale,
                )
            else:
                eta_part = _eta_part_job_average_fallback(
                    sum_compute_s=sum_compute_s,
                    job_done_idx=job_done_idx,
                    total_jobs=total_jobs,
                )
        elif job_done_idx < total_jobs:
            eta_part = "time left: … (need first job for ETA)"
        else:
            eta_part = "entire batch complete"

        print(
            f"[{job_done_idx}/{total_jobs}] ({pct:.1f}%) {state_name} z{z} | "
            f"elapsed {_fmt_duration(total_elapsed_s)} | {eta_part} | "
            f"{n_out:,} tiles in {elapsed_task:.1f}s → {out.name}",
            flush=True,
        )

    total_wall = time.perf_counter() - run_t0
    print(f"Done. {_fmt_duration(total_wall)} total for {total_jobs} cache file(s).", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
