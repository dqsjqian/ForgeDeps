"""Verified, immutable dependency sources for source-tree CMake consumers.

This cache is separate from the editable workspaces in ``deps_sources``.
Existing cache entries are verified, never repaired over local changes. Its
content-addressed layout is new; offline use requires populating this cache.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname
import zipfile

from . import deps_build as deps


def _digest(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValueError("Source downloads require a complete SHA256")
    return value.lower()


def _directory(path: Path) -> Path:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError(f"Source cache directory is not a real directory: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache(path: Path) -> Path:
    return _directory(Path(path).expanduser().absolute()).resolve()


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    reserved = getattr(ntpath, "isreserved", lambda part: PureWindowsPath(part).is_reserved())
    if (not value or path.is_absolute() or "\\" in value or not path.parts
            or ".." in path.parts
            or any(":" in part or part.endswith((".", " ")) or reserved(part)
                   for part in path.parts)):
        raise ValueError(f"Unsafe source file path: {value}")
    return path.as_posix()


def _tree_digest(root: Path) -> str:
    """Include empty directories, executable bits and all files; reject links."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Source cache tree is not a real directory: {root}")
    entries = {}
    pending = [root]
    while pending:
        for path in pending.pop().iterdir():
            mode = path.lstat().st_mode
            relative = path.relative_to(root).as_posix()
            if stat.S_ISDIR(mode):
                entries[relative] = {"directory": True}
                pending.append(path)
            elif stat.S_ISREG(mode):
                entries[relative] = {"sha256": deps.sha256(path),
                                     "executable": bool(mode & 0o111)}
            else:
                raise ValueError(f"Source cache contains a link or special file: {path}")
    return hashlib.sha256(json.dumps(entries, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _download(cache: Path, url: str, digest: str, offline: bool, *, archive: bool) -> Path:
    return fetch_download(url, digest, cache / "downloads", offline=offline, archive=archive)


def fetch_download(url: str, sha256: str, directory: Path, *, offline: bool = False,
                   archive: bool = True) -> Path:
    """Fetch a verified file into an explicit download directory, without extracting."""
    digest = _digest(sha256)
    parsed = urlsplit(url)
    local = None
    if parsed.scheme == "file":
        if parsed.netloc not in ("", "localhost") or parsed.query or parsed.fragment:
            raise ValueError("Local source file URLs cannot name a remote host or query")
        local = Path(url2pathname(parsed.path))
        if not local.is_absolute() or str(local).startswith(("//", "\\\\")):
            raise ValueError("Local source file URLs must name an absolute non-UNC path")
    else:
        deps.https_url(url)
    downloads = _directory(Path(directory).absolute())
    # The spelling of a URL's basename must not become a local filename.
    filename = "source.zip" if archive and urlsplit(url).path.lower().endswith(".zip") else "source"
    dependency = SimpleNamespace(url=url, sha256=digest, archive_name=filename)
    with deps.lock_output(downloads / digest):
        if local is not None:
            content = downloads / digest
            if content.is_symlink() or (content.exists() and not content.is_file()):
                raise ValueError(f"Source content cache is not a regular file: {content}")
            if content.exists():
                if deps.sha256(content) != digest:
                    raise ValueError(f"Content cache SHA256 mismatch; preserved: {content}")
            elif not offline:
                if local.is_symlink() or not local.is_file():
                    raise ValueError(f"Local source is not a regular file: {local}")
                with tempfile.TemporaryDirectory(prefix=".local-", dir=downloads) as temporary:
                    candidate = Path(temporary) / "download"
                    shutil.copyfile(local, candidate)
                    if deps.sha256(candidate) != digest:
                        raise ValueError(f"Local source SHA256 verification failed: {local}")
                    candidate.replace(content)
        return deps.download(downloads, dependency, offline, user_agent="forgedeps")


def _verify(entry: Path, digest: str) -> Path:
    if entry.is_symlink() or not entry.is_dir():
        raise ValueError(f"Source cache entry is not a real directory: {entry}")
    receipt = entry / "manifest.json"
    if receipt.is_symlink() or not receipt.is_file():
        raise ValueError(f"Source cache manifest is missing or unsafe: {receipt}")
    try:
        record = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"Invalid source cache manifest: {receipt}") from error
    source = entry / "source"
    if (not isinstance(record, dict) or record.get("schema") != 1
            or record.get("sha256") != digest
            or record.get("tree_sha256") != _tree_digest(source)):
        raise ValueError(f"Source cache was modified; preserved: {source}")
    if {path.name for path in entry.iterdir()} != {"manifest.json", "source"}:
        raise ValueError(f"Source cache entry has unexpected files; preserved: {entry}")
    return source


def _publish(stage: Path, entry: Path, digest: str) -> Path:
    deps.atomic_json(stage / "manifest.json", {
        "schema": 1, "sha256": digest, "tree_sha256": _tree_digest(stage / "source"),
    })
    stage.rename(entry)
    return entry / "source"


def fetch_archive(name: str, version: str, url: str, sha256: str, cache: Path,
                  offline: bool = False) -> dict:
    """Fetch a SHA256-pinned archive and verify its extracted tree on every use.

    Return absolute ``source_dir`` and ``archive`` strings plus ``sha256``.
    Name and version are caller labels; content determines cache identity.
    """
    if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
        raise ValueError("Source archive name and version must be non-empty strings")
    digest, cache = _digest(sha256), _cache(cache)
    entries = _directory(cache / "archives")
    entry = entries / digest
    with deps.lock_output(entry):
        archive = _download(cache, url, digest, offline, archive=True)
        if entry.exists() or entry.is_symlink():
            source = _verify(entry, digest)
        else:
            with tempfile.TemporaryDirectory(prefix=".extract-", dir=entries) as temporary:
                stage = Path(temporary)
                unpack = stage / "unpack"
                unpack.mkdir()
                try:
                    extracted = deps.extract(archive, unpack, "")
                except (tarfile.TarError, zipfile.BadZipFile) as error:
                    raise ValueError(f"Invalid source archive: {archive}") from error
                if list(unpack.iterdir()) != [extracted]:
                    raise ValueError("Source archive must contain exactly one top-level directory")
                extracted.rename(stage / "source")
                unpack.rmdir()
                source = _publish(stage, entry, digest)
        return {"source_dir": str(source), "archive": str(archive), "sha256": digest}


def fetch_file(name: str, url: str, sha256: str, cache: Path,
               relative_path: str | None = None, offline: bool = False) -> dict:
    """Fetch one verified file below an immutable include/base directory."""
    if not isinstance(name, str) or not name:
        raise ValueError("Source file name must be a non-empty string")
    relative = _relative(relative_path if relative_path is not None
                         else unquote(urlsplit(url).path.rsplit("/", 1)[-1]))
    digest, cache = _digest(sha256), _cache(cache)
    entries = _directory(cache / "files")
    key = digest + "-" + hashlib.sha256(relative.encode("utf-8")).hexdigest()
    entry = entries / key
    with deps.lock_output(entry):
        downloaded = _download(cache, url, digest, offline, archive=False)
        if entry.exists() or entry.is_symlink():
            base = _verify(entry, digest)
        else:
            with tempfile.TemporaryDirectory(prefix=".file-", dir=entries) as temporary:
                stage = Path(temporary)
                destination = stage / "source" / relative
                destination.parent.mkdir(parents=True)
                shutil.copyfile(downloaded, destination)
                destination.chmod(0o644)
                base = _publish(stage, entry, digest)
        target = base / relative
        if target.is_symlink() or not target.is_file() or deps.sha256(target) != digest:
            raise ValueError(f"Cached source file was modified; preserved: {target}")
        return {"file": str(target), "base": str(base), "sha256": digest}


def _git(directory: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(directory), *map(str, arguments)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise ValueError(f"Git operation failed in {directory}: {result.stderr.strip()}")
    return result.stdout.strip()


def _checkout_revision(destination: Path, name: str) -> str | None:
    if destination.is_symlink():
        raise ValueError(f"Refusing a symbolic-link checkout: {destination}")
    if not destination.exists():
        return None
    administration = destination / ".git"
    if not destination.is_dir() or not administration.is_dir() or administration.is_symlink():
        raise ValueError(f"Refusing a non-Git directory or linked worktree/submodule: {destination}")
    top = Path(_git(destination, "rev-parse", "--show-toplevel")).resolve()
    if top != destination.resolve():
        raise ValueError(f"Checkout is not a standalone Git worktree: {destination}")
    revision = _git(destination, "rev-parse", "HEAD")
    exclusions = []
    # Adopt checkouts created by the previous bootstrap without rewriting its
    # informational marker. Never hide edits to a tracked or altered marker.
    marker_name = f".pinned-{name}-sha"
    marker = destination / marker_name
    if (marker.is_file() and not marker.is_symlink()
            and not _git(destination, "ls-files", "--", marker_name)
            and marker.read_text(encoding="ascii") == revision + "\n"):
        exclusions.append(f":(exclude){marker_name}")
    dirty = _git(destination, "status", "--porcelain", "--untracked-files=all",
                 "--", ".", *exclusions)
    if dirty:
        raise ValueError(f"Dependency checkout has local edits; preserved: {destination}\n{dirty}")
    return revision


def bootstrap_git(file: Path, name: str, destination: Path, *, version: str | None = None,
                  update: bool = False, offline: bool = False, source: str | Path | None = None,
                  cache: Path | None = None) -> dict:
    """Resolve and transactionally fetch one locked Git source.

    Unlike editable dependency workspaces, dirty or linked worktrees are
    rejected. A version change retains the previous checkout as a sibling
    backup. Offline use needs a matching lock and either the desired checkout
    or an explicitly supplied local Git mirror. Returned paths are strings.
    """
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise ValueError("Git dependency name must be a simple identifier")
    file = Path(file).expanduser().absolute()
    destination = Path(destination).expanduser().absolute()
    if file.is_symlink() or not file.is_file():
        raise ValueError(f"Dependency lock is not a regular file: {file}")
    if destination.is_symlink():
        raise ValueError(f"Refusing a symbolic-link checkout: {destination}")
    destination = destination.resolve()
    file = file.resolve()
    if destination == destination.parent or destination == file.parent or destination in file.parents:
        raise ValueError("Git dependency destination cannot contain its lock file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    resolution_cache = Path(cache) if cache is not None else destination.parent / ".source-resolution"
    resolution_cache = _cache(resolution_cache)
    with deps.lock_output(file), deps.lock_output(destination):
        before = _checkout_revision(destination, name)
        with tempfile.TemporaryDirectory(prefix=".bootstrap-", dir=destination.parent) as temporary:
            transaction = Path(temporary)
            effective = transaction / "dependencies.json"
            selection = deps.resolve(
                file, effective, only=[name], update=update,
                versions={name: version} if version is not None else {},
                context=deps.Context(resolution_cache, offline=offline, user_agent="forgedeps"))
            record = selection["dependencies"][name]
            if record["source"].get("artifact") != "git":
                raise ValueError("Git bootstrap requires a Git dependency declaration")
            revision = record["revision"]
            payload = deps.load_json(effective)
            payload.pop("_base_sha256", None)
            result = {"source_dir": str(destination.resolve()), "revision": revision}
            if before == revision:
                deps.atomic_json(file, payload)
                return result
            remote = str(source) if source is not None else record["url"]
            local = Path(remote).expanduser()
            if local.exists():
                remote = str(local.resolve())
            else:
                deps.https_url(remote)
                if offline:
                    raise ValueError("Offline Git bootstrap requires a local mirror or the locked checkout")
            staged = transaction / "checkout"
            staged.mkdir()
            _git(staged, "init", "--quiet")
            _git(staged, "remote", "add", "origin", remote)
            try:
                _git(staged, "fetch", "--quiet", "--depth", "1", "origin", revision)
            except ValueError:
                _git(staged, "fetch", "--quiet", "--tags", "origin")
            _git(staged, "checkout", "--quiet", "--detach", revision)
            if _git(staged, "rev-parse", "HEAD") != revision:
                raise ValueError("Fetched Git checkout does not match its locked revision")
            if _checkout_revision(destination, name) != before:
                raise ValueError("Dependency checkout changed while fetching; preserved")
            backup = None
            published = False
            try:
                if before is not None:
                    backup = Path(tempfile.mkdtemp(prefix=destination.name + "-backup-", dir=destination.parent))
                    backup.rmdir()
                    destination.rename(backup)
                staged.rename(destination)
                published = True
                deps.atomic_json(file, payload)
            except BaseException:
                if published:
                    destination.rename(staged)
                if backup is not None and backup.exists():
                    backup.rename(destination)
                raise
            if backup is not None:
                result["backup"] = str(backup.resolve())
            return result
