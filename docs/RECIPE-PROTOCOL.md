# External recipe protocol

A recipe package is ordinary Python code plus declared templates, patches and
license assets. It is trusted build code: installing/injecting one authorizes
its build commands. ForgeDeps does not execute downloaded project metadata as
Python code and does not make an untrusted recipe safe.

## BuildRequest

Use a source-readable Python function or `functools.partial` as `builder(ctx)`.
The callback receives a disposable source snapshot, a disposable build directory,
the final installation prefix, exact dependency prefixes and artifact references,
options, the target probe, configuration, job count and build environment.

Use `ctx.run([executable, argument, ...])` for build commands. Arguments are not
shell strings. Diagnostics are streamed to stderr and retained in `build.log`;
`capture_output=True` is available when a recipe needs to parse tool output.
The callback must install only below `ctx.prefix`. It must not reset the original
source tree or write outputs into another dependency's prefix.

`recipe_input_files` maps stable logical names to the actual files used by the
recipe: helper modules, templates, patches, generated-input definitions and
external license material. Their contents, not their filesystem locations, enter
the identity. Builder/validator code, closures and defaults are also recorded.
Do not hide ABI inputs in undeclared globals or custom environment variables;
pass options and custom environment values explicitly. Standard compiler flags,
SDK selection and runtime environment inputs are tracked automatically.

`requires` contains complete `Artifact` references, not unverified library paths.
Both the build fingerprint and generation are tracked. A dependency replacement
invalidates its dependent recipes. Use `artifacts` for exact required installed
files and `license_files` for upstream license files. A `validate(ctx)` callback
can perform additional checks before publication. Missing files, validation
failure or concurrent source/recipe changes roll back the installation.

`header_only=True` chooses the readable `headers` slot. Use it only when the
installed result is independent of the target compiler and ABI; code generators,
configured platform headers and compiled tools require an actual target. Declared
target, options and environment still participate in its identity.

## ProjectConfig bridge

`Dependency` is a convenient declarative adapter over BuildRequest. Its built-in
kinds are `cmake`, `generated` (injected CMakeLists text) and `git` (Git source with
CMake build). Other kinds map to `ProjectConfig.builders[kind]`, a BuildContext
callback. Source resolution uses the project's lock file; unknown kinds are not
silently sent through the CMake runner.

Declare transitive names in `requires`. The adapter orders the selected graph and
rejects missing recipes or cycles. `builder_inputs` lists external helper and
asset files. `patches_dir`, `patch_versions`, `post_install` and `post_build` are
external policy. `resolve_recipe(locked_dependency, record)` can transform a
version-specific template after generic lock validation.

CMake options passed to the environment and recipe are probed together. The
runner replays the selected compiler, effective ABI settings and generator.
Installation prefix and dependency prefix search are managed by ForgeDeps;
express required libraries through the dependency graph. Custom builders must
honor the same probed toolchain, or explicitly reject unsupported settings.

## Lifetimes

`build_package` returns an immutable reference to one generation. A later
replacement at the same path does not update that reference. `verify_artifact`
checks bytes and the transitive artifact graph; `verify_request` also reevaluates
the current build request without rebuilding. `Environment.lease()` validates
under shared reader locks and holds all required slots until the context exits.

Always hold a lease for the complete consumption session. Writers take exclusive
locks in the same deterministic path order. Nested readers and subprocess readers
are supported; an in-process reader-to-writer upgrade is rejected. Process exit
releases OS locks. Transaction diagnostics are retained after failure; successful
replacement removes its rollback copy, leaving one current installation.
