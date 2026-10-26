"""Release packaging for the build pipeline.

Creates a versioned archive (zip on Windows, tar.gz elsewhere) containing
built binaries, libraries, headers and license files.
"""
from __future__ import annotations

import platform
import re
import subprocess
import tarfile
import zipfile
from pathlib import Path


def detect_version(root: Path) -> str:
    """Best-effort version detection: CHANGELOG.md, then git tag."""
    changelog = root / "CHANGELOG.md"
    if changelog.is_file():
        for line in changelog.read_text(encoding="utf-8").splitlines()[:20]:
            m = re.match(r"##\s*\[?v?(\d+\.\d+\.\d+)", line)
            if m:
                return m.group(1)
    try:
        out = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            capture_output=True, text=True, cwd=root, timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip().lstrip("v")
    except (subprocess.TimeoutExpired, OSError):
        pass
    return "unknown"


def create_package(
    name: str,
    root: Path,
    build_dir: Path,
    *,
    include_dirs: tuple[str, ...] = ("bin", "lib"),
    extra_files: tuple[str, ...] = ("LICENSE", "THIRD_PARTY_NOTICES.md"),
    include_headers: tuple[Path, ...] = (),
    fmt: str = "auto",
    dist_dir: Path | None = None,
) -> Path:
    """Create a release archive. Returns the archive path.

    Args:
        name: Project name used in the archive filename.
        root: Project root directory.
        build_dir: CMake build directory (bin/lib taken from here).
        include_dirs: Subdirectories of build_dir to include.
        extra_files: Files relative to root to include (licenses...).
        include_headers: Extra directories whose files are included
            (e.g. include/, modules/*/include).
        fmt: "auto", "zip" or "tgz". Auto picks zip on Windows.
        dist_dir: Output directory (default: <root>/dist).
    """
    version = detect_version(root)
    plat = platform.system().lower()
    arch = platform.machine().lower()
    if fmt == "auto":
        fmt = "zip" if plat == "windows" else "tgz"
    ext = "zip" if fmt == "zip" else "tar.gz"

    dist = dist_dir or (root / "dist")
    dist.mkdir(exist_ok=True)
    archive = dist / f"{name}-{version}-{plat}-{arch}.{ext}"
    print(f"[package] Creating {archive}...")

    files: list[Path] = []
    for sub in include_dirs:
        d = build_dir / sub
        if d.is_dir():
            files += [p for p in d.rglob("*") if p.is_file()]
    for hd in include_headers:
        if hd.is_dir():
            files += [p for p in hd.rglob("*") if p.is_file()]
    for rel in extra_files:
        p = root / rel
        if p.is_file():
            files.append(p)

    def arcname(f: Path) -> str:
        for base in (build_dir, root):
            try:
                return str(f.relative_to(base))
            except ValueError:
                pass
        return f.name

    if fmt == "zip":
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                zf.write(f, arcname(f))
    else:
        with tarfile.open(archive, "w:gz") as tf:
            for f in files:
                tf.add(f, arcname=arcname(f))
    print(f"[package] Created: {archive} ({len(files)} files)")
    return archive
