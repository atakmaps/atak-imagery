# ATAK Imagery Constitution

Updated: 2026-10-10

This file is binding. `AGENTS.md`, `docs/DEVELOPER-GUIDE.md`, `CONTRIBUTING.md`, the README, and local Cursor rules describe how to comply with it. When they disagree with this file, this file wins and the other document is wrong.

The maintainer reviews and merges pull requests. `main` is branch-protected. The maintainer may bypass that protection for their own work. Everyone else lands changes through a pull request the maintainer accepts.

GitHub: `atakmaps/atak-imagery`.

## I. Two programs, two entry points

This repository ships two desktop programs:

- **ATAK Device Installer** — USB install of ATAK and a plugin, then the imagery flow.
- **ATAK Imagery Downloader** — imagery, elevation, and SQLite when the phone is already set up.

They share download code. They do not share a first screen. Device Installer is `scripts/atak_adb_deploy.py`. When it finishes, it tells the user to start **ATAK Imagery Downloader** separately. It does not launch that wizard. The standalone downloader is `scripts/atak_downloader_finalbuild.py`. `scripts/atak_downloader_from_installer.py` is the entry that skips the USB intro when imagery is started as a follow-on. Do not fold the installer and the downloader into one script, and do not delete that wrapper because the installer does not call it today.

## II. Linux sources are the source of truth

Behavior changes go in `scripts/`. Windows copies are produced by `scripts/sync_windows_build.py` into `windows_build/*_win.py`, plus Windows-only patches inside that sync. Do not edit a Linux runtime file to add Windows behavior (`win32`, hidden console subprocesses, `AppData` paths). The sync script refuses to run if those markers appear in Linux sources.

## III. Version and ATAK support

The version string is the `VERSION` file at the repo root. Welcome screens, Linux zip names, and Windows installer version follow it.

ATAK **5.6 or newer** is supported. `scripts/atak_version_policy.py` rejects 5.5.x. Plugin APKs come from GitHub releases of `atakmaps/TAK-UV-PRO` and `atakmaps/TAK-MESHCORE`, matched to the ATAK version on the phone. This repo does not contain those plugins' source.

## IV. Secrets and generated data stay out of git

`deploy.env` is local. Commit changes to `deploy.env.example` when a new default key is required. Do not commit API tokens, filled-in `deploy.env`, phone serials, or logcats.

Do not commit virtualenvs, downloaded tiles, DTED, SQLite caches, or multi-gigabyte ATAK loadout zips. Precomputed `*.tiles.gz` plan files are release assets, not source.

## V. What a public Linux release contains

A Linux release the maintainer publishes uses the asset `atak-imagery-vX.Y.Z-linux-install.zip` from `scripts/build_release.py`. The GitHub "Source code" zip is not the install instructions. Release notes for that asset tell the user to unzip and run `install_linux.sh`, and they name the two launchers **ATAK Device Installer** and **ATAK Imagery Downloader**.

Those Linux notes do not include Windows install steps unless the maintainer asked to publish Windows in the same release.

The end-user zip ships `README.md` and the runtime. It does not ship this constitution, agent guides, or Windows build docs. Contributors read those from the git clone.

## VI. Release actions are explicit

Bumping `VERSION`, building the Linux zip, tagging, attaching `*-plugin-assets`, or building the Windows installer happens only when that is the stated point of the change. A bugfix pull request does not bump `VERSION` or retag plugin-asset releases.

## VII. Review before merge

Contributors, including AI agents, open a pull request. They do not merge it, force-push `main`, or publish a GitHub release. The pull request says what changed, whether Linux, Windows, or both were exercised, and whether `VERSION` or release assets were touched.
