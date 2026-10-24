# AGENT.md — atak-imagery / pipeline

Updated: 2026-10-24

## Goal
Keep the Linux Device Installer and Imagery Downloader stable; Windows follows by sync.

## Done
- **v1.3.56**: "Update complete" and update error notices use the app font and are centered (`show_notice` in `git_update_check.py`). Takes effect on the update after 1.3.56, since the running install shows the dialog.
- v1.3.55: TAK-MESHCORE option removed from the Device Installer; TAK-UV-PRO renamed to **TAK-UV-CORE** everywhere (plugin package id `com.uvpro.plugin` unchanged; source repo `atakmaps/TAK-UV-CORE`).
- v1.3.54: update dialog visible on Cinnamon (off-screen 1x1 root).
- Windows copies in `windows_build/` synced; **no Windows EXE built** for 1.3.56.

## Next
- Retest plugin load: press Continue only after accepting the plugin prompt on the phone (step 6). A 2 s Continue earlier left the plugin unloaded.
- Optional hardening: delay Continue until the phone prompt is answered.

## Pointers
- Release: https://github.com/atakmaps/atak-imagery/releases/tag/v1.3.56
- Release flow: feature branch -> PR (main is protected) -> `python3 scripts/build_release.py` -> `gh release create` + `v1.3.56-plugin-assets`.
- Last user commit (pipeline): see `git log` on `main` for "Release v1.3.56".
