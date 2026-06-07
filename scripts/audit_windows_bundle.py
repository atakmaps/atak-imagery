#!/usr/bin/env python3
"""
Pre-build audit for Windows EXEs — run on Linux (CI) or Windows VM before PyInstaller.

  python3 scripts/audit_windows_bundle.py
  python3 scripts/audit_windows_bundle.py --smoke-imports   # Windows build venv only
"""

from __future__ import annotations

import argparse
import py_compile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIN = ROOT / "windows_build"
SCRIPTS = ROOT / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from windows_bundle_manifest import (  # noqa: E402
    HELPER_MODULES,
    HIDDEN_IMPORTS,
    PIP_PACKAGES,
    WIN_RUNTIME_MODULES,
    validate_bundle_tree,
)


def _compile_all_py() -> list[str]:
    errors: list[str] = []
    for path in sorted(WIN.glob("*.py")):
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            errors.append(f"Syntax error in {path.name}: {exc.msg}")
    return errors


def _check_pip_packages() -> list[str]:
    errors: list[str] = []
    for pkg in PIP_PACKAGES:
        if pkg == "pyinstaller":
            mod = "PyInstaller"
        elif pkg == "charset-normalizer":
            mod = "charset_normalizer"
        else:
            mod = pkg.replace("-", "_")
        try:
            __import__(mod)
        except ImportError:
            errors.append(f"pip package not installed: {pkg!r} (pip install -r requirements-windows-build.txt)")
    return errors


def _smoke_import_modules() -> list[str]:
    """Import Python modules that must be bundled (skip mgrs native .pyd on Linux)."""
    errors: list[str] = []
    if str(WIN) not in sys.path:
        sys.path.insert(0, str(WIN))

    # Third-party first — clearer error messages.
    for mod in ("requests", "certifi", "urllib3", "packaging"):
        try:
            __import__(mod)
        except ImportError as exc:
            errors.append(f"Cannot import {mod!r}: {exc}")

    try:
        import mgrs  # noqa: F401
    except ImportError:
        pass  # optional on Linux maintainer machine
    except OSError as exc:
        errors.append(f"mgrs installed but native library failed: {exc}")

    for mod in WIN_RUNTIME_MODULES + HELPER_MODULES:
        try:
            __import__(mod)
        except Exception as exc:
            errors.append(f"Cannot import {mod!r}: {exc}")

    # Lazy-import chain exercised by full download → SQLite flow.
    try:
        from atak_osmdroid_sqlite_footprint import report_lines  # noqa: F401
    except Exception as exc:
        errors.append(f"Cannot import atak_osmdroid_sqlite_footprint.report_lines: {exc}")

    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit Windows EXE bundle completeness")
    ap.add_argument(
        "--smoke-imports",
        action="store_true",
        help="Import bundled modules (needs pip deps; mgrs optional on Linux)",
    )
    args = ap.parse_args()

    all_errors: list[str] = []

    all_errors.extend(validate_bundle_tree())
    all_errors.extend(_compile_all_py())

    if args.smoke_imports:
        all_errors.extend(_check_pip_packages())
        all_errors.extend(_smoke_import_modules())

    # Warn if hidden-import list drifted from known modules (sanity only).
    expected = set(WIN_RUNTIME_MODULES + HELPER_MODULES)
    for mod in expected:
        if mod not in HIDDEN_IMPORTS:
            all_errors.append(f"HIDDEN_IMPORTS missing module {mod!r} — update windows_bundle_manifest.py")

    if all_errors:
        print("Windows bundle audit FAILED:", file=sys.stderr)
        for err in all_errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print("Windows bundle audit OK")
    if args.smoke_imports:
        print(f"  Compiled {len(list(WIN.glob('*.py')))} modules in windows_build/")
        print(f"  Smoke-imported {len(expected)} runtime/helper modules")
    return 0


if __name__ == "__main__":
    sys.exit(main())
