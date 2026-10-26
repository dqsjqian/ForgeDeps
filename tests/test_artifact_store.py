from __future__ import annotations

import contextlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

# Also executable as a fresh-process worker on every supported host.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from forgedeps.artifacts import Artifact, BuildRequest, Environment
from forgedeps.home import prepare_home, register_workspace, resolve_home
from forgedeps.leases import ArtifactLease
from forgedeps.store import build_package, ensure_environment, verify_artifact, verify_request


def wait_for(path, seconds=10):
    deadline = time.monotonic() + seconds
    while not Path(path).exists():
        if time.monotonic() > deadline:
            raise RuntimeError(f"Timed out waiting for {path}")
        time.sleep(0.01)


def plain_builder(ctx):
    counter = ctx.options.get("counter")
    if counter:
        with open(counter, "a", encoding="utf-8") as stream:
            stream.write("build\n")
    if ctx.options.get("started"):
        Path(ctx.options["started"]).touch()
        wait_for(ctx.options["release"])
    (ctx.prefix / "include").mkdir()
    shutil.copy2(ctx.source_snapshot / "payload.h", ctx.prefix / "include/payload.h")
    (ctx.prefix / "context.json").write_text(json.dumps({
        "source": str(ctx.source_snapshot), "build": str(ctx.build_dir),
        "prefix": str(ctx.prefix), "dependencies": [str(p) for p in ctx.dependency_prefixes],
        "flags": ctx.environment.get("CFLAGS", ""),
    }), encoding="utf-8")
    if ctx.options.get("crash"):
        os._exit(77)
    if ctx.options.get("fail"):
        raise RuntimeError("intentional build failure")


def failing_validation(ctx):
    raise ValueError("intentional validation failure")


def cmake_builder(ctx):
    with open(ctx.options["counter"], "a", encoding="utf-8") as stream:
        stream.write("compile\n")
    ctx.run(["cmake", "-S", ctx.source_snapshot, "-B", ctx.build_dir,
             "-G", "Ninja", f"-DCMAKE_INSTALL_PREFIX={ctx.prefix}",
             f"-DCMAKE_BUILD_TYPE={ctx.configuration}",
             f"-DCMAKE_C_FLAGS={ctx.options.get('flags', '')}"])
    ctx.run(["cmake", "--build", ctx.build_dir, "--parallel", str(ctx.jobs)])
    ctx.run(["cmake", "--install", ctx.build_dir])


def log_builder(ctx):
    ctx.run([sys.executable, "-c", "import sys; print('native stdout'); print('native stderr',file=sys.stderr)"])
    result = ctx.run([sys.executable, "-c", "print('captured')"], capture_output=True, text=True)
    assert result.stdout.strip() == "captured"


def dependency_read_builder(ctx):
    # Real child-process CMake checks must be able to read dependencies while
    # the parent owns this output slot exclusively.
    code = """import json, os
from pathlib import Path
from forgedeps.environments import read_environment
from forgedeps.build_kit.session import assert_parent_covers_environment
selected = read_environment(Path(os.environ['FORGEDEPS_ENVIRONMENT']))
with selected.lease():
    assert_parent_covers_environment(selected)
    print('child dependency check passed')
"""
    ctx.run([sys.executable, "-c", code],
            env={"PYTHONPATH": str(Path(__file__).resolve().parents[1])}, timeout=5)
    (ctx.prefix / "complete").touch()


