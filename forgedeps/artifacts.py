"""Public, library-independent protocol for external build recipes."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
import shlex
import subprocess
import sys
import threading
from typing import Callable, Mapping, Sequence


def artifact_present(prefix: Path, artifact: str) -> bool:
    """Accept the equivalent static-library spelling on Windows toolchains."""
    candidate = Path(prefix) / artifact
    if candidate.is_file():
        return True
    if not artifact.endswith(".a") or not candidate.parent.is_dir():
        return False
    stem = candidate.stem.removeprefix("lib")
    names = {name + suffix for name in (stem, "lib" + stem) for suffix in (".a", ".lib")}
    return any(entry.is_file() and entry.name.lower() in names
               for entry in candidate.parent.iterdir())


@dataclass(frozen=True)
class Artifact:
    name: str
    target_label: str
    prefix: Path
    fingerprint: str
    generation: str
    requires: tuple["Artifact", ...] = ()

    @property
    def manifest(self) -> Path:
        return Path(self.prefix) / "manifest.json"

    def to_dict(self) -> dict:
        return {"name": self.name, "target_label": self.target_label,
                "prefix": str(self.prefix), "fingerprint": self.fingerprint,
                "generation": self.generation,
                "requires": [item.to_dict() for item in self.requires]}

    @classmethod
    def from_dict(cls, value: dict) -> "Artifact":
        if not isinstance(value, dict):
            raise ValueError("Artifact reference must be an object")
        try:
            return cls(name=value["name"], target_label=value["target_label"],
                       prefix=Path(value["prefix"]), fingerprint=value["fingerprint"],
                       generation=value["generation"],
                       requires=tuple(cls.from_dict(item) for item in value.get("requires", [])))
        except (KeyError, TypeError) as error:
            raise ValueError("Invalid artifact reference") from error


@dataclass(frozen=True)
class BuildContext:
    source_snapshot: Path
    build_dir: Path
    prefix: Path
    dependency_prefixes: tuple[Path, ...]
    options: Mapping
    environment: Mapping[str, str]
    target: Mapping
    requires: Mapping[str, Artifact]
    jobs: int
    configuration: str
    log_path: Path | None = None

    def run(self, argv: Sequence[str | Path], *, cwd: Path | None = None,
            env: Mapping[str, str] | None = None, **kwargs):
        """Run an argument vector; tee native diagnostics to stderr and build.log.

        capture_output keeps its usual subprocess semantics and also records
        captured output. Explicit stdout/stderr redirection is unsupported:
        recipes should use capture_output when they need to inspect output.
        """
        command = [str(item) for item in argv]
        if not command:
            raise ValueError("Recipe command cannot be empty")
        environment = dict(self.environment)
        if env:
            environment.update(env)
        if "stdout" in kwargs or "stderr" in kwargs:
            raise ValueError("BuildContext.run uses capture_output instead of stdout/stderr overrides")
        line = "+ " + (subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)) + "\n"
        log = self.log_path.open("ab") if self.log_path is not None else None
        def output(data, *, display=True):
            if data is None:
                return
            raw = data.encode("utf-8", errors="replace") if isinstance(data, str) else data
            if log is not None:
                log.write(raw)
                log.flush()
            if display:
                sys.stderr.write(raw.decode("utf-8", errors="replace"))
                sys.stderr.flush()
        try:
            output(line)
            check = kwargs.pop("check", True)
            capture = kwargs.pop("capture_output", False)
            if capture or "input" in kwargs:
                result = subprocess.run(command, cwd=str(cwd or self.build_dir), env=environment,
                                        capture_output=True, check=False, **kwargs)
                output(result.stdout, display=not capture)
                output(result.stderr, display=not capture)
                if check:
                    result.check_returncode()
                return result
            timeout = kwargs.pop("timeout", None)
            kwargs.pop("text", None)
            kwargs.pop("encoding", None)
            kwargs.pop("errors", None)
            process = subprocess.Popen(command, cwd=str(cwd or self.build_dir), env=environment,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
            def pump():
                assert process.stdout is not None
                for data in iter(process.stdout.readline, b""):
                    output(data)
                process.stdout.close()
            reader = threading.Thread(target=pump, daemon=True)
            reader.start()
            try:
                code = process.wait(timeout=timeout)
            except BaseException:
                process.kill()
                process.wait()
                raise
            finally:
                reader.join()
            result = subprocess.CompletedProcess(command, code)
            if check:
                result.check_returncode()
            return result
        finally:
            if log is not None:
                log.close()


@dataclass(frozen=True)
class BuildRequest:
    name: str
    source: Path
    builder: Callable[[BuildContext], None]
    recipe_input_files: Mapping[str, Path] | Sequence[Path]
    target: Mapping
    options: Mapping = field(default_factory=dict)
    environment: Mapping[str, str] = field(default_factory=dict)
    requires: Sequence[Artifact] = ()
    artifacts: Sequence[str] = ()
    license_files: Sequence[str] = ()
    header_only: bool = False
    validate: Callable[[BuildContext], None] | None = None
    jobs: int = field(default_factory=lambda: min(os.cpu_count() or 1, 8))
    configuration: str = "Release"
    # Optional identity captured while selecting a managed source. This guards
    # replacement between source selection and the store's own snapshot.
    expected_source_content: str | None = None


@dataclass(frozen=True)
class Environment:
    artifacts: tuple[Artifact, ...]

    @property
    def prefixes(self) -> tuple[Path, ...]:
        result = []
        def visit(item):
            if Path(item.prefix) not in result:
                result.append(Path(item.prefix))
                for dependency in item.requires:
                    visit(dependency)
        for item in self.artifacts:
            visit(item)
        return tuple(result)

    @property
    def refs(self) -> tuple[dict, ...]:
        return tuple(item.to_dict() for item in self.artifacts)

    def to_dict(self) -> dict:
        return {"prefixes": [str(path) for path in self.prefixes], "refs": list(self.refs)}

    def lease(self):
        from .leases import ArtifactLease
        return ArtifactLease(self.artifacts)
