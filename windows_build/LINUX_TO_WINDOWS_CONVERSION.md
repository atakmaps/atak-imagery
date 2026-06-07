# Linux → Windows conversion checklist

**Policy:** `scripts/` is Linux truth. **Never edit Linux for Windows.**  
Every Windows build runs `scripts/sync_windows_build.py`, which copies Linux sources into `windows_build/` and applies the patches below.

When Linux changes, re-run sync and update this list + patch functions if new Linux code needs Windows-specific handling.

---

## Workflow

```bash
# From repo root (Linux maintainer, or automatically on Windows before PyInstaller)
python3 scripts/sync_windows_build.py
python3 -m py_compile windows_build/atak_*_win.py windows_build/*.py
```

Sync **refuses to run** if Linux runtime files contain Windows-only markers (`win32`, `run_hidden`, `AppData\...`, etc.).

---

## 1. Automatic renames (all `*_win.py` modules)

| Linux | Windows copy |
|-------|----------------|
| `atak_adb_deploy.py` | `atak_adb_deploy_win.py` |
| `atak_downloader_finalbuild.py` | `atak_downloader_finalbuild_win.py` |
| `atak_downloader_from_installer.py` | `atak_downloader_from_installer_win.py` |
| `atak_imagery_sqlite_builder_finalbuild.py` | `atak_imagery_sqlite_builder_finalbuild_win.py` |
| `atak_dted_downloader.py` | `atak_dted_downloader_win.py` |

**String substitutions** (`SUBSTITUTIONS` in sync script):

- All `import` / docstring references: `atak_*` → `atak_*_win`
- `from atak_adb_deploy import` → `from atak_adb_deploy_win import`
- USB help text: udev rules hint → Windows platform-tools download URL

**Status:** ✅ in `sync_windows_build.py` → `_apply_substitutions()`

---

## 2. Patches on `atak_adb_deploy_win.py`

| # | Change | Why | Sync function |
|---|--------|-----|----------------|
| A1 | `DEFAULT_MESHCORE_PLUGIN_REPO` → `%USERPROFILE%\Documents\ATAK\Plugins\MeshcoreAtak` | Linux path invalid on Windows | `_patch_adb_deploy_win` |
| A2 | Split imports: tkinter / `git_update_check` / `tk_window_scaling` in separate `try` blocks | PyInstaller import failures should not disable entire GUI | `_patch_adb_deploy_win` |
| A3 | Replace `ensure_gui_path_for_adb()` body with Windows adb search paths (`AppData`, `tools\platform-tools` beside EXE/repo) | Desktop/EXE launches have short PATH | `_patch_adb_deploy_win` |
| A4 | `PROJECT_ROOT` = directory beside frozen EXE (not `MEIPASS`) | `deploy.env` lives next to installed EXE | `_patch_adb_deploy_win` |
| A5 | `load_deploy_env_file()`: copy `deploy.env.example` if `deploy.env` missing | First-run frozen install | `_patch_adb_deploy_win` |
| A6 | Installer log dir: `%LOCALAPPDATA%\atak-pipeline\installer_logs` (not `~/.local/share/...`) | Writable on Windows frozen bundle | `_patch_adb_deploy_win` |
| A7 | `import run_hidden` from `win_subprocess` | Hide adb console windows | `_inject_win_subprocess` |
| A8 | `run_adb()` / `adb_available()`: use `run_hidden`; soft-fail on missing adb / timeout | No crash if adb absent; no CMD flash | `_patch_subprocess_calls` |

**Status:** ✅ all in sync

---

## 3. Patches on `atak_downloader_finalbuild_win.py`

| # | Change | Why | Sync function |
|---|--------|-----|----------------|
| D1 | Import `ensure_gui_path_for_adb` from `atak_adb_deploy_win` | Find adb on Windows PATH | `_patch_downloader_win` |
| D2 | `main()`: call `ensure_gui_path_for_adb()` at startup (log warning on failure) | Same as A3 for standalone downloader | `_patch_downloader_win` |
| D3 | `_run_adb()` / `adb_available()`: `run_hidden` + soft-fail | Same as A8 | `_patch_subprocess_calls` |
| D4 | `_resolve_adb_serial_for_push()`: return early if `not adb_available()` | Skip push when no adb | `_patch_downloader_win` |
| D5 | `show_downloader_session_exit_dialog()`: do **not** call `ensure_window_stacking(parent)` before modal | Windows focus pulse breaks Finish dialog (multi-click) | `_patch_downloader_win` |

**Status:** ✅ all in sync

---

## 4. Patches on `atak_dted_downloader_win.py`

| # | Change | Why | Sync function |
|---|--------|-----|----------------|
| T1 | All adb `subprocess.run(...)` → `run_hidden(...)` | Hide console during device push | `_patch_dted_win` |

**Status:** ✅ in sync  
**Note:** `os.startfile()` for upload folder already in Linux truth (no patch).

---

## 5. `atak_imagery_sqlite_builder_finalbuild_win.py`

