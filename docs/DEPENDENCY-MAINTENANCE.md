# Dependency maintenance

Use an upstream release when it satisfies the project's requirements. When a
reproducible upstream defect affects required behavior, check the latest stable
release and relevant upstream changes first. If the defect remains, maintain a
fork instead of leaving the needed capability indefinitely blocked.

A maintenance fork must:

- retain upstream history, authorship and licenses, with an upstream remote;
- document its upstream base, the defect and the supported behavior;
- keep the fix focused and include a regression that fails on the upstream base;
- validate supported platforms and publish immutable tags and source hashes;
- use an identifiable downstream version, retaining ABI compatibility when
  claimed, and expose capabilities when consumers need conditional behavior;
- track upstream releases, review fixes and retire the downstream patch when an
  upstream version satisfies the same regressions.

A fork does not authorize unrelated feature expansion or silent automatic
upgrades. Consumers pin the exact release, revision and archive hash in their
lock. Publish verified source artifacts before updating consumers; update the
consumer lock and recipe together. Preserve existing local edits and reject
modified caches rather than overwriting them.

For ForgeDeps releases, run the offline transaction, source-cache and CLI suites
on Linux, macOS and Windows, verify the installed wheel includes the CLI and
CMake module, and build an independent consumer. Push reviewed source, require
green CI, then create the immutable release tag. Existing tags are never moved.
