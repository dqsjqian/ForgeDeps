"""Keep shared artifact generations leased throughout consumer build sessions."""
from __future__ import annotations

from contextlib import contextmanager
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

from ..artifacts import Environment
from ..store import verify_artifact
from ..environments import read_environment
from ..home import resolve_home
from ..leases import artifact_graph


def source_checkout_path(root: Path, *, name, home=None) -> Path:
    """Preserve an existing consumer checkout; new checkouts belong to HOME."""
    if not name or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in name):
        raise ValueError("Invalid source checkout name")
    legacy = Path(root).resolve() / "build/deps" / name
    if legacy.exists() or legacy.is_symlink():
        return legacy
    return resolve_home(home) / "sources" / name


def _parent_receipts(environment):
    if "FORGEDEPS_ENVIRONMENTS" in environment:
        try:
            paths = json.loads(environment["FORGEDEPS_ENVIRONMENTS"])
        except (TypeError, ValueError) as error:
            raise ValueError("FORGEDEPS_ENVIRONMENTS must be a JSON array of receipt paths") from error
        if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
            raise ValueError("FORGEDEPS_ENVIRONMENTS must be a JSON array of receipt paths")
        return tuple(Path(path) for path in paths)
    single = environment.get("FORGEDEPS_ENVIRONMENT")
    return (Path(single),) if single else None


def merge_environments(paths) -> Environment:
    roots = []
    for path in paths:
        roots.extend((path if isinstance(path, Environment) else read_environment(Path(path))).artifacts)
    # The graph rejects different fingerprints/generations for the same slot,
    # including conflicts which occur only in transitive dependencies.
    graph = artifact_graph(roots)
    return Environment(tuple(graph.values()))


def assert_parent_covers_environment(selected: Environment, environment=None):
    """A child may only consume generations held by its parent's build lease."""
    paths = _parent_receipts(os.environ if environment is None else environment)
    if paths is None:
        return
    covered = artifact_graph(merge_environments(paths).artifacts)
    for prefix, artifact in artifact_graph(selected.artifacts).items():
        if covered.get(prefix) != artifact:
            raise ValueError(f"Dependency is outside the parent build lease: {prefix}; "
                             "prepare all configurations before starting the shared build session")


def _configure_directory(command, cwd):
    if (Path(str(command[0])).name.lower() not in ("cmake", "cmake.exe")
            or "-S" not in command or "-B" not in command):
        return None
    path = Path(command[command.index("-B") + 1])
    return (cwd / path).resolve() if not path.is_absolute() else path.resolve()


def _find_receipts(directories):
    found = set()
    for directory in directories:
        for path in directory.rglob("forgedeps-environment.json"):
            # A separate cached project below this build tree is not consumed
            # unless it is explicitly one of this session's configurations.
            parents = path.parent.relative_to(directory).parts
            ancestor = directory
            unrelated = False
            for part in parents:
                ancestor = ancestor / part
                if (ancestor / "CMakeCache.txt").is_file():
                    unrelated = True
                    break
            if not unrelated:
                found.add(path.resolve())
    return sorted(found, key=str)


def _prefix_environments(directories, environment):
    """Cover verified shared SDKs supplied directly through CMake search paths."""
    prefixes = set()
    for value in environment.get("CMAKE_PREFIX_PATH", "").split(os.pathsep):
        if value:
            prefixes.add(Path(value).expanduser().absolute())
    for directory in directories:
        cache = directory / "CMakeCache.txt"
        if not cache.is_file():
            continue
        for line in cache.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.startswith(("#", "//")):
                continue
            key, value = line.split("=", 1)
            name = key.split(":", 1)[0]
            if name != "CMAKE_PREFIX_PATH" and not name.endswith("_DIR"):
                continue
            for item in value.split(";"):
                path = Path(item)
                if not item or not path.is_absolute():
                    continue
                if name == "CMAKE_PREFIX_PATH":
                    prefixes.add(path)
                else:
                    # Foo_DIR commonly points below a shared prefix's lib/cmake.
                    for candidate in (path, *path.parents):
                        if candidate.parent.parent.name == "build" and (candidate / "manifest.json").is_file():
                            prefixes.add(candidate)
                            break
    roots = [verify_artifact(path) for path in sorted(prefixes, key=str)
             if path.parent.parent.name == "build" and (path / "manifest.json").exists()]
    return [Environment(tuple(roots))] if roots else []


