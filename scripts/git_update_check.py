"""
Optional update checks for ATAK Imagery Pipeline.

Two modes:
  run_startup_git_update_check   — git clone only; fetches origin/main and offers to pull + restart.
  run_startup_release_update_check — zip installs; hits GitHub releases API and opens the download
                                     page if a newer version exists. Skipped in git clones (the git
                                     check already handles those) and PyInstaller bundles.

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
from pathlib import Path
from typing import List, Optional, Tuple

GITHUB_REPO = "atakmaps/atak-imagery"
_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases"


def read_version_file(repo_root: Path) -> str:
    vf = repo_root / "VERSION"
    if vf.is_file():
        line = vf.read_text(encoding="utf-8").strip().splitlines()
        return (line[0] if line else "").strip() or "unknown"
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

    from tk_window_scaling import ensure_window_stacking

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

    from tk_window_scaling import ensure_window_stacking

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
            root.destroy()
            return

        if not state.update_available:
            root.destroy()
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
        ensure_window_stacking(root)
        root.update_idletasks()
        if not messagebox.askyesno(app_title, body, parent=root):
            root.destroy()
            return

        _perform_update_and_restart(repo_root, app_title, root)
        root.destroy()

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


def _gh_get(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers=_GH_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


class _ReleaseCheckState:
    __slots__ = ("done", "error", "update_available", "remote_version", "release_body")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.error: Optional[str] = None
        self.update_available = False
        self.remote_version = ""
        self.release_body = ""


def _worker_check_github_release(local_version: str, state: _ReleaseCheckState) -> None:
    try:
        data = json.loads(_gh_get(_RELEASES_API, timeout=8))
        tag = (data.get("tag_name") or "").strip()
        if not tag:
            return
        state.remote_version = tag.lstrip("v")
        state.release_body = (data.get("body") or "").strip()
        if _parse_semver(tag) > _parse_semver(local_version):
            state.update_available = True
    except Exception as exc:
        state.error = str(exc)
    finally:
        state.done.set()


class _DownloadState:
    __slots__ = ("done", "error", "current_file", "total", "completed")

    def __init__(self, total: int) -> None:
        self.done = threading.Event()
        self.error: Optional[str] = None
        self.current_file = ""
        self.total = total
        self.completed = 0


def _worker_download_and_apply(
    scripts_dir: Path,
    repo_root: Path,
    dl_state: _DownloadState,
) -> None:
    """Download all scripts/*.py files + VERSION from main branch and replace in place."""
    import shutil
    import tempfile

    try:
        # Get file list for scripts/
        entries = json.loads(_gh_get(f"{_GH_CONTENTS_API}/scripts", timeout=15))
        py_files = [e for e in entries if e.get("type") == "file" and e["name"].endswith(".py")]
        dl_state.total = len(py_files) + 1  # +1 for VERSION

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
    clones (handled by run_startup_git_update_check) and PyInstaller bundles.
    """
    if getattr(sys, "frozen", False):
        return

    # Git clones are handled by run_startup_git_update_check; avoid a double-dialog.
    if find_repo_root(script_path.parent) is not None:
        return

    scripts_dir = script_path.parent          # …/atak-imagery/scripts/
    repo_root = scripts_dir.parent            # …/atak-imagery/

    local_version = read_version_file(repo_root)
    if not local_version or local_version == "unknown":
        return

    check_state = _ReleaseCheckState()
    threading.Thread(
        target=_worker_check_github_release,
        args=(local_version, check_state),
        daemon=True,
    ).start()

    import tkinter as tk
    from tkinter import messagebox, ttk

    from tk_window_scaling import ensure_window_stacking

    root = tk.Tk()
    root.withdraw()

    def _lift() -> None:
        root.deiconify()
        root.update_idletasks()
        ensure_window_stacking(root)
        root.update_idletasks()

    # ---- download + progress phase ----------------------------------------
    def start_download() -> None:
        dl_state = _DownloadState(total=1)
        threading.Thread(
            target=_worker_download_and_apply,
            args=(scripts_dir, repo_root, dl_state),
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
                    root.destroy()
                    return
                _lift()
                messagebox.showinfo(
                    app_title,
                    "Update complete. The application will now restart.",
                    parent=root,
                )
                root.destroy()
                os.execv(sys.executable, [sys.executable, *sys.argv])
            else:
                root.after(150, poll_dl)

        root.after(150, poll_dl)

    # ---- version-check phase -----------------------------------------------
    def finish() -> None:
        if check_state.error or not check_state.update_available:
            root.destroy()
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
        body += "Update now? The scripts will be replaced and the app will restart automatically."

        _lift()
        if not messagebox.askyesno(app_title, body, parent=root):
            root.destroy()
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
