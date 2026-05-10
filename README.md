# ATAK Imagery

Cross-platform ATAK imagery pipeline with simple one-click install.

## Linux: run `install_linux.sh` first

On Linux, **always run the installer before using the apps**. The file **`install_linux.sh`** in the **project root** (same folder as `README.md`) prepares **both** programs—**ATAK Device Installer** and **ATAK Imagery Downloader**—and the Python environment they rely on. Skipping it and running the `.py` files by hand will usually fail or miss dependencies.

Source repository: `https://github.com/atakmaps/atak-imagery`

### How to run the installer

1. Put the project on your machine and open a **terminal**:
   - **Clone:** `git clone https://github.com/atakmaps/atak-imagery.git` then `cd atak-imagery`
   - **Linux install zip:** under [Releases](https://github.com/atakmaps/atak-imagery/releases), download **`atak-imagery-v*-linux-install.zip`** for your version from **Assets** (not the auto-generated “Source code (zip)”, which uses a different folder layout). The **Assets** zip stores every file under a root directory **`atak-imagery/`** inside the archive.

     Run `unzip` in the **same directory as the `.zip` file** (a **parent** folder—usually `Downloads`). That directory will gain a **`atak-imagery`** folder next to the zip:

     ```bash
     cd ~/Downloads
     unzip atak-imagery-v1.3.0-linux-install.zip
     ls atak-imagery/install_linux.sh
     cd atak-imagery
     chmod +x install_linux.sh
     ./install_linux.sh
     ```

     If `ls` does not show `install_linux.sh` under `atak-imagery/`, you may have unzipped from **inside** an empty `atak-imagery` you created first (that nests a second `atak-imagery`). Remove that folder, stay in the parent directory (e.g. `Downloads`), and run `unzip` again. Use the real zip filename if you downloaded a different release.

     The script may ask for **`sudo`** for system packages. Some file managers can run `install_linux.sh` with a double-click; if nothing happens, use the terminal block above.

2. If you **cloned** instead of using the zip, run the installer from the repo root:

   ```bash
   cd atak-imagery
   chmod +x install_linux.sh
   ./install_linux.sh
   ```

### What `install_linux.sh` does

The root script runs **`scripts/install_linux.sh`**, which:

- Installs or checks **system packages** needed for the pipeline (Python 3, pip/venv, Tkinter, Zenity, Android **adb**, etc.) via apt, dnf, or pacman when it recognizes your distro.
- Creates or repairs a **virtual environment** at **`.venv/`** and installs Python dependencies from **`requirements.txt`**.
- Copies **`deploy.env.example`** to **`deploy.env`** the first time, so **ATAK Device Installer** has a config template to edit.
- Writes **`run_atak_pipeline_with_device.sh`** and **`run_atak_pipeline.sh`** in the project root (wrappers that call the correct Python entry points with that venv).
- Installs **two desktop shortcuts** (under `~/.local/share/applications/` and on `~/Desktop` when it exists):
  - **ATAK Device Installer** — USB setup: install ATAK and your plugin on the phone, then continue into the map workflow.
  - **ATAK Imagery Downloader** — download imagery and build packages when the device is already configured.

After a successful run, use those desktop entries or the two shell scripts above. You only need to run **`install_linux.sh`** again if you move the tree, recreate the venv, or need to refresh system/Python dependencies.

---

## Current stable release (Linux / source)

**Linux / source release:** `v1.3.19` (tag **`v1.3.19`** on GitHub).

**Windows:** A new Windows packaged build is **not** included in this cycle. **Use Windows release `2.8`** until a newer Windows installer is published. Source copies under `windows_build/` include the same startup behaviors when run with Python.

Version **1.3.19** highlights:

- **Radius zoom range reset:** removed z17/z18 selection from the downloader UI; both state and radius workflows now cap at z16.
- **Imagery-only startup flow hardening:** imagery-only mode now follows the same skip-intro workflow used by maps+add-ons, avoiding the separate upfront device-check path that was reported as crashing.
- **Linux/Windows parity:** the same downloader behavior changes were applied in both `scripts/` and `windows_build/` script variants.

### Previous release (v1.3.18)

- **Installer plugin-step UI cleanup:** removed text indicating bundled add-on plugin installation from the "Installing plugin" window.
- **Current behavior reflected in wizard copy:** plugin installation messaging now reflects UV-PRO-only install behavior.

### Previous release (v1.3.17)

- **Installer downgrade recovery:** when plugin install returns `INSTALL_FAILED_VERSION_DOWNGRADE`, the installer now retries downgrade flags and, if still blocked, performs a full uninstall/reinstall recovery automatically.
- **Strict uninstall verification for plugin-only flow:** plugin-only install now verifies the old UV-PRO package is truly removed before proceeding, reducing repeated version-code mismatch failures.
- **Improved package-state checks:** installer now checks package presence directly via `pm list packages` to make uninstall handling deterministic across devices.

### Previous release (v1.3.16)

- **Precomputed tile-plan cache refresh:** updated `scripts/data/tile_plans/v1/*.tiles.gz` coverage now includes states through Illinois to reduce state-planning compute time during imagery runs.
- **Faster first-run state planning on included states:** downloader can load cache-backed tile plans (`Tile plan (cache)`) for newly included states instead of recomputing.
- **Linux install bundle updated:** release asset includes the refreshed tile-plan cache set.

### Previous release (v1.3.15)

- **Device Installer (plugin-only) reliability:** plugin-only flow now force-uninstalls the old UV-PRO package before install to avoid version/signature mismatch, then proceeds through the plugin confirmation workflow and completion path.
- **Maps + add-ons UX flow:** when both options are selected, add-ons preflight runs before imagery, including completion messaging and disconnect guidance before download scope.
- **Radius zoom options expanded:** radius mode now offers zoom levels through **z18** (state mode remains capped at **z16**) with an explicit warning about z17/z18 data growth.

### Previous release (v1.3.14)

- **Add-ons refresh workflow hardening:** the downloader now uses the same checked add-ons routine (close ATAK, verify device, detect missing items, confirm install list) for both **refresh-only** and **maps + refresh** flows.
- **Maps + refresh execution order:** when both options are selected, add-ons refresh runs first. A completion dialog confirms device work is done before imagery starts.
- **KMZ/ZIP delivery path update:** `.kmz` and `.zip` add-ons are now copied to **`/sdcard/Download`** for manual ATAK import; the pipeline no longer unzips KML bundles automatically.

### Previous release (v1.3.13)

- **Imagery Downloader — same add-ons as the device installer:** after a successful download (and DTED stage), the downloader **pushes** everything under `scripts/data/mobile_xml/` (map XML, KMZ, import ZIPs) and **installs** bundled add-on APKs from `scripts/data/bundled_plugins/` when `ANDROID_SERIAL` is set — matching the installer bundles. **TAK‑UV‑PRO** is still not bundled; it is only installed via the Device Installer flow.
### Previous release (v1.3.12)

- **Device Installer — bundled add-on plugins:** extra ATAK plugin APKs under `scripts/data/bundled_plugins/` are installed over adb after the appropriate step (**ATAK only** → after ATAK + map assets; **ATAK + UV‑PRO** or **plugin only** → after TAK‑UV‑PRO). **TAK‑UV‑PRO** is never bundled; it still comes only from GitHub Releases / manifest / `deploy.env` so devices always pick up the latest published build.
- **Map / import add-ons:** `scripts/data/mobile_xml/` continues to ship map-source XML, KMZ, and import ZIPs (synced from your local “Add Ons for Build” folder before release). Subfolders are supported; device push uses each file’s basename.
- **Large CIV loadout zips:** multi‑gigabyte `ATAK-CIV*_loadout.zip` files are **gitignored**; copy them into `scripts/data/mobile_xml/` on the machine that runs `scripts/build_release.py` if the published Linux install zip must include them.

### Previous release (v1.3.11)

- **Device Installer — session log file:** the installer writes a timestamped log (and updates `LATEST_LOG.txt` in the same folder) so crashes after “Continue” can be diagnosed. Paths: `scripts/logs/` when run from source, or `~/.local/share/atak-pipeline/installer_logs/` when bundled. Stderr prints `Installer log: …` on startup.

### Previous release (v1.3.10)

- **Download-first workflow restored:** standalone imagery runs now skip upfront device verification, download imagery first, then prompt once to connect the device before the DTED/device check stage.
- **DTED local-path browser restored:** the "Local Elevation Location" selector is back on the download scope screen so local state ZIP trees can be used before network fetches.
- **State-name URL fix for DTED server paths:** multi-word state names now map to underscore server slugs (for example `North_Dakota`) so valid server files are found.
- **Stability fix for Linux Tk crash:** state boundary GeoJSON loading is preloaded on the main thread before worker startup to avoid the `Tcl_AsyncDelete` crash path seen during state-mode downloads.

### Previous release (v1.3.9)

- **Cancel works immediately during DTED download:** pressing Cancel now responds within one 256 KB chunk instead of waiting for the entire state ZIP to finish. The cancel signal is checked on every chunk in `download_file`; the partial `.part` file is cleaned up automatically.
- **Suppress benign semaphore warning:** the `resource_tracker: leaked semaphore` warning that appeared on exit after a cancel is now suppressed — it was harmless noise from the hard-exit path.

### Previous release (v1.3.8)

- **Imagery Downloader — DTED state dialog removed:** the "Local Elevation Location" section has been fully removed from the download scope screen; DTED is always handled automatically.
- **Imagery Downloader — DTED skip flag fix:** `mark_standalone_dted_skip()` is now set as soon as DTED processing begins, not only when a new zip is produced. This prevents the standalone DTED downloader from launching after the SQLite build when all required states were already on the device.
- **Imagery Downloader — "Calculating coverage" window:** the zenity progress window shown while computing radius tile counts is now wider (400 × 120 px) so the text fits comfortably.
- **Window stacking — no more rapid flashing:** `ensure_window_stacking` now does three quiet nudges (150 ms apart) instead of 30 pulses per 3 seconds, and `focus_force()` has been removed — eliminating the rapid-pulse visual artifact seen on the installer and summary dialogs.

### Previous release (v1.3.0)

- **Device Installer — installer-only mode:** no longer launches the imagery downloader automatically. A new **install selection screen** lets you choose ATAK + Plugin, ATAK only, or Plugin only. A final completion screen confirms the install and reminds you to run the imagery downloader separately.
- **Device Installer — signature mismatch recovery:** if the device has the plugin signed with a different certificate (`INSTALL_FAILED_UPDATE_INCOMPATIBLE`), the installer automatically performs a full uninstall then reinstalls cleanly — no manual intervention required.
- **Device Installer — ATAK setup bold note:** step 4 now shows a bold reminder to complete the ATAK setup before clicking Continue.
- **Imagery Downloader — radius naming:** a name field on the “Select radius or state” screen lets you give each radius download a unique name, preventing subsequent builds from overwriting previous ones. The field is disabled unless radius mode is selected.
- **Imagery Downloader — zoom cascade in radius mode:** selecting a zoom level now auto-selects all coarser zooms (same pyramid behavior as state mode).
- **DTED — conditional download:** before downloading, the pipeline queries the device’s installed DTED manifest (`.dted_states.json`). States already on the device are skipped; only missing states are downloaded and pushed. The manifest is updated on the device after each successful push.
- **Window stacking:** improved `ensure_window_stacking` reliability on Linux/X11 window managers.

**Auto-update requirements:** **Git** on `PATH`, network to `origin`, and a clone with `origin` pointing at this repository. Release zips and PyInstaller bundles without `.git` skip the check silently.

**Maintainer note (Windows / PyInstaller):** When packaging with a `.spec` that lists hidden imports explicitly, include **`tk_window_scaling`** and **`git_update_check`**.

### Previous release (v1.2.0)

- **Screen-aware Tk windows** (`scripts/tk_window_scaling.py`): main dialogs scale to fit small laptops and grow modestly on large displays.
- **Optional in-app update check** (`scripts/git_update_check.py`): when running from a git clone, fetches `origin/main` in the background and offers to pull and restart if new commits exist.
- **Linux install zip on Releases:** full tree under `atak-imagery/` for `install_linux.sh`; built with `python3 scripts/build_release.py`.
- **Tile-plan cache policy:** keep `scripts/data/tile_plans/v1/*.tiles.gz` out of git history; include them in the release zip asset instead.
- **One-command upload:** `scripts/build_and_upload_release_asset.sh <tag>` builds `dist/atak-imagery-v*-linux-install.zip` and uploads it to the GitHub release.

### Previous release (v1.0.0)

- **ATAK Device Installer**: production wizard only (debug skip controls removed); post-plugin instructions including device **OK** for plugin install; **Continue** before launching the imagery downloader
- **ATAK Imagery Downloader**: same SQLite handoff dialog when launched from the installer as in standalone; blocks **District of Columbia** as the only state selection, with an explanation; clearer errors when no states remain to download
- **DTED step**: pushes per-state `ATAK_SQL*.sqlite` file(s) and the DTED zip to the device under `/sdcard/atak/imagery` and `/sdcard/atak/DTED` (override with `ATAK_DEVICE_FILES_ROOT`); post-build **Yes/No** raw-imagery cleanup; adb restart of ATAK and completion dialog
- **Installer**: `deploy.env.example` seed; portable root paths in root launchers

---

## Overview

This project provides a streamlined pipeline for:

- imagery download
- SQLite creation for ATAK imagery packages
- DTED package download
- final ATAK-ready output packaging

Primary Linux/source scripts:

- `scripts/atak_downloader_finalbuild.py` — standalone Imagery Downloader
- `scripts/atak_downloader_from_installer.py` — same core, launched only after Device Installer
- `scripts/atak_imagery_sqlite_builder_finalbuild.py`
- `scripts/atak_dted_downloader.py`
- `scripts/build_tile_plan_cache.py` — optional: precompute per-state tile lists into `data/tile_plans/v1/*.tiles.gz` so downloads skip the slow “scanning tile coverage” step (see `scripts/data/tile_plans/README.md`)
- `scripts/git_update_check.py` — optional startup update offer for git clones (`origin/main`)
- `scripts/tk_window_scaling.py` — scales Tk geometry to the display

Windows-specific build copies:

- `windows_build/atak_downloader_finalbuild_win.py`
- `windows_build/atak_downloader_from_installer_win.py`
- `windows_build/atak_imagery_sqlite_builder_finalbuild_win.py`
- `windows_build/atak_dted_downloader_win.py`

---

## Critical Project Rule

**Do not treat Linux runtime scripts and Windows EXE scripts as the same thing anymore.**

### Linux / source truth

Linux runtime and source-truth pipeline live in:

```text
scripts/
```
