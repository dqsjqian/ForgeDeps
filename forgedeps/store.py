"""Verified single-current artifact slots for external dependency recipes.

Slots have stable paths. Fingerprints are receipts, never directory names.
Consumers using these paths hold ArtifactLease for their entire build session.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
import functools
import hashlib
import inspect
import json
import ntpath
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import shlex
import subprocess
import stat
import tempfile
import time
import uuid

from .artifacts import Artifact, BuildContext, BuildRequest, Environment
from .home import prepare_home, resolve_home
from .leases import artifact_graph, slot_locks
from .process_environment import merge_environments, select_environment
from . import deps_sources as sources
from . import fsops

SCHEMA = 1
# These inherited variables can change compilation or dependency discovery.
# Nonstandard recipe inputs must be supplied explicitly through environment.
BUILD_ENVIRONMENT_KEYS = (
    "CC", "CXX", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS", "CL", "_CL_", "LINK", "_LINK_",
    # RC and WINDRES are deliberately absent: CMake finds the resource compiler
    # itself and the probe records the resolved tool, while an inherited name
    # made the identity depend on the launching shell (MSYS2 exports it, a
    # Python-launched configure does not), producing two identities per job.
    "AR", "ARFLAGS", "AS", "LD", "NM", "RANLIB", "MAKEFLAGS",
    "CMAKE_TOOLCHAIN_FILE", "CMAKE_PREFIX_PATH", "CMAKE_GENERATOR", "CMAKE_GENERATOR_PLATFORM",
    "CMAKE_GENERATOR_TOOLSET", "PKG_CONFIG", "PKG_CONFIG_PATH", "PKG_CONFIG_LIBDIR",
    "PKG_CONFIG_SYSROOT_DIR", "SDKROOT", "MACOSX_DEPLOYMENT_TARGET",
    "IPHONEOS_DEPLOYMENT_TARGET", "TVOS_DEPLOYMENT_TARGET", "WATCHOS_DEPLOYMENT_TARGET",
    "ANDROID_NDK_HOME", "ANDROID_NDK_ROOT", "NDK_ROOT", "INCLUDE", "LIB", "LIBPATH",
    "WindowsSDKVersion", "WindowsSdkDir", "UCRTVersion", "UniversalCRTSdkDir",
    "VCToolsVersion", "VCToolsInstallDir", "VCINSTALLDIR", "VSCMD_ARG_TGT_ARCH",
    "VSCMD_ARG_HOST_ARCH", "SOURCE_DATE_EPOCH", "CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH",
    "LIBRARY_PATH", "COMPILER_PATH", "GCC_EXEC_PREFIX", "CCC_OVERRIDE_OPTIONS",
    "OBJC_INCLUDE_PATH", "CMAKE_INCLUDE_PATH", "CMAKE_LIBRARY_PATH", "CMAKE_PROGRAM_PATH",
    "DEVELOPER_DIR", "ANDROID_HOME", "ANDROID_PLATFORM", "ANDROID_ABI", "WindowsSDKLibVersion",
)


def _json(value):
    def convert(item):
        if isinstance(item, Path):
            return str(item)
        if is_dataclass(item):
            return asdict(item)
        raise TypeError(f"Not a declared JSON build input: {type(item).__name__}")
    try:
        return json.loads(json.dumps(value, default=convert, sort_keys=True, allow_nan=False))
    except (ValueError, TypeError) as error:
        raise ValueError("Build inputs must contain JSON values or explicit file paths") from error


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _name(value):
    reserved = getattr(ntpath, "isreserved", lambda name: PureWindowsPath(name).is_reserved())
    if (not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", value)
            or value.endswith(".") or reserved(value)):
        raise ValueError(f"Artifact names/target labels must be safe lowercase ASCII: {value!r}")
    return value


def _relative(value):
    if not isinstance(value, str):
        raise ValueError("Artifact paths must be strings")
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError(f"Unsafe artifact path: {value}")
    reserved = getattr(ntpath, "isreserved", lambda name: PureWindowsPath(name).is_reserved())
    for part in path.parts:
        if (part in (".", "..") or part.endswith((".", " ")) or ":" in part
                or any(ord(char) < 32 for char in part) or reserved(part)):
            raise ValueError(f"Unsafe artifact path: {value}")
    return path.as_posix()


def _directory(path):
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError(f"Expected a real artifact directory: {path}")
    path.mkdir(parents=True, exist_ok=True)


def _inventory(prefix, *, check_links=True):
    if prefix.is_symlink() or not prefix.is_dir():
        raise ValueError(f"Artifact prefix is not a real directory: {prefix}")
    result = {}
    pending = [prefix]
    while pending:
        for path in pending.pop().iterdir():
            relative = path.relative_to(prefix).as_posix()
            if relative == "manifest.json":
                continue
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                resolved = path.resolve()
                if check_links and (prefix not in resolved.parents or not resolved.exists()):
                    raise ValueError(f"Artifact contains an external/dangling symlink: {path}")
                result[relative] = {"link": os.readlink(path)}
            elif stat.S_ISREG(mode):
                result[relative] = {"sha256": _sha(path), "executable": bool(mode & 0o111)}
            elif stat.S_ISDIR(mode):
                result[relative] = {"directory": True}
                pending.append(path)
            else:
                raise ValueError(f"Artifact contains a special file: {path}")
    return result


def _callback_identity(callback, discovered, active=None):
    if callback is None:
        return None
    if isinstance(callback, functools.partial):
        return {"partial": _callback_identity(callback.func, discovered, active),
                "args": _json(callback.args), "kwargs": _json(callback.keywords or {})}
    if not inspect.isfunction(callback):
        raise ValueError("External recipe callbacks must be Python functions or functools.partial")
    active = set() if active is None else active
    if id(callback) in active:
        return {"recursive": callback.__qualname__}
    active.add(id(callback))
    try:
        source_file = inspect.getsourcefile(callback)
        if not source_file or not Path(source_file).is_file():
            raise ValueError("External recipe callbacks must have inspectable source files")
        source_file = Path(source_file)
        discovered.add(source_file.resolve())
        closure = {}
        for name, cell in zip(callback.__code__.co_freevars, callback.__closure__ or ()):
            value = cell.cell_contents
            closure[name] = (_callback_identity(value, discovered, active)
                             if inspect.isfunction(value) or isinstance(value, functools.partial)
                             else _json(value))
        helpers = {}
        for name in callback.__code__.co_names:
            helper = callback.__globals__.get(name)
            if inspect.isfunction(helper) and helper.__module__ == callback.__module__:
                helpers[name] = _callback_identity(helper, discovered, active)
        return {"function": callback.__qualname__, "source": inspect.getsource(callback),
                "defaults": _json(callback.__defaults__), "kwdefaults": _json(callback.__kwdefaults__),
                "closure": closure, "helpers": helpers}
    except (OSError, TypeError) as error:
        raise ValueError("Cannot fingerprint external recipe callback") from error
    finally:
        active.remove(id(callback))


def _recipe_identity(request):
    declared = request.recipe_input_files
    if hasattr(declared, "items"):
        files = dict(declared)
    else:
        files = {}
        for path in declared:
            path = Path(path)
            if path.name in files:
                raise ValueError("Recipe inputs with duplicate basenames need explicit logical names")
            files[path.name] = path
    records = {}
    for name, path in files.items():
        _relative(name)
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Recipe input must be a real file: {path}")
        records[name] = _sha(path)
    discovered = set()
    callbacks = {"builder": _callback_identity(request.builder, discovered),
                 "validate": _callback_identity(request.validate, discovered)}
    # Content identities omit import-module aliases and installation locations.
    code_files = sorted({_sha(path) for path in discovered})
    return {"inputs": records, "callbacks": callbacks, "code_files": code_files}


def _manifest(prefix):
    path = prefix / "manifest.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Artifact has no verified manifest; preserved: {prefix}")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("schema") != SCHEMA:
            raise ValueError("unsupported artifact manifest")
        artifact = Artifact.from_dict(record["artifact"])
        _name(artifact.name)
        _name(artifact.target_label)
        if (artifact.prefix != prefix or prefix != prefix.resolve()
                or prefix.name != artifact.target_label or prefix.parent.name != artifact.name
                or prefix.parent.parent.name != "build"):
            raise ValueError("artifact location changed")
        if (not re.fullmatch(r"[0-9a-f]{64}", artifact.fingerprint)
                or not re.fullmatch(r"[0-9a-f]{32}", artifact.generation)
                or _fingerprint(record["inputs"]) != artifact.fingerprint):
            raise ValueError("artifact identity is invalid")
        return record, artifact
    except (ValueError, TypeError, KeyError, OSError) as error:
        raise ValueError(f"Invalid artifact manifest; preserved: {path}: {error}") from error


def _verify_slot(artifact: Artifact | Path) -> Artifact:
    expected = artifact if isinstance(artifact, Artifact) else None
    prefix = Path(expected.prefix if expected else artifact).absolute()
    record, actual = _manifest(prefix)
    if expected is not None and actual != expected:
        raise ValueError(f"Artifact selection/generation changed; rebuild the environment: {prefix}")
    if record.get("files") != _inventory(prefix):
        raise ValueError(f"Artifact was modified or is incomplete; preserved: {prefix}")
    return actual


def verify_artifact(artifact: Artifact | Path) -> Artifact:
    """Read-only validation. Hold ArtifactLease when validation precedes use."""
    actual = _verify_slot(artifact)
    prefix = actual.prefix
    for path, dependency in artifact_graph((actual,)).items():
        if path == prefix:
            continue
        saved, current = _manifest(path)
        if current != dependency or saved.get("files") != _inventory(path):
            raise ValueError(f"Artifact dependency changed; rebuild the environment: {path}")
    return actual


def _write_manifest(prefix, record):
    path = prefix / "manifest.json"
    temporary = prefix / (".manifest-" + uuid.uuid4().hex)
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _journal(transaction, value):
    temporary = transaction / ".journal"
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(transaction / "journal.json")


def _recover(root, prefix):
    """Recover a killed writer only while owning this slot's OS lock."""
    for transaction in (root / "state/transactions").iterdir():
        if transaction.is_symlink() or not transaction.is_dir():
            continue
        path = transaction / "journal.json"
        if path.is_symlink() or not path.is_file():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("prefix") != str(prefix):
            continue
        if record.get("state") == "committed":
            _verify_slot(prefix)
            fsops.remove_tree(transaction)
            continue
        if record.get("state") != "installing":
            continue
        previous = record.get("previous")
        backup = transaction / "previous"
        if previous is not None:
            expected = Artifact.from_dict(previous)
            if expected.prefix != prefix:
                raise ValueError(f"Invalid interrupted transaction: {transaction}")
            if backup.exists():
                saved = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
                # Saved absolute internal symlinks still name the canonical
                # slot while its tree is temporarily under this transaction.
                # Compare their exact receipt text without dereferencing them.
                if (saved.get("artifact") != previous or saved.get("files") != _inventory(backup, check_links=False)
                        or _fingerprint(saved.get("inputs")) != expected.fingerprint):
                    raise ValueError(f"Interrupted transaction backup is modified; preserved: {transaction}")
                if prefix.exists():
                    prefix.rename(transaction / "failed")
                backup.rename(prefix)
            else:
                _verify_slot(expected)  # Killed before moving the old slot.
        elif prefix.exists():
            prefix.rename(transaction / "failed")
        record["state"] = "rolled_back"
        _journal(transaction, record)


