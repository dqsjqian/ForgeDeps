#!/usr/bin/env python3
"""Offline regressions for the unified dependency library (no network access)."""
import io
import json
import os
import shlex
import sys
import tarfile
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import fixtures
from forgedeps import deps_build as deps
from forgedeps import init_project

init_project(fixtures.CONFIG)

SHA = "a" * 40
DIGEST = "b" * 64
SOURCE = {"provider": "github", "repo": "example/library", "artifact": "git", "tag_prefix": "v"}


class FakeContext:
    offline = False

    def __init__(self):
        self.calls = []
        self.releases = [
            {"tag_name": "v1.9.0"}, {"tag_name": "v2.0.0-rc1", "prerelease": True},
            {"tag_name": "v1.10.0"}, {"tag_name": "v99.0.0", "draft": True},
        ]
        self.tags = []
        self.annotation = None
        self.fail = False

    def github_json(self, path):
        self.calls.append(path)
        if self.offline or self.fail:
            raise AssertionError("Unexpected network lookup")
        if "/releases?" in path:
            return self.releases
        if "/tags?" in path:
            return self.tags
        if "/releases/tags/" in path:
            tag = path.rsplit("/", 1)[1]
            return next((x for x in self.releases if x["tag_name"] == tag), None)
        if "/git/ref/tags/" in path:
            return {"object": self.annotation or {"type": "commit", "sha": SHA}}
        if "/git/tags/" in path:
            return {"object": {"type": "commit", "sha": SHA}}
        raise AssertionError(path)

    def download_digest(self, url, expected_sha256=None):
        if self.offline:
            raise AssertionError("Unexpected download")
        self.calls.append(url)
        return DIGEST


