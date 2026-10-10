# GitHub Copilot

The project guide is [`AGENTS.md`](../AGENTS.md). Read it before suggesting code. Policy is [`CONSTITUTION.md`](../CONSTITUTION.md). How the programs are built is [`docs/DEVELOPER-GUIDE.md`](../docs/DEVELOPER-GUIDE.md).

ATAK Imagery is two Tk programs. Device Installer is `scripts/atak_adb_deploy.py`. Imagery Downloader is `scripts/atak_downloader_finalbuild.py`. Change Linux sources in `scripts/`, then generate Windows copies with `scripts/sync_windows_build.py`. Do not commit `deploy.env` or bump `VERSION` unless the task is a release.
