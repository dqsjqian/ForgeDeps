"""Exercise the public CLI in independent processes and a real CMake build."""
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

from forgedeps import deps_build as deps


class CliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="external consumer ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))

    def run_cli(self, *arguments, success=True):
        result = subprocess.run([sys.executable, "-m", "forgedeps", *map(str, arguments)],
                                cwd=self.root, env=self.env, capture_output=True,
                                encoding="utf-8", errors="replace")
        if success:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def test_file_fetch_stdout_is_json_and_offline_edits_are_rejected(self):
        header = self.root / "fixture header.h"
        header.write_bytes(b"#define EXTERNAL_FIXTURE 42\n")
        arguments = ("fetch-file", "--name", "fixture", "--url", header.as_uri(),
                     "--sha256", hashlib.sha256(header.read_bytes()).hexdigest(),
                     "--cache", self.root / "cache", "--as", "fixture/header.h")
        first = json.loads(self.run_cli(*arguments).stdout)
        self.assertEqual(Path(first["file"]).read_bytes(), header.read_bytes())
        header.unlink()
        self.assertEqual(json.loads(self.run_cli(*arguments, "--offline").stdout), first)
        Path(first["file"]).write_text("local edit", encoding="utf-8")
        failed = self.run_cli(*arguments, "--offline", success=False)
        self.assertEqual(failed.stdout, "")
        self.assertIn("modified", failed.stderr)
        self.assertEqual(Path(first["file"]).read_text(), "local edit")

    @unittest.skipUnless(shutil.which("cmake"), "CMake unavailable")
    def test_external_install_verify_and_configuration_mismatch(self):
        contents = {
            "LICENSE": "Fixture license\n",
            "fixture.c": "int fixture(void) { return 42; }\n",
            "fixture.h": "int fixture(void);\n",
            "CMakeLists.txt": '''cmake_minimum_required(VERSION 3.20)
project(ExternalFixture C)
add_library(fixture STATIC fixture.c)
install(TARGETS fixture ARCHIVE DESTINATION lib)
install(FILES fixture.h DESTINATION include)
''',
        }
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as archive:
            for name, content in contents.items():
                encoded = content.encode()
                member = tarfile.TarInfo("source/" + name)
                member.size = len(encoded)
                member.mode = 0o644
                archive.addfile(member, io.BytesIO(encoded))
        digest = hashlib.sha256(data.getvalue()).hexdigest()
        work = self.root / "work"
        (work / "cache").mkdir(parents=True)
        (work / "cache" / digest).write_bytes(data.getvalue())
        spec = {"provider": "github", "repo": "example/fixture", "artifact": "archive"}
        record = {"requested": "latest", "version": "1.0", "revision": "a" * 40,
                  "tag": "v1.0", "url": "https://example.invalid/fixture.tar.gz",
                  "sha256": digest, "request_hash": deps.request_hash(spec)}
        lock = self.root / "dependencies.json"
        lock.write_text(json.dumps({"schema": 2, "dependencies": {
            "fixture": {**spec, "resolved": record}}}), encoding="utf-8")
        config = self.root / "project_config.py"
        config.write_text('''from forgedeps import Dependency, ProjectConfig
CONFIG = ProjectConfig(name="external-fixture", recipes=(Dependency(
    name="fixture", version="", url="", sha256="", root="", kind="cmake",
    license="MIT", license_files=("LICENSE",),
    artifacts=("include/fixture.h", "lib/libfixture.a")),))
''', encoding="utf-8")
        arguments = ("--project-config", config, "--file", lock, "--work", work,
                     "--build-config", "Debug", "--cmake-option=-DFIXTURE_OPTION=ON")
        installed = self.run_cli("install", *arguments, "--offline", "--jobs", "1")
        result = json.loads(installed.stdout)
        self.assertTrue(result["verified"])
        self.assertIn("cmake --build", installed.stderr)
        self.assertIn("Installing:", installed.stderr)
        self.assertEqual(json.loads(self.run_cli("verify", *arguments).stdout), result)
        prefix = Path(result["prefix"])
        before = {p.relative_to(prefix): p.read_bytes() for p in prefix.rglob("*") if p.is_file()}
        failed = self.run_cli("verify", *arguments, "--build-config", "Release", success=False)
        self.assertIn("build configuration differs", failed.stderr)
        self.assertEqual(failed.stdout, "")
        self.assertEqual(before, {p.relative_to(prefix): p.read_bytes()
                                  for p in prefix.rglob("*") if p.is_file()})
        # Read and resolve must remain machine-readable and keep the source lock unchanged.
        original = lock.read_bytes()
        read = json.loads(self.run_cli("read", "--file", lock).stdout)
        resolved = json.loads(self.run_cli("resolve", "--file", lock, "--offline").stdout)
        self.assertEqual(read, resolved)
        self.assertEqual(lock.read_bytes(), original)
        self.run_cli("read", "--file", lock, "--output", lock, success=False)

    def test_cmake_module_and_command_version_are_available(self):
        directory = Path(self.run_cli("cmake-dir").stdout.strip())
        self.assertTrue((directory / "ForgeDeps.cmake").is_file())
        self.assertEqual(self.run_cli("--version").stdout.strip(), "forgedeps 0.4.0")


if __name__ == "__main__":
    unittest.main()
