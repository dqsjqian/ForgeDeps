# aria-deps

Project-agnostic dependency pipeline for C/C++ projects: resolve locked
dependencies, fetch sources, apply patches, build and transactionally install
them into a prefix.

Extracted from [AriaRead](https://github.com/dqsjqian/AriaRead)'s `tools/build.py`.

## What it does

- **Resolve**: `dependencies.json` lock records (GitHub/sqlite.org/bellard.org
  providers, pluggable) → pinned versions, URLs, SHA256
- **Fetch**: verified immutable archive/file caches for source-tree consumers,
  transactional Git bootstrapping, or editable `deps/<name>/` workspaces for
  prefix builds (user edits are preserved, tracked by content)
- **Build**: per-dependency recipes (`cmake` / `openssl` / `generated` kinds),
  patches applied to disposable snapshots (never touches user sources)
- **Install**: transactional prefix install with manifest, component reuse by
  content identity, rollback on failure
- **Verify**: read-only check that the prefix matches current sources
  (for CMake configure-time validation)

## Usage

Python 3.10 or newer is required. Install the released package into the same
Python environment that runs your build entry point:

```bash
python -m pip install "git+https://github.com/dqsjqian/AriaDeps.git@v0.3.0"
```

Projects can share the resolver and source cache while choosing their own
build model: use the source with `add_subdirectory`, or install dependencies
into a verified prefix with project-supplied recipes. Platform SDKs, Qt,
Android Gradle packaging and toolchain selection remain the consumer's policy.

### Command line and CMake

The `aria-deps` command and `python -m aria_deps` are equivalent. JSON results
go to stdout; progress and native build output go to stderr.

```bash
aria-deps resolve --file dependencies.json --offline
aria-deps read --file dependencies.json --only json
aria-deps fetch-git --file dependencies.json --name library --destination deps/library
aria-deps cmake-dir
```

Resolution reuses the locked selection unless explicitly updated. `--output`
writes an effective lock outside the input, useful for CMake configuration.
`--version library=1.2.3-vendor.1` selects an explicit published downstream tag;
explicit prereleases are also supported. Automatic `latest` selection excludes
prereleases and downstream suffixes. Every GitHub selection pins its full Git
revision, and archives/files pin their SHA256.

`fetch-archive` and `fetch-file` require a URL, SHA256 and cache directory. They
validate downloads and existing extracted contents on every call. Local edits
are rejected and preserved. Offline calls use only already populated caches.
The immutable cache is separate from editable prefix-build workspaces. A new
cache requires one online population; old project-specific caches are not
silently trusted or deleted.

`fetch-git` verifies the locked commit and refuses dirty, linked-worktree and
symlink destinations. Updates stage a new checkout before replacing the old
one; failures restore the old lock and checkout. Successful replacements retain
the previous checkout as a sibling backup. `--source` accepts an explicit local
repository or mirror; with `--offline`, it must be local and contain the commit.

The installed `cmake/AriaDeps.cmake` exposes `aria_deps_resolve`,
`aria_deps_fetch_archive` and `aria_deps_fetch_file`. It delegates all resolution
and fetching to the same Python implementation. The independent
[hello-json example](examples/hello-json) shows a complete C++ consumer that
does not depend on any Aria library:

```bash
cmake -S examples/hello-json -B build/hello-json
cmake --build build/hello-json --config Release
ctest --test-dir build/hello-json -C Release --output-on-failure
cmake -S examples/hello-json -B build/hello-json -DEXAMPLE_OFFLINE=ON
```

### Prefix builds

```python
from pathlib import Path
from aria_deps import Dependency, ProjectConfig, install
from aria_deps import providers

RECIPES = (
    Dependency(name="example", version="", url="", sha256="",
               license="MIT", license_files=("LICENSE",), root="", kind="cmake"),
)

def configure(recipes, profile, tls_backend):
    # project policy: filter/rewrite recipes per profile
    return recipes

config = ProjectConfig(
    name="myproject",
    recipes=RECIPES,
    configure=configure,
    providers={"sqlite": providers.resolve_sqlite},
    patches_dir=Path("recipes/patches"),
    patch_versions={"foo": {"1.0"}},
)

install(Path("dependencies.json"), Path("build/deps"), Path("build/deps/prefix"),
        Path("deps"), config=config, profile="runtime")
```

The example expects a schema-2 `dependencies.json` declaration with a matching
`example` entry. Normal installs reuse its resolved release, commit and SHA256;
they do not silently upgrade it.

`install(..., build_config="Debug", cmake_options=("-DFEATURE=ON",))` passes the
selected configuration through configure, build and install. Configuration and
extra CMake options are part of component identity, so a Release prefix cannot
be silently reused for Debug. Project hook code and its imported helper module
contents also invalidate affected components when changed.

For configure-time checks, pass the consumer's expected settings explicitly:
`verify(..., build_config="Release", cmake_options=())`. A mismatch fails
without changing the prefix. Omitting either option keeps its recorded value
for compatibility with existing callers. Hooks must be source-readable Python
functions defined in a file; built-ins and dynamically created functions are
rejected with a diagnostic because their implementation cannot be fingerprinted.

The same operations are available through the CLI. The configuration module
must export `CONFIG` or `make_config()` returning a `ProjectConfig`:

```bash
aria-deps install --project-config recipes.py --file dependencies.json --work build/deps --build-config Debug
aria-deps verify --project-config recipes.py --file dependencies.json --work build/deps --build-config Debug
```

Pass the same compiler, toolchain, configuration and `--cmake-option=-DNAME=VALUE`
arguments to both operations. A mismatch fails without modifying the prefix.

## Layout

All directories are caller-provided; nothing is hardcoded to a repository root.
The dependency work directory follows the build directory, so each
compiler/build-dir gets isolated snapshots, caches and installed libraries.

Recipes with `requires` must be listed after their prerequisites. There is no
version-constraint solver or remote binary registry. Prefixes are tied to their
recorded location and build environment. This is a reproducible source-selection
and build-validation mechanism, not a hermetic SDK or a guarantee that every
compiler emits byte-identical binaries. Consumers must explicitly identify
external SDKs and libraries that affect their recipes.

See [dependency maintenance](docs/DEPENDENCY-MAINTENANCE.md) for downstream fork
policy and release requirements.

## Tests

```bash
python -m unittest discover -s tests -t tests -p "test_*.py"
```

CI runs the offline suite on Linux, macOS and Windows, with Python 3.10 and
3.13, and checks installation of the distribution. The Windows compiler test
is skipped on non-Windows hosts; install/upgrade/rollback tests run everywhere.
