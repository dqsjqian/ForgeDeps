# Changelog

## 0.3.0

- Add the `aria-deps` CLI for resolution, read-only validation, source fetching,
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

- New `aria_deps.build_kit` subpackage: shared build pipeline mechanics for
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
