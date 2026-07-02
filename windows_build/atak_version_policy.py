"""ATAK 5.6+ support policy — block legacy 5.5.x installs and plugin assets."""

from __future__ import annotations

import re

MIN_ATAK_MAJOR = 5
MIN_ATAK_MINOR = 6

_UNSUPPORTED_ATAK_MSG = (
    "ATAK 5.5.x is no longer supported. Use ATAK 5.6+ and matching plugin assets only."
)


def atak_version_numbers(atak_version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", (atak_version or "").strip().lstrip("v")))


def atak_version_is_supported(atak_version: str) -> bool:
    nums = atak_version_numbers(atak_version)
    if len(nums) < 2:
        return False
    major, minor = nums[0], nums[1]
    return (major, minor) >= (MIN_ATAK_MAJOR, MIN_ATAK_MINOR)


def require_supported_atak_version(atak_version: str) -> None:
    ver = (atak_version or "").strip()
    if not ver:
        raise RuntimeError("ATAK version is required (manifest atak_version or ATAK_CIV_VERSION).")
    if not atak_version_is_supported(ver):
        raise RuntimeError(f"{_UNSUPPORTED_ATAK_MSG} (got {ver!r}).")


def is_blocked_legacy_55_apk_filename(name: str) -> bool:
    """True for ATAK 5.5.x plugin/build filenames (uv55, -5.5.0-civ-, etc.)."""
    lower = (name or "").lower()
    if not lower.endswith(".apk"):
        return False
    if re.search(r"5\.5\.[0-9]", lower):
        return True
    if re.search(r"[-_.]551[-_.]", lower):
        return True
    if re.search(r"[-_.]55[-_.]", lower) and re.search(r"(^|[-_.])(uv|mc|mesh)", lower):
        return True
    return False
