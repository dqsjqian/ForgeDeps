"""Environment detection for the build pipeline.

Provides portable helpers to locate build tools and SDKs across
Windows, macOS and Linux. All functions return None (or a sensible
default) instead of raising when something is not found; the caller
decides whether a missing tool is fatal.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path


def cpu_count() -> int:
    """Parallel job count: JOBS env, else OS CPU count, else 4."""
    try:
        return int(os.environ.get("JOBS", os.cpu_count() or 4))
    except ValueError:
        return 4


def has_tool(name: str) -> bool:
    """Check if a tool is available on PATH."""
    return shutil.which(name) is not None


def find_qt_prefix() -> Path | None:
    """Locate a Qt6 install prefix.

    Search order:
        1. QT_DIR environment variable
        2. macOS: `brew --prefix qt`
        3. Windows: MSYS2 UCRT64 layout (MSYS2_ROOT or common paths)
        4. Linux: common install locations

    Returns the prefix Path, or None if not found. A prefix is accepted
    only when it contains lib/cmake/Qt6/Qt6Config.cmake (or the
    Windows/MSYS2 variant cmake/Qt6/Qt6Config.cmake).
    """
    env_dir = os.environ.get("QT_DIR")
    if env_dir:
        p = Path(env_dir)
        if (p / "lib" / "cmake" / "Qt6" / "Qt6Config.cmake").is_file():
            return p
        if (p / "cmake" / "Qt6" / "Qt6Config.cmake").is_file():
            return p
        return None

    system = platform.system()
    if system == "Darwin":
        brew = shutil.which("brew")
        if brew:
            try:
                out = subprocess.run(
                    [brew, "--prefix", "qt"],
                    capture_output=True, text=True, timeout=15,
                )
                if out.returncode == 0:
                    p = Path(out.stdout.strip())
                    if (p / "lib" / "cmake" / "Qt6" / "Qt6Config.cmake").is_file():
                        return p
            except (subprocess.TimeoutExpired, OSError):
                pass
    elif system == "Windows":
        msys2_root = os.environ.get("MSYS2_ROOT")
        candidates: list[Path] = []
        if msys2_root:
            candidates.append(Path(msys2_root) / "ucrt64")
        candidates += [
            Path("C:/msys64/ucrt64"),
            Path("D:/msys64/ucrt64"),
            Path.home() / "msys64" / "ucrt64",
        ]
        for c in candidates:
            if (c / "lib" / "cmake" / "Qt6" / "Qt6Config.cmake").is_file():
                return c
    else:
        for c in (
            Path("/usr/lib/qt6"),
            Path("/usr/local/qt6"),
            Path.home() / "opt" / "qt6",
        ):
            if (c / "lib" / "cmake" / "Qt6" / "Qt6Config.cmake").is_file():
                return c
    return None


def find_msys2_bin() -> Path | None:
    """Locate the MSYS2 UCRT64 bin directory (Windows only).

    Returns None on non-Windows platforms or when MSYS2 is not found.
    """
    if platform.system() != "Windows":
        return None
    msys2_root = os.environ.get("MSYS2_ROOT")
    candidates: list[Path] = []
    if msys2_root:
        candidates.append(Path(msys2_root) / "ucrt64" / "bin")
    candidates += [
        Path("C:/msys64/ucrt64/bin"),
        Path("D:/msys64/ucrt64/bin"),
        Path.home() / "msys64" / "ucrt64" / "bin",
    ]
    for c in candidates:
        if (c / "g++.exe").is_file():
            return c
    objdump = shutil.which("objdump.exe") or shutil.which("objdump")
    if objdump:
        return Path(objdump).parent
    return None


def is_gcc(cc: str | None = None) -> bool:
    """Heuristic: does the C compiler look like GCC?"""
    cc = cc or os.environ.get("CC", "")
    if cc:
        return "gcc" in cc.lower()
    default_cc = shutil.which("cc") or shutil.which("gcc")
    return bool(default_cc and "gcc" in Path(default_cc).name.lower())
