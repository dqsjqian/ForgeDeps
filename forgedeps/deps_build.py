"""Project-agnostic dependency pipeline: resolve, fetch, patch, build, install.

Each project supplies its own recipes (what to build), providers (where to
fetch from) and policy (which subset under which configuration) via
ProjectConfig. This module only provides the mechanism: no project names,
no hardcoded recipe tables, no private build scripts.

Layout contract (all roots are caller-provided)
----------------------------------------------
<source>/<name>/            editable source workspace, one flat directory each
<source>/.<project>-sources/ workspace records plus replacement backups
<work>/cache/               SHA256-verified download archives
<work>/runs/                disposable snapshots; patches are applied here only
<work>/prefix/              installed libraries

Behaviour contract
------------------
* The lock only decides what a MISSING workspace starts from. Afterwards the
  directory's actual contents are the build input; edits, pulls and branch
  switches are the user's responsibility, tracked by content identity.
* Workspaces are never compiled in place or reset. Snapshots under runs/ are
  compiled instead, so patches never touch user files.
* A component is reused only with identical source contents, recipe, patches,
  toolchain and installed files; any change also rebuilds every static
  consumer of that component. Old binaries are never relabelled as new sources.
* Replacements preserve the previous prefix/workspace and roll back on failure.
* verify() is read-only: CMake calls it at configure time; it never downloads.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import inspect
import json
import ntpath
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import deps_sources as sources
from ._console import safe_print as print


_GLOBAL_PROVIDERS: dict = {}


def register_provider(name: str, resolve_fn) -> None:
    """Register a provider globally (alternative to per-project config)."""
    _GLOBAL_PROVIDERS[name] = resolve_fn


@dataclass(frozen=True)
class ProjectConfig:
    """Project-specific configuration for the dependency pipeline.

    Everything that varies per project lives here; the pipeline itself
    never hardcodes a project name, recipe table or provider.
    """

    #: Short project identifier, e.g. "ariaread". Derives state directory
    #: names (".<name>-sources"), manifest paths ("share/<name>-deps/"),
    #: default UA strings and generated script names.
    name: str
    #: Tuple of Dependency recipes this project builds.
    recipes: tuple = ()
    #: Policy function: (recipes, profile, tls_backend) -> selected recipes.
    #: Defaults to returning recipes unchanged.
    configure: object = None
    #: Extra providers: {name: resolve_fn(spec, requested, context)}.
    #: The "github" provider is always available.
    providers: dict = field(default_factory=dict)
    #: Directory holding patch files referenced by recipes (may be None).
    patches_dir: object = None
    #: Patch version whitelist: {dep_name: {version: ...}}.
    patch_versions: dict = field(default_factory=dict)
    #: External build implementations: kind -> fn(BuildContext).
    builders: dict = field(default_factory=dict)
    #: Optional fn(locked_dependency, resolved_record) -> Dependency.
    resolve_recipe: object = None
    #: Prefix for environment variable overrides, e.g. "ARIAREAD".
    #: Defaults to name.upper().
    env_prefix: object = None

    def __post_init__(self):
        if self.env_prefix is None:
            object.__setattr__(self, "env_prefix", self.name.upper())

    @property
    def state_dirname(self) -> str:
        return f".{self.name}-sources"

    @property
    def manifest_path(self):
        return Path(f"share/{self.name}-deps/manifest.json")

    @property
    def user_agent(self) -> str:
        return f"{self.name}-deps"

    def effective_providers(self) -> dict:
        result = {"github": resolve_github}
        result.update(_GLOBAL_PROVIDERS)
        result.update(self.providers)
        return result

    def effective_configure(self):
        if self.configure is not None:
            return self.configure
        return lambda recipes, profile, tls_backend: list(recipes)


_ACTIVE: "ProjectConfig | None" = None


def init_project(config: ProjectConfig) -> None:
    """Set the active project for this process.

    A process builds one project's dependencies; internal functions resolve
    project-derived paths through this instead of threading config through
    every signature. Public API accepts an explicit config= to override.
    """
    global _ACTIVE
    _ACTIVE = config
    sources.set_state_dirname(config.state_dirname)


def _project(config: "ProjectConfig | None" = None) -> ProjectConfig:
    result = config if config is not None else _ACTIVE
    if result is None:
        raise DependencyError("No project configured; call init_project() or pass config=")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Lock file: resolve and verify dependencies.json
# ─────────────────────────────────────────────────────────────────────────────


class DependencyError(ValueError):
    """A dependency request cannot be resolved or verified."""


def load_json(path: Path, *, optional: bool = False) -> dict:
    if optional and not path.exists():
        return {"schema": 2, "dependencies": {}}
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict) or value.get("schema") != 2:
        raise DependencyError(f"Unsupported dependency schema: {path}")
    if not isinstance(value.get("dependencies"), dict):
        raise DependencyError(f"Missing dependency dictionary: {path}")
    return value


def dependency_spec(entry: dict) -> dict:
    if not isinstance(entry, dict):
        raise DependencyError("Dependency entries must be objects")
    return {key: value for key, value in entry.items() if key != "resolved"}


def request_hash(spec: dict) -> str:
    """Portable length-prefixed UTF-8 encoding for request identities."""
    encoded = bytearray(b"aria-dependency-request-v1\n")
    for key, value in sorted(spec.items()):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key) or not isinstance(value, str):
            raise DependencyError("Dependency declarations require simple string fields")
        for part in (key, value):
            data = part.encode("utf-8")
            encoded.extend(str(len(data)).encode("ascii") + b":" + data)
    return hashlib.sha256(encoded).hexdigest()


def _entry_record(entry: dict, *, allow_missing: bool = False) -> dict | None:
    spec = dependency_spec(entry)
    fingerprint = request_hash(spec)
    resolved = entry.get("resolved")
    if resolved is None and allow_missing:
        return None
    if not isinstance(resolved, dict):
        raise DependencyError("Missing or invalid resolved dependency result")
    if resolved.get("request_hash") != fingerprint:
        if allow_missing:
            return None
        raise DependencyError("Dependency declaration changed; resolve it before building")
    record = {**resolved, "source": spec}
    validate_record(record)
    return record


def read_resolved(file: Path, only: list | None = None) -> dict:
    """Read/verify selected results without discovery or changing the file."""
    entries = load_json(file)["dependencies"]
    selected = set(only or entries)
    if selected - entries.keys():
        raise DependencyError("Selections must name a declared dependency")
    return {"schema": 1, "dependencies": {
        name: _entry_record(entries[name]) for name in sorted(selected)
    }}


def atomic_json(path: Path, value: dict) -> None:
    content = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


@contextlib.contextmanager
def lock_output(path: Path):
    # Keep coordination files outside source roots, including when the lock
    # file itself is a version-controlled build input.
    directory = Path(tempfile.gettempdir()) / "aria-dependency-locks"
    directory.mkdir(exist_ok=True)
    key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
    with (directory / key).open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def https_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise DependencyError("Dependency downloads require an HTTPS URL without credentials")
    return url


class Context:
    def __init__(self, cache_dir: Path, offline: bool = False, *, user_agent: str | None = None):
        self.cache_dir = cache_dir
        self.offline = offline
        self.user_agent = user_agent

    def request_user_agent(self) -> str:
        if self.user_agent is not None:
            return self.user_agent
        return _ACTIVE.user_agent if _ACTIVE is not None else "forgedeps"

    def get_bytes(self, url: str) -> bytes:
        if self.offline:
            raise DependencyError("Offline resolution requires a matching lock record")
        request = urllib.request.Request(https_url(url), headers={"User-Agent": self.request_user_agent()})
        with urllib.request.urlopen(request, timeout=120) as response:
            https_url(response.url)
            return response.read()

    def get_text(self, url: str) -> str:
        return self.get_bytes(url).decode("utf-8")

    def github_json(self, path: str):
        if self.offline:
            raise DependencyError("Offline resolution requires a matching lock record")
        if not path.startswith("repos/") or ".." in path.split("/"):
            raise DependencyError("Invalid GitHub API path")
        # gh uses its existing credential store without exposing a token to
        # this process or to release-asset download hosts.
        if shutil.which("gh"):
            result = subprocess.run(["gh", "api", "--hostname", "github.com", path],
                                    capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", check=False)
            if result.returncode == 0:
                return json.loads(result.stdout)
            if "404" in result.stderr:
                return None
        try:
            return json.loads(self.get_text("https://api.github.com/" + path))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise DependencyError(f"GitHub API request failed with HTTP {error.code}") from error

    def download_digest(self, url: str, expected_sha256: str | None = None) -> str:
        if self.offline:
            raise DependencyError("Offline resolution cannot discover a new archive checksum")
        https_url(url)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        # Discovery always downloads afresh when no official digest exists.
        # A mutable upstream URL must not silently reuse an obsolete archive
        # during an explicit update. Normal locked builds never call here.
        with tempfile.TemporaryDirectory(prefix="resolve-", dir=self.cache_dir) as temporary:
            archive = Path(temporary) / "download"
            request = urllib.request.Request(url, headers={"User-Agent": self.request_user_agent()})
            with urllib.request.urlopen(request, timeout=120) as response, archive.open("wb") as output:
                https_url(response.url)
                while chunk := response.read(1024 * 1024):
                    digest.update(chunk)
                    output.write(chunk)
            actual = digest.hexdigest()
            if expected_sha256 and actual != expected_sha256.lower():
                raise DependencyError("Downloaded dependency does not match its published SHA256")
            destination = self.cache_dir / actual
            if destination.is_symlink():
                raise DependencyError("Resolver cache archive must not be a symbolic link")
            if destination.exists():
                if not destination.is_file() or hashlib.sha256(destination.read_bytes()).hexdigest() != actual:
                    raise DependencyError("Resolver cache archive was modified")
            else:
                os.replace(archive, destination)
            return actual


def tag_version(tag: str, spec: dict, *, explicit: bool = False) -> str | None:
    prefix = spec.get("tag_prefix", "v")
    if not tag.startswith(prefix):
        return None
    version = tag[len(prefix):].replace(spec.get("tag_separator", "."), ".")
    # Automatic selection stays stable. An explicit version may name a
    # prerelease or downstream patch, but still has to resolve to a real tag.
    suffix = r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?" if explicit else ""
    if re.fullmatch(r"\d+(?:\.\d+){0,3}" + suffix + r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?", version):
        return version
    return None


def version_key(version: str) -> tuple:
    return tuple(int(part) for part in version.split("+")[0].split("."))


def github_pages(context: Context, path: str):
    for page in range(1, 101):
        values = context.github_json(f"{path}?per_page=100&page={page}")
        if values is None:
            return
        if not isinstance(values, list):
            raise DependencyError("Unexpected GitHub release listing")
        yield from values
        if len(values) < 100:
            return
    raise DependencyError("GitHub release listing exceeded the supported pagination limit")


def tag_revision(context: Context, repo: str, tag: str) -> str:
    reference = context.github_json(f"repos/{repo}/git/ref/tags/{urllib.parse.quote(tag, safe='')}")
    if not reference or "object" not in reference:
        raise DependencyError(f"Release tag was not found: {repo} {tag}")
    obj = reference["object"]
    visited = set()
    while obj.get("type") == "tag":
        sha = obj.get("sha", "")
        if sha in visited or len(visited) >= 10:
            raise DependencyError("Invalid annotated tag chain")
        visited.add(sha)
        annotation = context.github_json(f"repos/{repo}/git/tags/{sha}")
        if not annotation:
            raise DependencyError("Annotated tag object was not found")
        obj = annotation["object"]
    if obj.get("type") != "commit" or not re.fullmatch(r"[0-9a-f]{40}", obj.get("sha", "")):
        raise DependencyError("Dependency tag must resolve to a full Git commit")
    return obj["sha"]


def resolve_github(spec: dict, requested: str, context: Context) -> dict:
    repo = spec.get("repo", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise DependencyError("GitHub dependencies require an owner/repository")
    artifact = spec.get("artifact", "archive")
    if artifact not in {"archive", "file", "release-asset", "git"}:
        raise DependencyError("Unknown GitHub artifact type")
    release = None
    if requested == "latest":
        candidates = []
        for candidate in github_pages(context, f"repos/{repo}/releases"):
            version = tag_version(candidate.get("tag_name", ""), spec)
            if version and not candidate.get("draft") and not candidate.get("prerelease"):
                candidates.append((version_key(version), version, candidate))
        if candidates:
            _, version, release = max(candidates, key=lambda item: item[0])
            tag = release["tag_name"]
        elif artifact != "release-asset":
            tags = [(tag_version(item.get("name", ""), spec), item.get("name", ""))
                    for item in github_pages(context, f"repos/{repo}/tags")]
            tags = [(version, tag) for version, tag in tags if version]
            if not tags:
                raise DependencyError(f"No stable release tag was found for {repo}")
            version, tag = max(tags, key=lambda item: version_key(item[0]))
        else:
            raise DependencyError(f"No stable release with published assets was found for {repo}")
    else:
        prefix = spec.get("tag_prefix", "v")
        version = requested[len(prefix):] if prefix and requested.startswith(prefix) else requested
        version = version.replace(spec.get("tag_separator", "."), ".")
        separator = spec.get("tag_separator", ".")
        tag = spec.get("tag_template", "{prefix}{separated_version}").format(
            prefix=prefix, version=version, version_underscore=version.replace(".", "_"),
            separated_version=version.replace(".", separator))
        if requested.strip() != requested or tag_version(tag, spec, explicit=True) != version:
            raise DependencyError("Explicit dependency versions must be numeric tagged versions")
        release = context.github_json(f"repos/{repo}/releases/tags/{urllib.parse.quote(tag, safe='')}")
        if release and release.get("draft"):
            raise DependencyError("Dependency versions must select published releases")
    revision = tag_revision(context, repo, tag)
    record = {"version": version, "tag": tag, "revision": revision}
    if artifact == "git":
        record.update(url=f"https://github.com/{repo}.git", sha256="")
    elif artifact == "archive":
        record["url"] = f"https://codeload.github.com/{repo}/tar.gz/{revision}"
        record["sha256"] = context.download_digest(record["url"])
        record["checksum_source"] = "downloaded-over-https"
    elif artifact == "file":
        path = spec.get("path", "")
        if not path or path.startswith("/") or ".." in path.split("/"):
            raise DependencyError("Invalid repository file path")
        record["url"] = f"https://raw.githubusercontent.com/{repo}/{revision}/{path}"
        record["sha256"] = context.download_digest(record["url"])
        record["checksum_source"] = "downloaded-over-https"
    else:
        if not release:
            raise DependencyError(f"The requested release has no asset metadata: {repo} {tag}")
        asset_name = spec["asset"].format(version=version, tag=tag,
                                          version_underscore=version.replace(".", "_"))
        assets = [asset for asset in release.get("assets", []) if asset.get("name") == asset_name]
        if len(assets) != 1:
            raise DependencyError(f"Expected exactly one release asset named {asset_name}")
        asset = assets[0]
        record["url"] = https_url(asset["browser_download_url"])
        official = asset.get("digest") or ""
        if re.fullmatch(r"sha256:[0-9a-fA-F]{64}", official):
            record["sha256"] = official[7:].lower()
            record["checksum_source"] = "github-release-asset"
        else:
            record["sha256"] = context.download_digest(record["url"])
            record["checksum_source"] = "downloaded-over-https"
    return record


def validate_record(record: dict) -> None:
    if not isinstance(record, dict) or not isinstance(record.get("source"), dict):
        raise DependencyError("Invalid locked dependency record")
    if not isinstance(record.get("version"), str) or not record["version"]:
        raise DependencyError("Locked dependency has no resolved version")
    if not isinstance(record.get("requested"), str) or not record["requested"]:
        raise DependencyError("Locked dependency has no version request")
    https_url(record.get("url", ""))
    if record["source"].get("provider") == "github":
        if not re.fullmatch(r"[0-9a-f]{40}", record.get("revision", "")):
            raise DependencyError("Locked GitHub dependency must contain a full commit")
    if record["source"].get("artifact") != "git" and not re.fullmatch(r"[0-9a-f]{64}", record.get("sha256", "")):
        raise DependencyError("Locked download must contain SHA256")


def resolve_record(spec: dict, requested: str, context: Context, config=None) -> dict:
    providers = config.effective_providers() if config is not None else {"github": resolve_github}
    provider = providers.get(spec.get("provider"))
    if provider is None:
        raise DependencyError(f"Unsupported dependency provider: {spec.get('provider')}")
    record = provider(spec, requested, context)
    record.update(requested=requested, source=spec)
    validate_record(record)
    return record


def resolve(file: Path, effective_file: Path | None = None, *,
            versions: dict | None = None, only: list | None = None,
            update: bool = False, context: Context | None = None,
            config: "ProjectConfig | None" = None) -> dict:
    """Resolve selected entries atomically in one file, or an isolated output.

    An effective output binds the complete input-file hash, so editing the
    checked-in resolution cannot resurrect an older build-directory selection.
    Returns a verified in-memory view for the selected names only.
    """
    output = effective_file or file
    versions = versions or {}
    context = context or Context(file.parent / "build/deps/resolver",
                                 user_agent=config.user_agent if config is not None else None)
    with lock_output(output):
        document = load_json(file)
        entries = document["dependencies"]
        selected = set(only or entries)
        if (set(versions) | selected) - entries.keys():
            raise DependencyError("Version overrides and selections must name a declared dependency")
        if set(versions) - selected:
            raise DependencyError("Every version override must also be selected by --only")
        separate = output.resolve() != file.resolve()
        source_hash = hashlib.sha256(file.read_bytes()).hexdigest()
        previous = load_json(output, optional=True) if separate else document
        effective_entries = (previous["dependencies"]
                             if separate and previous.get("_base_sha256") == source_hash else {})
        # Copy declaration/result objects so no partial mutation is exposed on
        # disk if a later selected dependency fails to resolve.
        result = {"schema": 2, "dependencies": json.loads(json.dumps(entries))}
        if separate:
            result["_base_sha256"] = source_hash
            for name, entry in effective_entries.items():
                if name in entries and dependency_spec(entry) == dependency_spec(entries[name]):
                    old = _entry_record(entry, allow_missing=True)
                    if old:
                        result["dependencies"][name]["resolved"] = dict(entry["resolved"])
        for name in sorted(selected):
            spec = dependency_spec(entries[name])
            fingerprint = request_hash(spec)
            requested = versions.get(name, spec.get("version") or "latest")
            if not isinstance(requested, str) or not requested.strip():
                raise DependencyError(f"Invalid version selector: {name}")
            explicit = name in versions or spec.get("version") not in (None, "", "latest")
            record = None
            if not update:
                candidates = [entries[name]]
                if name in effective_entries and dependency_spec(effective_entries[name]) == spec:
                    candidates.append(effective_entries[name])
                for candidate in candidates:
                    old = _entry_record(candidate, allow_missing=True)
                    if old and (not explicit or requested == old.get("requested")
                                or requested in {old.get("version"), old.get("tag")}):
                        record = dict(old)
                        if explicit:
                            record["requested"] = requested
                        break
            if record is None:
                if context.offline:
                    raise DependencyError(f"No matching resolved result for {name} ({requested}); resolve online first")
                record = resolve_record(spec, requested, context, config)
            result["dependencies"][name]["resolved"] = {
                **{key: value for key, value in record.items() if key != "source"},
                "request_hash": fingerprint,
            }
        atomic_json(output, result)
        return read_resolved(output, only=sorted(selected))


# ─────────────────────────────────────────────────────────────────────────────
# Verified, transactional installation cache (the prefix)
# ─────────────────────────────────────────────────────────────────────────────


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def inventory(root):
    result = {}
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root).as_posix()
        if relative == _project().manifest_path.as_posix():
            continue
        if path.is_symlink():
            target = path.resolve()
            if root.resolve() not in target.parents or not target.exists():
                raise ValueError(f'Dependency prefix contains a dangling/external symlink: {path}')
            result[relative] = {'link': os.readlink(path)}
        elif path.is_file():
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            result[relative] = {'sha256': digest.hexdigest(), 'executable': bool(path.stat().st_mode & 0o111)}
        elif not path.is_dir():
            raise ValueError(f'Unsupported file in dependency prefix: {path}')
    return result


def compiler(command):
    parts = [command] if Path(command).is_file() else shlex.split(command, posix=os.name != 'nt')
    if os.name == 'nt':
        parts = [part.strip(chr(34)) for part in parts]
    executable = shutil.which(parts[0]) if parts else None
    if not executable:
        raise ValueError(f'Compiler is unavailable: {command}')
    # MSVC /Bv includes the compiler-pass paths in its output. Invoke the
    # canonical executable so PATH lookup and CMake's absolute spelling produce
    # the same banner without dropping any compiler-version information.
    executable = str(Path(executable).resolve())
    argument = '/Bv' if Path(executable).stem.lower() == 'cl' else '--version'
    # Children are pinned to UTF-8 (python children reconfigure their streams,
    # and CL=/utf-8 makes cl emit UTF-8), so decode explicitly. The locale
    # codec on Chinese Windows (GBK) would crash on the very output this
    # function exists to capture.
    result = subprocess.run([executable, *parts[1:], argument], text=True,
                            encoding='utf-8', errors='replace',
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return {'path': str(Path(executable).resolve()),
            'binary_sha256': hashlib.sha256(Path(executable).read_bytes()).hexdigest(),
            'arguments': parts[1:],
            'version': result.stdout.strip()}


def same_compiler(recorded, actual):
    # GCC aliases cc/gcc and c++/g++ can be identical binaries with different
    # argv[0] banners. Keep the version details while normalizing that one token.
    def identity(record):
        value = {key: item for key, item in record.items() if key != 'path'}
        banner = value['version'].split(' ', 1)
        value['version'] = banner[1] if len(banner) == 2 else banner[0]
        return value
    return identity(recorded) == identity(actual)


def verify_location(context, prefix):
    # Native Windows and MSYS Python can spell the same directory and CPU
    # differently; these are host identities, not the target compiler flags.
    aliases = {'amd64': 'x86_64', 'x64': 'x86_64', 'aarch64': 'arm64'}

    def architecture(machine):
        return aliases.get(machine.lower(), machine.lower())

    actual = {'prefix': str(prefix.resolve()), 'platform': platform.system(),
              'machine': platform.machine()}
    if (Path(context['prefix']).resolve() != prefix.resolve()
            or context['platform'].lower() != actual['platform'].lower()
            or architecture(context['machine']) != architecture(actual['machine'])):
        recorded = {name: context[name] for name in actual}
        raise ValueError(f'Dependency prefix location/platform changed; rebuild required; '
                         f'recorded={recorded!r}, actual={actual!r}')


def build_context(prefix, c=None, cxx=None):
    default_c = 'cl' if shutil.which('cl') else ('cc' if shutil.which('cc') else 'gcc')
    default_cxx = 'cl' if shutil.which('cl') else ('c++' if shutil.which('c++') else 'g++')
    environment = {key: os.environ.get(key, '') for key in (
        'CC', 'CXX', 'CL', '_CL_', 'CFLAGS', 'CXXFLAGS', 'CPPFLAGS', 'LDFLAGS',
        'CMAKE_GENERATOR', 'CMAKE_GENERATOR_PLATFORM', 'CMAKE_GENERATOR_TOOLSET',
        'CMAKE_TOOLCHAIN_FILE', 'SDKROOT', 'MACOSX_DEPLOYMENT_TARGET',
        'INCLUDE', 'LIB', 'LIBPATH', 'WindowsSDKVersion', 'VCToolsVersion')}
    toolchain = environment['CMAKE_TOOLCHAIN_FILE']
    if toolchain:
        environment['toolchain_sha256'] = hashlib.sha256(Path(toolchain).read_bytes()).hexdigest()
    return {'prefix': str(prefix), 'platform': platform.system(),
            'machine': platform.machine(), 'configuration': 'Release',
            'c': compiler(c or os.environ.get('CC') or default_c),
            'cxx': compiler(cxx or os.environ.get('CXX') or default_cxx),
            'environment': environment}


def verify_environment(context, toolchain=None):
    recorded = context['environment']
    # CC/CXX are compared through the selected compiler, and a generator may
    # change without changing the ABI (for example VS -> Ninja with the same cl).
    for key in ('CL', '_CL_', 'CFLAGS', 'CXXFLAGS', 'CPPFLAGS', 'LDFLAGS',
                'SDKROOT', 'MACOSX_DEPLOYMENT_TARGET', 'INCLUDE', 'LIB', 'LIBPATH',
                'WindowsSDKVersion', 'VCToolsVersion'):
        old, current = recorded.get(key, ''), os.environ.get(key, '')
        if key == 'CL':
            old = re.sub(r'(?<!\S)/utf-8(?!\S)', '', old).strip()
            current = re.sub(r'(?<!\S)/utf-8(?!\S)', '', current).strip()
        if old != current:
            raise ValueError(f'Dependency ABI environment changed ({key}); rebuild required')
    selected = toolchain if toolchain is not None else os.environ.get('CMAKE_TOOLCHAIN_FILE', '')
    previous = recorded.get('CMAKE_TOOLCHAIN_FILE', '')
    if bool(selected) != bool(previous):
        raise ValueError('Dependency CMake toolchain differs; rebuild required')
    if selected:
        if Path(selected).resolve() != Path(previous).resolve() or hashlib.sha256(Path(selected).read_bytes()).hexdigest() != recorded.get('toolchain_sha256'):
            raise ValueError('Dependency CMake toolchain changed; rebuild required')


def read_state(prefix):
    path = prefix / _project().manifest_path
    if not path.exists():
        return None
    if path.is_symlink():
        raise ValueError(f'Dependency state must not be a symlink: {path}')
    state = json.loads(path.read_text(encoding='utf-8'))
    if state.get('schema') != 2:
        return None  # Legacy prefixes are preserved as backups, never trusted for reuse.
    if state.get('files') != inventory(prefix):
        raise ValueError(f'Dependency prefix has local modifications or missing files; preserved: {prefix}')
    return state


def write_state(prefix, state):
    state = {**state, 'schema': 2, 'files': inventory(prefix)}
    path = prefix / _project().manifest_path
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.manifest-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def prefix_lock(prefix):
    prefix.parent.mkdir(parents=True, exist_ok=True)
    path = prefix.parent / (prefix.name + '.install-lock')
    try:
        path.mkdir()
    except FileExistsError as error:
        raise ValueError(f'Dependency install already active, or interrupted lock needs inspection: {path}') from error
    try:
        (path / 'owner.json').write_text(json.dumps({'pid': os.getpid(), 'host': platform_node()}))
        yield
    finally:
        (path / 'owner.json').unlink(missing_ok=True)
        path.rmdir()


def platform_node():
    import platform
    return platform.node()


@contextlib.contextmanager
def installation(prefix, identity, expected, metadata):
    """Build at the final absolute prefix; restore the old install on failure.

    The previous prefix only lives as a rollback target: once the new state is
    committed it is deleted, so successful builds never accumulate backups.
    A failed transaction keeps the replacement directory for diagnosis.
    """
    if prefix.is_symlink():
        raise ValueError(f'Refusing symlink installation prefix: {prefix}')
    previous = read_state(prefix) if prefix.exists() else None
    completed = set(previous.get('completed', [])) if previous and previous.get('identity') == identity else set()
    if expected <= completed:
        yield None, previous
        return
    backup = None
    if prefix.exists():
        backup = prefix.with_name(prefix.name + '-replacing-' + uuid.uuid4().hex[:12])
        prefix.rename(backup)
    try:
        if completed:
            shutil.copytree(backup, prefix, symlinks=True)
        else:
            prefix.mkdir(parents=True)
        state = {**metadata, 'identity': identity, 'completed': sorted(completed)}
        yield completed, state
        write_state(prefix, state)
    except BaseException:
        if prefix.exists():
            failed = prefix.with_name(prefix.name + '-failed-' + uuid.uuid4().hex[:12])
            prefix.rename(failed)
            print(f'Preserved incomplete dependency installation: {failed}', flush=True)
        if backup:
            backup.rename(prefix)
        raise
    else:
        if backup:
            sources.remove_retry(backup)


# ─────────────────────────────────────────────────────────────────────────────
# Recipes: every dependency, declared exactly once
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Dependency:
    """One locked dependency source plus the recipe used to install it."""

    name: str
    version: str
    url: str
    sha256: str
    license: str
    #: License files inside the archive, copied to share/licenses/<name>/
    license_files: tuple[str, ...]
    #: Top-level directory inside the archive; empty locates the single root
    root: str
    #: cmake | generated | git, or a key in ProjectConfig.builders
    kind: str
    #: Extra arguments passed to CMake
    options: tuple[str, ...] = ()
    #: CMakeLists.txt written into the source root when kind == "generated"
    cmake_lists: str = ""
    #: Files that must exist after installation (prefix-relative), self-check
    artifacts: tuple[str, ...] = ()
    #: Patch applied to the snapshot before building (tools/_build/patches/)
    patch: str = ""
    #: Whether the project honours CMAKE_INSTALL_LIBDIR (GNUInstallDirs)
    uses_libdir: bool = False
    #: Extra files copied into the source root (tools/_build/patches/)
    extra_files: tuple[str, ...] = ()
    #: Provenance of the recorded hash, written into the manifest
    hash_note: str = "SHA256 verified against dependencies.json resolved metadata"
    revision: str = ""
    #: Libraries consumed by this recipe; changes invalidate its static consumers
    requires: tuple[str, ...] = ()
    #: Optional post-install hook: fn(prefix, source, dep) -> list of extra copied files.
    #: Replaces hardcoded per-dependency special cases.
    post_install: object = None
    #: Optional post-build hook: fn(prefix, source, dep) -> None, runs after
    #: the build but before artifact verification (e.g. zlib Windows rename).
    post_build: object = None
    #: External builder/helper/template inputs; content, not location, is hashed.
    builder_inputs: tuple[Path, ...] = ()
    #: Pure header installations can share one compiler-independent slot.
    header_only: bool = False
    #: Source transport, independent of the builder kind; set by lock resolution.
    source_kind: str = ""

    @property
    def archive_name(self) -> str:
        return self.url.rsplit("/", 1)[-1]


def locked_recipes(lock, recipes, config=None):
    entries = lock.get('dependencies', {})
    patch_versions = config.patch_versions if config is not None else {}
    result = []
    for recipe in recipes:
        entry = entries.get(recipe.name)
        if not entry:
            raise ValueError(f'Missing locked dependency: {recipe.name}')
        validate_record(entry)
        version = entry['version']
        if recipe.patch and version not in patch_versions.get(recipe.name, ()):
            raise ValueError(f'{recipe.name} {version} needs an explicitly reviewed build recipe/patch; '
                             f'supported: {sorted(patch_versions.get(recipe.name, ()))}')
        revision = entry.get('revision', '')
        digest = entry.get('sha256', '')
        source_kind = 'git' if entry.get('source', {}).get('artifact') == 'git' else 'archive'
        if source_kind == 'git':
            if len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision):
                raise ValueError(f'{recipe.name} requires a full locked Git revision')
        elif len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError(f'{recipe.name} requires a locked SHA256')
        locked = replace(recipe, version=version, url=entry['url'], sha256=digest,
                         revision=revision, source_kind=source_kind,
                         hash_note=entry.get('checksum_source', recipe.hash_note))
        if config is not None and config.resolve_recipe is not None:
            locked = config.resolve_recipe(locked, entry)
            if not isinstance(locked, Dependency) or locked.name != recipe.name:
                raise ValueError('resolve_recipe must return a Dependency with the same name')
        result.append(locked)
    return result


def recipe_digest(dependencies):
    """Fingerprint the build pipeline, independent of CLI storage syntax.

    How a workspace was first populated (download, extract, clone) is not part
    of the recipe: the workspace's actual content identity covers its result.
    What a snapshot includes, and everything done to the snapshot, is.
    """
    patches_dir = _project().patches_dir
    patches = {name: sha256(patches_dir / name) for dep in dependencies
               for name in (dep.patch, *dep.extra_files) if name} if patches_dir else {}
    functions = (run, apply_patch, prepare_source, copy_licenses, windows_toolchain,
                 artifact_present, build_environment, cmake_arguments, install_component,
                 dependency_selection, compiler, build_context,
                 sources.files, sources.inventory, sources.snapshot)
    functions += (build_cmake,)
    functions += tuple(_project().builders[kind] for kind in sorted({dep.kind for dep in dependencies})
                       if kind in _project().builders)
    if _project().resolve_recipe is not None:
        functions += (_project().resolve_recipe,)
    def recipe_record(dep):
        record = asdict(dep)
        record['builder_inputs'] = sorted(sha256(Path(path)) for path in dep.builder_inputs)
        # Project hooks and their helpers affect the installed result. Hash
        # their module contents as well as the function, not just its name.
        for key in ('post_install', 'post_build'):
            hook = record[key]
            if hook is None:
                record[key] = None
            else:
                try:
                    if not inspect.isfunction(hook):
                        raise TypeError('not a Python function')
                    hook_source = inspect.getsource(hook)
                    module_file = inspect.getsourcefile(hook)
                except (OSError, TypeError) as error:
                    raise ValueError(f'{dep.name} {key} must be a source-readable Python function') from error
                record[key] = {
                    'name': hook.__module__ + '.' + hook.__qualname__,
                    'source': hook_source,
                    'module_sha256': sha256(Path(module_file)) if module_file else '',
                    'helpers': {
                        name: sha256(Path(inspect.getsourcefile(helper)))
                        for name in hook.__code__.co_names
                        for helper in [hook.__globals__.get(name)]
                        if inspect.isfunction(helper) and inspect.getsourcefile(helper)
                    },
                }
        return record
    return fingerprint({'recipes': [recipe_record(dep) for dep in dependencies],
                        'patches': patches,
                        'pipeline': {fn.__name__: inspect.getsource(fn) for fn in functions}})


def dependency_selection(names, dependencies):
    by_name = {dep.name: dep for dep in dependencies}
    all_names = set(by_name)
    selected = {name.strip() for name in names.split(',') if name.strip()} or all_names
    if selected - all_names:
        raise ValueError(f'Unknown dependencies: {sorted(selected - all_names)}')
    # Static consumers must be built against this installation's matching libraries.
    while True:
        required = {name for selected_name in selected for name in by_name[selected_name].requires}
        if required - all_names:
            raise ValueError(f'Missing dependency recipes: {sorted(required - all_names)}')
        if required <= selected:
            break
        selected.update(required)
    return selected


def component_identities(dependencies, context, sources_by_name=None):
    """Version/patch changes rebuild the component and its consumers, not its peers."""
    if 'environment' in context:
        # The selected compiler records already cover the executable, its hash,
        # version and arguments. CC/CXX only retain the caller's spelling (or
        # absence), which changes when switching between the standalone builder
        # and tools/build.py. A generator alone does not change the target ABI;
        # retain its platform/toolset and every other build environment input.
        context = {**context, 'environment': {
            key: value for key, value in context['environment'].items()
            if key not in ('CC', 'CXX', 'CMAKE_GENERATOR')}}
    identities = {}
    for dep in dependencies:
        missing = set(dep.requires) - identities.keys()
        if missing:
            raise ValueError(f'Dependency recipes are not topologically ordered: {dep.name}: {sorted(missing)}')
        # Content only: committing an edit without changing files needs no rebuild.
        identities[dep.name] = fingerprint({
            'recipe': recipe_digest([dep]), 'context': context,
            'source': ((sources_by_name or {}).get(dep.name) or {}).get('content'),
            'requires': {name: identities[name] for name in dep.requires}})
    return identities


def reusable_components(previous, identities):
    if not previous:
        return set()
    components = previous.get('components', {})
    # Older installations lack ownership receipts. Preserve the old prefix as a
    # backup, but rebuild it once rather than guessing which files belong to whom.
    owned = set()
    for name in previous.get('completed', []):
        component = components.get(name, {})
        files = component.get('files', [])
        if not files or owned.intersection(files):
            return set()
        owned.update(files)
    if owned != set(previous.get('files', {})):
        return set()
    return {name for name in previous.get('completed', [])
            if components[name].get('identity') == identities.get(name)}


def copy_component_files(source, destination, files):
    """Copy verified owned files; retain symlinks for their final prefix location."""
    for relative in files:
        origin, target = source / relative, destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if origin.is_symlink():
            target.symlink_to(os.readlink(origin), target_is_directory=origin.is_dir())
        else:
            shutil.copy2(origin, target)


def verify_prefix(prefix, resolution, dependencies, selected, c=None, cxx=None, toolchain=None,
                  c_arg1='', cxx_arg1='', source_dir=None, build_config=None, cmake_options=None):
    """Read-only check that the prefix was built from the current sources."""
    rebuild = 'run python tools/build.py deps (add --profile tests for test builds)'
    state = read_state(prefix)
    if not state:
        raise ValueError(f'Unverified/legacy prefix {prefix}: {rebuild}')
    if not state.get('source_dir') or not state.get('components'):
        raise ValueError(f'Legacy prefix has no editable source receipt: {rebuild}')
    context = state['context']
    if build_config is not None and context.get('configuration') != build_config:
        raise ValueError(f'Dependency build configuration differs; expected {build_config}; {rebuild}')
    if cmake_options is not None and context.get('cmake_options', []) != list(cmake_options):
        raise ValueError(f'Dependency CMake options differ; {rebuild}')
    source_dir = Path(source_dir or state['source_dir'])
    identities = sources.identities(source_dir, dependencies)
    if not selected <= identities.keys():
        raise ValueError(f'Dependency source missing in {source_dir}: {sorted(selected - identities.keys())}; {rebuild}')
    for dep in dependencies:
        if dep.name in selected:
            sources.report(source_dir, dep, identities[dep.name])
    component_identities_now = component_identities(dependencies, state['context'], identities)
    stale = selected - reusable_components(state, component_identities_now)
    if stale:
        raise ValueError(f'Dependency sources, selection or recipe changed since the last dependency build '
                         f'({", ".join(sorted(stale))}); {rebuild}')
    if not selected <= set(state.get('completed', [])):
        raise ValueError(f'Dependency prefix is incomplete: {rebuild}')
    context = state['context']
    verify_environment(context, toolchain)
    verify_location(context, prefix)
    for name, command, arguments in [('c', c, c_arg1), ('cxx', cxx, cxx_arg1)]:
        if command:
            if arguments:
                # CMake exposes the executable and its pre-command arguments
                # separately. Preserve the executable as one token, then let
                # compiler() parse the argument string with host quoting rules.
                quote = subprocess.list2cmdline if os.name == 'nt' else shlex.join
                command = quote([command]) + ' ' + arguments
            actual = compiler(command)
            if same_compiler(context[name], actual):
                continue
            raise ValueError(f'Dependency {name} compiler differs from project compiler; rebuild required; '
                             f'recorded={context[name]!r}, actual={actual!r}')
    return state


def build_environment(prefix, c=None, cxx=None, toolchain=None):
    if toolchain is not None:
        os.environ['CMAKE_TOOLCHAIN_FILE'] = toolchain
    if shutil.which('cmake') is None:
        raise ValueError('CMake and a C/C++ toolchain are required')
    if shutil.which('cl') and '/utf-8' not in os.environ.get('CL', ''):
        os.environ['CL'] = (os.environ.get('CL', '') + ' /utf-8').strip()
    if sys.platform == 'win32' and os.environ.get('CMAKE_GENERATOR_PLATFORM', '').lower() not in ('', 'x64'):
        raise ValueError('Windows dependency recipes currently target x64')
    context = build_context(prefix, c, cxx)
    if sys.platform == 'win32':
        families = {'msvc' if Path(context[key]['path']).stem.lower() in ('cl', 'clang-cl') else 'mingw'
                    for key in ('c', 'cxx')}
        if len(families) != 1:
            raise ValueError('C and C++ compilers must use the same Windows toolchain (MSVC or MinGW)')
    # Every registered builder must consume the same selected compiler.
    for variable, key in [('CC', 'c'), ('CXX', 'cxx')]:
        command = [context[key]['path'], *context[key]['arguments']]
        os.environ[variable] = subprocess.list2cmdline(command) if os.name == 'nt' else shlex.join(command)
    return context


def cmake_arguments(prefix, context, config='Release'):
    common = [f'-DCMAKE_INSTALL_PREFIX={prefix}', f'-DCMAKE_BUILD_TYPE={config}',
              f'-DCMAKE_TRY_COMPILE_CONFIGURATION={config}',
              '-DCMAKE_POSITION_INDEPENDENT_CODE=ON', '-DBUILD_SHARED_LIBS=OFF',
              f'-DCMAKE_PREFIX_PATH={prefix}',
              f"-DCMAKE_C_COMPILER={context['c']['path']}",
              f"-DCMAKE_CXX_COMPILER={context['cxx']['path']}"]
    if context['environment']['CMAKE_TOOLCHAIN_FILE']:
        common.append('-DCMAKE_TOOLCHAIN_FILE=' + context['environment']['CMAKE_TOOLCHAIN_FILE'])
    for name, key in [('C', 'c'), ('CXX', 'cxx')]:
        if context[key]['arguments']:
            arguments = context[key]['arguments']
            quoted = subprocess.list2cmdline(arguments) if os.name == 'nt' else shlex.join(arguments)
            common.append(f'-DCMAKE_{name}_COMPILER_ARG1=' + quoted)
    return common


def prepare_workspace(work, source_dir, dep, offline, explicit=False):
    """deps/<name>: a Git clone the user can pull, or the unpacked release archive."""
    def populate(destination):
        if dep.source_kind == 'git' or (not dep.source_kind and dep.kind == 'git'):
            fetch_git(work, destination, dep, offline)
        else:
            archive = download(work / 'cache', dep, offline)
            source = extract(archive, destination.parent / 'extracted', '')
            source.rename(destination)
    return sources.ensure(source_dir, dep, populate, explicit=explicit)


def apply_patch(source: Path, patch_file: Path) -> None:
    """Apply a unified diff (-p1).

    Implemented locally instead of calling `patch`: the pipeline depends only on
    the Python standard library and `patch` is not always present on Windows.
    Already-applied hunks are skipped, so the result is idempotent.
    """
    lines = patch_file.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        while index < len(lines) and not lines[index].startswith("--- "):
            index += 1
        if index >= len(lines):
            return
        old_name = lines[index][4:].strip()
        new_name = lines[index + 1][4:].strip() if index + 1 < len(lines) else ""
        index += 2

        def strip_prefix(name: str) -> str:
            # Strip a/ or b/ and an optional trailing timestamp.
            name = name.split("\t", 1)[0]
            return name[2:] if name.startswith(("a/", "b/")) else name

        target = source / strip_prefix(new_name or old_name)
        original = target.read_text(encoding="utf-8").splitlines() if target.is_file() else []
        updated = list(original)

        while index < len(lines) and lines[index].startswith("@@"):
            header = lines[index]
            index += 1
            numbers = header.split("@@")[1].strip()
            old_start = int(numbers.split(",")[0].lstrip("-")) - 1
            removed, added = [], []
            while index < len(lines) and not lines[index].startswith(("@@", "--- ", "diff ")):
                line = lines[index]
                index += 1
                if line.startswith("\\"):
                    continue
                if line.startswith("+"):
                    added.append(line[1:])
                elif line.startswith("-"):
                    removed.append(line[1:])
                elif line.startswith(" "):
                    added.append(line[1:])
                    removed.append(line[1:])
                elif line == "":
                    added.append("")
                    removed.append("")
                else:
                    break
            if not removed:
                # A hunk adding a new file: skip if the content is already there
                # (otherwise every run would insert another copy).
                if any(updated[start:start + len(added)] == added
                       for start in range(len(updated) - len(added) + 1)):
                    continue
                updated[old_start:old_start] = added
                continue
            position = next((start for start in range(len(updated) - len(removed) + 1)
                             if updated[start:start + len(removed)] == removed), None)
            if position is None:
                # Already applied: the new content is present; skip (idempotent).
                if any(updated[start:start + len(added)] == added
                       for start in range(len(updated) - len(added) + 1)):
                    continue
                raise ValueError(f"Patch cannot be applied: {patch_file.name} -> {target.name}")
            updated[position:position + len(removed)] = added

        target.write_text("\n".join(updated) + ("\n" if updated else ""), encoding="utf-8")


def prepare_source(source: Path, dependency: Dependency) -> None:
    if dependency.patch:
        patch = _project().patches_dir / dependency.patch
        if not patch.is_file():
            raise ValueError(f"Patch missing: {patch}")
        apply_patch(source, patch)
    for name in dependency.extra_files:
        extra = _project().patches_dir / name
        if not extra.is_file():
            raise ValueError(f"Extra file missing: {extra}")
        shutil.copyfile(extra, source / name)
    if dependency.cmake_lists:
        (source / "CMakeLists.txt").write_text(dependency.cmake_lists, encoding="utf-8")


def copy_licenses(prefix: Path, source: Path, dependency: Dependency) -> list[str]:
    target = prefix / "share/licenses" / dependency.name
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    for relative in dependency.license_files:
        origin = source / relative
        if not origin.is_file():
            raise ValueError(f"{dependency.name} license file missing: {origin}")
        shutil.copyfile(origin, target / origin.name)
        copied.append(origin.name)
    # Preserve optional upstream notices without inventing one when absent.
    for filename in ('NOTICE', 'NOTICE.txt'):
        origin = source / filename
        if origin.is_file() and filename not in copied:
            shutil.copyfile(origin, target / filename)
            copied.append(filename)
    if dependency.post_install is not None:
        copied.extend(dependency.post_install(prefix, source, dependency))
    return copied


def artifact_present(prefix: Path, artifact: str) -> bool:
    """Require a real header/config or an exact static-library platform spelling."""
    candidate = prefix / artifact
    if candidate.is_file():
        return True
    if not artifact.endswith('.a') or not candidate.parent.is_dir():
        return False
    stem = candidate.stem.removeprefix('lib')
    stems = (stem, 'lib' + stem)
    names = {name + suffix for name in stems for suffix in ('.a', '.lib')}
    return any(entry.is_file() and entry.name.lower() in names for entry in candidate.parent.iterdir())


def windows_toolchain() -> str:
    """Classify the selected compiler, independent of unrelated PATH entries."""
    selected = os.environ.get('CC', '')
    if selected:
        parts = [selected] if Path(selected).is_file() else shlex.split(selected, posix=os.name != 'nt')
        name = Path(parts[0].strip(chr(34))).stem.lower()
        return 'msvc' if name in ('cl', 'clang-cl') else 'mingw'
    return 'msvc' if shutil.which('cl') and shutil.which('nmake') else 'mingw'


def build_cmake(source: Path, build: Path, prefix: Path, jobs: int,
                dependency: Dependency, common: list[str], config: str = 'Release') -> None:
    generator = os.environ.get('CMAKE_GENERATOR', '').strip()
    selected = windows_toolchain() if sys.platform == 'win32' else ''
    visual_studio = generator.lower().startswith('visual studio') or (not generator and selected == 'msvc')
    configure = ['cmake']
    if visual_studio:
        if selected != 'msvc':
            raise ValueError('Visual Studio generator requires an MSVC-compatible compiler')
        configure += ['-A', os.environ.get('CMAKE_GENERATOR_PLATFORM') or 'x64']
    elif not generator and selected == 'mingw':
        configure += ['-G', 'Ninja' if shutil.which('ninja') else 'MinGW Makefiles']
    libdir = ['-DCMAKE_INSTALL_LIBDIR=lib'] if dependency.uses_libdir else []
    run([*configure, '-S', str(source), '-B', str(build), *common, *libdir, *dependency.options])
    # --config is accepted by single-config generators and required by VS,
    # Xcode and Ninja Multi-Config, including MSVC builds without a VS generator.
    run(['cmake', '--build', str(build), '--parallel', str(jobs), '--config', config])
    run(['cmake', '--install', str(build), '--config', config])


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(cache: Path, dependency: Dependency, offline: bool, *, user_agent: str | None = None) -> Path:
    """Download (or reuse) the archive and verify its SHA256."""
    archive = cache / (dependency.sha256 + "-" + dependency.archive_name)
    if archive.is_symlink():
        raise ValueError(f"Refusing symbolic-link archive cache: {archive}")
    content = cache / dependency.sha256
    if not archive.exists() and content.is_file() and not content.is_symlink():
        if sha256(content) != dependency.sha256:
            raise ValueError(f"Content cache SHA256 mismatch; preserved: {content}")
        shutil.copyfile(content, archive)
    legacy = cache / dependency.archive_name
    if not archive.exists() and legacy.is_file() and not legacy.is_symlink() and sha256(legacy) == dependency.sha256:
        shutil.copyfile(legacy, archive)
    expected = dependency.sha256
    if archive.exists():
        if not archive.is_file() or archive.is_symlink():
            raise ValueError(f"Cache path is not a regular file: {archive}")
        if expected and sha256(archive) != expected:
            raise ValueError(f"Cache SHA256 verification failed; preserved: {archive}")
        print(f"Reusing verified archive: {archive.name}", flush=True)
        return archive
    if offline:
        raise ValueError(f"Offline cache missing: {archive}")

    request = urllib.request.Request(dependency.url,
                                     headers={"User-Agent": user_agent if user_agent is not None else _project().user_agent})
    print(f"Downloading: {dependency.url}\nExpected SHA256: {expected or '(not pinned)'}",
          flush=True)
    # An interrupted download or a failed verification only removes this run's
    # temporary file; an existing archive is never overwritten.
    with tempfile.TemporaryDirectory(prefix="download-", dir=cache) as temporary:
        candidate = Path(temporary) / dependency.archive_name
        try:
            with urllib.request.urlopen(request, timeout=120) as response, \
                    candidate.open("wb") as output:
                https_url(response.url)
                shutil.copyfileobj(response, output)
        except urllib.error.HTTPError as error:  # noqa: PERF203
            raise ValueError(f"Download failed (HTTP {error.code}): {dependency.url}") from error
        actual = sha256(candidate)
        if expected and actual != expected:
            raise ValueError(f"Download SHA256 verification failed: {dependency.url}\nActual: {actual}")
        if not expected:
            print(f"Downloaded SHA256: {actual}", flush=True)
        candidate.replace(archive)
    return archive


def extract(archive: Path, destination: Path, root_name: str) -> Path:
    """Safe extraction: refuse absolute paths, escapes and non-regular members."""
    reserved = getattr(ntpath, 'isreserved',
                       lambda name: PureWindowsPath(name).is_reserved())
    seen = {}

    def safe(member_name: str) -> PurePosixPath:
        path = PurePosixPath(member_name)
        if (path.is_absolute() or ".." in path.parts or "\\" in member_name
                or not path.parts or (root_name and path.parts[0] != root_name)
                or any(':' in part or part.endswith(('.', ' ')) or reserved(part)
                       for part in path.parts)):
            raise ValueError(f"Unsafe archive member rejected: {member_name}")
        target = (destination / member_name).resolve()
        if destination.resolve() not in target.parents:
            raise ValueError(f"Refusing escaping archive member: {member_name}")
        return path

    def validate(member_name, directory):
        path = safe(member_name)
        key = path.as_posix().casefold()
        if key in seen and not (directory and seen[key]):
            raise ValueError(f"Unsafe archive member duplicates another path: {member_name}")
        if any(seen.get(parent.as_posix().casefold()) is False for parent in path.parents):
            raise ValueError(f"Unsafe archive member has a file parent: {member_name}")
        if not directory and any(name.startswith(key + '/') for name in seen):
            raise ValueError(f"Unsafe archive member conflicts with a directory: {member_name}")
        seen[key] = directory

    def write(target: Path, data, executable: bool) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as output:
            output.write(data)
        target.chmod(0o755 if executable else 0o644)

    if archive.suffix == ".zip" or archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as package:
            for info in package.infolist():
                kind = (info.external_attr >> 16) & 0o170000
                if kind not in (0, 0o040000, 0o100000):
                    raise ValueError(f"Unsafe archive member is not regular: {info.filename}")
                validate(info.filename, info.is_dir())
            for info in package.infolist():
                if info.is_dir():
                    continue
                write(destination / info.filename, package.read(info),
                      bool(info.external_attr >> 16 & 0o111))
    else:
        with tarfile.open(archive) as package:
            for member in package.getmembers():
                if not (member.isdir() or member.isfile()):
                    raise ValueError(f"Unsafe archive member is not regular: {member.name}")
                validate(member.name, member.isdir())
            for member in package.getmembers():
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError(f"Refusing non-regular archive member: {member.name}")
                source = package.extractfile(member)
                if source is None:
                    raise ValueError(f"Cannot read archive member: {member.name}")
                with source:
                    write(destination / member.name, source.read(),
                          bool(member.mode & 0o111))
    if root_name:
        return destination / root_name
    entries = [entry for entry in destination.iterdir() if entry.is_dir()]
    if len(entries) != 1:
        raise ValueError(f"Cannot locate the archive's top-level directory: {destination}")
    return entries[0]


def run(command: list[str], cwd: Path | None = None) -> None:
    print("+ " + shlex.join(command), flush=True)
    subprocess.run(command, check=True, cwd=str(cwd) if cwd else None)


def fetch_git(work: Path, workspace: Path, dependency: Dependency, offline: bool) -> None:
    """Clone the locked revision straight into a new editable workspace.

    build/deps/git/<name>/<revision> belongs to the former layout. A clean
    copy there is reused once for migration (and left for older build trees);
    no second per-revision tree is created any more.
    """
    legacy = work / "git" / dependency.name / dependency.revision
    if sources.copy_legacy_clone(legacy, workspace, dependency.revision):
        return
    if offline:
        raise ValueError(f"Offline source missing for {dependency.name}: create {workspace} "
                         "online once, or provide it manually")
    sources.clone(workspace, dependency.url, dependency.revision)


def output_path(value: Path) -> Path:
    path = value.expanduser().resolve()
    if path in (Path.home(), Path(path.anchor)) or path in Path.home().parents:
        raise ValueError(f"Refusing the home directory or filesystem root as output: {path}")
    systems = ["/usr", "/bin", "/sbin", "/etc", "/System", "/Library", "/opt"]
    if os.name == 'nt':
        systems.extend(os.environ[key] for key in
                       ('SystemRoot', 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramData')
                       if os.environ.get(key))
    for system in systems:
        root = Path(system).resolve()
        if path == root or root in path.parents:
            raise ValueError(f"Refusing to install into a system directory: {path}")
    if path.exists() and not path.is_dir():
        raise ValueError(f"Output path is not a directory: {path}")
    if ";" in str(path):
        raise ValueError("Output paths cannot contain CMake's list separator ';'")
    return path


# ─────────────────────────────────────────────────────────────────────────────
# Identity, reuse and verification
# ─────────────────────────────────────────────────────────────────────────────


def install_component(dep, available, run_root, prefix, jobs, common, expected_source=None, config='Release',
                      c_compiler=None):
    # One flat directory per dependency; a rebuild discards the previous one.
    holder = run_root / dep.name
    shutil.rmtree(holder, ignore_errors=True)
    holder.mkdir()
    source = holder / 'source'
    actual_source = sources.snapshot(available, source, expected_source)
    prepare_source(source, dep)
    builder = _project().builders.get(dep.kind)
    if builder is not None:
        from .artifacts import BuildContext
        build = holder / 'build'
        build.mkdir()
        target = build_context(prefix, c_compiler)
        context = BuildContext(source, build, prefix, (prefix,),
                               {'cmake_options': tuple(common) + dep.options}, dict(os.environ),
                               target, {}, jobs, config)
        builder(context)
    elif dep.kind in ('cmake', 'generated', 'git'):
        build_cmake(source, holder / 'build', prefix, jobs, dep, common, config)
    else:
        raise ValueError(f'No external builder registered for recipe kind: {dep.kind}')
    licenses = copy_licenses(prefix, source, dep)
    if dep.post_build is not None:
        dep.post_build(prefix, source, dep)
    missing = [artifact for artifact in dep.artifacts if not artifact_present(prefix, artifact)]
    if missing:
        raise ValueError(f'{dep.name} missing expected artifacts: {missing}')
    return {'name': dep.name, 'version': dep.version, 'url': dep.url,
            'sha256': dep.sha256, 'revision': dep.revision,
            'actual_source': actual_source,
            'license': dep.license, 'license_files': licenses}


# ─────────────────────────────────────────────────────────────────────────────
# Public entry points (used by tools/build.py only)
# ─────────────────────────────────────────────────────────────────────────────


def resolve_selection(file, work, recipes, *, offline=False, versions=(), update=False, config=None):
    """Resolve the lock in place for every recipe declared by this selection."""
    names = {dep.name for dep in recipes}
    for name in versions:
        if name not in names:
            raise ValueError(f'Unknown dependency version override: {name}')
    return resolve(file, versions=dict(versions), only=sorted(names), update=update,
                   context=Context(work / 'cache', offline=offline), config=config)


def _layout(work, prefix, source_dir):
    """Validate the three output locations and their separations."""
    if Path(prefix).is_symlink():
        raise ValueError(f'Refusing symlink installation prefix: {prefix}')
    work = output_path(work)
    prefix = output_path(prefix)
    source_dir = output_path(source_dir)
    if prefix == source_dir or prefix in source_dir.parents or source_dir in prefix.parents:
        raise ValueError('the source directory overlaps the installed prefix')
    for directory in ('cache', 'git', 'runs'):
        other = work / directory
        if prefix == other or prefix in other.parents or other in prefix.parents:
            raise ValueError('the installed prefix overlaps the source/build cache')
        if source_dir == other or source_dir in other.parents or other in source_dir.parents:
            raise ValueError('the source directory overlaps the source/build cache')
    return work, prefix, source_dir


def install(file, work, prefix, source_dir, *, config, profile='tests', tls_backend='auto',
            only='', offline=False, versions=(), update=False, jobs=None,
            c=None, cxx=None, toolchain=None, build_config='Release', cmake_options=()):
    """Resolve, prepare workspaces and install every selected dependency."""
    init_project(config)
    recipes = config.effective_configure()(config.recipes, profile, tls_backend)
    selected = dependency_selection(only, recipes)
    work, prefix, source_dir = _layout(work, prefix, source_dir)
    resolve_selection(file, work, recipes, offline=offline, versions=versions, update=update,
                      config=config)
    resolution = read_resolved(file, [dep.name for dep in recipes])
    dependencies = locked_recipes(resolution, recipes, config)
    context = build_environment(prefix, c, cxx, toolchain)
    context['configuration'] = build_config
    context['cmake_options'] = list(cmake_options)
    jobs = jobs or min(os.cpu_count() or 1, 8)

    (work / 'cache').mkdir(parents=True, exist_ok=True)
    with prefix_lock(prefix):
        # Sources are user workspaces, never patched or reset during builds.
        # Initial download/migration finishes before replacing an old prefix.
        available = {dep.name: prepare_workspace(work, source_dir, dep, offline,
                                                 update or dep.name in versions)
                     for dep in dependencies if dep.name in selected}
        identities = sources.identities(source_dir, dependencies)
        metadata = {'resolution': fingerprint(resolution), 'recipe': recipe_digest(dependencies),
                    'context': context, 'generator': 'tools/build.py',
                    'source_dir': str(source_dir), 'sources': identities}
        component_ids = component_identities(dependencies, context, identities)
        # Transaction identity must follow component identity rules as well.
        identity = fingerprint({'metadata': metadata, 'components': component_ids})
        previous = read_state(prefix) if prefix.exists() else None
        completed = reusable_components(previous, component_ids)
        if selected <= completed:
            print(f'Reusing verified dependency prefix: {prefix}')
            return prefix
        runs = work / 'runs'
        runs.mkdir(parents=True, exist_ok=True)
        # Component files that must survive the prefix swap are staged under
        # runs/.staged for the duration of the transaction only.
        staged = runs / '.staged'
        shutil.rmtree(staged, ignore_errors=True)
        if completed and previous.get('identity') != identity:
            staged.mkdir()
            for name in sorted(completed):
                copy_component_files(prefix, staged, previous['components'][name]['files'])
        common = [*cmake_arguments(prefix, context, build_config), *cmake_options]
        try:
            with installation(prefix, identity, selected, metadata) as (done, state):
                if done is None:
                    return prefix
                records = [record for record in previous.get('dependencies', []) if record['name'] in completed] if previous else []
                components = {name: previous['components'][name] for name in completed}
                if staged.exists():
                    for name in sorted(completed):
                        copy_component_files(staged, prefix, components[name]['files'])
                    done.update(completed)
                for name in sorted(completed):
                    print(f'Reusing verified component: {name}', flush=True)
                for dep in dependencies:
                    if dep.name not in selected - done:
                        continue
                    before = inventory(prefix)
                    records.append(install_component(dep, available[dep.name], runs, prefix,
                                                     jobs, common, identities[dep.name],
                                                     config=build_config,
                                                     c_compiler=context['c']['path']))
                    after = inventory(prefix)
                    changed = [name for name, content in before.items() if after.get(name) != content]
                    if changed:
                        raise ValueError(f'{dep.name} overwrote another component: {changed}')
                    components[dep.name] = {'identity': component_ids[dep.name],
                                            'files': sorted(after.keys() - before.keys())}
                    done.add(dep.name)
                state.update(completed=sorted(done), dependencies=records, components=components)
        finally:
            shutil.rmtree(staged, ignore_errors=True)
        print(f'Installed dependency prefix from current editable sources: {prefix}')
        return prefix


def verify(file, work, prefix, source_dir, *, config, profile='tests', tls_backend='auto', only='',
           c=None, cxx=None, c_arg1='', cxx_arg1='', toolchain=None, cmake_platform='',
           build_config=None, cmake_options=None):
    """Read-only verification that the prefix matches sources and requested settings.

    None preserves the recorded setting for callers predating these options.
    Pass an explicit configuration/options tuple to enforce the consumer's ABI.
    """
    init_project(config)
    recipes = config.effective_configure()(config.recipes, profile, tls_backend)
    selected = dependency_selection(only, recipes)
    work, prefix, source_dir = _layout(work, prefix, source_dir)
    resolution = read_resolved(file, [dep.name for dep in recipes])
    dependencies = locked_recipes(resolution, recipes, config)
    verify_prefix(prefix, resolution, dependencies, selected, c, cxx, toolchain,
                   c_arg1, cxx_arg1, source_dir, build_config, cmake_options)
    if sys.platform == 'win32' and cmake_platform.lower() not in ('', 'x64'):
        raise ValueError('The Windows dependency prefix targets x64; configure the project for x64')
    print(f'Verified dependency prefix and current source contents: {prefix}')
    return prefix


def update_lock(file, work, *, config=None, only=(), versions=(), offline=False):
    """Refresh dependency selections; the next build updates the sources."""
    return resolve(file, versions=dict(versions), only=list(only) or None, update=True,
                   context=Context(work / 'cache', offline=offline), config=config)


def cache_key(work):
    """A GitHub Actions cache scope for the actual host compiler and SDK.

    Dependency versions/recipes are appended by the workflow. A restore key
    can therefore reuse unchanged components across dependency updates. The
    builder still verifies every restored file and never trusts this key.
    """
    context = build_context(Path(work) / 'prefix')
    context['cmake'] = subprocess.check_output(['cmake', '--version'], text=True)
    context['runner_image'] = os.environ.get('ImageVersion', '')
    return fingerprint(context)