def worker(mode, record):
    if mode == "--hold":
        artifact = Artifact.from_dict(record["artifact"])
        with ArtifactLease(artifact):
            Path(record["started"]).touch()
            wait_for(record["release"], 30)
    else:
        request = BuildRequest("tiny", Path(record["source"]), plain_builder,
                               {"recipe.py": Path(__file__)}, {"label": "test-release"},
                               options=record.get("options", {}), artifacts=("include/payload.h",))
        result = build_package(request, home=Path(record["home"]))
        print(json.dumps(result.to_dict()))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "shared workspace"
        self.home.mkdir()
        self.source = self.root / "consumer-a/source"
        self.source.mkdir(parents=True)
        (self.source / "payload.h").write_text("#define ANSWER 1\n", encoding="utf-8")
        (self.source / "LICENSE").write_text("fixture license\n", encoding="utf-8")
        self.counter = self.root / "build-count"

    def request(self, **changes):
        base = BuildRequest("tiny", self.source, plain_builder, {"recipe.py": Path(__file__)},
                            {"label": "test-release"}, options={"counter": str(self.counter)},
                            artifacts=("include/payload.h",), license_files=("LICENSE",))
        return replace(base, **changes)

    def build(self, request=None, **kwargs):
        return build_package(request or self.request(), home=self.home, **kwargs)

    def process(self, mode, **record):
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), mode,
                                    json.dumps(record)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
        self.addCleanup(cleanup)
        return process

    def finish(self, process, code=0):
        stdout, stderr = process.communicate(timeout=30)
        self.assertEqual(code, process.returncode, stderr)
        return stdout

    def test_two_consumer_sources_share_one_current_slot(self):
        first = self.build()
        other_source = self.root / "consumer-b/source"
        shutil.copytree(self.source, other_source)
        second = self.build(self.request(source=other_source))
        self.assertEqual(first, second)
        self.assertEqual(self.counter.read_text().splitlines(), ["build"])
        self.assertEqual(first.prefix, self.home / "build/tiny/test-release")
        self.assertEqual(list(first.prefix.parent.iterdir()), [first.prefix])
        self.assertEqual(list((self.home / "state/transactions").iterdir()), [])
        self.assertTrue((first.prefix / "share/licenses/tiny/LICENSE").is_file())
        inputs = json.loads(first.manifest.read_text())["inputs"]
        self.assertNotIn(str(self.source), json.dumps(inputs))
        self.assertNotIn(str(other_source), json.dumps(inputs))
        with Environment((first,)).lease():
            verify_request(self.request(), first, home=self.home)

    def test_pure_headers_ignore_ambient_compiler_selectors(self):
        request = self.request(header_only=True, target={})
        with mock.patch.dict(os.environ, {"CC": "first-compiler", "CXX": "first-cxx"}):
            first = self.build(request)
        with mock.patch.dict(os.environ, {"CC": "second-compiler", "CXX": "second-cxx"}):
            second = self.build(request)
        self.assertEqual(first, second)
        self.assertEqual(self.counter.read_text().splitlines(), ["build"])

    def test_source_changed_after_selection_preserves_previous_artifact(self):
        first = self.build()
        recorded = json.loads(first.manifest.read_text())["inputs"]["source_content"]
        selected = self.request(expected_source_content=recorded)
        (self.source / "payload.h").write_text("#define ANSWER 9\n")
        with self.assertRaisesRegex(ValueError, "Managed source changed after selection"):
            self.build(selected)
        verify_artifact(first)
        self.assertEqual(self.counter.read_text().splitlines(), ["build"])

    def test_source_content_ignores_mtime_but_refreshes_output_mtime(self):
        first = self.build()
        original = (self.source / "payload.h").stat()
        old_output = (first.prefix / "include/payload.h").stat().st_mtime_ns
        (self.source / "payload.h").write_text("#define ANSWER 2\n")
        os.utime(self.source / "payload.h", ns=(original.st_atime_ns, original.st_mtime_ns))
        second = self.build()
        self.assertEqual(first.prefix, second.prefix)
        self.assertNotEqual(first.fingerprint, second.fingerprint)
        self.assertNotEqual(first.generation, second.generation)
        self.assertGreater((second.prefix / "include/payload.h").stat().st_mtime_ns, old_output)
        with self.assertRaisesRegex(ValueError, "generation changed"):
            verify_artifact(first)

    def test_flags_toolchain_recipe_and_ambient_environment_are_inputs(self):
        first = self.build()
        second = self.build(self.request(target={"label": "test-release", "compiler": "v2"}))
        self.assertNotEqual(first.fingerprint, second.fingerprint)
        third = self.build(self.request(environment={"CFLAGS": "-DCHANGED=1"}))
        self.assertNotEqual(second.fingerprint, third.fingerprint)
        with mock.patch.dict(os.environ, {"CFLAGS": "-DCHANGED=2"}):
            fourth = self.build()
            self.assertEqual(json.loads((fourth.prefix / "context.json").read_text())["flags"], "-DCHANGED=2")
        self.assertNotEqual(third.fingerprint, fourth.fingerprint)
        helper = self.root / "recipe-helper.txt"
        helper.write_text("v1")
        request = self.request(recipe_input_files={"support/helper": helper})
        before = self.build(request)
        helper.write_text("v2")
        self.assertNotEqual(before.fingerprint, self.build(request).fingerprint)

    def test_dependency_change_rebuilds_same_dependent_slot(self):
        dependency_request = self.request(name="base")
        dependency = self.build(dependency_request)
        parent = self.build(self.request(requires=(dependency,)))
        replacement = self.build(replace(dependency_request, options={"feature": True}))
        with self.assertRaisesRegex(ValueError, "dependency changed"):
            verify_artifact(parent)
        updated = self.build(self.request(requires=(replacement,)))
        self.assertNotEqual(parent.fingerprint, updated.fingerprint)
        self.assertEqual(updated.requires, (replacement,))

    def test_validation_and_manifest_failures_restore_previous_bytes(self):
        previous = self.build()
        manifest = previous.manifest.read_bytes()
        with self.assertRaisesRegex(ValueError, "validation failure"):
            self.build(self.request(validate=failing_validation))
        self.assertEqual(previous.manifest.read_bytes(), manifest)
        self.assertEqual(verify_artifact(previous), previous)
        with mock.patch("forgedeps.store._write_manifest", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.build(self.request(options={"changed": True}))
        self.assertEqual(previous.manifest.read_bytes(), manifest)
        self.assertEqual(verify_artifact(previous), previous)

    def test_tampered_artifact_is_preserved_even_for_rebuild(self):
        previous = self.build()
        header = previous.prefix / "include/payload.h"
        header.write_text("user change")
        with self.assertRaisesRegex(ValueError, "modified"):
            self.build(self.request(options={"changed": True}))
        self.assertEqual(header.read_text(), "user change")

    def test_verify_request_is_read_only_and_rejects_current_input_changes(self):
        previous = self.build()
        before = {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in self.home.rglob("*") if p.is_file()}
        with self.assertRaisesRegex(ValueError, "inputs changed"):
            verify_request(self.request(options={"flags": "-O0"}), previous, home=self.home)
        after = {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_header_label_does_not_drop_target_identity(self):
        first = self.build(self.request(header_only=True, target={"architecture": "a"}))
        second = self.build(self.request(header_only=True, target={"architecture": "b"}))
        self.assertEqual(first.prefix, self.home / "build/tiny/headers")
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_context_paths_are_home_owned_and_native_logs_persist(self):
        first = self.build()
        context = json.loads((first.prefix / "context.json").read_text())
        for key in ("source", "build", "prefix"):
            self.assertIn(self.home, Path(context[key]).parents)
        with contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = self.build(self.request(builder=log_builder, artifacts=(), license_files=()))
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("native stdout", stderr.getvalue())
        log = (result.prefix / "build.log").read_text()
        for text in ("native stdout", "native stderr", "captured"):
            self.assertIn(text, log)

    def test_symlinks_and_wrong_config_rejected_before_build(self):
        with self.assertRaisesRegex(ValueError, "configuration differs"):
            self.build(self.request(target={"label": "test-release", "configuration": "Debug"}))
        prepare_home(self.home)
        (self.home / "build/tiny").symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "real artifact directory"):
            self.build()
        self.assertFalse(self.counter.exists())

    def test_invalid_reference_never_creates_locks_outside_home(self):
        artifact = Artifact("tiny", "test", self.root / "unrelated/prefix", "a" * 64, "b" * 32)
        with self.assertRaisesRegex(ValueError, "reference location"):
            with ArtifactLease(artifact):
                pass
        self.assertFalse((self.root / "state").exists())

    def test_case_aliases_are_rejected_before_creating_slots(self):
        for changes in ({"name": "Tiny"}, {"target": {"label": "Native-Release"}}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "lowercase ASCII"):
                self.build(self.request(**changes))
        self.assertFalse((self.home / "build").exists())

    def test_concurrent_process_builds_compile_once(self):
        started, release = self.root / "started", self.root / "release"
        options = {"counter": str(self.counter), "started": str(started), "release": str(release)}
        first = self.process("--build", source=str(self.source), home=str(self.home), options=options)
        wait_for(started)
        second = self.process("--build", source=str(self.source), home=str(self.home), options=options)
        time.sleep(0.15)
        self.assertIsNone(second.poll())
        self.assertEqual(self.counter.read_text().splitlines(), ["build"])
        release.touch()
        self.assertEqual(json.loads(self.finish(first)), json.loads(self.finish(second)))
        self.assertEqual(self.counter.read_text().splitlines(), ["build"])

    def test_consumer_lease_blocks_replacement_and_process_exit_releases(self):
        previous = self.build()
        started, release = self.root / "leased", self.root / "release"
        holder = self.process("--hold", artifact=previous.to_dict(), started=str(started), release=str(release))
        wait_for(started)
        writer = self.process("--build", source=str(self.source), home=str(self.home), options={"changed": True})
        time.sleep(0.15)
        self.assertIsNone(writer.poll())
        self.assertEqual(verify_artifact(previous), previous)
        holder.kill()
        holder.communicate(timeout=10)
        replacement = Artifact.from_dict(json.loads(self.finish(writer)))
        self.assertNotEqual(previous.generation, replacement.generation)
        with self.assertRaisesRegex(ValueError, "generation changed"):
            with ArtifactLease(previous):
                pass

    def test_read_leases_nest_and_allow_other_process_readers(self):
        previous = self.build()
        started, release = self.root / "nested-reader", self.root / "release-reader"
        with ArtifactLease(previous, timeout=1):
            with ArtifactLease(previous, timeout=1):
                holder = self.process("--hold", artifact=previous.to_dict(),
                                      started=str(started), release=str(release))
                wait_for(started)
                release.touch()
                self.finish(holder)

    def test_read_to_write_upgrade_fails_instead_of_deadlocking(self):
        previous = self.build()
        with ArtifactLease(previous):
            with self.assertRaisesRegex(ValueError, "active lease"):
                self.build(self.request(options={"changed": True}))

    def test_writer_allows_child_dependency_checks_with_shared_dependency_locks(self):
        base = self.build(self.request(name="base"))
        built = self.build(self.request(builder=dependency_read_builder, requires=(base,),
                                       artifacts=("complete",), license_files=()))
        self.assertIn("child dependency check passed", (built.prefix / "build.log").read_text())

    def test_killed_writer_restores_old_slot_on_next_build(self):
        previous = self.build()
        manifest = previous.manifest.read_bytes()
        writer = self.process("--build", source=str(self.source), home=str(self.home), options={"crash": True})
        self.finish(writer, 77)
        restored = self.build()
        self.assertEqual(previous, restored)
        self.assertEqual(restored.manifest.read_bytes(), manifest)

    @unittest.skipUnless(shutil.which("cmake") and shutil.which("ninja"), "CMake and Ninja required")
    def test_real_library_compile_shared_by_two_consumers_and_incremental_header_update(self):
        (self.source / "CMakeLists.txt").write_text('''cmake_minimum_required(VERSION 3.20)
project(tiny LANGUAGES C)
add_library(tiny STATIC tiny.c)
install(TARGETS tiny ARCHIVE DESTINATION lib)
install(FILES payload.h DESTINATION include)
''')
        (self.source / "tiny.c").write_text("#ifndef LIBRARY_ANSWER\n#define LIBRARY_ANSWER 7\n#endif\nint library_answer(void) { return LIBRARY_ANSWER; }\n")
        request = self.request(builder=cmake_builder, artifacts=("include/payload.h",))
        artifact = self.build(request)
        other_source = self.root / "consumer-b/source"
        shutil.copytree(self.source, other_source)
        self.assertEqual(artifact, self.build(replace(request, source=other_source)))
        self.assertEqual(self.counter.read_text().splitlines(), ["compile"])
        consumers = []
        for name in ("a", "b"):
            directory = self.root / name
            directory.mkdir()
            (directory / "CMakeLists.txt").write_text('''cmake_minimum_required(VERSION 3.20)
project(consumer LANGUAGES C)
find_library(TINY_LIBRARY NAMES tiny REQUIRED PATHS "${SHARED_PREFIX}/lib" NO_DEFAULT_PATH)
add_executable(consumer main.c)
target_include_directories(consumer PRIVATE "${SHARED_PREFIX}/include")
target_link_libraries(consumer PRIVATE "${TINY_LIBRARY}")
''')
            (directory / "main.c").write_text('#include <stdio.h>\n#include <payload.h>\nint library_answer(void);\nint main(void){printf("%d\\n", ANSWER + library_answer());return 0;}\n')
            build = directory / "build"
            with ArtifactLease(artifact):
                subprocess.run(["cmake", "-S", str(directory), "-B", str(build), "-G", "Ninja",
                                f"-DSHARED_PREFIX={artifact.prefix}"], check=True, capture_output=True)
                subprocess.run(["cmake", "--build", str(build)], check=True, capture_output=True)
                executable = build / ("consumer.exe" if os.name == "nt" else "consumer")
                self.assertEqual(subprocess.check_output([str(executable)], text=True).strip(), "8")
            consumers.append((build, executable))
        old = (self.source / "payload.h").stat()
        (self.source / "payload.h").write_text("#define ANSWER 2\n")
        os.utime(self.source / "payload.h", ns=(old.st_atime_ns, old.st_mtime_ns))
        updated = self.build(request)
        for build, executable in consumers:
            with ArtifactLease(updated):
                subprocess.run(["cmake", "--build", str(build)], check=True, capture_output=True)
                self.assertEqual(subprocess.check_output([str(executable)], text=True).strip(), "9")
        changed_flags = self.build(replace(request, options={"counter": str(self.counter),
                                                            "flags": "-DLIBRARY_ANSWER=20"}))
        self.assertNotEqual(changed_flags.fingerprint, updated.fingerprint)
        with ArtifactLease(changed_flags):
            for build, executable in consumers:
                subprocess.run(["cmake", "--build", str(build)], check=True, capture_output=True)
                self.assertEqual(subprocess.check_output([str(executable)], text=True).strip(), "22")
        self.assertEqual(self.counter.read_text().splitlines(), ["compile"] * 3)


class HomeTests(unittest.TestCase):
    def test_home_precedence_and_wheel_requires_registration(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            registry = base / "config/workspace.json"
            workspace = register_workspace(base / "registered", registry=registry)
            explicit = base / "explicit"
            explicit.mkdir()
            with mock.patch("forgedeps.home.registry_path", return_value=registry), \
                    mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(resolve_home(), workspace)
                with mock.patch.dict(os.environ, {"FORGEDEPS_HOME": str(explicit)}):
                    self.assertEqual(resolve_home(), explicit)
                    self.assertEqual(resolve_home(workspace), workspace)
                registry.unlink()
                with mock.patch("forgedeps.home._source_root", return_value=None):
                    with self.assertRaisesRegex(ValueError, "forgedeps init"):
                        resolve_home()
                registry.parent.mkdir(exist_ok=True)
                registry.write_text("invalid")
                with self.assertRaisesRegex(ValueError, "Invalid ForgeDeps workspace registry"):
                    resolve_home()
            self.assertTrue((workspace / "state/locks").is_dir())
            self.assertTrue((workspace / "state/transactions").is_dir())
            self.assertFalse((workspace / "locks").exists())

    def test_source_checkout_default_does_not_depend_on_cwd(self):
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch("forgedeps.home.registry_path", return_value=Path(temporary) / "missing"), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_home(), Path(__file__).resolve().parents[1])


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--build", "--hold"):
        worker(sys.argv[1], json.loads(sys.argv[2]))
    else:
        unittest.main()
