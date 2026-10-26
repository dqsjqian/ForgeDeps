"""Locate an explicitly managed workspace; never infer it from consumer cwd."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile


def registry_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "forgedeps/workspace.json"


def _real_directory(path: Path, *, create=False) -> Path:
    path = Path(path).expanduser().absolute()
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError(f"ForgeDeps workspace is not a real directory: {path}")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError(f"ForgeDeps workspace does not exist; run forgedeps init: {path}")
    return path.resolve()


def _source_root() -> Path | None:
    package = Path(__file__).resolve().parent
    candidate = package.parent
    project = candidate / "pyproject.toml"
    # An installed wheel has no project file at its package parent. Require
    # this package's own layout and identity; an arbitrary cwd is never read.
    if package.name != "forgedeps" or not project.is_file():
        return None
    text = project.read_text(encoding="utf-8")
    section = re.search(r"(?ms)^\[project\]\s*(.*?)(?=^\[|\Z)", text)
    if not section or not re.search(r'''(?m)^name\s*=\s*["']forgedeps["']\s*$''', section.group(1)):
        return None
    if not (candidate / "forgedeps/__init__.py").is_file():
        return None
    return candidate


def resolve_home(home: Path | None = None) -> Path:
    if home is not None:
        return _real_directory(home)
    if "FORGEDEPS_HOME" in os.environ:
        if not os.environ["FORGEDEPS_HOME"]:
            raise ValueError("FORGEDEPS_HOME cannot be empty")
        return _real_directory(Path(os.environ["FORGEDEPS_HOME"]))
    registry = registry_path()
    if registry.is_symlink():
        raise ValueError(f"Refusing symbolic-link workspace registry: {registry}")
    if registry.exists():
        try:
            record = json.loads(registry.read_text(encoding="utf-8"))
            if record.get("schema") != 1 or not isinstance(record.get("workspace"), str):
                raise ValueError("unsupported registry")
            path = Path(record["workspace"])
            if not path.is_absolute():
                raise ValueError("workspace must be absolute")
            return _real_directory(path)
        except (OSError, ValueError, AttributeError) as error:
            raise ValueError(f"Invalid ForgeDeps workspace registry; run forgedeps init: {registry}") from error
    source = _source_root()
    if source is not None:
        return _real_directory(source)
    raise ValueError("No ForgeDeps workspace is registered. Run forgedeps init <workspace> "
                     "or set FORGEDEPS_HOME; consumer cwd and site-packages are never used.")


def prepare_home(home: Path | None = None) -> Path:
    root = resolve_home(home)
    for directory in ("tmp", "build", "state", "state/locks", "state/transactions"):
        _real_directory(root / directory, create=True)
    return root


def register_workspace(path: Path, *, registry: Path | None = None) -> Path:
    root = _real_directory(path, create=True)
    target = registry_path() if registry is None else Path(registry)
    if target.is_symlink():
        raise ValueError(f"Refusing symbolic-link workspace registry: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         prefix=".workspace-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"schema": 1, "workspace": str(root)}, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return prepare_home(root)
