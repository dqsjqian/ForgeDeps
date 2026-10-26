"""Real compilation checks for target identity; no target executable is run."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from forgedeps.toolchains import _label, probe_toolchain


@contextlib.contextmanager
def working_directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


@unittest.skipUnless(shutil.which("cmake"), "CMake unavailable")
class ToolchainProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="aria toolchain tests ")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.home = cls.root / "workspace"
        cls.home.mkdir()
        cls.baseline = probe_toolchain(home=cls.home)

    def probe(self, **kwargs):
        return probe_toolchain(home=self.home, **kwargs)

    def test_native_compiles_both_languages_and_records_actual_inputs(self):
        target = self.baseline
        self.assertTrue(target["label"].endswith("-release"))
        self.assertEqual(target["configuration"], "Release")
        for language in ("c", "cxx"):
            compiler = target[language]
            self.assertTrue(Path(compiler["path"]).is_file())
            self.assertRegex(compiler["binary_sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(compiler["id"])
            self.assertTrue(compiler["version"])
            self.assertEqual(compiler["abi"][0]["arch"], target["machine"])
            self.assertNotEqual(compiler["abi"][0]["platform"], "unknown")
        self.assertTrue(target["tools"]["cmake"]["version"])
        self.assertTrue(target["toolchain"]["inputs"])
        self.assertNotIn(str(self.home), json.dumps(target))
        self.assertEqual(list((self.home / "tmp").iterdir()), [])

    def test_consumer_cwd_python_and_temporary_home_do_not_change_identity(self):
        consumer = self.root / "unrelated consumer"
        consumer.mkdir()
        another_home = self.root / "second workspace"
        another_home.mkdir()
        with working_directory(consumer):
            actual = probe_toolchain(home=another_home, environment={
                "PWD": str(consumer), "VIRTUAL_ENV": str(consumer / "venv"),
                "PYTHONHOME": str(consumer / "python"), "PYTHONPATH": str(consumer / "modules"),
            })
        self.assertEqual(actual, self.baseline)

    def test_effective_flags_change_identity_without_changing_human_label(self):
        changed = self.probe(cmake_options=("-DCMAKE_CXX_FLAGS:STRING=-DARIA_PROBE_ABI=1",))
        self.assertEqual(changed["label"], self.baseline["label"])
        self.assertNotEqual(changed, self.baseline)
        fragments = changed["cxx"]["compile"]["compileCommandFragments"]
        self.assertIn("-DARIA_PROBE_ABI=1", " ".join(item["fragment"] for item in fragments))

    def test_configuration_changes_actual_runtime_flags_and_label(self):
        debug = self.probe(configuration="Debug")
        self.assertTrue(debug["label"].endswith("-debug"))
        self.assertNotEqual(debug["cxx"]["compile"], self.baseline["cxx"]["compile"])

    def test_lto_compiles_requested_flags_and_still_reads_real_abi(self):
        if self.baseline["cxx"]["id"] not in ("AppleClang", "GNU", "MSVC"):
            self.skipTest("LTO linker/plugin availability is not guaranteed for this compiler")
        changed = self.probe(cmake_options=("-DCMAKE_INTERPROCEDURAL_OPTIMIZATION=ON",))
        self.assertEqual(changed["machine"], self.baseline["machine"])
        self.assertEqual(changed["abi"], self.baseline["abi"])
        flags = " ".join(item["fragment"] for item in changed["cxx"]["compile"]["compileCommandFragments"])
        self.assertTrue("lto" in flags or "/GL" in flags, flags)
        self.assertNotEqual(changed, self.baseline)

    def test_normal_toolchain_variables_and_included_file_content_are_recorded(self):
        directory = self.root / "toolchain space"
        directory.mkdir()
        toolchain = directory / "toolchain.cmake"
        included = directory / "settings.cmake"
        toolchain.write_text('include("${CMAKE_CURRENT_LIST_DIR}/settings.cmake")\n', encoding="utf-8")
        included.write_text('set(CMAKE_CXX_FLAGS "-DARIA_NORMAL_TOOLCHAIN=7")\n', encoding="utf-8")
        first = self.probe(toolchain=toolchain)
        self.assertEqual(first["cmake"]["CMAKE_CXX_FLAGS"], "-DARIA_NORMAL_TOOLCHAIN=7")
        first_input = next(item for item in first["toolchain"]["inputs"] if item["path"] == str(included.resolve()))
        # Even a same-flags edit must change the file content identity.
        included.write_text(included.read_text() + "# changed toolchain content\n", encoding="utf-8")
        second = self.probe(toolchain=toolchain)
        second_input = next(item for item in second["toolchain"]["inputs"] if item["path"] == str(included.resolve()))
        self.assertEqual(first["label"], second["label"])
        self.assertNotEqual(first_input["sha256"], second_input["sha256"])
        self.assertEqual(first["cxx"]["compile"], second["cxx"]["compile"])

    def test_explicit_nonexistent_compiler_does_not_fall_back(self):
        with self.assertRaisesRegex(ValueError, "probe failed"):
            self.probe(cxx=str(self.root / "does-not-exist-cxx"))

    def test_unknown_cross_target_is_not_labelled_as_host(self):
        toolchain = self.root / "unknown.cmake"
        toolchain.write_text("set(CMAKE_SYSTEM_NAME Generic)\n"
                             "set(CMAKE_SYSTEM_PROCESSOR imaginary)\n"
                             "set(CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY)\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "disagrees with compiled target|CMake toolchain probe failed"):
            self.probe(toolchain=toolchain)

    def test_probe_project_cannot_be_replaced(self):
        with self.assertRaisesRegex(ValueError, "cannot replace"):
            self.probe(cmake_options=("--preset=consumer",))

    def test_cmake_builder_replays_probed_flags_pic_and_selected_tool(self):
        from forgedeps.artifacts import BuildContext
        from forgedeps.environments import _cmake_build
        options = ("-DCMAKE_C_FLAGS=-DARIA_REPLAY_ABI=7", "-DCMAKE_CXX_FLAGS=-DARIA_REPLAY_ABI=7")
        target = self.probe(cmake_options=options)
        base = self.root / "real recipe"
        source, build, prefix = (base / name for name in ("source", "build", "install"))
        source.mkdir(parents=True)
        build.mkdir()
        (source / "CMakeLists.txt").write_text('''cmake_minimum_required(VERSION 3.21)
project(Replay LANGUAGES C CXX)
add_library(replay STATIC replay.c replay.cpp)
install(TARGETS replay ARCHIVE DESTINATION lib)
file(WRITE "${CMAKE_BINARY_DIR}/actual.json"
  "{\\"pic\\":\\"${CMAKE_POSITION_INDEPENDENT_CODE}\\",\\"flags\\":\\"${CMAKE_CXX_FLAGS}\\",\\"compiler\\":\\"${CMAKE_CXX_COMPILER}\\"}")
''', encoding="utf-8")
        for language in ("c", "cpp"):
            (source / ("replay." + language)).write_text(
                "#if ARIA_REPLAY_ABI != 7\n#error selected flags were not replayed\n#endif\n"
                f"int replay_{language}(void) {{ return ARIA_REPLAY_ABI; }}\n", encoding="utf-8")
        # A later ambient flags change must not replace the recorded flags.
        environment = dict(os.environ, CFLAGS="-DARIA_REPLAY_ABI=0", CXXFLAGS="-DARIA_REPLAY_ABI=0")
        ctx = BuildContext(source, build, prefix, (), {"cmake_options": list(options)},
                           environment, target, {}, 2, "Release")
        with contextlib.redirect_stderr(io.StringIO()):
            _cmake_build(ctx)
        actual = json.loads((build / "actual.json").read_text())
        self.assertEqual(actual["flags"], target["cmake"]["CMAKE_CXX_FLAGS"])
        self.assertEqual(actual["pic"], target["cmake"]["CMAKE_POSITION_INDEPENDENT_CODE"])
        self.assertEqual(Path(actual["compiler"]).resolve(), Path(target["cxx"]["path"]).resolve())
        self.assertTrue(list((prefix / "lib").iterdir()))

    def test_toolchain_flag_prefix_is_applied_once_when_builder_replays(self):
        from forgedeps.artifacts import BuildContext
        from forgedeps.environments import _cmake_build
        base = self.root / "additive toolchain"
        base.mkdir()
        toolchain = base / "toolchain.cmake"
        toolchain.write_text('set(CMAKE_CXX_FLAGS "" CACHE STRING "Caller flags")\n'
                             'set(CMAKE_CXX_FLAGS "-DARIA_PLATFORM_ABI=9 ${CMAKE_CXX_FLAGS}")\n', encoding="utf-8")
        target = self.probe(toolchain=toolchain)
        self.assertEqual(target["cmake_cache"]["CMAKE_CXX_FLAGS"], "")
        source, build, prefix = (base / name for name in ("source", "build", "install"))
        source.mkdir()
        build.mkdir()
        (source / "CMakeLists.txt").write_text('''cmake_minimum_required(VERSION 3.21)
project(Replay LANGUAGES C CXX)
add_library(replay STATIC replay.cpp)
install(TARGETS replay ARCHIVE DESTINATION lib)
file(WRITE "${CMAKE_BINARY_DIR}/flags.txt" "${CMAKE_CXX_FLAGS}")
''', encoding="utf-8")
        (source / "replay.cpp").write_text("int replay() { return ARIA_PLATFORM_ABI; }\n", encoding="utf-8")
        ctx = BuildContext(source, build, prefix, (), {"cmake_options": []},
                           dict(os.environ), target, {}, 2, "Release")
        with contextlib.redirect_stderr(io.StringIO()):
            _cmake_build(ctx)
        self.assertEqual((build / "flags.txt").read_text(), target["cmake"]["CMAKE_CXX_FLAGS"])

    @unittest.skipUnless(sys.platform == "darwin", "Apple cross SDK smoke requires macOS")
    def test_ios_device_and_simulator_are_different_actual_targets(self):
        def sdk(name):
            result = subprocess.run(["xcrun", "--sdk", name, "--show-sdk-path"],
                                    capture_output=True, text=True)
            if result.returncode:
                self.skipTest(f"{name} SDK unavailable")
            return result.stdout.strip()
        targets = []
        for name in ("iphoneos", "iphonesimulator"):
            targets.append(self.probe(cmake_options=(
                "-DCMAKE_SYSTEM_NAME=iOS", "-DCMAKE_OSX_ARCHITECTURES=arm64",
                "-DCMAKE_OSX_SYSROOT=" + sdk(name), "-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY")))
        self.assertEqual(targets[0]["platform"], "iOS")
        self.assertEqual(targets[1]["platform"], "iOS")
        self.assertTrue(targets[0]["label"].startswith("ios-arm64-"))
        self.assertTrue(targets[1]["label"].startswith("iossimulator-arm64-"))
        self.assertNotEqual(targets[0]["sdk"], targets[1]["sdk"])

    @unittest.skipIf(os.name == "nt", "Alternate native GNU discovery is Unix-only")
    def test_available_gcc_is_used_without_substituting_clang(self):
        candidates = [(shutil.which(f"gcc-{version}"), shutil.which(f"g++-{version}"))
                      for version in (16, 15, 14, 13, 12)]
        candidates.append((shutil.which("gcc"), shutil.which("g++")))
        for c, cxx in candidates:
            if c and cxx:
                banner = subprocess.check_output([c, "--version"], text=True)
                if "clang" not in banner.lower():
                    target = self.probe(c=c, cxx=cxx)
                    self.assertEqual(target["c"]["id"], "GNU")
                    self.assertEqual(target["cxx"]["id"], "GNU")
                    self.assertEqual(Path(target["c"]["path"]).resolve(), Path(c).resolve())
                    self.assertIn("-gcc-", target["label"])
                    return
        self.skipTest("Native GCC unavailable")


class TargetLabelTests(unittest.TestCase):
    def test_msvc_patch_is_metadata_not_a_label_segment(self):
        marker = {"C": [{"arch": "x86_64", "platform": "windows"}]}
        outputs = []
        for version in ("19.50.35722", "19.50.35728"):
            outputs.append(_label({"CMAKE_SYSTEM_NAME": "Windows", "CMAKE_CXX_COMPILER_ID": "MSVC",
                                   "CMAKE_CXX_COMPILER_VERSION": version}, marker, "Release")[0])
        self.assertEqual(outputs, ["windows-x64-msvc2026-release"] * 2)

    def test_android_api_is_not_confused_with_host_linux(self):
        record = {"CMAKE_SYSTEM_NAME": "Android", "CMAKE_CXX_COMPILER_ID": "Clang"}
        labels = []
        for api in ("26", "35"):
            markers = {"C": [{"arch": "arm64", "platform": "android", "android_api": api}]}
            labels.append(_label(record, markers, "Release")[0])
        self.assertEqual(labels, ["android-api26-arm64-clang-release", "android-api35-arm64-clang-release"])


if __name__ == "__main__":
    unittest.main()
