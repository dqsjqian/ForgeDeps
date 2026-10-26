"""Run the installed-style CMake bridge in an unrelated, space-bearing path."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

from aria_deps.deps_build import request_hash


@unittest.skipUnless(shutil.which("cmake"), "CMake unavailable")
class CMakeBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cmake external consumer ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = Path(__file__).resolve().parents[1]
        self.env = dict(os.environ, PYTHONPATH=str(self.package))

    def run_cmake(self, body, *, success=True):
        script = self.root / "consumer.cmake"
        script.write_text('cmake_minimum_required(VERSION 3.21)\n'
                          'execute_process(COMMAND "${AriaDeps_PYTHON_EXECUTABLE}" '
                          '-m aria_deps cmake-dir OUTPUT_VARIABLE modules '
                          'OUTPUT_STRIP_TRAILING_WHITESPACE COMMAND_ERROR_IS_FATAL ANY)\n'
                          'include("${modules}/AriaDeps.cmake")\n' + body,
                          encoding="utf-8")
        result = subprocess.run(["cmake", f"-DAriaDeps_PYTHON_EXECUTABLE={Path(sys.executable).as_posix()}",
                                 "-P", str(script)], cwd=self.root, env=self.env,
                                capture_output=True, encoding="utf-8", errors="replace")
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_resolve_outputs_and_explicit_offline_selection(self):
        spec = {"provider": "github", "repo": "example/fixture", "artifact": "archive"}
        record = {"requested": "latest", "version": "1.0", "revision": "a" * 40,
                  "tag": "v1.0", "url": "https://example.invalid/fixture.tar.gz",
                  "sha256": "b" * 64, "request_hash": request_hash(spec)}
        lock = self.root / "dependencies.json"
        lock.write_text(json.dumps({"schema": 2, "dependencies": {
            "fixture": {**spec, "resolved": record}}}), encoding="utf-8")
        original = lock.read_bytes()
        body = '''aria_deps_resolve(NAME FIXTURE FILE "${CMAKE_CURRENT_LIST_DIR}/dependencies.json"
    REQUEST_VERSION 1.0 STATE_DIR "${CMAKE_CURRENT_LIST_DIR}/effective locks" OFFLINE
    VERSION version URL url SHA256 hash REVISION revision TAG tag)
file(WRITE "${CMAKE_CURRENT_LIST_DIR}/results.txt" "${version}\n${url}\n${hash}\n${revision}\n${tag}\n")
'''
        self.run_cmake(body)
        self.assertEqual((self.root / "results.txt").read_text().splitlines(),
                         [record[key] for key in ("version", "url", "sha256", "revision", "tag")])
        self.run_cmake(body)
        failed = self.run_cmake(body.replace("REQUEST_VERSION 1.0", "REQUEST_VERSION 2.0"), success=False)
        self.assertIn("No matching resolved result", failed.stderr)
        self.assertEqual(lock.read_bytes(), original)
        self.assertEqual(len(list((self.root / "effective locks").glob("*/dependencies.json"))), 1)

    def test_fetch_paths_and_offline_cache_validation(self):
        header = self.root / "source header.h"
        header.write_bytes(b"#define FIXTURE_VALUE 42\n")
        header_hash = hashlib.sha256(header.read_bytes()).hexdigest()
        archive_path = self.root / "source archive.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            member = tarfile.TarInfo("fixture/value.txt")
            member.size = 3
            archive.addfile(member, io.BytesIO(b"42\n"))
        archive_hash = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        body = f'''aria_deps_fetch_file(NAME fixture URL "{header.as_uri()}" SHA256 "{header_hash}"
    CACHE "${{CMAKE_CURRENT_LIST_DIR}}/cache space" AS "fixture/header.h" FILE header BASE base ${{mode}})
aria_deps_fetch_archive(NAME fixture VERSION 1.0 URL "{archive_path.as_uri()}" SHA256 "{archive_hash}"
    CACHE "${{CMAKE_CURRENT_LIST_DIR}}/cache space" SOURCE_DIR source ARCHIVE archive ${{mode}})
file(WRITE "${{CMAKE_CURRENT_LIST_DIR}}/results.txt" "${{header}}\n${{base}}\n${{source}}\n${{archive}}\n")
'''
        self.run_cmake(body)
        first = (self.root / "results.txt").read_text()
        cached_header, base, source, archive = map(Path, first.splitlines())
        self.assertEqual(cached_header, base / "fixture/header.h")
        self.assertEqual(cached_header.read_bytes(), header.read_bytes())
        self.assertEqual((source / "value.txt").read_bytes(), b"42\n")
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), archive_hash)
        header.unlink()
        archive_path.unlink()
        self.run_cmake("set(mode OFFLINE)\n" + body)
        self.assertEqual((self.root / "results.txt").read_text(), first)
        (source / "value.txt").write_bytes(b"modified\n")
        failed = self.run_cmake("set(mode OFFLINE)\n" + body, success=False)
        self.assertIn("modified", failed.stderr)


if __name__ == "__main__":
    unittest.main()
