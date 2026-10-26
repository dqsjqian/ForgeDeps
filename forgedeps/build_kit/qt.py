"""Qt6 support and Windows DLL deployment for the build pipeline.

Provides:
    deploy_qt_dlls: post-build hook deploying Qt + runtime DLLs next to the
                    executable on Windows (windeployqt + objdump closure copy).
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from pathlib import Path


def deploy_qt_dlls(exe_name: str = "app.exe"):
    """Create a post-build hook deploying DLLs next to the executable.

    Usage:
        Pipeline(..., qt_required=True, post_build=[deploy_qt_dlls("application.exe")])

    Steps (Windows only, no-op elsewhere):
        1. windeployqt -> Qt DLLs + plugins
        2. objdump-based recursive copy of MSYS2/runtime DLLs
        System DLLs (kernel32, ntdll, ...) are intentionally skipped.
    """
    def hook(build_dir: Path, ctx: dict):
        if platform.system() != "Windows":
            return
        bin_dir = build_dir / "bin"
        exe = bin_dir / exe_name
        if not exe.is_file():
            print(f"[deploy] exe not found: {exe}")
            return
        msys2_bin: Path | None = ctx.get("msys2_bin")
        if msys2_bin is None or not (msys2_bin / "objdump.exe").is_file():
            print("[deploy] objdump not found, skipping DLL deployment")
            return

        windeployqt = msys2_bin / "windeployqt.exe"
        if windeployqt.is_file():
            print("[deploy] windeployqt...")
            subprocess.run(
                [str(windeployqt), "--no-translations",
                 "--no-system-d3d-compiler", "--no-opengl-sw", str(exe)],
                capture_output=True,
            )
        else:
            print("[deploy] windeployqt not found, skipping Qt plugins")

        system_dll = re.compile(
            r"^(kernel32|user32|gdi32|advapi32|shell32|ole32|oleaut32|comdlg32|"
            r"ws2_32|crypt32|dwmapi|winmm|version|shcore|ucrtbase|vcruntime|"
            r"winhttp|wldap32|netapi32|secur32|authz|mpr|rpcrt4|userenv|usp10|"
            r"uxtheme|d3d11|d3d12|dxgi|ntdll|dwrite)",
            re.IGNORECASE,
        )

        def get_imports(path: Path) -> list[str]:
            out = subprocess.run(
                [str(msys2_bin / "objdump.exe"), "-p", str(path)],
                capture_output=True, text=True,
            )
            return [m.group(1).strip()
                    for line in out.stdout.splitlines()
                    if (m := re.search(r"DLL Name:\s*(.+)", line))]

        windir = os.environ.get("WINDIR", r"C:\Windows")
        extra = [(Path(windir) / "System32" / "downlevel",
                  re.compile(r"^api-ms-win-", re.IGNORECASE))]

        print("[deploy] copying runtime dependencies...")
        queue, processed, copied = [exe], set(), 0
        while queue:
            f = queue.pop(0)
            if f in processed:
                continue
            processed.add(f)
            for dll in get_imports(f):
                if system_dll.match(dll):
                    continue
                dest = bin_dir / dll
                if dest.is_file():
                    queue.append(dest)
                    continue
                candidates = [msys2_bin, bin_dir]
                candidates += [p for p, rx in extra if rx.match(dll)]
                for src_dir in candidates:
                    src = src_dir / dll
                    if src.is_file():
                        shutil.copy2(src, dest)
                        print(f"  + {dll}")
                        copied += 1
                        queue.append(dest)
                        break
        print(f"[deploy] done. {copied} DLL(s) copied.")
    return hook
