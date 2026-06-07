"""Run subprocesses without flashing console windows on Windows."""
from __future__ import annotations

import subprocess
import sys
from typing import Any, Sequence

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _hidden_kwargs() -> dict[str, Any]:
    if sys.platform != "win32":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {
        "creationflags": CREATE_NO_WINDOW,
        "startupinfo": si,
    }


def run_hidden(
    args: Sequence[str] | str,
    *,
    capture_output: bool = False,
    text: bool = False,
    timeout: float | None = None,
    check: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    for key, value in _hidden_kwargs().items():
        kwargs.setdefault(key, value)
    return subprocess.run(
        args,
        capture_output=capture_output,
        text=text,
        timeout=timeout,
        check=check,
        **kwargs,
    )
