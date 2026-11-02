# Changelog

## 0.4.12 — 2026-11-02

- Reuse matching, verified artifact slots under shared locks before requesting
  an exclusive writer. Nested dependency builds no longer wait on their own
  parent's reader lease when a first configure generates its environment receipt.
- Treat an inherited build lease as a read-only dependency handoff: verify the
  declared generation and current inputs, and reject forced, changed or undeclared
  builds immediately rather than waiting for an impossible lock upgrade.
- Keep interrupted transaction recovery on the exclusive writer path and recheck
  inputs after releasing shared locks. Preserve artifact integrity checks and
  full-session consumer leases.
- Cover nested recipe subprocesses, first CMake configure under a parent lease,
  reuse without inherited receipts, and fail-fast write rejection.
- Report the current release version from the CLI as well as package metadata.

## 0.4.11 — 2026-11-01

- Stop a parent leased session's receipt from leaking into nested recipe builds: `deps_build.build_cmake` now runs its cmake invocations with a child environment stripped of `FORGEDEPS_ENVIRONMENT`/`FORGEDEPS_ENVIRONMENTS` (case-insensitive, Windows semantics).
- Only append `--check` in `ForgeDeps.cmake` when the output already exists; a first configure inside a leased parent session generates the output instead of failing with Errno 2 on the not-yet-generated `forgedeps-environment.json`. Existing outputs still verify as before.

## 0.4.10 — 2026-10-27

- Validate installed static-library artifacts with the same `.a`/`.lib` spelling
  equivalence used by recipe builds, so MSVC-produced curl archives pass the
  store's final integrity check.

## [0.4.0] - 2026-10-26

- Add a shared artifact workspace with one current installation per library and
  readable target label, internal manifests, transactional replacement and recovery.
- Probe the actual compiler, target ABI, SDK, flags and toolchain inputs; share
  verified artifacts across independent projects without consumer-path identity.
- Add reader leases covering entire consumer sessions, exclusive writers,
  source/recipe/transitive validation and incremental-rebuild timestamp handling.
- Add externally injected BuildRequest/BuildContext recipes, environment build/check
  APIs, workspace initialization and shared CMake environment consumption.
- Move library-specific OpenSSL, SQLite, QuickJS and zlib mechanisms out of the
  core into the separate ForgeRecipes package. Legacy custom kinds require an
  explicit builder registration.

## 0.3.0

- Add the `forgedeps` CLI for resolution, read-only validation, source fetching,
  Git bootstrapping, prefix installation and verification.
- Add an installed CMake bridge backed by the same Python implementation and
  an independent C++ JSON consumer example.
- Add SHA256-addressed immutable archive/file caches with content verification,
  concurrent access protection and explicit offline behavior.
- Add transactional Git bootstrapping with pinned commits, local mirror support,
  dirty-tree protection, backups and rollback.
- Allow explicitly selected published downstream/prerelease tags while keeping
  automatic latest selection restricted to stable upstream version spellings.
- Export resolution and source-fetch APIs for thin consumer wrappers. Existing
  editable source workspaces and prefix recipes remain supported.

## 0.2.1

- Fix OpenSSL compiler selection for environments with clang wrappers.

## 0.2.0

- New `forgedeps.build_kit` subpackage: shared build pipeline mechanics for
  C++ projects (deps -> build -> test -> bench -> package).
- `Pipeline` class with per-project configuration (dependencies, CMake flags,
  Qt requirements, post-build hooks, custom build directories, validation).
- Portable environment detection: CPU count, Ninja, Qt6, MSYS2, OpenSSL.
- Versioned release packaging (zip/tgz) with headers and licenses.
- Windows DLL deployment helper (windeployqt + objdump closure copy).

## 0.1.1

- Carry the selected build configuration through every CMake step and include
  configuration plus caller-supplied CMake options in cache identity.
- Track project hook and helper source changes when reusing installed components.
- Reject unsafe cross-platform archive paths and non-regular members before
  extracting any files; preserve existing files on failure.
- Restore the offline install, upgrade and failed-upgrade rollback regression.
- Keep Unicode dependency paths usable when build logs use a legacy encoding.
- Add Linux, macOS and Windows package and contract CI, and the MIT license file.
