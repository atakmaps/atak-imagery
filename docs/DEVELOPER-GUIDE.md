# ATAK Imagery developer guide

Updated: 2026-10-10

This is how the pipeline is built. Binding rules are in [`CONSTITUTION.md`](../CONSTITUTION.md). Pull requests are in [`CONTRIBUTING.md`](../CONTRIBUTING.md). AI assistants start at [`AGENTS.md`](../AGENTS.md).

`README.md` is the user install guide. `windows_agent.md`, `HANDOFF_*.md`, and `RELEASE_CHECKLIST.md` are maintainer session notes. When they disagree with this guide or the code, follow this guide and the code.

The version that builds is the single line in the root `VERSION` file.

## What a user runs

| Program | Linux entry | After `install_linux.sh` |
|---|---|---|
| ATAK Device Installer | `scripts/atak_adb_deploy.py` | desktop launcher and `run_atak_pipeline_with_device.sh` |
| ATAK Imagery Downloader | `scripts/atak_downloader_finalbuild.py` | desktop launcher and `run_atak_pipeline.sh` |

The installer does not start the downloader. Its last screen tells the user to exit and run **ATAK Imagery Downloader**. `scripts/atak_downloader_from_installer.py` sets `ATAK_DOWNLOADER_LAUNCHED_FROM_DEVICE_INSTALLER` so the shared core skips the standalone USB intro when that entry is used. The installer defines that path and does not call it. Leave the wrapper in place. Do not wire an automatic handoff unless the maintainer asks.

`install_linux.sh` in the repo root starts `scripts/install_linux.sh`. That script installs system packages (Python, Tk, adb), creates `.venv`, installs `requirements.txt`, copies `deploy.env.example` to `deploy.env` on first run, and installs the two desktop entries. Arch is the exception for native GIS libraries: pacman installs GDAL, GEOS, and PROJ, then pip prefers binary wheels against those. Debian and Fedora installs rely on pip wheels for geopandas, shapely, and rasterio and do not apt/dnf-install GDAL. The installed copy lives under `~/.local/share/atak-imagery` unless `ATAK_PIPELINE_HOME` is set.

Windows ships the same two programs as `ATAKDeviceInstaller.exe` and `ATAKImageryDownloader.exe`, built on a Windows machine from `windows_build/`. See the Windows section below.

## Device Installer flow

`atak_adb_deploy.py` is a Tk wizard:

1. Read `deploy.env` at the repo root (or the install copy).
2. Resolve an ATAK CIV APK. `ATAK_DEPLOY_MANIFEST_URL` wins when set. The manifest JSON carries `atak_version` and `atak_apk_url`. Otherwise `ATAK_CIV_APK_URL` and `ATAK_CIV_VERSION` are used. Versions below 5.6 are rejected by `atak_version_policy.py`.
3. Install ATAK over adb (`ATAK_PACKAGE_NAME`, default `com.atakmap.app.civ`). Signature mismatch can uninstall and retry. A lower versionCode retries with `--allow-downgrade`, then the legacy `-d` flag.
4. Install the plugin the user picked, or skip it. The choice screen is five options: ATAK + TAK-UV-PRO, ATAK + TAK-MESHCORE, ATAK only, TAK-UV-PRO only, TAK-MESHCORE only. ATAK-only skips the plugin steps. Plugin-only skips the ATAK install and first-run steps. Every option ends on the same completion screen: exit, then run the Imagery Downloader yourself.

These are two APK products. `atakmaps/TAK-UV-PRO` is the UV-PRO plugin (that APK also contains MeshCore support). `atakmaps/TAK-MESHCORE` is the MeshCore-only plugin. The installer offers both. Do not collapse them into one choice because the UV-PRO plugin can speak MeshCore.

UV-PRO resolve order in `resolve_plugin_apk`:

