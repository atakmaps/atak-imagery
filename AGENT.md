# AGENT.md — atak-imagery / pipeline

Updated: 2026-10-24

## Goal
Keep the Linux Device Installer and Imagery Downloader stable; Windows follows by sync.

## Done
- **v1.3.57** (Linux): fixed Tk default-root bug after the add-ons step (radius/MGRS fields read empty) via `install_tk_destroy_guard()` in `git_update_check.py`; protected-import password prompt shown on main thread; add-ons refresh runs once (preflight only); TAK-UV-CORE excluded from add-ons by package id.
- v1.3.56: update notices use app font and are centered (`show_notice`). Windows `ATAKSetup-v1.3.56.exe` was published by the Windows session.
- v1.3.55: TAK-MESHCORE removed from the Device Installer; TAK-UV-PRO renamed TAK-UV-CORE (package id `com.uvpro.plugin` unchanged).
- **No Windows EXE built** for 1.3.57.

## Next
- Windows VM: `git pull origin main`, run `windows_build\build_windows_exe.ps1`, upload `ATAKSetup-v1.3.57.exe` to the v1.3.57 release.
- Retest Device Installer plugin load: press Continue only after accepting the plugin prompt on the phone.
- Optional: investigate "messed up tiles" report from a radius run (log not yet reviewed).

## Pointers
- Release: https://github.com/atakmaps/atak-imagery/releases/tag/v1.3.57
- Release flow: feature branch -> PR (main protected) -> `python3 scripts/build_release.py` -> `gh release create` + `v1.3.57-plugin-assets`.