@contextmanager
def leased_environment(receipts, *, env=None):
    """Yield a child environment backed by an immutable merged lease receipt."""
    environment = dict(os.environ if env is None else env)
    selected = merge_environments(receipts)
    assert_parent_covers_environment(selected, environment)
    temporary = None
    with selected.lease():
        if selected.artifacts:
            # All transitive references were validated by the lease. Keep the
            # immutable handoff inside the managed workspace, never cwd / /tmp.
            home = selected.artifacts[0].prefix.parent.parent.parent
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=home / "tmp",
                                             prefix="lease-", suffix=".json", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"schema": 1, **selected.to_dict()}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            paths = [str(temporary)]
            environment["FORGEDEPS_ENVIRONMENT"] = str(temporary)
        else:
            paths = []
            environment.pop("FORGEDEPS_ENVIRONMENT", None)
        environment["FORGEDEPS_ENVIRONMENTS"] = json.dumps(paths)
        try:
            yield environment
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def run_session(commands, *, cwd=None, env=None, run_command=None,
                command_environment=None, receipts=(), post_build=None):
    """Prepare every configuration, lease their union, then build/test/package.

    Commands preceding the first configure (usually source fetches) run once.
    Every configure runs once to prepare its receipt and again under the read
    lease. All remaining commands run under that lease in their original order.
    run_command receives (command, environment); post_build receives environment.
    """
    directory = Path(cwd or Path.cwd()).resolve()
    environment = dict(os.environ if env is None else env)
    commands = [list(map(str, command)) for command in commands]
    if any(not command for command in commands):
        raise ValueError("Build session commands cannot be empty")
    def execute(command, child_env):
        effective = command_environment(command, child_env) if command_environment else child_env
        if run_command is not None:
            return run_command(command, effective)
        print("+ " + shlex.join(command), flush=True)
        return subprocess.run(command, cwd=directory, env=effective, check=True)
    configurations = [(index, _configure_directory(command, directory))
                      for index, command in enumerate(commands)]
    configurations = [(index, path) for index, path in configurations if path is not None]
    inherited = _parent_receipts(environment)
    first = configurations[0][0] if configurations else (
        0 if receipts or inherited is not None or environment.get("CMAKE_PREFIX_PATH") else len(commands))
    for command in commands[:first]:
        execute(command, environment)
    if inherited is None:
        for index, _ in configurations:
            execute(commands[index], environment)
    directories = [path for _, path in configurations]
    paths = list(receipts) + _find_receipts(directories) + _prefix_environments(directories, environment)
    # In a nested session the parent already owns the selected union. Include
    # it so nested configure-only commands cannot accidentally drop coverage.
    if inherited is not None:
        paths.extend(inherited)
    with leased_environment(paths, env=environment) as child_env:
        for index, _ in configurations:
            execute(commands[index], child_env)
        current = list(receipts) + _find_receipts(directories) + _prefix_environments(directories, child_env)
        assert_parent_covers_environment(merge_environments(current), child_env)
        configured = {index for index, _ in configurations}
        for index, command in enumerate(commands[first:], first):
            if index not in configured:
                execute(command, child_env)
        if post_build is not None:
            return post_build(child_env)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", action="append", type=Path, default=[],
                        help="Consume all active nested receipts below this CMake build tree")
    parser.add_argument("--configure", action="store_true",
                        help="Prepare a CMake configure, then repeat it under the shared lease")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")
    try:
        directories = [path.resolve() for path in args.build_dir]
        receipts = _find_receipts(directories) + _prefix_environments(directories, os.environ)
        if args.configure:
            if _configure_directory(command, Path.cwd()) is None:
                raise ValueError("--configure requires a cmake -S ... -B ... command")
            run_session([command])
            return 0
        if not args.build_dir:
            raise ValueError("--build-dir is required when consuming an existing configuration")
        with leased_environment(receipts) as environment:
            return subprocess.run(command, env=environment).returncode
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"build-session: {error}", file=sys.stderr)
        return error.returncode if isinstance(error, subprocess.CalledProcessError) else 2


if __name__ == "__main__":
    raise SystemExit(main())