1. `ATAK_PLUGIN_APK` — local file, overrides everything else
2. `ATAK_PLUGIN_GITHUB_REPO` — Latest GitHub release, APK name matched to the phone's ATAK version (default repo `atakmaps/TAK-UV-PRO`)
3. `ATAK_PLUGIN_REPO` — newest `.apk` under a directory
4. `ATAK_PLUGIN_APK_URL` — download
5. `plugin_apk_url` on the manifest

MeshCore resolve order in `resolve_meshcore_plugin_apk` is separate:

1. `ATAK_MESHCORE_PLUGIN_APK` — local file
2. `ATAK_MESHCORE_PLUGIN_GITHUB_REPO` — Latest release, same ATAK-version match (default `atakmaps/TAK-MESHCORE`)
3. `ATAK_MESHCORE_PLUGIN_REPO` — newest `.apk` in a directory
4. `ATAK_MESHCORE_PLUGIN_APK_URL` — download

`ATAK_PLUGIN_APK` does not override the MeshCore choice. Use `ATAK_MESHCORE_PLUGIN_APK` for that.

Optional `ATAK_DEPLOY_REPORT_URL` receives a JSON POST after ATAK install and after plugin install. `ATAK_DEPLOY_API_TOKEN` is a bearer token for that POST. Both stay in local `deploy.env`.

Add-on plugin APKs are not installed by the Device Installer. The imagery downloader installs those.

## Imagery Downloader flow

`atak_downloader_finalbuild.py` is the shared core. Standalone launch shows the USB/adb intro. The installer launch does not.

The wizard order is download scope (whole state or fixed radius), place, zoom, summary, then an output folder. Tiles land at:

```text
<parent>/Imagery/<state or radius name>/<zoom>/<x>/<y>.jpg
```

Existing tiles are skipped on a re-run. Zoom coverage for US states uses precomputed tile lists when `scripts/data/tile_plans/v1/*.tiles.gz` is present, so the UI does not re-walk every tile rectangle. Those gzip files are release assets. Maintainers rebuild them with `scripts/build_tile_plan_cache.py` from `scripts/data/us_states.geojson`. A full state×zoom run takes hours.

After a successful download, if an adb serial is set, the downloader pushes map/import files and installs add-on plugins. Add-on APKs and protected import zips are downloaded from the GitHub release named in `deploy.env`:

- `ATAK_ADDON_PLUGIN_GITHUB_REPO` / `ATAK_ADDON_PLUGIN_RELEASE_TAG` (default `atakmaps/atak-imagery` and `v<VERSION>-plugin-assets`)
- `ATAK_PROTECTED_IMPORT_GITHUB_REPO` / `ATAK_PROTECTED_IMPORT_RELEASE_TAG` for password-gated imports

Protected-import entries are a comma-separated list (`ATAK_PROTECTED_IMPORT_ASSET_NAMES`). Each entry is `Asset.zip`, or `Asset.zip@/sdcard/atak/some/dir`, or `Asset.zip@/sdcard/atak/some/dir>File name on device`. The part before `@` is the GitHub asset. The path after `@` is the device directory. The name after `>` is the filename to write. With no `@`, the directory is `/sdcard/atak/tools/import`. The shipped default sends the AmRRON CSV through that import folder and `AmRRON.Forms.xml.zip` to `/sdcard/atak/tools/reports/templates` as `AmRRON Forms.xml`. That default is not the only routing the parser accepts.

It then offers DTED (`atak_dted_downloader.py`) and builds an osmdroid-style SQLite cache (`atak_imagery_sqlite_builder_finalbuild.py`). The SQLite step imports every numeric zoom folder under the selected imagery tree. When `.last_imagery_session_states.txt` exists, only the states from that session are packed, so older `Imagery/<State>/` trees are not turned into extra databases.

`scripts/imagery_tile_selection.py` is the shared tile-rectangle helper. `scripts/usgs_throughput_probe.py` feeds the zoom-screen ETA.

## Where a change goes