def _prepare_request(request, home, *, create):
    if not isinstance(request, BuildRequest):
        raise ValueError("build_package requires a BuildRequest")
    name = _name(request.name)
    target = _json(request.target)
    if not isinstance(target, dict):
        raise ValueError("Build target must be an object")
    label = "headers" if request.header_only else _name(target.get("label"))
    if not isinstance(request.jobs, int) or request.jobs < 1:
        raise ValueError("Build jobs must be a positive integer")
    if not isinstance(request.configuration, str) or not request.configuration:
        raise ValueError("Build configuration must be a non-empty string")
    if target.get("configuration", request.configuration) != request.configuration:
        raise ValueError("Build configuration differs from the probed target")
    options = _json(request.options)
    if not isinstance(options, dict):
        raise ValueError("Build options must be an object")
    environment = _json(request.environment)
    if not isinstance(environment, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                for k, v in environment.items()):
        raise ValueError("Build environment must map string names to string values")
    # Pure headers have no ambient compiler inputs. Explicit recipe environment
    # still participates, but CMake's first-configure CC/CXX exports do not.
    inherited = {} if request.header_only else select_environment(BUILD_ENVIRONMENT_KEYS, os.environ)
    # Windows names are case-insensitive; a mixed-case recipe spelling must
    # replace the uppercase copy inherited from the process, not join it.
    environment = merge_environments(inherited, environment)
    # A probed compiler is authoritative. CMake exports CC/CXX during its first
    # configure but not cached reconfiguration; normalize those selectors to the
    # actual compiler/arguments so equivalent consumption sessions share identity.
    for variable, key in (("CC", "c"), ("CXX", "cxx")):
        compiler = target.get(key)
        if isinstance(compiler, dict) and compiler.get("path"):
            command = [compiler["path"], *compiler.get("arguments", [])]
            quote = subprocess.list2cmdline if os.name == "nt" else shlex.join
            environment[variable] = quote(command)
    artifacts = tuple(_relative(path) for path in request.artifacts)
    licenses = tuple(_relative(path) for path in request.license_files)
    if len({Path(path).name for path in licenses}) != len(licenses):
        raise ValueError("License filenames collide in the installed license directory")
    root = prepare_home(home) if create else resolve_home(home)
    source = Path(request.source).expanduser().absolute()
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f"Source must be a real directory: {source}")
    source = source.resolve()
    if source == root or source in root.parents:
        raise ValueError("Source directory cannot contain the ForgeDeps workspace")
    parent = root / "build" / name
    if create:
        _directory(parent)
    prefix = parent / label
    if prefix.is_symlink() or prefix != prefix.resolve():
        raise ValueError(f"Refusing symbolic-link/noncanonical artifact slot: {prefix}")
    graph = artifact_graph(request.requires)
    if prefix in graph:
        raise ValueError("An artifact cannot depend on its own slot")
    requires = tuple(request.requires)
    if len({item.name for item in requires}) != len(requires):
        raise ValueError("Direct dependency names must be unique")
    return root, source, prefix, graph, requires, target, options, environment, artifacts, licenses


