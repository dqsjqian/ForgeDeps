"""Command-line front end for project-independent dependency operations."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stdout
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

from . import deps_build as pipeline
from . import providers


@contextmanager
def diagnostic_output():
    """Route Python and inherited child-process build output to stderr."""
    sys.stdout.flush()
    saved = os.dup(1)
    try:
        os.dup2(2, 1)
        with redirect_stdout(sys.stderr):
            yield
    finally:
        sys.stderr.flush()
        os.dup2(saved, 1)
        os.close(saved)


def load_config(path: Path | None) -> pipeline.ProjectConfig:
    if path is None:
        return pipeline.ProjectConfig(
            name="aria-deps",
            providers={"sqlite": providers.resolve_sqlite, "quickjs": providers.resolve_quickjs},
        )
    path = path.resolve(strict=True)
    name = "_aria_deps_config_" + hashlib.sha256(str(path).encode()).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot load project configuration: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
        config = module.make_config() if hasattr(module, "make_config") else getattr(module, "CONFIG", None)
    finally:
        sys.path.pop(0)
    if not isinstance(config, pipeline.ProjectConfig):
        raise ValueError("Project configuration must export CONFIG or make_config() returning ProjectConfig")
    return config


def versions(values: list[str]) -> dict[str, str]:
    result = {}
    for value in values:
        name, separator, version = value.partition("=")
        if not separator or not name or not version or name in result:
            raise ValueError("--version requires unique NAME=VERSION assignments")
        result[name] = version
    return result


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--version", action="version", version="aria-deps 0.3.0")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("cmake-dir", help="print the installed CMake module directory")
    resolve = commands.add_parser("resolve", help="resolve or reuse locked dependency selections")
    resolve.add_argument("--file", type=Path, default=Path("dependencies.json"))
    resolve.add_argument("--output", type=Path, help="write an effective lock without changing the input")
    resolve.add_argument("--cache", "--cache-dir", type=Path)
    resolve.add_argument("--only", action="append", default=[])
    resolve.add_argument("--version", action="append", default=[], metavar="NAME=VERSION")
    resolve.add_argument("--offline", action="store_true")
    resolve.add_argument("--update", action="store_true")
    resolve.add_argument("--project-config", type=Path)
    read = commands.add_parser("read", help="validate a lock without network access or mutation")
    read.add_argument("--file", type=Path, default=Path("dependencies.json"))
    read.add_argument("--only", action="append", default=[])
    read.add_argument("--output", type=Path, help="write the validated JSON view")

    for kind in ("archive", "file"):
        fetch = commands.add_parser("fetch-" + kind, help="fetch and verify a pinned " + kind)
        fetch.add_argument("--name", required=True)
        fetch.add_argument("--url", required=True)
        fetch.add_argument("--sha256", required=True)
        fetch.add_argument("--cache", type=Path, required=True)
        fetch.add_argument("--offline", action="store_true")
        if kind == "archive":
            fetch.add_argument("--version", required=True)
        else:
            fetch.add_argument("--as", dest="relative_path")
    git = commands.add_parser("fetch-git", help="transactionally bootstrap a locked Git dependency")
    git.add_argument("--file", type=Path, default=Path("dependencies.json"))
    git.add_argument("--name", required=True)
    git.add_argument("--destination", type=Path, required=True)
    git.add_argument("--version")
    git.add_argument("--source", help="explicit local repository or mirror")
    git.add_argument("--cache", type=Path)
    git.add_argument("--offline", action="store_true")
    git.add_argument("--update", action="store_true")

    for command in ("install", "verify"):
        build = commands.add_parser(command, help=command + " a configured dependency prefix")
        build.add_argument("--project-config", type=Path, required=True)
        build.add_argument("--file", type=Path, default=Path("dependencies.json"))
        build.add_argument("--work", type=Path, default=Path("build/deps"))
        build.add_argument("--prefix", type=Path)
        build.add_argument("--sources", type=Path)
        build.add_argument("--profile", default="tests")
        build.add_argument("--tls-backend", default="auto")
        build.add_argument("--only", default="")
        build.add_argument("--c")
        build.add_argument("--cxx")
        build.add_argument("--toolchain", type=Path)
        build.add_argument("--build-config", default="Release")
        build.add_argument("--cmake-option", action="append", default=[])
        if command == "install":
            build.add_argument("--offline", action="store_true")
            build.add_argument("--update", action="store_true")
            build.add_argument("--version", action="append", default=[], metavar="NAME=VERSION")
            build.add_argument("--jobs", type=int)
    return root


def selection(values: list[str]) -> list[str] | None:
    return [part for value in values for part in value.split(",") if part] or None


def dispatch(args):
    if args.command == "resolve":
        config = load_config(args.project_config)
        return pipeline.resolve(
            args.file, args.output, versions=versions(args.version), only=selection(args.only),
            update=args.update, config=config,
            context=pipeline.Context(args.cache or args.file.parent / "build/deps/resolver",
                                     offline=args.offline, user_agent="aria-deps"),
        )
    if args.command == "read":
        result = pipeline.read_resolved(args.file, selection(args.only))
        if args.output:
            if args.output.resolve() == args.file.resolve():
                raise ValueError("read --output must not overwrite the input lock")
            pipeline.atomic_json(args.output, result)
        return result
    if args.command.startswith("fetch-"):
        from . import source_cache
        if args.command == "fetch-archive":
            return source_cache.fetch_archive(args.name, args.version, args.url, args.sha256,
                                               args.cache, offline=args.offline)
        if args.command == "fetch-file":
            return source_cache.fetch_file(args.name, args.url, args.sha256, args.cache,
                                            relative_path=args.relative_path, offline=args.offline)
        return source_cache.bootstrap_git(
            args.file, args.name, args.destination, version=args.version, update=args.update,
            offline=args.offline, source=args.source, cache=args.cache,
        )
    config = load_config(args.project_config)
    prefix = args.prefix or args.work / "prefix"
    sources = args.sources or args.work / "sources"
    kwargs = dict(config=config, profile=args.profile, tls_backend=args.tls_backend,
                  only=args.only, c=args.c, cxx=args.cxx,
                  toolchain=str(args.toolchain.resolve()) if args.toolchain else None,
                  build_config=args.build_config, cmake_options=tuple(args.cmake_option))
    if args.command == "install":
        if args.jobs is not None and args.jobs < 1:
            raise ValueError("--jobs must be positive")
        pipeline.install(args.file, args.work, prefix, sources, **kwargs,
                         offline=args.offline, update=args.update,
                         versions=versions(args.version), jobs=args.jobs)
    else:
        pipeline.verify(args.file, args.work, prefix, sources, **kwargs)
    return {"prefix": str(prefix.resolve()), "verified": True}


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if args.command == "cmake-dir":
        print(Path(__file__).resolve().parent / "cmake")
        return 0
    try:
        # Keep stdout machine-readable even when builds and downloaders log.
        with diagnostic_output():
            result = dispatch(args)
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"aria-deps: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
