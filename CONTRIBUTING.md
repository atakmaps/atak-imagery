# Contributing to ATAK Imagery

Updated: 2026-10-10

ATAK Imagery is two desktop programs: one installs ATAK and a plugin over USB, and one downloads imagery and builds the SQLite cache ATAK reads. Bug fixes and new features, including layout, are welcome. The maintainer reads the pull request and merges it when they agree.

Binding rules: [`CONSTITUTION.md`](CONSTITUTION.md). How the programs are put together: [`docs/DEVELOPER-GUIDE.md`](docs/DEVELOPER-GUIDE.md). AI assistants: [`AGENTS.md`](AGENTS.md). Users install from [`README.md`](README.md).

## Pull requests

`main` is protected. Open a pull request. The maintainer is the reviewer. Do not merge your own pull request and do not force-push `main`.

In the description include:

- what changed and why
- which program you ran (Device Installer, Imagery Downloader, or neither)
- whether the change is Linux-only or also needs a Windows sync
- a note if you touched `VERSION`, `deploy.env.example`, or release packaging

Do not put plugin source from `TAK-UV-PRO` or `TAK-MESHCORE` into this repository. The installer downloads those APKs from GitHub.

## Build

Linux:

```bash
git clone https://github.com/atakmaps/atak-imagery.git
cd atak-imagery
chmod +x install_linux.sh
./install_linux.sh
```

That creates `.venv` and `deploy.env` (from `deploy.env.example`). `deploy.env` stays on your machine. Change `deploy.env.example` only when every install needs a new default key.

A behavior change belongs in `scripts/`. Windows copies are generated:

```bash
python3 scripts/sync_windows_build.py
```

Do not edit `windows_build/*_win.py` to fix a bug that also exists on Linux. Fix `scripts/`, then sync.

## What needs extra care

- Leave `atak_downloader_from_installer.py` as the thin skip-intro entry. The Device Installer does not launch it; the last screen tells the user to start the Imagery Downloader. The standalone app stays `atak_downloader_finalbuild.py`.
- Keep TAK-UV-PRO and TAK-MESHCORE as separate installer choices and separate APK resolve orders. Do not merge them because the UV-PRO APK also supports MeshCore.
- ATAK older than 5.6 stays rejected (`scripts/atak_version_policy.py`).
- `scripts/build_release.py` is the Linux user zip. It ships `README.md` and the runtime, not these developer docs, not `deploy.env`, and not Windows packaging.
- A release ( `VERSION` bump, zip, git tag, `vX.Y.Z-plugin-assets` assets, Windows EXE) is something the maintainer asks for.

## Issues

Include the OS, whether you used the Linux install zip or a git clone, the `VERSION` string, ATAK version on the phone if a device step failed, and the steps. Do not paste `deploy.env` if it contains a token, and do not paste a phone serial.