def _request_inputs(request, source, requires, target, options, environment, artifacts, licenses):
    actual_source = sources.identity(source)
    if (request.expected_source_content is not None
            and actual_source['content'] != request.expected_source_content):
        raise ValueError('Managed source changed after selection; retry the environment build')
    recipe = _recipe_identity(request)
    inputs = {"protocol": SCHEMA, "name": request.name, "target": target,
              "header_only": request.header_only, "configuration": request.configuration,
              "recipe": recipe, "source_content": actual_source["content"],
              "options": options, "environment": environment,
              "requires": {item.name: {"fingerprint": item.fingerprint, "generation": item.generation}
                           for item in requires},
              "artifacts": artifacts, "license_files": licenses}
    return inputs, actual_source, recipe


def verify_request(request: BuildRequest, artifact: Artifact, *, home: Path | None = None) -> Artifact:
    """Read-only check against current source, recipe, target and options.

    Consumers hold an ArtifactLease while checking and using this selection.
    No directories are created and a mismatch never starts a build.
    """
    (_, source, prefix, graph, requires, target, options, environment,
     artifacts, licenses) = _prepare_request(request, home, create=False)
    if artifact.prefix != prefix:
        raise ValueError("Artifact reference does not match the requested slot")
    actual = verify_artifact(artifact)
    for dependency in graph.values():
        verify_artifact(dependency)
    inputs, _, _ = _request_inputs(request, source, requires, target, options, environment, artifacts, licenses)
    if _fingerprint(inputs) != actual.fingerprint:
        raise ValueError("Artifact inputs changed; rebuild the environment: "
                         f"{prefix}{_input_differences(prefix, inputs)}")
    return actual