class DependencyTests(unittest.TestCase):
    """Lock resolution: selection, pinning and failure semantics."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="fixture-dependencies-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = self.root / "dependencies.json"
        self.lock = self.manifest
        self.context = FakeContext()
        self.write_manifest({"library": SOURCE})

    def write_manifest(self, dependencies):
        previous = json.loads(self.manifest.read_text())["dependencies"] if self.manifest.exists() else {}
        entries = {}
        for name, spec in dependencies.items():
            entries[name] = dict(spec)
            if name in previous and "resolved" in previous[name]:
                entries[name]["resolved"] = previous[name]["resolved"]
        self.manifest.write_text(json.dumps({"schema": 2, "dependencies": entries}))

    def resolve(self, **kwargs):
        return deps.resolve(self.manifest, context=self.context, **kwargs)

    def test_source_transport_is_independent_of_builder_kind(self):
        self.write_manifest({"library": {**SOURCE, "artifact": "archive"}})
        lock = self.resolve()
        recipe = replace(fixtures.CONFIG.recipes[0], name="library", kind="git", patch="")
        locked = deps.locked_recipes(lock, [recipe])[0]
        self.assertEqual(locked.kind, "git")
        self.assertEqual(locked.source_kind, "archive")
        self.assertEqual(locked.sha256, DIGEST)

    def test_latest_means_highest_stable_release(self):
        record = self.resolve()["dependencies"]["library"]
        self.assertEqual(record["version"], "1.10.0")
        self.assertEqual(record["revision"], SHA)
        self.assertEqual(record["requested"], "latest")

    def test_locked_resolution_is_offline_and_does_not_rewrite(self):
        self.resolve()
        content = self.lock.read_bytes()
        timestamp = self.lock.stat().st_mtime_ns
        self.context.offline = True
        self.context.calls.clear()
        self.resolve()
        self.assertEqual(self.context.calls, [])
        self.assertEqual(self.lock.read_bytes(), content)
        self.assertEqual(self.lock.stat().st_mtime_ns, timestamp)

    def test_update_is_explicit_and_refreshes_latest(self):
        self.resolve()
        self.context.releases.append({"tag_name": "v2.0.0"})
        self.assertEqual(self.resolve()["dependencies"]["library"]["version"], "1.10.0")
        self.assertEqual(self.resolve(update=True)["dependencies"]["library"]["version"], "2.0.0")

    def test_explicit_version_wins_and_remains_locked(self):
        self.write_manifest({"library": {**SOURCE, "version": "1.9.0"}})
        result = self.resolve(versions={"library": "1.10.0"})
        self.assertEqual(result["dependencies"]["library"]["version"], "1.10.0")
        # Removing a command-line override restores an explicit manifest request.
        self.assertEqual(self.resolve()["dependencies"]["library"]["version"], "1.9.0")
        self.write_manifest({"library": SOURCE})
        self.resolve(versions={"library": "1.9.0"})
        self.context.offline = True
        self.assertEqual(self.resolve()["dependencies"]["library"]["version"], "1.9.0")

    def test_mixed_persistent_pins_and_latest(self):
        self.write_manifest({"fixed_one": {**SOURCE, "version": "1.9.0"},
                             "fixed_two": {**SOURCE, "version": "1.10.0"},
                             "rolling": SOURCE})
        self.context.releases.append({"tag_name": "v2.0.0"})
        selected = self.resolve(update=True)["dependencies"]
        self.assertEqual({name: record["version"] for name, record in selected.items()},
                         {"fixed_one": "1.9.0", "fixed_two": "1.10.0", "rolling": "2.0.0"})
        self.context.releases.append({"tag_name": "v3.0.0"})
        self.resolve(update=True, only=["rolling"])
        next_selection = deps.read_resolved(self.manifest)["dependencies"]
        self.assertEqual(next_selection["rolling"]["version"], "3.0.0")
        for name in ["fixed_one", "fixed_two"]:
            self.assertEqual(next_selection[name], selected[name])

    def test_selecting_the_already_locked_version_needs_no_network(self):
        self.resolve()
        self.context.offline = True
        record = self.resolve(versions={"library": "1.10.0"})["dependencies"]["library"]
        self.assertEqual(record["requested"], "1.10.0")

    def test_explicit_latest_policy_still_locks_normal_builds(self):
        self.write_manifest({"library": {**SOURCE, "version": "latest"}})
        self.resolve(versions={"library": "1.9.0"})
        self.context.offline = True
        self.assertEqual(self.resolve()["dependencies"]["library"]["version"], "1.9.0")

    def test_unknown_version_override_is_rejected(self):
        with self.assertRaisesRegex(deps.DependencyError, "declared dependency"):
            self.resolve(versions={"typo": "1.0"})
        self.assertNotIn("resolved", json.loads(self.manifest.read_text())["dependencies"]["library"])

    def test_explicit_branch_or_malformed_version_is_rejected_before_network(self):
        for version in ["main", "latest-mira", "2.0.0-", "2.0.0-rc..1", "../bad"]:
            with self.subTest(version=version), self.assertRaises(deps.DependencyError):
                self.resolve(versions={"library": version})
        self.assertEqual(self.context.calls, [])

    def test_explicit_downstream_and_prerelease_versions_remain_pinned(self):
        for version in ["1.70.0-mira.1", "2.0.0-rc1"]:
            with self.subTest(version=version):
                record = self.resolve(versions={"library": version})["dependencies"]["library"]
                self.assertEqual(record["version"], version)
                self.assertEqual(record["tag"], "v" + version)
                self.assertEqual(record["revision"], SHA)
                self.context.calls.clear()
                self.context.offline = True
                self.assertEqual(self.resolve(versions={"library": version})["dependencies"]["library"], record)
                self.assertEqual(self.context.calls, [])
                self.context.offline = False

    def test_downstream_versions_are_not_selected_automatically(self):
        self.context.releases.append({"tag_name": "v9.0.0-mira.1", "prerelease": False})
        self.assertEqual(self.resolve()["dependencies"]["library"]["version"], "1.10.0")

    def test_explicit_draft_release_is_rejected(self):
        with self.assertRaisesRegex(deps.DependencyError, "published"):
            self.resolve(versions={"library": "99.0.0"})

    def test_override_outside_selection_is_not_silently_ignored(self):
        self.write_manifest({"library": SOURCE, "other": SOURCE})
        with self.assertRaisesRegex(deps.DependencyError, "selected by --only"):
            self.resolve(only=["library"], versions={"other": "1.9.0"})
        self.assertNotIn("resolved", json.loads(self.manifest.read_text())["dependencies"]["library"])

    def test_malformed_locked_record_is_reported_without_mutation(self):
        self.lock.write_text(json.dumps({"schema": 2, "dependencies": {"library": {**SOURCE, "resolved": []}}}))
        before = self.lock.read_bytes()
        with self.assertRaisesRegex(deps.DependencyError, "invalid resolved"):
            self.resolve()
        self.assertEqual(self.lock.read_bytes(), before)

    def test_new_selection_offline_does_not_mutate_lock(self):
        self.resolve()
        old = self.lock.read_bytes()
        self.context.offline = True
        with self.assertRaisesRegex(deps.DependencyError, "matching resolved"):
            self.resolve(versions={"library": "1.9.0"})
        self.assertEqual(self.lock.read_bytes(), old)

    def test_resolution_failure_is_transactional(self):
        self.resolve()
        self.write_manifest({"library": SOURCE, "broken": {"provider": "unsupported"}})
        old = self.lock.read_bytes()
        with self.assertRaisesRegex(deps.DependencyError, "Unsupported"):
            self.resolve(update=True)
        self.assertEqual(self.lock.read_bytes(), old)

    def test_effective_lock_preserves_source_and_unselected_records(self):
        self.resolve()
        original = self.lock.read_bytes()
        effective = self.root / "build" / "effective.json"
        result = deps.resolve(self.manifest, effective,
                              versions={"library": "1.9.0"}, context=self.context)
        self.assertEqual(self.lock.read_bytes(), original)
        self.assertEqual(result["dependencies"]["library"]["version"], "1.9.0")

    def test_lock_hash_revision_and_url_are_validated(self):
        record = self.resolve()["dependencies"]["library"]
        for changed in [{"revision": "main"}, {"url": "file:///tmp/private"},
                        {"source": {**SOURCE, "artifact": "archive"}, "sha256": "bad"}]:
            with self.subTest(changed=changed), self.assertRaises(deps.DependencyError):
                deps.validate_record({**record, **changed})

    def test_tag_only_projects_and_annotated_tags(self):
        self.context.releases = []
        self.context.tags = [{"name": "main"}, {"name": "v2.2.0"}, {"name": "v3.0.0-beta"}]
        self.context.annotation = {"type": "tag", "sha": "c" * 40}
        record = self.resolve()["dependencies"]["library"]
        self.assertEqual(record["tag"], "v2.2.0")
        self.assertEqual(record["revision"], SHA)

    def test_tag_separator_and_asset_templates(self):
        source = {**SOURCE, "tag_prefix": "curl-", "tag_separator": "_",
                  "artifact": "release-asset", "asset": "curl-{version}.tar.xz"}
        self.write_manifest({"library": source})
        self.context.releases = [{"tag_name": "curl-8_22_0", "assets": [
            {"name": "curl-8.22.0.tar.xz", "browser_download_url": "https://example.invalid/curl.tar.xz",
             "digest": "sha256:" + DIGEST}]}]
        record = self.resolve(versions={"library": "8.22.0"})["dependencies"]["library"]
        self.assertEqual(record["version"], "8.22.0")
        self.assertEqual(record["tag"], "curl-8_22_0")
        self.assertEqual(record["sha256"], DIGEST)
        self.assertEqual(record["checksum_source"], "github-release-asset")

    def test_request_fingerprint_is_portable_and_detects_edits(self):
        self.assertEqual(deps.request_hash(SOURCE),
                         "1044731845c62e4a50b0251f6039d78e6ce1233cdcac02a2b086809fb6526f54")
        self.resolve()
        value = json.loads(self.manifest.read_text())
        self.assertNotIn("source", value["dependencies"]["library"]["resolved"])
        value["dependencies"]["library"]["repo"] = "example/another"
        self.manifest.write_text(json.dumps(value))
        with self.assertRaisesRegex(deps.DependencyError, "declaration changed"):
            deps.read_resolved(self.manifest)
        self.context.offline = True
        with self.assertRaisesRegex(deps.DependencyError, "matching resolved"):
            self.resolve()

    def test_effective_output_tracks_base_changes_and_keeps_other_overrides(self):
        self.write_manifest({"library": SOURCE, "other": SOURCE})
        self.resolve()
        effective = self.root / "build" / "effective.json"
        deps.resolve(self.manifest, effective, versions={"library": "1.9.0"},
                     only=["library"], context=self.context)
        deps.resolve(self.manifest, effective, versions={"other": "1.9.0"},
                     only=["other"], context=self.context)
        self.assertEqual(deps.read_resolved(effective)["dependencies"]["library"]["version"], "1.9.0")
        before = self.manifest.read_bytes()
        self.context.offline = True
        deps.resolve(self.manifest, effective, versions={"library": "1.9.0"},
                     only=["library"], context=self.context)
        self.assertEqual(self.manifest.read_bytes(), before)
        # Deleting a persisted result invalidates the isolated output too.
        value = json.loads(self.manifest.read_text())
        del value["dependencies"]["library"]["resolved"]
        self.manifest.write_text(json.dumps(value))
        with self.assertRaisesRegex(deps.DependencyError, "matching resolved"):
            deps.resolve(self.manifest, effective, only=["library"], context=self.context)
        self.context.offline = False
        self.context.releases.append({"tag_name": "v3.0.0"})
        selected = deps.resolve(self.manifest, effective, only=["library"], context=self.context)
        self.assertEqual(selected["dependencies"]["library"]["version"], "3.0.0")

    def test_read_only_selection_allows_an_unresolved_other_entry(self):
        self.write_manifest({"library": SOURCE, "other": SOURCE})
        self.resolve(only=["library"])
        record = deps.read_resolved(self.manifest, only=["library"])["dependencies"]["library"]
        self.assertEqual(record["version"], "1.10.0")
        with self.assertRaisesRegex(deps.DependencyError, "Missing or invalid"):
            deps.read_resolved(self.manifest)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.prefix = self.root / 'prefix'

    def test_license_collection_runs_post_install_hook(self):
        # Generic hook mechanism: the fixture's libbar carries a post_install
        # hook (project-specific logic like SPDX extraction lives in the
        # project's recipes, not in this package).
        source = self.root / 'source'
        source.mkdir(parents=True)
        (source / 'LICENSE').write_text('selected MIT license')
        (source / 'NOTICE').write_text('selected upstream attribution')
        dep = next(dep for dep in fixtures.RECIPES if dep.name == 'libbar')
        copied = deps.copy_licenses(self.prefix, source, dep)
        out = self.prefix / 'share/licenses/libbar'
        self.assertEqual((out / 'LICENSE').read_text(), 'selected MIT license')
        self.assertEqual((out / 'NOTICE').read_text(), 'selected upstream attribution')
        self.assertIn('HOOK.txt', copied)
        self.assertEqual((out / 'HOOK.txt').read_text(), 'hook ran\n')

    def install(self, identity='one', content='1.0', selected=None):
        selected = selected or {'library'}
        with deps.installation(self.prefix, identity, selected, {'lock': identity}) as (done, state):
            if done is not None:
                (self.prefix / 'library.a').write_text(content)
                done.update(selected)
                state['completed'] = sorted(done)
                state['version'] = content

    def test_exact_identity_reuses_verified_installation(self):
        self.install()
        with deps.installation(self.prefix, 'one', {'library'}, {}) as (done, state):
            self.assertIsNone(done)
            self.assertEqual(state['version'], '1.0')
        self.assertFalse(list(self.root.glob('prefix-backup-*')))

    def test_changed_version_rebuilds_and_leaves_no_backup(self):
        self.install()
        with deps.installation(self.prefix, 'two', {'library'}, {'lock': 'two'}) as (done, state):
            self.assertFalse((self.prefix / 'library.a').exists())
            (self.prefix / 'library.a').write_text('2.0')
            state['completed'] = ['library']
        self.assertEqual((self.prefix / 'library.a').read_text(), '2.0')
        self.assertEqual(deps.read_state(self.prefix)['lock'], 'two')
        # A committed transaction deletes its rollback target.
        self.assertEqual(list(self.root.glob('prefix*')), [self.prefix])

    def test_failed_upgrade_restores_original_prefix_and_preserves_failure(self):
        self.install()
        before = (self.prefix / fixtures.CONFIG.manifest_path).read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'build failed'):
            with deps.installation(self.prefix, 'two', {'library'}, {}) as (done, state):
                (self.prefix / 'partial').write_text('preserve me')
                raise RuntimeError('build failed')
        self.assertEqual((self.prefix / fixtures.CONFIG.manifest_path).read_bytes(), before)
        self.assertEqual((self.prefix / 'library.a').read_text(), '1.0')
        failed, = self.root.glob('prefix-failed-*')
        self.assertTrue((failed / 'partial').exists())
        deps.read_state(self.prefix)

    def test_partial_install_completion_preserves_verified_existing_components(self):
        self.install()
        with deps.installation(self.prefix, 'one', {'library', 'headers'}, {}) as (done, state):
            self.assertEqual(done, {'library'})
            self.assertEqual((self.prefix / 'library.a').read_text(), '1.0')
            (self.prefix / 'header.h').write_text('header')
            state['completed'] = ['library', 'headers']
        self.assertEqual(set(deps.read_state(self.prefix)['completed']), {'library', 'headers'})

    def test_modified_installed_file_is_never_overwritten(self):
        self.install()
        (self.prefix / 'library.a').write_text('local edit')
        with self.assertRaisesRegex(ValueError, 'local modifications'):
            self.install('two', '2.0')
        self.assertEqual((self.prefix / 'library.a').read_text(), 'local edit')
        self.assertEqual(list(self.root.glob('prefix*')), [self.prefix])

    def test_missing_installed_file_is_not_blessed_as_new_version(self):
        self.install()
        (self.prefix / 'library.a').unlink()
        with self.assertRaisesRegex(ValueError, 'missing files'):
            self.install('two', '2.0')
        self.assertEqual(json.loads((self.prefix / fixtures.CONFIG.manifest_path).read_text())['version'], '1.0')

    def test_added_local_file_is_preserved(self):
        self.install()
        (self.prefix / 'notes.txt').write_text('local notes')
        with self.assertRaises(ValueError):
            self.install('two')
        self.assertEqual((self.prefix / 'notes.txt').read_text(), 'local notes')

    def test_legacy_artifacts_are_replaced_not_relabelled(self):
        self.prefix.mkdir()
        (self.prefix / 'library.a').write_text('unknown old binary')
        self.install('new', 'new binary')
        self.assertEqual((self.prefix / 'library.a').read_text(), 'new binary')
        self.assertEqual(list(self.root.glob('prefix*')), [self.prefix])  # no leftovers

    def test_stale_sources_are_rejected_before_cmake_can_use_prefix(self):
        self.install()
        state = deps.read_state(self.prefix)
        state['source_dir'] = str(self.root / 'sources')
        state['context'] = {'environment': {'CC': '', 'CXX': ''}}
        state['components'] = {'library': {'identity': 'stale', 'files': ['library.a']}}
        state['files'] = {'library.a': {}}
        deps.write_state(self.prefix, state)
        with patch.object(deps, 'recipe_digest', return_value='recipe'), \
                patch.object(deps.sources, 'identities', return_value={'library': {}}):
            with self.assertRaisesRegex(ValueError, 'changed since the last dependency build'):
                deps.verify_prefix(self.prefix, {'new': 'lock'}, [], {'library'})

    def test_manifest_write_failure_also_restores_old_installation(self):
        self.install()
        original = (self.prefix / fixtures.CONFIG.manifest_path).read_bytes()
        with patch.object(deps, 'write_state', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.install('two', '2.0')
        self.assertEqual((self.prefix / fixtures.CONFIG.manifest_path).read_bytes(), original)
        self.assertEqual((self.prefix / 'library.a').read_text(), '1.0')

    def test_concurrent_install_is_rejected(self):
        with deps.prefix_lock(self.prefix):
            with self.assertRaisesRegex(ValueError, 'already active'):
                with deps.prefix_lock(self.prefix):
                    self.fail('second writer entered')
        self.assertFalse((self.root / 'prefix.install-lock').exists())

    def test_prefix_symlink_is_rejected(self):
        destination = self.root / 'real'
        destination.mkdir()
        try:
            self.prefix.symlink_to(destination, target_is_directory=True)
        except OSError:
            self.skipTest('symlink privilege unavailable')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.install()


if __name__ == '__main__':
    unittest.main()


class SourceTests(unittest.TestCase):
    """Archives, git workspaces, patches and recipe identity."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def dep(self, data, url='https://example.invalid/source.tar.gz'):
        return replace(fixtures.RECIPES[0], version='1.0', url=url,
                       sha256=deps.hashlib.sha256(data).hexdigest())

    def test_context_download_needs_no_active_build_project(self):
        response = io.BytesIO(b'public resolution')
        response.url = 'https://example.invalid/metadata'
        with patch.object(deps, '_ACTIVE', None), \
                patch.object(deps.urllib.request, 'urlopen', return_value=response) as network:
            self.assertEqual(deps.Context(self.root).get_bytes(response.url), b'public resolution')
        self.assertEqual(network.call_args.args[0].get_header('User-agent'), 'forgedeps')

    def test_exact_offline_content_cache_is_reused(self):
        data = b'locked archive'
        dep = self.dep(data)
        (self.root / dep.sha256).write_bytes(data)
        # Imported helpers may use a Windows redirected console, unlike the
        # entry point, which explicitly configures UTF-8. Progress must work.
        output = io.BytesIO()
        with io.TextIOWrapper(output, encoding='cp1252') as console, patch.object(deps.sys, 'stdout', console):
            archive = deps.download(self.root, dep, True)
            console.flush()
            self.assertIn(b'Reusing verified archive', output.getvalue())
        self.assertEqual(archive.read_bytes(), data)

    def test_changed_hash_cannot_reuse_same_archive_basename(self):
        old = self.dep(b'old')
        (self.root / old.archive_name).write_bytes(b'old')
        with self.assertRaisesRegex(ValueError, 'Offline cache missing'):
            deps.download(self.root, self.dep(b'new'), True)
        self.assertEqual((self.root / old.archive_name).read_bytes(), b'old')

    def test_broken_archive_symlink_never_writes_outside_cache(self):
        data = b'archive'
        dep = self.dep(data)
        (self.root / dep.sha256).write_bytes(data)
        outside = self.root / 'outside'
        archive = self.root / (dep.sha256 + '-' + dep.archive_name)
        try:
            archive.symlink_to(outside)
        except OSError:
            self.skipTest('symlink privilege unavailable')
        with self.assertRaisesRegex(ValueError, 'symbolic-link'):
            deps.download(self.root, dep, True)
        self.assertFalse(outside.exists())
        self.assertTrue(archive.is_symlink())

    def test_unrelated_or_shared_library_does_not_satisfy_static_artifact(self):
        lib = self.root / 'lib'
        lib.mkdir()
        for name in ('libfixture_other.a', 'libfixture.dylib', 'fixture.dll'):
            (lib / name).write_text('wrong binary')
        self.assertFalse(deps.artifact_present(self.root, 'lib/libfixture.a'))
        (lib / 'fixture.lib').write_text('correct static spelling')
        self.assertTrue(deps.artifact_present(self.root, 'lib/libfixture.a'))


    def test_corrupt_content_cache_is_rejected_and_preserved(self):
        dep = self.dep(b'original')
        path = self.root / dep.sha256
        path.write_bytes(b'local edit')
        with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
            deps.download(self.root, dep, True)
        self.assertEqual(path.read_bytes(), b'local edit')

    def git_fixture(self, name='Mira'):
        source = self.root / (name + '-origin')
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(source)], check=True)
        (source / 'file').write_text('original')
        subprocess.run(['git', '-C', str(source), 'add', 'file'], check=True)
        subprocess.run(['git', '-C', str(source), '-c', 'user.name=Fixture', '-c',
                        'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture'], check=True)
        return source, subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'],
                                                text=True).strip()

    def test_legacy_git_cache_is_copied_only_when_clean_at_the_revision(self):
        source, revision = self.git_fixture()
        dep = replace(fixtures.RECIPES[-1], version='1', revision=revision)
        legacy = self.root / 'git' / dep.name / revision
        legacy.parent.mkdir(parents=True)
        deps.shutil.copytree(source, legacy)
        destination = self.root / 'deps' / dep.name
        destination.parent.mkdir(parents=True)
        deps.fetch_git(self.root, destination, dep, True)
        self.assertTrue((destination / 'file').is_file())
        self.assertTrue(legacy.is_dir())  # the legacy cache is never moved
        # A dirty legacy cache is not migrated; offline then fails clearly.
        (legacy / 'local edit').write_text('keep')
        with self.assertRaisesRegex(ValueError, 'Offline source missing'):
            deps.fetch_git(self.root, self.root / 'deps' / 'another', dep, True)

    def test_new_version_never_receives_old_patch(self):
        from dataclasses import replace
        patched = replace(fixtures.RECIPES[0], patch="libfoo-1.0.patch")
        recipes = (patched,) + fixtures.RECIPES[1:]
        config = fixtures.make_config(recipes=recipes,
                                      patch_versions={'libfoo': {'1.0'}})
        records = {dep.name: {'version': '1.0',
                              'requested': 'latest', 'url': 'https://example.invalid/a',
                              'sha256': 'a' * 64, 'revision': 'b' * 40,
                              'source': {'artifact': 'git' if dep.kind == 'git' else 'archive'}}
                   for dep in recipes}
        # whitelisted version passes
        locked = deps.locked_recipes({'dependencies': records}, recipes, config)
        self.assertEqual(locked[0].version, '1.0')
        # new version without review fails
        records['libfoo']['version'] = '9.9'
        with self.assertRaisesRegex(ValueError, 'explicitly reviewed'):
            deps.locked_recipes({'dependencies': records}, recipes, config)

    def test_compiler_alias_banner_is_accepted_but_version_change_is_not(self):
        cc = {'path': '/usr/bin/cc', 'arguments': [], 'binary_sha256': 'same', 'version': 'cc (GCC) 16.0\nmore'}
        gcc = {**cc, 'path': '/usr/bin/gcc', 'version': 'gcc (GCC) 16.0\nmore'}
        self.assertTrue(deps.same_compiler(cc, gcc))
        self.assertFalse(deps.same_compiler(cc, {**gcc, 'version': 'gcc (GCC) 17.0\nmore'}))

    def test_abi_flags_and_toolchain_changes_are_rejected(self):
        context = {'environment': {'CXXFLAGS': '-DOLD_ABI'}}
        with patch.dict(deps.os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'CXXFLAGS'):
                deps.verify_environment(context)
            deps.verify_environment({'environment': {'CL': '/utf-8'}})
            toolchain = self.root / 'toolchain.cmake'
            toolchain.write_text('old')
            context = {'environment': {'CMAKE_TOOLCHAIN_FILE': str(toolchain),
                       'toolchain_sha256': deps.hashlib.sha256(b'old').hexdigest()}}
            deps.verify_environment(context, str(toolchain))
            toolchain.write_text('new')
            with self.assertRaisesRegex(ValueError, 'toolchain changed'):
                deps.verify_environment(context, str(toolchain))

    def test_msvc_generators_keep_platform_and_configuration_separate(self):
        for generator in ('Visual Studio 18 2026', 'Ninja', 'Ninja Multi-Config', 'NMake Makefiles'):
            with self.subTest(generator=generator), patch.dict(deps.os.environ, {'CMAKE_GENERATOR': generator}, clear=True), \
                    patch.object(deps.sys, 'platform', 'win32'), \
                    patch.object(deps, 'windows_toolchain', return_value='msvc'), \
                    patch.object(deps, 'run') as run:
                deps.build_cmake(self.root, self.root / 'build', self.root / 'prefix', 1, fixtures.RECIPES[0], [])
                commands = [call.args[0] for call in run.call_args_list]
                self.assertEqual('-A' in commands[0], generator.startswith('Visual Studio'))
                self.assertEqual(commands[1][-2:], ['--config', 'Release'])
                self.assertEqual(commands[2][-2:], ['--config', 'Release'])

    def test_component_build_forwards_debug_to_cmake(self):
        dependency = replace(fixtures.RECIPES[0], artifacts=(), post_build=None)
        run_root = self.root / 'runs'
        run_root.mkdir()
        with patch.object(deps.sources, 'snapshot', return_value={}), \
                patch.object(deps, 'prepare_source'), patch.object(deps, 'copy_licenses', return_value=[]), \
                patch.object(deps, 'build_cmake') as build:
            deps.install_component(dependency, self.root / 'source', run_root,
                                   self.root / 'prefix', 2, [], config='Debug')
        self.assertEqual(build.call_args.args[-1], 'Debug')

    def test_hook_source_changes_invalidate_recipe_identity(self):
        dependency = fixtures.RECIPES[1]
        expected = deps.recipe_digest([dependency])
        original = deps.inspect.getsource
        with patch.object(deps.inspect, 'getsource', side_effect=lambda function:
                          original(function) + ('\n# changed' if function is dependency.post_build else '')):
            self.assertNotEqual(deps.recipe_digest([dependency]), expected)

    def test_opaque_hooks_have_an_actionable_error(self):
        for hook in (print, eval('lambda prefix, source, dep: None')):
            with self.subTest(hook=hook), self.assertRaisesRegex(ValueError, 'source-readable Python function'):
                deps.recipe_digest([replace(fixtures.RECIPES[0], post_build=hook)])

    def test_selected_gcc_is_not_overridden_by_msvc_on_path(self):
        with patch.dict(deps.os.environ, {'CC': '/toolchain/gcc'}, clear=True), \
                patch.object(deps.shutil, 'which', return_value='/unrelated/cl'):
            self.assertEqual(deps.windows_toolchain(), 'mingw')
        with patch.dict(deps.os.environ, {'CC': '"/toolchain with spaces/cl.exe"'}, clear=True):
            self.assertEqual(deps.windows_toolchain(), 'msvc')






    def test_compiler_path_with_spaces_is_one_executable(self):
        executable = self.root / 'compiler with spaces'
        executable.write_text('fixture')
        result = subprocess.CompletedProcess([], 0, stdout='compiler 1.0')
        with patch.object(deps.shutil, 'which', return_value=str(executable)), \
                patch.object(deps.subprocess, 'run', return_value=result) as run:
            record = deps.compiler(str(executable))
        self.assertEqual(run.call_args.args[0], [str(executable.resolve()), '--version'])
        self.assertEqual(record['path'], str(executable.resolve()))

    def test_verify_prefix_preserves_separate_compiler_arguments(self):
        executable = self.root / 'compiler with spaces'
        executable.write_text('fixture compiler')
        result = subprocess.CompletedProcess([], 0, stdout='compiler 1.0')
        quote = subprocess.list2cmdline if os.name == 'nt' else shlex.join
        arguments = ['/DTEST=1', 'argument with spaces'] if os.name == 'nt' else ['-arch', 'arm64', '-I/include with spaces']
        with patch.object(deps.shutil, 'which', return_value=str(executable)), \
                patch.object(deps.subprocess, 'run', return_value=result):
            recorded = deps.compiler(quote([str(executable), *arguments]))
            state = {'resolution': deps.fingerprint({}), 'recipe': 'recipe', 'completed': ['library'],
                     'context': {'c': recorded, 'cxx': recorded}, 'source_dir': str(self.root / 'sources'),
                     'components': {'library': {'identity': 'stale', 'files': []}}}
            with patch.object(deps, 'read_state', return_value=state), \
                    patch.object(deps.sources, 'identities', return_value={'library': {}}), \
                    patch.object(deps, 'verify_environment'), patch.object(deps, 'verify_location'), \
                    patch.object(deps, 'recipe_digest', return_value='recipe'), \
                    patch.object(deps, 'reusable_components', return_value={'library'}):
                deps.verify_prefix(self.root, {}, [], {'library'}, str(executable), str(executable),
                                   c_arg1=quote(arguments), cxx_arg1=quote(arguments))
                with self.assertRaisesRegex(ValueError, 'compiler differs'):
                    deps.verify_prefix(self.root, {}, [], {'library'}, str(executable),
                                       c_arg1=quote([*arguments, '-different-abi']))

    def test_native_windows_and_msys_host_spellings_are_equivalent(self):
        context = {'prefix': self.root.as_posix(), 'platform': 'Windows', 'machine': 'AMD64'}
        with patch.object(deps.platform, 'system', return_value='Windows'), \
                patch.object(deps.platform, 'machine', return_value='x86_64'):
            deps.verify_location(context, self.root)
            with self.assertRaisesRegex(ValueError, 'location/platform changed'):
                deps.verify_location(context, self.root / 'another-prefix')
            with self.assertRaisesRegex(ValueError, 'location/platform changed'):
                deps.verify_location({**context, 'machine': 'x86'}, self.root)

    def test_native_msvc_path_alias_and_utf8_flag_preserve_identity(self):
        if os.name != 'nt' or not deps.shutil.which('cl'):
            self.skipTest('Requires native Windows MSVC')
        executable = Path(deps.shutil.which('cl')).resolve()
        with patch.dict(os.environ, {'CL': (os.environ.get('CL', '') + ' /utf-8').strip()}):
            recorded = deps.compiler('cl')
        actual = deps.compiler(executable.as_posix())
        self.assertTrue(deps.same_compiler(recorded, actual),
                        f'recorded={recorded!r}, actual={actual!r}')

    def test_compiler_and_patch_changes_alter_identity(self):
        baseline = {'lock': 'same', 'compiler': 'A', 'patch': 'one'}
        self.assertNotEqual(deps.fingerprint(baseline), deps.fingerprint({**baseline, 'compiler': 'B'}))
        dep = replace(next(dep for dep in fixtures.RECIPES if dep.name == 'libfoo'), version='1', url='url', sha256='a' * 64,
                        patch='libfoo-1.patch')
        with tempfile.TemporaryDirectory() as patches:
            cfg = fixtures.make_config(patches_dir=Path(patches))
            deps.init_project(cfg)
            try:
                with patch.object(deps, 'sha256', return_value='one'):
                    old = deps.recipe_digest([dep])
                with patch.object(deps, 'sha256', return_value='two'):
                    new = deps.recipe_digest([dep])
                self.assertNotEqual(old, new)
            finally:
                deps.init_project(fixtures.CONFIG)

    def test_recipe_identity_tracks_build_helpers_only(self):
        dependency = fixtures.RECIPES[0]
        expected = deps.recipe_digest([dependency])
        original = deps.inspect.getsource
        for helper in (deps.cmake_arguments, deps.build_environment, deps.prepare_source,
                       deps.windows_toolchain, deps.install_component, deps.build_context):
            with self.subTest(helper=helper.__name__), patch.object(deps.inspect, 'getsource',
                    side_effect=lambda function: original(function) + ('\n# changed' if function is helper else '')):
                self.assertNotEqual(deps.recipe_digest([dependency]), expected)

    def test_source_population_helpers_do_not_rebuild_unchanged_components(self):
        """How a workspace was created is not part of the recipe."""
        dependency = replace(fixtures.RECIPES[-1], version='1', url='https://example.invalid/m.git', revision='a' * 40)
        expected = deps.recipe_digest([dependency])
        original = deps.inspect.getsource
        for helper in (deps.download, deps.extract, deps.fetch_git):
            with self.subTest(helper=helper.__name__), patch.object(deps.inspect, 'getsource',
                    side_effect=lambda function: original(function) + ('\n# changed' if function is helper else '')):
                self.assertEqual(deps.recipe_digest([dependency]), expected)


