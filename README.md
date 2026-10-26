# ForgeDeps

ForgeDeps builds externally supplied C/C++ dependency recipes into a shared local
workspace. Independent projects can reuse the same verified installation when
the source, recipe, target toolchain, build options and dependencies agree.

The core contains generic source adapters, a CMake runner, a recipe protocol,
artifact verification and process locks. It contains **no third-party sources
or library-specific build recipes**. A project can supply its own recipes or
opt into a separate package such as [ForgeRecipes](https://github.com/dqsjqian/ForgeRecipes).
There is no dependency from the core package to that catalog.

## Install and choose a workspace

Python 3.14+ and a C/C++ compiler are required. Install a release by tag and
register one shared workspace per user:

```sh
python -m pip install "git+https://github.com/dqsjqian/ForgeDeps.git@v0.4.5"
forgedeps init /path/to/ForgeDeps
forgedeps home
```

### Bootstrapping a project from a fresh clone

`forgedeps/bootstrap.py` uses only the standard library, so a project can vendor
it next to its build script and call `ensure("requirements-build.txt")` before
importing ForgeDeps. On the first run it:

1. resolves the workspace (`FORGEDEPS_HOME`, then the registered workspace,
   then `~/.forgedeps`, which it registers);
2. creates `<workspace>/tools/python<X.Y>-<hash of the pins>/`, a virtual
   environment shared by every project with the same pins, and installs the
   pinned packages there without touching the user's interpreter;
3. installs CMake and Ninja into that environment only when they are not on
   `PATH`;
4. re-runs the original command with that environment.

A missing compiler is reported with the platform's install command instead of
being installed. `FORGEDEPS_BOOTSTRAP=0` turns the whole step off.

When running from a ForgeDeps source checkout, its repository root is the default
workspace. An installed wheel has no repository root: use `init` once or set
`FORGEDEPS_HOME`. Resolution order is an explicit API/CLI path, `FORGEDEPS_HOME`,
the registered workspace, then a verified tool source checkout. The consumer's
working directory and Python's site-packages are never implicit workspaces.

```text
ForgeDeps/
├── forgedeps/                      # generic implementation
├── tests/
├── examples/
├── docs/
├── downloads/<library>/            # verified downloads and Git mirrors
├── sources/<library>/              # current editable source workspace
├── build/<library>/<target>/       # one current installed artifact
│   ├── include/
│   ├── lib/
│   ├── bin/
│   ├── share/licenses/
│   ├── manifest.json
│   └── build.log
├── state/
│   ├── sources/
│   ├── locks/
│   └── transactions/
└── tmp/                            # disposable toolchain probes
```

Examples of target labels are `macos-arm64-appleclang-release` and
`windows-x64-msvc-release`. The manifest records precise compiler versions and
ABI details; directory names do not contain build fingerprints. Each library
and target label has **one current installation**, not a growing version tree.
A verified header-only recipe can use the `headers` slot.

## Inject a project policy

A configuration module exports `CONFIG` or `make_config()`:

```python
from forgedeps import Dependency, ProjectConfig

CONFIG = ProjectConfig(name="my-project", recipes=(
    Dependency(name="example", version="", url="", sha256="", root="",
               kind="cmake", license="MIT", license_files=("LICENSE",),
               artifacts=("include/example.h", "lib/libexample.a")),
))
```

The project's schema-2 `dependencies.json` supplies the resolved version,
immutable Git revision and archive SHA256 for `example`. The project name and
configuration-module location are not artifact identity inputs. Identical
recipes in two projects can therefore reuse one installation.

```sh
forgedeps resolve --file dependencies.json --project-config recipes.py
forgedeps environment --file dependencies.json --project-config recipes.py \
  --output build/dependencies.json --build-config Release
forgedeps run --environment build/dependencies.json -- python scripts/build.py
```

`environment` returns a list of prefixes and generation references. Consumers
use those prefixes with `find_package`, `find_library` or their build system's
normal imported-library support. They do not recompile the dependency or need
to copy its installed files into a second project-local dependency build.

Pass the same compiler, toolchain and `--cmake-option=-DNAME=VALUE` arguments
when checking a current environment:

```sh
forgedeps environment --check --file dependencies.json --project-config recipes.py \
  --output build/dependencies.json --build-config Release
```

This checks the lock, current source contents, recipe implementation, actual
toolchain and artifact files. It never fetches or replaces dependencies.
`verify-environment --environment FILE` checks the recorded artifact graph;
it does not reevaluate a project's current source selection.

The installed CMake module (`forgedeps cmake-dir`) exposes
`forgedeps_environment` and `forgedeps_use_environment`. The latter prepends
verified prefixes to `CMAKE_PREFIX_PATH` and tracks every manifest as a
configure dependency. Build drivers must hold an environment lease across
configure, build, tests and packaging. In Python:

```python
from pathlib import Path
from forgedeps import build_environment

selected = build_environment(Path("dependencies.json"), config=CONFIG)
with selected.lease():
    # Configure, build, test and package using selected.prefixes.
    pass
```

## Reuse and replacement rules

The tool probes the real CMake compiler and compiles target objects without
executing them. It records compiler binary/version, target architecture and
platform, SDK/toolchain inputs, effective flags and runtime ABI. Device and
simulator builds are distinct. Recipe code, declared helper/patch/template
files, actual source contents and transitive artifact identities also enter
the receipt. Changing a compiler or option cannot silently reuse an old binary.

Readers share a process-owned lease. A writer locks the dependency graph before
replacing a slot, so it waits for active consumers. Installation uses the final
canonical prefix because package metadata can embed it. A failed build restores
the previous verified installation; a journal allows recovery after an abruptly
terminated writer. Changed installed files receive fresh timestamps so ordinary
incremental builds also rebuild after an upstream header with an old mtime changes.

A source selection change replaces only an unchanged managed source workspace.
Local edits and unknown directories are preserved. An incompatible lock change
fails explicitly instead of relabelling the old source as the new version.
Use `--source NAME=PATH` to intentionally inject an editable checkout; its
contents are still hashed and snapshots are built outside the user's tree.

Two projects requiring different versions or options in the same readable slot
cannot use that slot simultaneously. Each gets its exact requested build after
current readers finish. There is no version-constraint solver or remote binary
registry. Installations are tied to their canonical location; this is not a
promise of arbitrary binary relocation or a completely hermetic SDK.

A distributed application must package its needed runtime libraries and notices.
A running installed application must not depend on a mutable shared build slot
remaining at yesterday's generation after its build lease ends.

## Custom builders and compatibility

`BuildRequest` / `BuildContext` provide the lower-level injection protocol for
non-CMake builders. See [the recipe protocol](docs/RECIPE-PROTOCOL.md).
Library-specific providers, Configure mappings, source patches and compatibility
aliases belong to external recipe packages. The core's default resolver supports
GitHub; other providers are injected through `ProjectConfig.providers`.

The older `install` / `verify` APIs and CLI commands remain available for an
explicit project-local combined prefix. They do not provide cross-project binary
reuse. Custom kinds now require `ProjectConfig.builders`; the previous built-in
OpenSSL builder and SQLite/QuickJS providers moved to ForgeRecipes. Migrate to
`environment` for the shared store. Source-only `fetch-archive`, `fetch-file`
and `fetch-git` remain available as generic primitives.

Versions only change through explicit resolution/update. Explicit downstream
and prerelease tags are supported; automatic `latest` chooses stable upstream
releases. See [dependency maintenance](docs/DEPENDENCY-MAINTENANCE.md) for the
focused-fork policy and release verification.

## Verification

```sh
python -m unittest discover -s tests -t tests -p 'test_*.py'
```

CI runs on Linux, macOS and Windows with Python 3.14. Tests compile and
run independent consumers, exercise source/options changes and preserved mtimes,
and use separate processes for concurrent builds, readers and killed writers.