def _input_differences(prefix: Path, current: dict) -> str:
    """Name the inputs that differ from the manifest, so a mismatch is actionable.

    A container, a generator or a toolchain probe can change one setting that
    is invisible in a fingerprint; the caller otherwise has no way to see which.
    """
    manifest = prefix / "manifest.json"
    try:
        saved = json.loads(manifest.read_text(encoding="utf-8")).get("inputs", {})
    except (OSError, ValueError):
        return ""
    # The manifest holds JSON, so tuples read back as lists and an empty
    # sequence changes type across the round trip. Compare the serialised form
    # so a real change is never buried under a type difference.
    saved = json.loads(json.dumps(saved, sort_keys=True, default=list))
    current = json.loads(json.dumps(current, sort_keys=True, default=list))
    lines = []
    for key in sorted(set(saved) | set(current)):
        before, after = saved.get(key), current.get(key)
        if before == after:
            continue
        if isinstance(before, dict) and isinstance(after, dict):
            inner = []
            for name in sorted(set(before) | set(after)):
                if before.get(name) != after.get(name):
                    inner.append(f"{name}: {before.get(name)!r} -> {after.get(name)!r}")
            lines.extend(f"  {key}.{item}" for item in inner) or lines.append(f"  {key}: changed")
        elif isinstance(before, list) and isinstance(after, list):
            added = [item for item in after if item not in before]
            removed = [item for item in before if item not in after]
            detail = []
            if removed:
                detail.append(f"removed {removed!r}")
            if added:
                detail.append(f"added {added!r}")
            lines.append(f"  {key}: {'; '.join(detail) or 'order or content changed'}")
        else:
            lines.append(f"  {key}: {before!r} -> {after!r}")
    return ("\n" + "\n".join(lines[:12])) if lines else ""