| # | Change | Why | Sync function |
|---|--------|-----|----------------|
| S1 | Module renames only | — | `_apply_substitutions` |
| S2 | Frozen EXE: inline `import atak_dted_downloader_win; dted.main()` instead of `Popen` | `sys.executable` on Windows is the EXE, not Python | **Already in Linux truth** — copies verbatim |

**Status:** ✅ no extra patches today

---

## 6. `atak_downloader_from_installer_win.py`

| # | Change | Why | Sync function |
|---|--------|-----|----------------|
| I1 | Module renames only | Device Installer → imagery entry wrapper | `_apply_substitutions` |

**Status:** ✅ no extra patches

---

## 7. Direct copies (shared logic, same filename in `windows_build/`)

Copied from `scripts/` **without** `_win` rename:

| File | Windows-specific? |
|------|-------------------|
| `imagery_tile_selection.py` | No — same code |
| `git_update_check.py` | Skips git/release update when `sys.frozen` (already in Linux) |
| `bundled_plugin_install.py` | No |
| `usgs_throughput_probe.py` | No |

**Status:** ✅ `_sync_direct_copies()`

---

## 8. Windows-only files (NOT copied from Linux — maintain separately)

| File | Purpose |
|------|---------|
| `windows_build/tk_window_scaling.py` | Gentle `lift()` on Windows; **do not** use Linux 3s topmost/focus pulse |
| `windows_build/win_subprocess.py` | `CREATE_NO_WINDOW` for adb/subprocess |
| `windows_build/windows_launcher.py` | PyInstaller entry: Imagery Downloader; `freeze_support()`; GUI only when `sys.frozen` |
| `windows_build/windows_installer_launcher.py` | PyInstaller entry: Device Installer; same guards |
| `windows_build/build_windows_exe.ps1` | PyInstaller dual EXE build + hidden imports + data bundle |
| `scripts/setup_windows_pipeline.ps1` | Windows machine setup (copied to `windows_build/` for reference) |
| `install_windows.cmd` | Maintainer setup entry (console — not end-user installer) |
| `ATAK_Setup.iss` | **Future:** standard Inno Setup wizard for release (post-debugging) |

### Launcher-specific rules (not in Linux)

| # | Change | Why |
|---|--------|-----|
| L1 | `multiprocessing.freeze_support()` before `main()` | Zoom probe spawn must not reopen Welcome screen |
| L2 | `if getattr(sys, "frozen", False): main()` | PyInstaller build must not open GUI |
| L3 | `_configure_frozen_tk()` sets `TCL_LIBRARY` / `TK_LIBRARY` from `_MEIPASS` | Tk works in onefile EXE |

**Status:** ✅ in launcher files; **not** generated by sync

---

## 9. Already in Linux truth (copies with renames only — do not duplicate in patches)

These exist in `scripts/` for PyInstaller/frozen builds on both platforms:

- `SCRIPT_DIR` / `RUNTIME_STATE_DIR` when `sys.frozen` (`_MEIPASS`, exe parent dir)
- Downloader → SQLite builder handoff via inline `import` when frozen
- SQLite builder → DTED handoff via inline `import` when frozen
- `git_update_check`: skip update dialogs when frozen
- DTED: `os.startfile()` branch for `sys.platform.startswith("win")`
- Zenity folder picker: skipped on Windows (falls through to Tk `filedialog`)

**When Linux adds new frozen/PyInstaller paths, verify sync still copies them — usually no new patch needed.**

---

## 10. Build / packaging (after sync, on Windows VM)

Not part of `sync_windows_build.py`, but required for working EXEs:

| Step | What |
|------|------|
| B1 | Bundle `windows_build/data/` → `scripts\data` in PyInstaller |
| B2 | Bundle Tcl/Tk 8.6 from Python install |
| B3 | Hidden-import all `*_win`, `win_subprocess`, `tk_window_scaling`, helpers |
| B4 | Copy `platform-tools` (adb) to `dist\tools\` |
| B5 | Copy `deploy.env.example`, `VERSION` beside EXEs |

---

## 11. TODO — add to sync when needed

| # | Gap | When to patch |
|---|-----|----------------|
| U1 | `aapt` subprocess calls (APK badging) still use plain `subprocess.run` | If console flash appears during plugin install |
| U2 | `git_update_check` git subprocess | If git installed and console flashes on startup (git clones only) |
| U3 | Non-adb `subprocess.Popen([sys.executable, script])` paths | Should not run when frozen; verify after Linux changes |
| U4 | New Linux subprocess / shell assumptions (`zenity`, `xdg-open`, `/proc`, udev) | Add Windows branch in sync patch when Linux adds them |

---

## 12. After every Linux release

1. Pull latest `scripts/` on build machine  
2. Run `python3 scripts/sync_windows_build.py`  
3. If sync fails → Linux file accidentally contains Windows markers → **fix Linux, do not patch around it**  
4. If new Linux feature needs Windows behavior → add row to sections 2–4 or §11, implement in sync, update this doc  
5. `py_compile` + test both EXEs on Windows VM  
6. *(Later)* Build `ATAKSetup.exe` from Inno Setup for end users  

---

*Canonical patch implementation: `scripts/sync_windows_build.py`*
