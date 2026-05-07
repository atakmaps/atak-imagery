# Handoff: US state tile-plan cache (`*.tiles.gz`)

**Scope:** This document is **only** about the precomputed **state × zoom** tile index caches used by the imagery downloader. It does not cover map downloads, add-ons, SQLite build, or DTED.

---

## What problem this solves

Before downloading USGS tiles, the downloader must know **which Web Mercator tiles (x, y)** intersect each **state polygon** (plus a **boundary buffer**) for each selected **zoom level**. Computing that set means scanning a large tile grid — **CPU-heavy** for big states at high zoom.

The **tile-plan cache** stores that list on disk so the downloader can **load** it instead of recomputing. Log lines distinguish:

- **`Tile plan (cache):`** — loaded from `*.tiles.gz`
- **`Tile plan (computed):`** — computed at runtime (slow path)

**Fixed-radius downloads** do **not** use these state caches; they call `compute_tiles_for_radius()` each run.

---

## What each file is

- **Path pattern:** `…/data/tile_plans/v1/<StateName>_z<zoom>.tiles.gz`  
  Example: `California_z16.tiles.gz`  
  State names with `/` are sanitized to `_` in the filename.

- **Format:** Gzip-compressed blob:
  - Binary **header** (magic `ATKP`, format id, zoom, **GeoJSON CRC-32**, **boundary buffer in meters**, tile count `n`)
  - **Body:** `n` pairs of big-endian **uint32** `(x, y)` tile indices

- **Validity:** At load time, the downloader checks **zoom**, **CRC-32 of `us_states.geojson`**, and **`STATE_BOUNDARY_BUFFER_MILES`** (via `DEFAULT_BOUNDARY_BUFFER_M`). Any mismatch → cache ignored → full recompute for that job.

**Implementation:** `scripts/imagery_tile_selection.py` — `try_load_tile_plan_cache`, `save_tile_plan_cache`, `build_tiles_for_state_result`.  
**Windows parity:** `windows_build/imagery_tile_selection.py` (keep in sync if geometry rules change).

---

## Where caches live in the repo

| Role | Directory |
|------|-----------|
| **Canonical (Linux / source)** | `scripts/data/tile_plans/v1/` |
| **Windows packaging source** | `windows_build/data/tile_plans/v1/` |

The downloader sets `TILE_PLAN_DIR` to that `v1` folder next to `data/us_states.geojson` (see `atak_downloader_finalbuild.py` / `atak_downloader_finalbuild_win.py`).

**Git:** `.gitignore` allows these blobs to be committed if explicitly added (`!scripts/data/tile_plans/v1/*.tiles.gz`). Repos may ship **no** caches, **partial** caches (e.g. one state for dev), or a **full** US set for release — team choice by size.

---

## How caches are produced (build machine)

**Script:** `scripts/build_tile_plan_cache.py`

**Inputs:**

- `scripts/data/us_states.geojson` (state polygons)
- `STATE_BOUNDARY_BUFFER_MILES` / buffer meters in `imagery_tile_selection.py` (must match runtime)
- Optional: `scripts/data/zoom_estimates_z10_z16.json` for ETA text only

**Outputs:**

- `scripts/data/tile_plans/v1/*.tiles.gz` (default `--out-dir`)

**Typical invocations:**

```bash
cd /path/to/pipeline/scripts
python3 build_tile_plan_cache.py
python3 build_tile_plan_cache.py --states Arkansas,Texas --zooms 14,15,16
python3 build_tile_plan_cache.py --skip-existing --exclude-states Alaska
```

**Server wrapper (run from repo `scripts/`):** `scripts/tile_plan_cache_on_server.sh` → `exec python3 build_tile_plan_cache.py "$@"`

**Long runs:** use `nohup`, `PYTHONUNBUFFERED=1`, `tee` to a log file; full CONUS × z10–z16 can take **many hours**.

**Parallelism:** Large bbox scans use multiple worker processes unless `--progress-interval` is set (legacy single-core verbose mode). `ATAK_TILE_PLAN_WORKERS` can cap workers.

---

## Maintainer VM (example — adjust if paths differ)

Team has used **`atak@10.93.10.90`** with checkout under e.g. **`~/atak/pipeline`**, log e.g. **`~/tile_plan_cache_full_us.log`**, output written to **`~/atak/pipeline/scripts/data/tile_plans/v1/`**.  
SSH and routing are **operator-dependent**; the agent may or may not reach private IPs from a given Cursor session.

---

## What we do with the data after it is built

1. **Optional:** Run **`scripts/verify_tile_plan_caches.py`** against `v1/` to confirm files load with the **current** GeoJSON CRC and buffer.

2. **Copy into release / dev trees** so end users (or CI-built zips) include the files:
   - **`rsync` / `scp`** from the build host to `scripts/data/tile_plans/v1/` (and mirror **`windows_build/data/tile_plans/v1/`** if Windows builds must match).

3. **Commit or attach to release artifacts** per team policy (size vs convenience).

4. **Do not** hand-edit `*.tiles.gz`; regenerate with `build_tile_plan_cache.py` if boundaries or buffer change.

---

## How it is “implemented into the build”

- **No separate compile step:** the downloader is plain Python; at **runtime** it looks for `TILE_PLAN_DIR / f"{state}_z{z}.tiles.gz"` when building the download plan.

- **PyInstaller / frozen builds:** whatever process bundles `scripts/data/` (or equivalent) must include **`data/tile_plans/v1/*.tiles.gz`** if you want offline cache hits in the binary. Missing directory → downloader still works, slower planning.

- **Linux vs Windows:** If release ships both, keep **`scripts/data/tile_plans/v1/`** and **`windows_build/data/tile_plans/v1/`** aligned when you intentionally ship caches for both.

---

## When you must rebuild all caches

- **`us_states.geojson`** replaced or edited (CRC changes).
- **`STATE_BOUNDARY_BUFFER_MILES`** (or equivalent buffer constant) changed in `imagery_tile_selection.py`.
- Intentional change to tile-inclusion rules in **`_compute_tiles_for_state`** / polygon logic (not only metadata).

After any of these, old `*.tiles.gz` files may **silently fail** the header check and fall back to compute — safe but slow. Regenerate to restore cache hits.

---

## Quick reference files

| File | Purpose |
|------|---------|
| `scripts/build_tile_plan_cache.py` | Batch generator |
| `scripts/verify_tile_plan_caches.py` | Sanity-check caches vs current GeoJSON |
| `scripts/tile_plan_cache_on_server.sh` | `cd scripts` + run builder |
| `scripts/fetch_tile_plan_caches.sh` | Pull selected `*.tiles.gz` from an SSH host (defaults in script may point at legacy hosts — override env vars) |
| `scripts/tile_plan_cache_remote_build.sh` | Laptop rsync-up / run / rsync-down pattern |
| `scripts/data/tile_plans/README.md` | Short maintainer README (overlaps with this handoff) |

---

*Focused handoff for tile-plan cache only; update when build hosts, paths, or release layout change.*