def build_package(request: BuildRequest, *, home: Path | None = None, force: bool = False) -> Artifact:
    (root, source, prefix, graph, requires, target, options, environment,
     artifacts, licenses) = _prepare_request(request, home, create=True)
    name, label = request.name, prefix.name
    with slot_locks([prefix, *graph], write_prefixes=(prefix,)):
        _recover(root, prefix)
        for dependency in graph.values():
            verify_artifact(dependency)
        inputs, actual_source, recipe = _request_inputs(
            request, source, requires, target, options, environment, artifacts, licenses)
        identity = _fingerprint(inputs)
        previous = None
        if prefix.exists():
            previous = _verify_slot(prefix)
            if previous.fingerprint == identity and not force:
                return previous
        transaction = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=root / "state/transactions"))
        backup = transaction / "previous"
        replaced = False
        started = False
        try:
            snapshot = transaction / "source"
            sources.snapshot(source, snapshot, actual_source)
            build = transaction / "build"
            build.mkdir()
            journal = {"prefix": str(prefix), "state": "installing",
                       "previous": previous.to_dict() if previous is not None else None}
            _journal(transaction, journal)
            if previous is not None:
                prefix.rename(backup)
                replaced = True
            prefix.mkdir()
            started = True
            process_env = merge_environments(os.environ, environment)
            dependencies = transaction / "requires-environment.json"
            dependencies.write_text(json.dumps({"schema": SCHEMA, **Environment(requires).to_dict()}),
                                    encoding="utf-8")
            process_env["FORGEDEPS_ENVIRONMENT"] = str(dependencies)
            process_env["FORGEDEPS_ENVIRONMENTS"] = json.dumps([str(dependencies)])
            context = BuildContext(snapshot, build, prefix,
                                   Environment(requires).prefixes, options, process_env, target,
                                   {item.name: item for item in requires}, request.jobs, request.configuration,
                                   prefix / "build.log")
            request.builder(context)
            for relative in licenses:
                original = snapshot / relative
                if not original.is_file():
                    raise ValueError(f"Required license missing from source: {relative}")
                destination = prefix / "share/licenses" / name / original.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, destination)
            for relative in artifacts:
                if not (prefix / relative).is_file():
                    raise ValueError(f"Expected installed artifact is missing: {relative}")
            if request.validate is not None:
                request.validate(context)
            if (prefix / "manifest.json").exists() or (prefix / "manifest.json").is_symlink():
                raise ValueError("Recipe installed reserved manifest.json")
            if sources.identity(source) != actual_source or _recipe_identity(request) != recipe:
                raise ValueError("Source or recipe changed during build; retry with the new inputs")
            # Header installers often preserve upstream mtimes. A stable slot
            # needs changed files to be newer so incremental consumers recompile.
            now = time.time_ns()
            for path in prefix.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    os.utime(path, ns=(now, now))
            current = Artifact(name, label, prefix, identity, uuid.uuid4().hex, requires)
            record = {"schema": SCHEMA, "artifact": current.to_dict(), "inputs": inputs,
                      "source_revision": actual_source.get("revision", ""),
                      "files": _inventory(prefix)}
            _write_manifest(prefix, record)
            verify_artifact(current)
            journal["state"] = "committed"
            _journal(transaction, journal)
        except BaseException:
            if started and prefix.exists():
                prefix.rename(transaction / "failed")
            if replaced:
                backup.rename(prefix)
            if started:
                journal["state"] = "rolled_back"
                _journal(transaction, journal)
            # Failed output is diagnostic data under HOME/state/transactions, never a usable
            # second slot. It is retained without changing the prior receipt.
            if not started:
                fsops.remove_tree(transaction)
            raise
        else:
            fsops.remove_tree(transaction)
            return current


def ensure_environment(requests, *, home: Path | None = None, force: bool = False) -> Environment:
    """Build requests in caller-provided dependency order; never infer upgrades."""
    artifacts = tuple(build_package(request, home=home, force=force) for request in requests)
    result = Environment(artifacts)
    # Validate the whole selection together, detecting a slot replacement
    # between sequential requests. Consumers acquire a new lease before use.
    with result.lease():
        pass
    return result