class BuilderIntegrationTests(unittest.TestCase):
    """Recipe selection, component identity and the real install transaction."""

    def test_default_configure_returns_recipes_unchanged(self):
        # Without a project policy, effective_configure is the identity.
        config = fixtures.make_config()
        recipes = config.effective_configure()(config.recipes, 'runtime', 'auto')
        self.assertEqual([d.name for d in recipes],
                         [d.name for d in fixtures.RECIPES])

    def test_custom_configure_policy_is_honored(self):
        # Projects inject their own selection policy (e.g. profile filtering).
        def policy(recipes, profile, tls_backend):
            return [r for r in recipes if not (r.name == 'libgit' and profile == 'runtime')]
        config = fixtures.make_config(configure=policy)
        runtime = config.effective_configure()(config.recipes, 'runtime', 'auto')
        self.assertNotIn('libgit', {d.name for d in runtime})
        tests = config.effective_configure()(config.recipes, 'tests', 'auto')
        self.assertIn('libgit', {d.name for d in tests})

    def test_git_recipe_locks_revision(self):
        git = next(dep for dep in fixtures.RECIPES if dep.name == 'libgit')
        self.assertEqual(git.kind, 'git')
        records = {dep.name: {'version': '1.0',
                              'requested': 'latest', 'url': 'https://example.invalid/a',
                              'sha256': 'a' * 64, 'revision': 'b' * 40,
                              'source': {'artifact': 'git' if dep.kind == 'git' else 'archive'}}
                   for dep in fixtures.RECIPES}
        locked = {dep.name: dep for dep in deps.locked_recipes({'dependencies': records},
                                                               fixtures.RECIPES, fixtures.CONFIG)}
        self.assertEqual(locked['libgit'].version, '1.0')
        self.assertEqual(locked['libgit'].revision, 'b' * 40)

    def test_component_changes_invalidate_transitive_consumers_only(self):
        recipes = [replace(fixtures.RECIPES[0], name='base', requires=()),
                   replace(fixtures.RECIPES[0], name='consumer', requires=('base',)),
                   replace(fixtures.RECIPES[0], name='indirect', requires=('consumer',)),
                   replace(fixtures.RECIPES[0], name='other', requires=())]
        original = deps.component_identities(recipes, {'compiler': 'one'})
        upgraded = deps.component_identities([replace(recipes[0], version='2'), *recipes[1:]], {'compiler': 'one'})
        self.assertNotEqual(original['base'], upgraded['base'])
        self.assertNotEqual(original['consumer'], upgraded['consumer'])
        self.assertNotEqual(original['indirect'], upgraded['indirect'])
        self.assertEqual(original['other'], upgraded['other'])
        self.assertEqual(deps.dependency_selection('indirect', recipes), {'base', 'consumer', 'indirect'})
        switched = deps.component_identities(recipes, {'compiler': 'two'})
        self.assertTrue(all(original[name] != switched[name] for name in original))

    def test_partial_ownership_receipts_are_never_reused(self):
        previous = {'completed': ['one', 'two'], 'files': {'one.h': {}, 'two.h': {}},
                    'components': {'one': {'identity': '1', 'files': ['one.h']},
                                   'two': {'identity': '2', 'files': ['two.h']}}}
        self.assertEqual(deps.reusable_components(previous, {'one': '1', 'two': '2'}), {'one', 'two'})
        self.assertEqual(deps.reusable_components(previous, {'one': 'new', 'two': '2'}), {'two'})
        previous['components']['one']['files'].append('two.h')
        self.assertEqual(deps.reusable_components(previous, {'one': '1', 'two': '2'}), set())

    def test_component_identity_ignores_compiler_spelling_and_generator_only(self):
        compiler = {'path': '/tools/cc', 'arguments': [], 'binary_sha256': 'binary',
                    'version': 'compiler 1.0'}
        context = {'c': compiler, 'cxx': compiler, 'environment': {
            'CC': '', 'CXX': '', 'CMAKE_GENERATOR': 'Unix Makefiles',
            'CMAKE_GENERATOR_PLATFORM': '', 'CMAKE_GENERATOR_TOOLSET': '',
            'CFLAGS': '', 'CXXFLAGS': '', 'CMAKE_TOOLCHAIN_FILE': ''}}
        recipes = [fixtures.RECIPES[0]]
        original = deps.component_identities(recipes, context)
        canonical = {**context, 'environment': {**context['environment'],
                     'CC': '/tools/cc', 'CXX': '/tools/cc', 'CMAKE_GENERATOR': 'Ninja'}}
        self.assertEqual(deps.component_identities(recipes, canonical), original)
        self.assertEqual(context['environment']['CC'], '')
        for key in ('CMAKE_GENERATOR_PLATFORM', 'CMAKE_GENERATOR_TOOLSET',
                    'CFLAGS', 'CXXFLAGS', 'CMAKE_TOOLCHAIN_FILE'):
            changed = {**canonical, 'environment': {**canonical['environment'], key: 'different'}}
            with self.subTest(environment=key):
                self.assertNotEqual(deps.component_identities(recipes, changed), original)
        for key, value in (('path', '/other/cc'), ('binary_sha256', 'different'),
                           ('version', 'compiler 2.0'), ('arguments', ['-m32'])):
            changed = {**canonical, 'c': {**compiler, key: value}}
            with self.subTest(compiler=key):
                self.assertNotEqual(deps.component_identities(recipes, changed), original)

    def test_old_component_identity_cannot_short_circuit_transaction_rebuild(self):
        with tempfile.TemporaryDirectory(prefix='fixture-receipt-migration-') as temporary:
            work = Path(temporary).resolve()
            prefix = work / 'prefix'
            dependency = next(dep for dep in fixtures.RECIPES if dep.name == 'libfoo')
            compiler = {'path': '/tools/cc', 'arguments': []}
            context = {'c': compiler, 'cxx': compiler,
                       'environment': {'CC': '', 'CXX': '', 'CMAKE_TOOLCHAIN_FILE': ''}}
            resolution = {'fixture': 'unchanged'}
            source_dir = work / 'sources'
            source = source_dir / dependency.name
            source.mkdir(parents=True)
            (source / 'header.h').write_text('fixture')
            metadata = {'resolution': deps.fingerprint(resolution), 'recipe': 'same-recipe',
                        'context': context, 'generator': 'tools/build.py'}
            # Emulate an installation produced before compiler spelling was
            # normalized, using the old whole-prefix transaction identity.
            old_component = deps.fingerprint({'recipe': 'same-recipe',
                                             'context': context, 'requires': {}})
            record = {'name': 'libfoo', 'version': 'fixture'}
            with deps.installation(prefix, deps.fingerprint(metadata), {'libfoo'}, metadata) as (done, state):
                (prefix / 'library.a').write_text('old build')
                state.update(completed=['libfoo'], dependencies=[record], components={
                    'json': {'identity': old_component, 'files': ['library.a']}})

            def install(*args, **kwargs):
                (prefix / 'library.a').write_text('rebuilt')
                return record

            cfg = fixtures.make_config(recipes=(dependency,))
            with patch.object(deps, 'resolve'), \
                    patch.object(deps, 'read_resolved', return_value=resolution), \
                    patch.object(deps, 'locked_recipes', return_value=[dependency]), \
                    patch.object(deps, 'build_environment', return_value=context), \
                    patch.object(deps, 'recipe_digest', return_value='same-recipe'), \
                    patch.object(deps, 'prepare_workspace', return_value=source), \
                    patch.object(deps, 'install_component', side_effect=install) as rebuilt:
                deps.install(work / 'dependencies.json', work, prefix, source_dir,
                             config=cfg, only='libfoo', jobs=1)
                rebuilt.assert_called_once()
                current = deps.component_identities([dependency], context,
                                                    deps.sources.identities(source_dir, [dependency]))
                self.assertEqual(deps.reusable_components(deps.read_state(prefix), current), {'libfoo'})
            self.assertEqual((prefix / 'library.a').read_text(), 'rebuilt')
            self.assertEqual(list(work.glob('prefix*')), [prefix])  # committed: no backups

    def test_windows_compilers_cannot_mix_msvc_and_mingw(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(deps.os.environ, {}, clear=True), \
                patch.object(deps.sys, 'platform', 'win32'), \
                patch.object(deps.shutil, 'which', return_value='available'), \
                patch.object(deps, 'build_context', return_value={
                    'c': {'path': '/tools/cl.exe'}, 'cxx': {'path': '/tools/g++.exe'}}):
            with self.assertRaisesRegex(ValueError, 'same Windows toolchain'):
                deps.build_environment(Path(temporary))

class OfflineTransactionTests(unittest.TestCase):
    """A real offline install/upgrade/rollback, including the doctest UTF-8 fix."""

    def setUp(self):
        if not deps.shutil.which('cmake'):
            self.skipTest('CMake unavailable')
        temporary = tempfile.TemporaryDirectory(prefix='fixture-deps-fixture-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / 'work'
        self.prefix = self.work / 'prefix'
        (self.work / 'cache').mkdir(parents=True)
        self.sources = self.root / 'sources'
        self.specs = {'schema': 2, 'dependencies': {dep.name: {'provider': 'github',
                     'repo': 'example/fixture', 'artifact': 'git' if dep.kind == 'git' else 'archive'}
                     for dep in fixtures.RECIPES}}
        self.records = {dep.name: {'version': next(iter(fixtures.CONFIG.patch_versions.get(dep.name, {'1.0'}))),
                                   'requested': 'latest', 'url': 'https://example.invalid/archive.tar.gz',
                                   'sha256': 'a' * 64, 'revision': 'b' * 40,
                                   'source': self.specs['dependencies'][dep.name]}
                        for dep in fixtures.RECIPES}
        self.file = self.root / 'dependencies.json'

        self.archive('1.0')

    def archive(self, version, fail=False):
        contents = {'LICENSE': 'Fixture license', 'json.hpp': version,
                    'include/nlohmann/notice.hpp':
                        '// SPDX-FileCopyrightText: Fixture Author\n// SPDX-License-Identifier: MIT\n',
                    'CMakeLists.txt': ('cmake_minimum_required(VERSION 3.20)\n'
                                       'project(Fixture C CXX)\n' +
                                       ('message(FATAL_ERROR "fixture build failure")\n' if fail else
                                        'file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/configuration.txt" "${CMAKE_BUILD_TYPE};${FIXTURE_OPTION}")\n'
                                        'install(FILES "${CMAKE_CURRENT_BINARY_DIR}/configuration.txt" DESTINATION share)\n'
                                        'install(FILES json.hpp DESTINATION include/nlohmann)\n' +
                                        ('install(FILES obsolete.hpp DESTINATION include/nlohmann)\n'
                                         if version == '1.0' else '')))}
        if version == '1.0':
            contents['obsolete.hpp'] = 'removed by the upgrade'
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w:gz') as package:
            for name, text in contents.items():
                encoded = text.encode()
                info = tarfile.TarInfo('source/' + name)
                info.size = len(encoded)
                package.addfile(info, io.BytesIO(encoded))
        digest = deps.hashlib.sha256(data.getvalue()).hexdigest()
        (self.work / 'cache' / digest).write_bytes(data.getvalue())
        self.records['libfoo'].update(version=version, sha256=digest)
        entries = {}
        for name, record in self.records.items():
            spec = self.specs['dependencies'][name]
            resolved = {key: value for key, value in record.items() if key != 'source'}
            resolved['request_hash'] = deps.request_hash(spec)
            entries[name] = {**spec, 'resolved': resolved}
        self.file.write_text(json.dumps({'schema': 2, 'dependencies': entries}))

    def invoke(self, *, versions=(), profile='tests', only='libfoo,libbar,libgit', verify=False,
               environment=None, build_config='Release', cmake_options=()):
        output = io.StringIO()
        env = dict(os.environ)
        if environment:
            env.update(environment)
        with patch.dict(os.environ, env, clear=False), redirect_stdout(output):
            cfg = fixtures.make_config()
            if verify:
                deps.verify(self.file, self.work, self.prefix, self.sources,
                            config=cfg, profile=profile, tls_backend='openssl', only=only,
                            build_config=build_config, cmake_options=cmake_options)
            else:
                deps.install(self.file, self.work, self.prefix, self.sources,
                             config=cfg, profile=profile, tls_backend='openssl', only=only,
                             offline=True, versions=versions, jobs=1,
                             build_config=build_config, cmake_options=cmake_options)
        return output.getvalue()

    def test_configuration_and_options_cannot_reuse_incompatible_prefix(self):
        self.invoke(only='libfoo')
        recorded = self.prefix / 'share/configuration.txt'
        self.assertEqual(recorded.read_text(), 'Release;')
        self.invoke(only='libfoo', build_config='Debug', cmake_options=('-DFIXTURE_OPTION=first',))
        self.assertEqual(recorded.read_text(), 'Debug;first')
        state = deps.read_state(self.prefix)
        self.assertEqual(state['context']['configuration'], 'Debug')
        self.assertEqual(state['context']['cmake_options'], ['-DFIXTURE_OPTION=first'])
        self.invoke(only='libfoo', build_config='Debug', cmake_options=('-DFIXTURE_OPTION=second',))
        self.assertEqual(recorded.read_text(), 'Debug;second')
        self.invoke(only='libfoo', verify=True, build_config='Debug',
                    cmake_options=('-DFIXTURE_OPTION=second',))
        with self.assertRaisesRegex(ValueError, 'build configuration differs'):
            self.invoke(only='libfoo', verify=True, build_config='Release',
                        cmake_options=('-DFIXTURE_OPTION=second',))
        with self.assertRaisesRegex(ValueError, 'CMake options differ'):
            self.invoke(only='libfoo', verify=True, build_config='Debug',
                        cmake_options=('-DFIXTURE_OPTION=first',))

    def test_offline_install_upgrade_and_failed_upgrade(self):
        # Install a single dependency offline.
        first = self.invoke(only='libfoo', profile='runtime')
        header = self.prefix / 'include/nlohmann/json.hpp'
        self.assertEqual(header.read_text(), '1.0')
        header_timestamp = header.stat().st_mtime_ns

        # A repeated install reuses the verified prefix byte-for-byte.
        repeated = self.invoke(only='libfoo', profile='runtime')
        self.assertIn('Reusing verified dependency prefix', repeated)
        self.assertEqual(header.stat().st_mtime_ns, header_timestamp)
        receipt = self.prefix / fixtures.CONFIG.manifest_path
        self.assertEqual(list(self.work.glob('prefix*')), [self.prefix])  # committed: no backups

        # Verification is read-only and does not touch the lock.
        snapshot = self.file.read_bytes()
        self.assertIn('Verified dependency prefix',
                      self.invoke(only='libfoo', profile='runtime', verify=True))
        self.assertEqual(self.file.read_bytes(), snapshot)

        # A tampered lock is rejected before CMake can use the prefix.
        modified = json.loads(snapshot)
        modified['dependencies']['libfoo']['version'] = '9.0'
        self.file.write_text(json.dumps(modified))
        with self.assertRaisesRegex(deps.DependencyError, 'declaration changed'):
            self.invoke(verify=True)
        self.assertEqual(header.read_text(), '1.0')
        self.file.write_bytes(snapshot)

        # An explicit version upgrade replaces the component.
        self.specs['dependencies']['libfoo']['version'] = '1.0'
        self.records['libfoo']['requested'] = '2.0'
        self.archive('2.0')
        upgraded = self.invoke(only='libfoo', versions={'libfoo': '2.0'})
        self.assertEqual(header.read_text(), '2.0')
        self.assertFalse((header.parent / 'obsolete.hpp').exists())

        # A failed upgrade rolls the prefix back to the last good version.
        self.records['libfoo']['requested'] = '3.0'
        self.archive('3.0', fail=True)
        with self.assertRaisesRegex(subprocess.CalledProcessError, 'returned non-zero exit status'):
            self.invoke(only='libfoo', versions={'libfoo': '3.0'})
        self.assertEqual(header.read_text(), '2.0')


    def assert_doctest_utf8_discovery(self, discovery_script):
        """Run the installed upstream discovery module and its generated tests."""
        with tempfile.TemporaryDirectory(prefix='fixture-doctest-discovery-') as temporary:
            root = Path(temporary)
            executable = root / 'doctest fixture.py'
            cases = ['中文检索,精确过滤', 'Unicode suite selection']
            label = '书源解析套件'
            executable.write_text(
                'import sys\n'
                f'cases = {cases!r}\n'
                f'label = {label!r}\n'
                'if "--list-test-cases" in sys.argv:\n'
                '    lines = ["[doctest] listing cases", *cases]\n'
                'elif "--list-test-suites" in sys.argv:\n'
                '    assert sys.argv[1] in ["--test-case=" + case for case in cases], sys.argv\n'
                '    lines = ["[doctest] listing suites", label]\n'
                'else:\n'
                '    assert sys.argv[1] in ["--test-case=" + case.replace(",", "\\\\,") for case in cases], sys.argv\n'
                '    lines = []\n'
                'sys.stdout.buffer.write(("\\n".join(lines) + "\\n").encode("utf-8"))\n',
                encoding='utf-8')
            ctest_file = root / 'CTestTestfile.cmake'
            runner_script = root / 'discovery-runner.cmake'
            runner_script.write_text(
                'cmake_minimum_required(VERSION 3.21)\n'
                f'include([==[{discovery_script.as_posix()}]==])\n',
                encoding='utf-8')
            command = ['cmake', '--trace-expand', '--trace-format=json-v1',
                       '--trace-source=' + str(discovery_script),
                       '-DTEST_EXECUTOR=' + sys.executable, '-DTEST_EXECUTABLE=' + str(executable),
                       '-DTEST_WORKING_DIR=' + str(root), '-DTEST_ADD_LABELS=TRUE',
                       '-DTEST_LIST=discovered_tests', '-DCTEST_FILE=' + str(ctest_file),
                       '-P', str(runner_script)]
            environment = {**os.environ, 'PYTHONIOENCODING': 'cp1252'}
            discovered = subprocess.run(command, env=environment, encoding='utf-8', capture_output=True)
            self.assertEqual(discovered.returncode, 0, discovered.stdout + discovered.stderr)
            # CMake only applies ENCODING on Windows. Check the executed calls
            # on every host as well as round-tripping real UTF-8 child output.
            # The call count itself varies with CMake's internal strategy.
            calls = [event for line in discovered.stderr.splitlines() if line.startswith('{')
                     for event in [json.loads(line)] if event.get('cmd') == 'execute_process']
            self.assertGreaterEqual(len(calls), 1)
            for call in calls:
                arguments = call['args']
                self.assertIn('ENCODING', arguments)
                self.assertEqual(arguments[arguments.index('ENCODING') + 1], 'UTF-8')
            listed = subprocess.run(['ctest', '--show-only=json-v1'], cwd=root,
                                    encoding='utf-8', capture_output=True)
            self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
            tests = json.loads(listed.stdout)['tests']
            self.assertEqual([test['name'] for test in tests], cases)
            # Byte-level acceptance first: the generated script itself must
            # carry the Chinese suite label as a bracket argument.
            generated = ctest_file.read_text(encoding='utf-8')
            self.assertIn(
                'LABELS [==[' + label + ']==]', generated,
                'generated script missing the Chinese label; calls='
                + repr([call.get('args') for call in calls])
                + '\nlabel trace='
                + repr([line for line in discovered.stderr.splitlines()
                        if 'add_labels' in line or 'TEST_ADD_LABELS' in line])
                + '\nscript:\n' + generated)
            for test in tests:
                properties = {entry['name']: entry['value'] for entry in test['properties']}
                if 'LABELS' in properties:
                    self.assertEqual(properties['LABELS'], [label])
                else:
                    self.fail('CTest did not report LABELS for ' + repr(test)
                              + '; generated script:\n' + generated)
                self.assertEqual(test['command'][-1],
                                 '--test-case=' + test['name'].replace(',', '\\,'))
            executed = subprocess.run(['ctest', '--output-on-failure'], cwd=root, env=environment,
                                      encoding='utf-8', capture_output=True)
            self.assertEqual(executed.returncode, 0, executed.stdout + executed.stderr)
