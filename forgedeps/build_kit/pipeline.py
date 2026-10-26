"""Build pipeline orchestration.

The Pipeline class implements the standard stages:
    deps -> build -> test -> bench -> package

Projects configure a Pipeline with their specifics (dependency commands,
CMake flags, Qt requirements, post-build hooks) and call .run(). The
mechanics (argument parsing, stage ordering, cmake invocation, ctest,
packaging) are shared; the configuration is per-project.
"""
from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import env
from .package import create_package


def _run(cmd, **kwargs):
    print(f"[build] {' '.join(str(c) for c in cmd)}", flush=True)
    subprocess.run(cmd, check=True, **kwargs)


class Pipeline:
    """Configurable build pipeline.

    Args:
        name: Project name (used in package filenames).
        root: Project root (default: parent of the calling build.py's dir).
        deps: List of (label, command) tuples for the deps stage.
        cmake_flags: Dict of CMake -D flags, or a callable(args) -> dict.
        qt_required: If True, Qt6 is located and CMAKE_PREFIX_PATH is set.
        post_build: List of callables(build_dir, context) run after compile.
        bench_pattern: Regex matching benchmark executable names.
        package_include: Extra header dirs to include in the package.
        extra_args: Callable(parser) adding project-specific CLI options.
        build_dir_fn: Callable(args) -> Path for custom build directory logic.
        validate_fn: Callable(args) for custom argument validation.
        custom_commands: Dict of {name: callable(args, pipeline) -> int} for
            project-specific commands (e.g. cache-key, deps-check). Each
            callable receives parsed args and the Pipeline instance, returns
            exit code.
    """

    def __init__(
        self,
        name: str,
        root: Path | None = None,
        deps: list[tuple[str, list[str]]] | None = None,
        cmake_flags: dict | None = None,
        qt_required: bool = False,
        post_build: list | None = None,
        bench_pattern: str = "bench",
        package_include: tuple[Path, ...] = (),
        extra_args: object = None,
        build_dir_fn: object = None,
        validate_fn: object = None,
        custom_commands: dict | None = None,
    ):
        self.name = name
        self.root = root or Path(sys.argv[0]).resolve().parents[1]
        self.deps = deps or []
        self.cmake_flags = cmake_flags or {}
        self.qt_required = qt_required
        self.post_build = post_build or []
        self.bench_pattern = re.compile(bench_pattern, re.IGNORECASE)
        self.package_include = package_include
        self.extra_args = extra_args
        self.build_dir_fn = build_dir_fn
        self.validate_fn = validate_fn
        self.custom_commands = custom_commands or {}
        self._qt_prefix: Path | None = None
        self._msys2_bin: Path | None = None

    def _default_build_dir(self, args) -> Path:
        if args.build_dir:
            return args.build_dir
        if self.build_dir_fn:
            return self.build_dir_fn(args)
        return self.root / "build" / args.config.lower()

    # ── CLI ──────────────────────────────────────────────────────────────
    def _make_parser(self) -> argparse.ArgumentParser:
        p = argparse.ArgumentParser(
            description=f"Build pipeline for {self.name}: "
                        "deps -> build -> test -> bench -> package"
        )
        choices = ("deps", "build", "test", "bench", "package", "all", "clean")
        choices += tuple(self.custom_commands.keys())
        p.add_argument(
            "command", nargs="?",
            choices=choices,
            default="all",
            help="Pipeline stage (default: all = deps+build+test)",
        )
        p.add_argument("--jobs", type=int, default=env.cpu_count())
        p.add_argument("--build-dir", type=Path, help="Override build directory")
        p.add_argument("--config", default="Release",
                       choices=("Release", "Debug", "RelWithDebInfo", "MinSizeRel"))
        p.add_argument("--no-deps", action="store_true", help="Skip deps stage")
        p.add_argument("--skip-test", action="store_true", help="Skip tests in 'all'")
        p.add_argument("--no-test", action="store_true",
                       help="Skip tests and set BUILD_TESTS=OFF")
        p.add_argument("--generator", help="CMake generator (default: Ninja if available)")
        p.add_argument("--package-format", default="auto", choices=("auto", "zip", "tgz"))
        p.add_argument("--cmake-arg", action="append", default=[],
                       help="Extra -DNAME=VALUE for CMake (repeatable)")
        if self.extra_args:
            self.extra_args(p)
        return p

    # ── Stages ───────────────────────────────────────────────────────────
    def stage_deps(self, args) -> int:
        print("[build] === Stage: deps ===")
        deps = self.deps(args) if callable(self.deps) else self.deps
        if not deps:
            print("[build] No dependencies configured, skipping")
            return 0
        for label, cmd in deps:
            # Allow dynamic commands: callable(args) -> list[str]
            if callable(cmd):
                cmd = cmd(args)
            if not cmd:
                continue
            print(f"[build] deps: {label}")
            _run(cmd, cwd=self.root)
        return 0

    def _resolve_cmake_flags(self, args) -> dict:
        flags = dict(self.cmake_flags(args) if callable(self.cmake_flags) else self.cmake_flags)
        if self.qt_required and self._qt_prefix:
            flags["CMAKE_PREFIX_PATH"] = str(self._qt_prefix)
        for extra in args.cmake_arg:
            m = re.fullmatch(r"-D([A-Za-z_][A-Za-z_0-9]*)(?::[A-Za-z_]+)?=(.*)",
                             extra, re.DOTALL)
            if not m:
                print(f"[build] Error: --cmake-arg must be -DNAME=VALUE: {extra}",
                      file=sys.stderr)
                sys.exit(1)
            flags[m.group(1)] = m.group(2)
        return flags

    def stage_build(self, args) -> Path:
        print("[build] === Stage: build ===")
        build_dir = self._default_build_dir(args)

        for tool in ("cmake", "git"):
            if not env.has_tool(tool):
                print(f"[build] Error: {tool} not on PATH", file=sys.stderr)
                sys.exit(1)

        if self.qt_required:
            if self._qt_prefix is None:
                print("[build] Error: Qt6 required but not found. "
                      "Install Qt6 or set QT_DIR.", file=sys.stderr)
                sys.exit(1)
            print(f"[build] Qt6: {self._qt_prefix}")

        if platform.system() == "Windows" and self._msys2_bin:
            os.environ["PATH"] = str(self._msys2_bin) + os.pathsep + os.environ["PATH"]

        cmd = ["cmake", "-S", str(self.root), "-B", str(build_dir)]
        if args.generator:
            cmd += ["-G", args.generator]
        elif env.has_tool("ninja"):
            cmd += ["-G", "Ninja"]
        cmd.append(f"-DCMAKE_BUILD_TYPE={args.config}")
        for k, v in self._resolve_cmake_flags(args).items():
            cmd.append(f"-D{k}={v}")

        print(f"[build] Configuring {args.config}")
        _run(cmd)
        print(f"[build] Building with {args.jobs} jobs")
        _run(["cmake", "--build", str(build_dir), "--parallel", str(args.jobs)])

        ctx = {"qt_prefix": self._qt_prefix, "msys2_bin": self._msys2_bin,
               "args": args, "root": self.root}
        for hook in self.post_build:
            hook(build_dir, ctx)

        print(f"[build] Complete: {build_dir}")
        return build_dir

    def stage_test(self, args, build_dir: Path) -> int:
        print("[build] === Stage: test ===")
        r = subprocess.run(
            ["ctest", "--test-dir", str(build_dir), "--output-on-failure",
             "--parallel", str(args.jobs)]
        )
        print("[build] All tests passed" if r.returncode == 0
              else "[build] Tests failed")
        return r.returncode

    def stage_bench(self, args, build_dir: Path) -> int:
        print("[build] === Stage: bench ===")
        bin_dir = build_dir / "bin"
        benches = [p for p in bin_dir.iterdir()
                   if p.is_file() and os.access(p, os.X_OK)
                   and self.bench_pattern.search(p.name)] if bin_dir.is_dir() else []
        if not benches:
            print("[build] No benchmark executables found, skipping")
            return 0
        rc = 0
        for b in sorted(benches):
            print(f"[build] Running {b.name}...")
            rc = subprocess.run([str(b)]).returncode or rc
        return rc

    def stage_package(self, args, build_dir: Path) -> int:
        print("[build] === Stage: package ===")
        create_package(
            self.name, self.root, build_dir,
            include_headers=self.package_include,
            fmt=args.package_format,
        )
        return 0

    # ── Run ──────────────────────────────────────────────────────────────
    def run(self, argv=None) -> int:
        args = self._make_parser().parse_args(argv)

        if self.validate_fn:
            self.validate_fn(args)

        if args.command == "clean":
            build_root = self.root / "build"
            print(f"[build] Removing {build_root}")
            shutil.rmtree(build_root, ignore_errors=True)
            return 0

        # Resolve environment up front
        if self.qt_required:
            qt_dir = getattr(args, "qt_dir", None)
            self._qt_prefix = Path(qt_dir) if qt_dir else None
            if self._qt_prefix is None and os.environ.get("QT_DIR"):
                self._qt_prefix = Path(os.environ["QT_DIR"])
            if self._qt_prefix is None:
                self._qt_prefix = env.find_qt_prefix()
        self._msys2_bin = env.find_msys2_bin()

        build_dir: Path = self._default_build_dir(args)

        def need_build() -> bool:
            return not (build_dir / "CMakeCache.txt").is_file()

        def ensure_built() -> Path:
            nonlocal build_dir
            if need_build():
                print("[build] No build tree found, building first...")
                build_dir = self.stage_build(args)
            return build_dir

        cmd = args.command
        if cmd == "all":
            stages = ["deps", "build", "test"]
            if args.no_deps:
                stages.remove("deps")
            if args.skip_test or args.no_test:
                stages.remove("test")
        else:
            stages = [cmd]

        rc = 0
        for stage in stages:
            if stage in self.custom_commands:
                rc = self.custom_commands[stage](args, self)
            elif stage == "deps":
                rc = self.stage_deps(args)
            elif stage == "build":
                build_dir = self.stage_build(args)
            elif stage == "test":
                build_dir = ensure_built()
                rc = self.stage_test(args, build_dir)
            elif stage == "bench":
                build_dir = ensure_built()
                rc = self.stage_bench(args, build_dir)
            elif stage == "package":
                build_dir = ensure_built()
                rc = self.stage_package(args, build_dir)
            if rc != 0:
                print(f"[build] Stage '{stage}' failed, aborting", file=sys.stderr)
                return rc

        print("[build] Pipeline complete")
        return 0
