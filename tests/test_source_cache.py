"""Real archive/file and Git transactions for immutable dependency sources."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import zipfile

from forgedeps import deps_build as deps
from forgedeps import source_cache as cache


class Response(io.BytesIO):
    url = "https://example.invalid/source"


def tar_bytes(members=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, data in (members or {"library/include/header.h": b"#define FIXTURE 1\n"}).items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


class SourceCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="source-cache-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root / "cache with spaces"
        self.data = tar_bytes()
        self.digest = hashlib.sha256(self.data).hexdigest()

    def fetch(self, *, offline=False):
        return cache.fetch_archive("library", "1.0", "https://example.invalid/library.tar.gz",
                                   self.digest, self.cache, offline=offline)

    def download(self, data=None):
        content = self.data if data is None else data
        return patch.object(deps.urllib.request, "urlopen", side_effect=lambda *a, **k: Response(content))

    def test_archive_is_reused_offline_and_shared_by_content(self):
        with self.download() as network:
            first = self.fetch()
            again = cache.fetch_archive("other", "2.0", "https://example.invalid/another.tgz",
                                        self.digest, self.cache, offline=True)
        self.assertEqual(first, again)
        self.assertEqual(network.call_count, 1)
        self.assertEqual((Path(first["source_dir"]) / "include/header.h").read_bytes(), b"#define FIXTURE 1\n")

    def test_zip_archive(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("library/header.h", b"zip fixture\n")
        data = output.getvalue()
        with self.download(data):
            result = cache.fetch_archive("library", "1", "https://example.invalid/library.zip",
                                         hashlib.sha256(data).hexdigest(), self.cache)
        self.assertEqual((Path(result["source_dir"]) / "header.h").read_bytes(), b"zip fixture\n")

    def test_offline_missing_does_not_access_network(self):
        with patch.object(deps.urllib.request, "urlopen", side_effect=AssertionError("network")):
            with self.assertRaisesRegex(ValueError, "Offline cache missing"):
                self.fetch(offline=True)

    def test_download_hash_mismatch_is_not_published(self):
        with self.download(b"wrong bytes"):
            with self.assertRaisesRegex(ValueError, "SHA256"):
                self.fetch()
        self.assertFalse((self.cache / "archives" / self.digest).exists())

    def test_hash_correct_invalid_archive_reports_validation_error(self):
        data = b"not an archive\n"
        digest = hashlib.sha256(data).hexdigest()
        for extension in (".tar.gz", ".zip"):
            with self.subTest(extension=extension), self.download(data):
                with self.assertRaisesRegex(ValueError, "Invalid source archive"):
                    cache.fetch_archive("library", "1", "https://example.invalid/bad" + extension,
                                         digest, self.cache)
                self.assertFalse((self.cache / "archives" / digest).exists())

    def test_modified_archive_is_preserved_and_rejected(self):
        with self.download():
            result = self.fetch()
        archive = Path(result["archive"])
        archive.write_bytes(b"modified")
        with self.assertRaisesRegex(ValueError, "SHA256"):
            self.fetch(offline=True)
        self.assertEqual(archive.read_bytes(), b"modified")

    def test_modified_sources_are_never_repaired(self):
        for mutation in ("contents", "extra", "empty-directory", "delete"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(dir=self.root) as temporary:
                self.cache = Path(temporary) / "cache"
                with self.download():
                    source = Path(self.fetch()["source_dir"])
                if mutation == "contents":
                    (source / "include/header.h").write_bytes(b"local work")
                elif mutation == "extra":
                    (source / "extra.txt").write_bytes(b"local work")
                elif mutation == "empty-directory":
                    (source / "extra").mkdir()
                else:
                    (source / "include/header.h").unlink()
                before = cache._tree_digest(source)
                with self.assertRaisesRegex(ValueError, "modified"):
                    self.fetch(offline=True)
                self.assertEqual(cache._tree_digest(source), before)

    def test_missing_manifest_does_not_adopt_existing_sources(self):
        with self.download():
            source = Path(self.fetch()["source_dir"])
        (source.parent / "manifest.json").unlink()
        with self.assertRaisesRegex(ValueError, "manifest"):
            self.fetch(offline=True)
        self.assertTrue((source / "include/header.h").is_file())

    @unittest.skipIf(os.name == "nt", "symlink creation can require Windows privileges")
    def test_symlink_sources_and_cache_are_rejected(self):
        with self.download():
            source = Path(self.fetch()["source_dir"])
        target = self.root / "outside.txt"
        target.write_text("keep")
        (source / "link").symlink_to(target)
        with self.assertRaisesRegex(ValueError, "link"):
            self.fetch(offline=True)
        alias = self.root / "alias"
        alias.symlink_to(self.cache, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "real directory"):
            cache.fetch_file("header", "https://example.invalid/h", self.digest, alias)
        self.assertEqual(target.read_text(), "keep")

    def test_unsafe_archive_does_not_escape(self):
        data = tar_bytes({"../outside.txt": b"bad"})
        with self.download(data):
            with self.assertRaisesRegex(ValueError, "Unsafe archive"):
                cache.fetch_archive("library", "1", "https://example.invalid/bad.tar.gz",
                                     hashlib.sha256(data).hexdigest(), self.cache)
        self.assertFalse((self.root / "outside.txt").exists())

    def test_concurrent_requests_publish_one_complete_source(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def receive(*args, **kwargs):
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(5))
            return Response(self.data)

        with patch.object(deps.urllib.request, "urlopen", side_effect=receive), ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.fetch)
            self.assertTrue(entered.wait(5))
            second = pool.submit(self.fetch)
            release.set()
            self.assertEqual(first.result(timeout=10), second.result(timeout=10))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(list((self.cache / "archives").iterdir())), 1)

    def test_file_has_requested_include_layout_and_detects_edits(self):
        data = b"// single header\n"
        digest = hashlib.sha256(data).hexdigest()
        with self.download(data) as network:
            result = cache.fetch_file("header", "https://example.invalid/header.h", digest,
                                       self.cache, "library/header.h")
            self.assertEqual(result, cache.fetch_file("header", "https://example.invalid/header.h",
                                                       digest, self.cache, "library/header.h", offline=True))
        self.assertEqual(network.call_count, 1)
        target = Path(result["file"])
        self.assertEqual(target, Path(result["base"]) / "library/header.h")
        self.assertEqual(target.read_bytes(), data)
        target.write_bytes(b"local header")
        with self.assertRaisesRegex(ValueError, "modified"):
            cache.fetch_file("header", "https://example.invalid/header.h", digest,
                               self.cache, "library/header.h", offline=True)
        self.assertEqual(target.read_bytes(), b"local header")

    def test_file_rejects_unsafe_paths_before_download(self):
        with patch.object(deps.urllib.request, "urlopen", side_effect=AssertionError("network")):
            for relative in ("../h", "/tmp/h", "C:/h", "a\\h", "NUL", "h.", ""):
                with self.subTest(path=relative), self.assertRaisesRegex(ValueError, "Unsafe"):
                    cache.fetch_file("header", "https://example.invalid/h", self.digest,
                                       self.cache, relative)

    def test_default_filename_decodes_url_and_supports_localhost(self):
        source = self.root / "header with spaces.h"
        source.write_bytes(b"// local header\n")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        url = source.as_uri().replace("file:///", "file://localhost/", 1)
        with patch.object(deps.urllib.request, "urlopen", side_effect=AssertionError("network")):
            result = cache.fetch_file("header", url, digest, self.cache, relative_path=None)
            self.assertEqual(Path(result["file"]).name, source.name)
            self.assertEqual(Path(result["file"]).read_bytes(), source.read_bytes())
            self.assertEqual(result, cache.fetch_file("header", url, digest, self.cache,
                                                       relative_path=None, offline=True))

    def test_encoded_unsafe_default_filename_is_rejected(self):
        with patch.object(deps.urllib.request, "urlopen", side_effect=AssertionError("network")):
            for filename in ("%2e%2e", "%2e%2e%2fheader.h", "%5cheader.h", "%2fheader.h"):
                with self.subTest(filename=filename), self.assertRaisesRegex(ValueError, "Unsafe"):
                    cache.fetch_file("header", "https://example.invalid/" + filename,
                                       self.digest, self.cache)

    def test_local_file_url_is_verified_and_offline_requires_cache(self):
        source = self.root / "local source.tar.gz"
        source.write_bytes(self.data)
        with patch.object(deps.urllib.request, "urlopen", side_effect=AssertionError("network")):
            with self.assertRaisesRegex(ValueError, "Offline cache missing"):
                cache.fetch_archive("library", "1", source.as_uri(), self.digest,
                                     self.cache, offline=True)
            result = cache.fetch_archive("library", "1", source.as_uri(), self.digest, self.cache)
            source.unlink()
            self.assertEqual(result, cache.fetch_archive("library", "1", source.as_uri(), self.digest,
                                                          self.cache, offline=True))

    def test_local_file_url_requires_hash_and_local_host(self):
        source = self.root / "header.h"
        source.write_bytes(b"wrong")
        with self.assertRaisesRegex(ValueError, "SHA256"):
            cache.fetch_file("header", source.as_uri(), self.digest, self.cache)
        for url in ("file://server/share/header.h", "file:////server/share/header.h",
                    "file:relative/header.h", "file:///tmp/header.h?query"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "Local source"):
                cache.fetch_file("header", url, self.digest, self.cache)


def git(directory, *arguments):
    result = subprocess.run(["git", "-C", str(directory), *map(str, arguments)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


@unittest.skipUnless(shutil.which("git"), "Git is required")
class GitBootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="source-bootstrap-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.origin = self.root / "local mirror"
        self.origin.mkdir()
        git(self.origin, "init", "--quiet")
        self.first = self.commit("first\n")
        self.second = self.commit("second\n")
        self.lock = self.root / "dependencies.json"
        self.destination = self.root / "deps" / "library"
        self.spec = {"provider": "github", "repo": "example/library", "artifact": "git", "tag_prefix": "v"}
        self.write_lock(self.first)

    def commit(self, contents):
        (self.origin / "library.txt").write_text(contents, encoding="utf-8")
        git(self.origin, "add", "library.txt")
        git(self.origin, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "--quiet", "-m", contents.strip())
        return git(self.origin, "rev-parse", "HEAD")

    def record(self, revision, version="1.0"):
        return {"version": version, "requested": version, "tag": "v" + version,
                "revision": revision, "sha256": "", "url": "https://github.com/example/library.git",
                "request_hash": deps.request_hash(self.spec)}

    def write_lock(self, revision):
        deps.atomic_json(self.lock, {"schema": 2, "dependencies": {
            "library": {**self.spec, "resolved": self.record(revision)}}})

    def fetch(self, **kwargs):
        kwargs.setdefault("source", self.origin)
        return cache.bootstrap_git(self.lock, "library", self.destination, **kwargs)

    def new_resolution(self, revision=None):
        return patch.object(deps, "resolve_record", return_value={
            **self.record(revision or self.second, "2.0"), "source": self.spec})

    def test_offline_bootstrap_from_local_mirror_and_reuse(self):
        with patch.object(deps.Context, "github_json", side_effect=AssertionError("network")):
            first = self.fetch(offline=True)
            again = self.fetch(offline=True, source=None)
        self.assertEqual(first, again)
        self.assertEqual(first["revision"], self.first)
        self.assertIsInstance(first["source_dir"], str)
        self.assertTrue(Path(first["source_dir"]).is_absolute())
        self.assertEqual(git(self.destination, "rev-parse", "HEAD"), self.first)
        self.assertEqual(git(self.destination, "branch", "--show-current"), "")

    def test_invalid_dependency_name_is_rejected_before_changes(self):
        before = self.lock.read_bytes()
        for name in ("", ".", "../library", "a/b", "a\\b", "name:bad", "-option", None):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "simple identifier"):
                cache.bootstrap_git(self.lock, name, self.destination, source=self.origin, offline=True)
        self.assertFalse(self.destination.parent.exists())
        self.assertEqual(self.lock.read_bytes(), before)

    def test_offline_missing_without_mirror_preserves_lock(self):
        before = self.lock.read_bytes()
        with self.assertRaisesRegex(ValueError, "Offline Git bootstrap"):
            self.fetch(offline=True, source=None)
        self.assertEqual(self.lock.read_bytes(), before)
        self.assertFalse(self.destination.exists())

    def test_version_switch_keeps_old_checkout_and_publishes_metadata(self):
        self.fetch(offline=True)
        with self.new_resolution():
            result = self.fetch(version="2.0")
        backup = Path(result["backup"])
        self.assertEqual(git(backup, "rev-parse", "HEAD"), self.first)
        self.assertEqual(git(self.destination, "rev-parse", "HEAD"), self.second)
        self.assertEqual((backup / "library.txt").read_text(), "first\n")
        self.assertEqual(deps.read_resolved(self.lock)["dependencies"]["library"]["revision"], self.second)

    def test_dirty_checkout_and_untracked_files_are_preserved(self):
        self.fetch(offline=True)
        for filename in ("library.txt", "untracked.txt"):
            path = self.destination / filename
            original = path.read_bytes() if path.exists() else None
            path.write_bytes(b"local work")
            before = self.lock.read_bytes()
            with self.subTest(path=filename), self.assertRaisesRegex(ValueError, "local edits"):
                self.fetch(offline=True)
            self.assertEqual(path.read_bytes(), b"local work")
            self.assertEqual(self.lock.read_bytes(), before)
            if original is None:
                path.unlink()
            else:
                path.write_bytes(original)

    def test_legacy_informational_marker_is_accepted_but_changes_are_rejected(self):
        self.fetch(offline=True)
        marker = self.destination / ".pinned-library-sha"
        marker.write_text(self.first + "\n", encoding="ascii")
        self.fetch(offline=True)
        marker.write_text("modified\n")
        with self.assertRaisesRegex(ValueError, "local edits"):
            self.fetch(offline=True)

    def test_plain_directory_and_linked_worktree_are_rejected(self):
        self.destination.mkdir(parents=True)
        (self.destination / "keep.txt").write_text("keep")
        with self.assertRaisesRegex(ValueError, "non-Git"):
            self.fetch(offline=True)
        self.assertEqual((self.destination / "keep.txt").read_text(), "keep")
        shutil.rmtree(self.destination)
        git(self.origin, "worktree", "add", "--detach", str(self.destination), self.first)
        with self.assertRaisesRegex(ValueError, "linked worktree"):
            self.fetch(offline=True)
        self.assertEqual(git(self.destination, "rev-parse", "HEAD"), self.first)
        git(self.origin, "worktree", "remove", str(self.destination))

    @unittest.skipIf(os.name == "nt", "symlink creation can require Windows privileges")
    def test_symlink_checkout_is_rejected(self):
        self.destination.parent.mkdir()
        self.destination.symlink_to(self.origin, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic-link"):
            self.fetch(offline=True)
        self.assertEqual(git(self.origin, "rev-parse", "HEAD"), self.second)

    def test_metadata_failure_rolls_back_checkout_and_lock(self):
        self.fetch(offline=True)
        before = self.lock.read_bytes()
        atomic_json = deps.atomic_json

        def failing(path, value):
            if path.resolve() == self.lock.resolve():
                raise OSError("metadata publication failed")
            return atomic_json(path, value)

        with self.new_resolution(), patch.object(deps, "atomic_json", side_effect=failing):
            with self.assertRaisesRegex(OSError, "metadata publication failed"):
                self.fetch(version="2.0")
        self.assertEqual(git(self.destination, "rev-parse", "HEAD"), self.first)
        self.assertEqual(self.lock.read_bytes(), before)
        self.assertEqual(list(self.destination.parent.glob("library-backup-*")), [])

    def test_initial_metadata_failure_does_not_leave_checkout(self):
        before = self.lock.read_bytes()
        atomic_json = deps.atomic_json

        def failing(path, value):
            if path.resolve() == self.lock.resolve():
                raise OSError("metadata publication failed")
            return atomic_json(path, value)

        with patch.object(deps, "atomic_json", side_effect=failing):
            with self.assertRaisesRegex(OSError, "metadata publication failed"):
                self.fetch(offline=True)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.lock.read_bytes(), before)

    def test_failed_git_fetch_keeps_previous_checkout_and_metadata(self):
        self.fetch(offline=True)
        before = self.lock.read_bytes()
        with self.new_resolution("f" * 40):
            with self.assertRaisesRegex(ValueError, "Git operation failed"):
                self.fetch(version="2.0")
        self.assertEqual(git(self.destination, "rev-parse", "HEAD"), self.first)
        self.assertEqual(self.lock.read_bytes(), before)

    def test_concurrent_bootstraps_reuse_one_checkout(self):
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.fetch(offline=True), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(list(self.destination.parent.glob("library-backup-*")), [])


if __name__ == "__main__":
    unittest.main()
