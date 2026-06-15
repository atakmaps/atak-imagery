"""
Optional update checks for ATAK Imagery Pipeline.

Two modes:
  run_startup_git_update_check   — git clone only; fetches origin/main and offers to pull + restart.
  run_startup_release_update_check — zip installs and frozen Windows EXEs; hits GitHub releases API
                                     and offers update (in-place script refresh or installer download).

Restart after git update uses os.execv with the same interpreter and argv.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.request
import webbrowser
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

GITHUB_REPO = "atakmaps/atak-imagery"
_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases"


def _center_window(win: object) -> None:
    """Center a Tk window on the screen."""
    try:
        win.update_idletasks()  # type: ignore[attr-defined]
        w = win.winfo_width()  # type: ignore[attr-defined]
        h = win.winfo_height()  # type: ignore[attr-defined]
        sw = win.winfo_screenwidth()  # type: ignore[attr-defined]
        sh = win.winfo_screenheight()  # type: ignore[attr-defined]
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2)
        win.geometry(f"+{x}+{y}")  # type: ignore[attr-defined]
    except Exception:
        pass


def _safe_destroy_tk(root: object) -> None:
    """Cancel pending after handlers before destroy (avoids Tcl_AsyncDelete on Linux)."""
    try:
        from tk_window_scaling import cancel_all_scheduled_after

        cancel_all_scheduled_after(root)  # type: ignore[arg-type]
    except Exception:
        pass
    try:
        root.destroy()  # type: ignore[union-attr]
    except Exception:
        pass


def read_version_file(repo_root: Path) -> str:
    vf = repo_root / "VERSION"
    if vf.is_file():
        line = vf.read_text(encoding="utf-8").strip().splitlines()
        return (line[0] if line else "").strip() or "unknown"
    return "unknown"


def installed_app_root(*, script_path: Path, is_frozen: bool) -> Path:
    """Root directory that holds VERSION for this install (zip, git, or frozen EXE)."""
    if is_frozen:
        return Path(sys.executable).resolve().parent
    repo_root = find_repo_root(script_path.parent)
    if repo_root is not None:
        return repo_root
    return script_path.parent.parent


def read_installed_version(*, script_path: Path, is_frozen: bool) -> str:
    """Read VERSION from the installed app tree (EXE dir for PyInstaller one-file builds)."""
    version = read_version_file(installed_app_root(script_path=script_path, is_frozen=is_frozen))
    if version != "unknown":
        return version
    if is_frozen:
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return read_version_file(Path(meipass))
    return "unknown"


def find_repo_root(start: Path) -> Optional[Path]:
    p = start.resolve()
    for _ in range(16):
        if (p / ".git").is_dir():
            return p
        if p.parent == p:
            break
        p = p.parent
    return None


def _run_git(repo: Path, *args: str, timeout: float = 180) -> Tuple[int, str, str]:
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", "git not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", "git timed out"
    except OSError as e:
        return 1, "", str(e)


class _GitUpdateState:
    __slots__ = ("done", "error", "update_available", "remote_version", "changelog", "behind")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.error: Optional[str] = None
        self.update_available = False
        self.remote_version = ""
        self.changelog: List[str] = []
        self.behind = 0


def _worker_fetch_and_compare(repo_root: Path, state: _GitUpdateState) -> None:
    try:
        code, _, err = _run_git(repo_root, "fetch", "origin", "main", timeout=180)
        if code != 0:
            state.error = err or "git fetch failed"
            return

        code, behind_txt, err = _run_git(repo_root, "rev-list", "--count", "HEAD..origin/main")
        if code != 0:
            state.error = err or "could not compare to origin/main"
            return
        try:
            n = int(behind_txt.strip())
        except ValueError:
            state.error = "unexpected git rev-list output"
            return
        if n <= 0:
            return

        state.update_available = True
        state.behind = n

        code, rv, _ = _run_git(repo_root, "show", "origin/main:VERSION", timeout=30)
        if code == 0 and rv.strip():
            state.remote_version = rv.strip().splitlines()[0].strip()
        else:
            code, tag, _ = _run_git(repo_root, "describe", "origin/main", "--tags", "--always", timeout=30)
            state.remote_version = tag.strip() if code == 0 else read_version_file(repo_root)

        code, log_out, _ = _run_git(
            repo_root,
            "log",
            "HEAD..origin/main",
            "--pretty=format:%s",
            "--no-decorate",
            "-n",
            "25",
            timeout=30,
        )
        state.changelog = [ln.strip() for ln in (log_out or "").splitlines() if ln.strip()]
    finally:
        state.done.set()


def _git_status_dirty(repo: Path) -> bool:
    code, out, _ = _run_git(repo, "status", "--porcelain", timeout=30)
    return code == 0 and bool(out.strip())


def _perform_update_and_restart(repo_root: Path, app_title: str, parent: Optional[object] = None) -> None:
    from tkinter import messagebox

    from tk_window_scaling import ensure_window_stacking, cancel_all_scheduled_after

    def _prep() -> None:
        if parent is not None:
            ensure_window_stacking(parent)  # type: ignore[arg-type]
            try:
                parent.update_idletasks()  # type: ignore[attr-defined]
            except Exception:
                pass

    if _git_status_dirty(repo_root):
        code, _, err = _run_git(repo_root, "stash", "push", "-u", "-m", "atak-pipeline auto-update", timeout=120)
        if code != 0:
            _prep()
            messagebox.showerror(
                app_title,
                f"Could not stash local changes:\n{err or 'git stash failed'}",
                parent=parent,
            )
            return

    code, _, err = _run_git(repo_root, "checkout", "main", timeout=60)
    if code != 0:
        code, _, err2 = _run_git(repo_root, "checkout", "-b", "main", "origin/main", timeout=60)
        if code != 0:
            _prep()
            messagebox.showerror(
                app_title,
                f"Could not checkout main:\n{err or err2 or 'git checkout failed'}",
                parent=parent,
            )
            return

    code, _, err = _run_git(repo_root, "pull", "origin", "main", "--ff-only", timeout=180)
    if code != 0:
        _prep()
        messagebox.showerror(
            app_title,
            f"Could not fast-forward main.\nResolve manually in:\n{repo_root}\n\n{err or 'git pull failed'}",
            parent=parent,
        )
        return

    _prep()
    messagebox.showinfo(app_title, "Update complete. The application will restart.", parent=parent)
    os.execv(sys.executable, [sys.executable, *sys.argv])


def run_startup_git_update_check(*, app_title: str, script_path: Path) -> None:
    """
    Call from main() before showing primary UI. May never return if user updates (os.execv).
    """
    if getattr(sys, "frozen", False):
        return

    repo_root = find_repo_root(script_path.parent)
    if repo_root is None:
        return

    state = _GitUpdateState()
    threading.Thread(target=_worker_fetch_and_compare, args=(repo_root, state), daemon=True).start()

    import tkinter as tk
    from tkinter import messagebox, ttk

    from tk_window_scaling import ensure_window_stacking, cancel_all_scheduled_after

    root = tk.Tk()
    root.withdraw()

    progress: Optional[tk.Toplevel] = None
    progress_timer: Optional[str] = None

    def show_progress() -> None:
        nonlocal progress
        if state.done.is_set() or progress is not None:
            return
        progress = tk.Toplevel(root)
        progress.title(app_title)
        progress.resizable(False, False)
        progress.transient(root)
        frm = tk.Frame(progress, padx=16, pady=12)
        frm.pack()
        tk.Label(frm, text="Checking for updates…").pack(anchor="w")
        bar = ttk.Progressbar(frm, mode="indeterminate", length=280)
        bar.pack(pady=(8, 0))
        bar.start(12)
        progress.update_idletasks()
        _center_window(progress)
        ensure_window_stacking(progress)

    progress_timer = root.after(2000, show_progress)

    def finish() -> None:
        nonlocal progress
        if progress_timer is not None:
            try:
                root.after_cancel(progress_timer)
            except tk.TclError:
                pass
        if progress is not None:
            try:
                progress.destroy()
            except tk.TclError:
                pass
            progress = None

        if state.error:
            _safe_destroy_tk(root)
            return

        if not state.update_available:
            _safe_destroy_tk(root)
            return

        local_v = read_version_file(repo_root)
        lines = state.changelog[:18]
        body = (
            f"Version {state.remote_version} is now available on main "
            f"(you are at {local_v}, {state.behind} new commit(s)).\n\n"
            f"Changes include:\n\n"
            + "\n".join(f"• {c}" for c in lines)
        )
        if len(state.changelog) > 18:
            body += "\n• …"
        body += (
            "\n\nUpdate now? Your repo will switch to branch main, fast-forward pull, "
            "and uncommitted changes will be stashed automatically if needed."
        )
        root.deiconify()
        root.update_idletasks()
        _center_window(root)
        ensure_window_stacking(root)
        root.update_idletasks()
        if not messagebox.askyesno(app_title, body, parent=root):
            _safe_destroy_tk(root)
            return

        _perform_update_and_restart(repo_root, app_title, root)
        _safe_destroy_tk(root)

    def poll() -> None:
        if state.done.is_set():
            finish()
        else:
            root.after(100, poll)

    root.after(50, poll)
    root.mainloop()


# ---------------------------------------------------------------------------
# GitHub releases API update check (works from zip installs — no git needed)
# ---------------------------------------------------------------------------

_GH_RAW = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main"
_GH_CONTENTS_API = f"https://api.github.com/repos/{GITHUB_REPO}/contents"
_GH_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "atak-imagery-update-check/1.0",
}


def _parse_semver(v: str) -> Tuple[int, ...]:
    """Convert 'v1.3.1' or '1.3.1' to (1, 3, 1) for comparison."""
    parts = []
    for p in v.strip().lstrip("v").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _windows_setup_asset(remote_version: str, setup_name: str = "", setup_url: str = "") -> Tuple[str, str]:
    """
    Return (download_url, filename) for the Windows ATAKSetup installer.

    Uses the GitHub release asset URL when the API returned one; otherwise builds the
    standard releases/download URL so updates work even before assets are listed.
    """
    version = remote_version.lstrip("v")
    name = setup_name or f"ATAKSetup-v{version}.exe"
    if setup_url:
        return setup_url, name
    url = f"https://github.com/{GITHUB_REPO}/releases/download/v{version}/{name}"
    return url, name


def _gh_get(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers=_GH_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


class _ReleaseCheckState:
    __slots__ = (
        "done",
        "error",
        "update_available",
        "remote_version",
        "release_body",
        "release_url",
        "linux_zip_url",
        "linux_zip_name",
        "windows_setup_url",
        "windows_setup_name",
    )

    def __init__(self) -> None:
        self.done = threading.Event()
        self.error: Optional[str] = None
        self.update_available = False
        self.remote_version = ""
        self.release_body = ""
        self.release_url = ""
        self.linux_zip_url = ""
        self.linux_zip_name = ""
        self.windows_setup_url = ""
        self.windows_setup_name = ""


def _worker_check_github_release(local_version: str, state: _ReleaseCheckState) -> None:
    try:
        data = json.loads(_gh_get(_RELEASES_API, timeout=8))
        tag = (data.get("tag_name") or "").strip()
        if not tag:
            return
        state.remote_version = tag.lstrip("v")
        state.release_body = (data.get("body") or "").strip()
        state.release_url = str(data.get("html_url") or _RELEASES_PAGE)
        assets = data.get("assets") or []
        for asset in assets:
            name = str(asset.get("name") or "")
            url = str(asset.get("browser_download_url") or "")
            if not url:
                continue
            lower = name.lower()
            if lower.endswith(".exe") and "ataksetup" in lower:
                state.windows_setup_name = name
                state.windows_setup_url = url
                continue
            if not name.endswith("-linux-install.zip"):
                continue
            state.linux_zip_name = name
            state.linux_zip_url = url
        if _parse_semver(tag) > _parse_semver(local_version):
            state.update_available = True
        if state.update_available and not state.windows_setup_url and state.remote_version:
            state.windows_setup_url, state.windows_setup_name = _windows_setup_asset(
                state.remote_version,
                state.windows_setup_name,
                "",
            )
    except Exception as exc:
        state.error = str(exc)
    finally:
        state.done.set()


class _DownloadState:
    __slots__ = ("done", "error", "current_file", "total", "completed", "bytes_mode")

    def __init__(self, total: int, *, bytes_mode: bool = False) -> None:
        self.done = threading.Event()
        self.error: Optional[str] = None
        self.current_file = ""
        self.total = total
        self.completed = 0
        self.bytes_mode = bytes_mode


def _extract_bundled_data_assets_from_linux_zip(zip_path: Path, repo_root: Path) -> int:
    """
    Refresh bundled downloader data assets from the Linux install zip into the installed tree:
    - tile plan caches (*.tiles.gz)
    - mobile xml/kmz/zip add-ons
    - bundled plugin APKs
    Returns number of extracted files.
    """
    scripts_data = repo_root / "scripts" / "data"
    scripts_data.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            name = info.filename
            if not name.startswith("atak-imagery/scripts/data/"):
                continue
            rel = name[len("atak-imagery/scripts/data/") :]
            if not rel or rel.endswith("/"):
                continue
            lower = rel.lower()
            allowed = (
                lower.startswith("tile_plans/v1/") and lower.endswith(".tiles.gz")
            ) or (
                lower.startswith("mobile_xml/") and lower.endswith((".xml", ".kmz", ".zip"))
            ) or (
                lower.startswith("bundled_plugins/") and lower.endswith(".apk")
            )
            if not allowed:
                continue
            out_path = scripts_data / rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, out_path.open("wb") as dst:
                dst.write(src.read())
            count += 1
    return count


def _worker_download_url_to_file(url: str, dest_path: Path, dl_state: _DownloadState) -> None:
    """Download a release asset to disk (Windows ATAKSetup.exe)."""
    try:
        req = urllib.request.Request(url, headers=_GH_HEADERS)
        with urllib.request.urlopen(req, timeout=600) as resp:
            cl = resp.headers.get("Content-Length", "").strip()
            total_bytes = int(cl) if cl.isdigit() else 0
            dl_state.bytes_mode = True
            dl_state.total = max(total_bytes, 1)
            dl_state.current_file = dest_path.name
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
            read = 0
            with tmp_path.open("wb") as out:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    out.write(chunk)
                    read += len(chunk)
                    dl_state.completed = read
                    if total_bytes <= 0:
                        dl_state.total = max(read, 1)
            tmp_path.replace(dest_path)
    except Exception as exc:
        dl_state.error = str(exc)
    finally:
        dl_state.done.set()


def _launch_windows_installer(installer_path: Path) -> None:
    """Run the downloaded ATAKSetup.exe and exit so files are not locked."""
    if not installer_path.is_file():
        raise FileNotFoundError(f"Installer not found: {installer_path}")
    import subprocess

    subprocess.Popen(
        [str(installer_path)],
        cwd=str(installer_path.parent),
        shell=False,
        close_fds=False,
    )
    os._exit(0)


def _worker_download_and_apply(
    scripts_dir: Path,
    repo_root: Path,
    linux_zip_url: str,
    linux_zip_name: str,
    dl_state: _DownloadState,
) -> None:
    """Download all scripts/*.py files + VERSION from main branch and replace in place."""
    import shutil
    import tempfile

    try:
        # Get file list for scripts/
        entries = json.loads(_gh_get(f"{_GH_CONTENTS_API}/scripts", timeout=15))
        py_files = [e for e in entries if e.get("type") == "file" and e["name"].endswith(".py")]
        # +1 for VERSION, +1 optional linux zip tile-plan cache extraction step.
        dl_state.total = len(py_files) + 1 + (1 if linux_zip_url else 0)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            # Download scripts
            for entry in py_files:
                name = entry["name"]
                dl_state.current_file = name
                url = entry.get("download_url") or f"{_GH_RAW}/scripts/{name}"
                content = _gh_get(url, timeout=30)
                (tmp / name).write_bytes(content)
                dl_state.completed += 1

            # Download VERSION
            dl_state.current_file = "VERSION"
            version_content = _gh_get(f"{_GH_RAW}/VERSION", timeout=15)
            (tmp / "VERSION").write_bytes(version_content)
            dl_state.completed += 1

            # Download latest release linux zip and refresh tile-plan caches if available.
            if linux_zip_url:
                dl_state.current_file = linux_zip_name or "linux-install.zip"
                zip_bytes = _gh_get(linux_zip_url, timeout=120)
                release_zip = tmp / "release_linux_install.zip"
                release_zip.write_bytes(zip_bytes)
                _extract_bundled_data_assets_from_linux_zip(release_zip, repo_root)
                dl_state.completed += 1

            # Apply — replace scripts in place
            for entry in py_files:
                name = entry["name"]
                src = tmp / name
                if src.exists():
                    shutil.copy2(src, scripts_dir / name)

            # Update VERSION at repo root
            shutil.copy2(tmp / "VERSION", repo_root / "VERSION")

    except Exception as exc:
        dl_state.error = str(exc)
    finally:
        dl_state.done.set()


def run_startup_release_update_check(*, app_title: str, script_path: Path) -> None:
    """Check GitHub releases API for a newer version.

    When a newer version is found, offers to download the updated scripts directly
    from the repository, replace them in the installed folder, and restart.

    Designed for zip installs (no .git folder). Skipped automatically for git
    clones (handled by run_startup_git_update_check).

    For PyInstaller/frozen builds, this checker prompts the user to download the
    latest installer from GitHub Releases instead of attempting in-place script replacement.
    """
    is_frozen = bool(getattr(sys, "frozen", False))

    # Git clones are handled by run_startup_git_update_check; avoid a double-dialog.
    if not is_frozen and find_repo_root(script_path.parent) is not None:
        return

    local_version = read_installed_version(script_path=script_path, is_frozen=is_frozen)
    if not local_version or local_version == "unknown":
        return

    if is_frozen:
        repo_root = installed_app_root(script_path=script_path, is_frozen=True)
        scripts_dir = repo_root
    else:
        scripts_dir = script_path.parent          # …/atak-imagery/scripts/
        repo_root = scripts_dir.parent            # …/atak-imagery/

    check_state = _ReleaseCheckState()
    threading.Thread(
        target=_worker_check_github_release,
        args=(local_version, check_state),
        daemon=True,
    ).start()

    import tkinter as tk
    from tkinter import messagebox, ttk

    from tk_window_scaling import ensure_window_stacking, cancel_all_scheduled_after

    root = tk.Tk()
    root.withdraw()

    def _lift() -> None:
        root.deiconify()
        root.update_idletasks()
        _center_window(root)
        ensure_window_stacking(root)
        root.update_idletasks()

    # ---- download + progress phase ----------------------------------------
    def start_download() -> None:
        dl_state = _DownloadState(total=1)
        threading.Thread(
            target=_worker_download_and_apply,
            args=(
                scripts_dir,
                repo_root,
                check_state.linux_zip_url,
                check_state.linux_zip_name,
                dl_state,
            ),
            daemon=True,
        ).start()

        prog_win = tk.Toplevel(root)
        prog_win.title(app_title)
        prog_win.resizable(False, False)
        frm = tk.Frame(prog_win, padx=20, pady=16)
        frm.pack()
        tk.Label(frm, text="Downloading update…", font=("Arial", 10, "bold")).pack(anchor="w")
        file_lbl = tk.Label(frm, text="", font=("Arial", 9), fg="gray40", width=40, anchor="w")
        file_lbl.pack(anchor="w", pady=(4, 6))
        bar = ttk.Progressbar(frm, mode="determinate", length=320, maximum=100)
        bar.pack(fill="x")
        prog_win.update_idletasks()
        _center_window(prog_win)
        ensure_window_stacking(prog_win)

        def poll_dl() -> None:
            if dl_state.total > 0:
                bar["value"] = 100 * dl_state.completed / dl_state.total
            file_lbl.configure(text=dl_state.current_file)

            if dl_state.done.is_set():
                prog_win.destroy()
                if dl_state.error:
                    _lift()
                    messagebox.showerror(
                        app_title,
                        f"Update failed:\n{dl_state.error}",
                        parent=root,
                    )
                    _safe_destroy_tk(root)
                    return
                _lift()
                messagebox.showinfo(
                    app_title,
                    "Update complete. The application will now restart.",
                    parent=root,
                )
                _safe_destroy_tk(root)
                os.execv(sys.executable, [sys.executable, *sys.argv])
            else:
                root.after(150, poll_dl)

        root.after(150, poll_dl)

    def _poll_download_progress(dl_state: _DownloadState, prog_win: tk.Toplevel, bar: ttk.Progressbar, file_lbl: tk.Label, on_success) -> None:
        if dl_state.total > 0:
            bar["value"] = 100 * dl_state.completed / dl_state.total
        file_lbl.configure(text=dl_state.current_file)

        if dl_state.done.is_set():
            prog_win.destroy()
            if dl_state.error:
                _lift()
                messagebox.showerror(
                    app_title,
                    f"Update failed:\n{dl_state.error}",
                    parent=root,
                )
                _safe_destroy_tk(root)
                return
            on_success()
            return
        root.after(150, lambda: _poll_download_progress(dl_state, prog_win, bar, file_lbl, on_success))

    def start_windows_setup_download(setup_url: str, setup_name: str) -> None:
        import tempfile

        update_dir = Path(tempfile.gettempdir()) / "atak-pipeline-update"
        dest_path = update_dir / setup_name
        dl_state = _DownloadState(total=1, bytes_mode=True)
        threading.Thread(
            target=_worker_download_url_to_file,
            args=(setup_url, dest_path, dl_state),
            daemon=True,
        ).start()

        prog_win = tk.Toplevel(root)
        prog_win.title(app_title)
        prog_win.resizable(False, False)
        frm = tk.Frame(prog_win, padx=20, pady=16)
        frm.pack()
        tk.Label(frm, text="Downloading update…", font=("Arial", 10, "bold")).pack(anchor="w")
        file_lbl = tk.Label(frm, text="", font=("Arial", 9), fg="gray40", width=48, anchor="w")
        file_lbl.pack(anchor="w", pady=(4, 6))
        bar = ttk.Progressbar(frm, mode="determinate", length=360, maximum=100)
        bar.pack(fill="x")
        prog_win.update_idletasks()
        _center_window(prog_win)
        ensure_window_stacking(prog_win)

        def on_success() -> None:
            _lift()
            messagebox.showinfo(
                app_title,
                "Download complete. The ATAK Pipeline installer will now run.\n"
                "This app will close so the update can replace the programs.",
                parent=root,
            )
            _safe_destroy_tk(root)
            _launch_windows_installer(dest_path)

        root.after(150, lambda: _poll_download_progress(dl_state, prog_win, bar, file_lbl, on_success))

    # ---- version-check phase -----------------------------------------------
    def finish() -> None:
        if check_state.error or not check_state.update_available:
            _safe_destroy_tk(root)
            return

        notes = check_state.release_body
        if len(notes) > 500:
            notes = notes[:500].rsplit("\n", 1)[0] + "\n…"

        body = (
            f"Version {check_state.remote_version} is available "
            f"(you are running {local_version}).\n\n"
        )
        if notes:
            body += notes + "\n\n"
        if is_frozen:
            if sys.platform.startswith("win"):
                setup_url, setup_name = _windows_setup_asset(
                    check_state.remote_version,
                    check_state.windows_setup_name,
                    check_state.windows_setup_url,
                )
                body += (
                    f"Update now? The latest installer will download and run:\n{setup_name}"
                )
            else:
                body += "Update now? The browser will open the latest release page."

            _lift()
            if messagebox.askyesno(app_title, body, parent=root):
                if sys.platform.startswith("win"):
                    root.withdraw()
                    start_windows_setup_download(setup_url, setup_name)
                    return
                try:
                    webbrowser.open(check_state.release_url or _RELEASES_PAGE)
                except Exception:
                    pass
            _safe_destroy_tk(root)
            return

        body += "Update now? The scripts will be replaced and the app will restart automatically."
        if check_state.linux_zip_url:
            body += (
                "\n\nThis update will also refresh bundled downloader data assets "
                "(tile-plan caches, map/import add-ons, and plugin APKs)."
            )

        _lift()
        if not messagebox.askyesno(app_title, body, parent=root):
            _safe_destroy_tk(root)
            return

        root.withdraw()
        start_download()

    def poll() -> None:
        if check_state.done.is_set():
            finish()
        else:
            root.after(200, poll)

    root.after(50, poll)
    root.mainloop()