| You are changing | Look here |
|---|---|
| USB install wizard, manifest, plugin APK choice | `scripts/atak_adb_deploy.py` |
| Skip-intro wrapper (not launched by the installer today) | `scripts/atak_downloader_from_installer.py` |
| Download wizard, tile fetch, map push | `scripts/atak_downloader_finalbuild.py` |
| Tile rectangles and radius selection | `scripts/imagery_tile_selection.py` |
| Which ATAK versions are allowed | `scripts/atak_version_policy.py` |
| Elevation download and clip | `scripts/atak_dted_downloader.py` |
| SQLite pack for ATAK | `scripts/atak_imagery_sqlite_builder_finalbuild.py` |
| Startup update check | `scripts/git_update_check.py` |
| Tk scaling and dialog stacking | `scripts/tk_window_scaling.py` |
| Linux install, venv, desktop files | `scripts/install_linux.sh`, root `install_linux.sh` |
| Default config keys | `deploy.env.example` |
| Linux zip contents | `scripts/build_release.py` |
| Windows copy of the above | produced by `scripts/sync_windows_build.py`; do not hand-edit `*_win.py` for a behavior change |

A behavior change in a Linux `scripts/atak_*.py` file is unfinished for Windows until sync has been run and the Windows-only patch list in `windows_build/LINUX_TO_WINDOWS_CONVERSION.md` still matches. Do the Linux change first. Then sync. Then patch only inside the sync script or the Windows-only files it preserves (`windows_build/tk_window_scaling.py`, `win_subprocess.py`, the PyInstaller launchers).

## Configuration

`deploy.env.example` is the committed template. `install_linux.sh` copies it to `deploy.env` and backfills keys that newer versions added. Forks change URLs there.

The defaults point ATAK installs at a manifest URL and point plugins at the public GitHub repos above. The manifest file itself is hosted beside the ATAK APK. This repository does not upload that JSON. When a new ATAK build is published, the manifest's `atak_version` and `atak_apk_url` are updated on that host.

Do not put a token, a phone serial, or a private path into `deploy.env.example`.

## Linux release zip

Maintainer release, from the repo root, after `VERSION` is the version you mean to ship:

```bash
python3 scripts/build_release.py
```

Output: `dist/atak-imagery-v<VERSION>-linux-install.zip`. The archive root is a folder named `atak-imagery/`. It includes `scripts/`, `install_linux.sh`, data the runtime needs, and `README.md`. It excludes `.venv`, `windows_build/`, `deploy.env`, `scripts/data/bundled_plugins/`, every markdown file except `README.md`, tile gz caches, and the internal handoff docs.

Users are told to unzip that asset and run `./install_linux.sh`. They are not told to use the GitHub source zip.

Add-on plugin APKs for that version are attached to the Git tag `v<VERSION>-plugin-assets` on `atakmaps/atak-imagery`, which is what `deploy.env.example` names. Bump those tag strings in the example when `VERSION` changes as part of a release.

## Windows

Windows is a second build of the same features, not a second product. From a Linux checkout, after Linux edits:

```bash
python3 scripts/sync_windows_build.py
python3 scripts/audit_windows_bundle.py
python3 -m py_compile windows_build/atak_*_win.py windows_build/*.py
```

On Windows, `install_windows.cmd` or `scripts/setup_windows_pipeline.ps1` runs that sync, then PyInstaller. A rebuild when dependencies are already present is `windows_build/build_windows_exe.ps1`. The file list PyInstaller must include is `scripts/windows_bundle_manifest.py`.

Inno Setup scripts: `ATAK_Setup.iss` (current installer) and `ATAKPipeline_Setup.iss`. Their version follows `VERSION`.

Public Linux release notes stay Linux-only unless the maintainer is publishing the Windows installer in that same announcement.

## What not to treat as the architecture

Local Cursor rules under `.cursor/rules/` repeat the Linux-notes policy, the Windows sync rule, and the maintainer's private add-on folder. That folder is on the maintainer's machine. It is not a path contributors have, and it is not in git. Public installs get add-ons from the `*-plugin-assets` release.
