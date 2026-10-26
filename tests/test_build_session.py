from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest

from forgedeps.artifacts import BuildRequest, Environment
from forgedeps.build_kit.session import (assert_parent_covers_environment, leased_environment,
                                         run_session, source_checkout_path)
from forgedeps.environments import write_environment
from forgedeps.store import build_package


def install(ctx):
    (ctx.prefix / "header.h").write_text((ctx.source_snapshot / "header.h").read_text())


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "header.h").write_text("fixture")
        self.request = BuildRequest("fixture", self.source, install, (), {"label": "native-release"})
        self.artifact = build_package(self.request, home=self.home)
        self.receipt = self.root / "build/nested/forgedeps-environment.json"
        self.receipt.parent.mkdir(parents=True)
        write_environment(self.receipt, Environment((self.artifact,)))

    def test_prepares_all_configurations_then_reconfigures_and_runs_under_union_lease(self):
        second = build_package(replace(self.request, name="second"), home=self.home)
        module = self.root / "build/module"
        module.mkdir()
        (module / "CMakeCache.txt").touch()
        write_environment(module / "forgedeps-environment.json", Environment((second,)))
        commands = [["fetch"], ["cmake", "-S", ".", "-B", str(self.root / "build")],
                    ["cmake", "--build", str(self.root / "build")],
                    ["cmake", "-S", "module", "-B", str(module)],
                    ["ctest", "--test-dir", str(module)], ["package"]]
        events = []
        def execute(command, environment):
            leased = "FORGEDEPS_ENVIRONMENTS" in environment
            events.append((command[0], "-S" in command, leased))
            if leased:
                assert_parent_covers_environment(Environment((self.artifact, second)), environment)
                with self.assertRaisesRegex(ValueError, "active lease"):
                    build_package(self.request, home=self.home, force=True)
        def package(environment):
            assert_parent_covers_environment(Environment((second,)), environment)
            events.append(("post", False, True))
        run_session(commands, cwd=self.root, env={}, run_command=execute, post_build=package)
        self.assertEqual(events, [("fetch", False, False), ("cmake", True, False),
                                  ("cmake", True, False), ("cmake", True, True),
                                  ("cmake", True, True), ("cmake", False, True),
                                  ("ctest", False, True), ("package", False, True),
                                  ("post", False, True)])
        self.assertEqual(list((self.home / "tmp").glob("lease-*")), [])

    def test_receipt_snapshot_is_immutable_and_rejects_uncovered_new_dependency(self):
        other = build_package(replace(self.request, name="other"), home=self.home)
        with leased_environment([self.receipt], env={}) as environment:
            # Changing a consumer receipt cannot expand the parent's lease.
            write_environment(self.receipt, Environment((other,)))
            with self.assertRaisesRegex(ValueError, "outside the parent build lease"):
                assert_parent_covers_environment(Environment((other,)), environment)
            assert_parent_covers_environment(Environment((self.artifact,)), environment)

    def test_explicit_shared_prefix_is_leased_without_generated_receipt(self):
        self.receipt.unlink()
        cache = self.root / "build/CMakeCache.txt"
        cache.write_text("CMAKE_PREFIX_PATH:PATH=" + str(self.artifact.prefix) + "\n")
        seen = []
        def execute(command, environment):
            if command[0] == "build":
                assert_parent_covers_environment(Environment((self.artifact,)), environment)
                with self.assertRaisesRegex(ValueError, "active lease"):
                    build_package(self.request, home=self.home, force=True)
                seen.append(True)
        run_session([["cmake", "-S", ".", "-B", str(cache.parent)], ["build"]],
                    env={}, cwd=self.root, run_command=execute)
        self.assertEqual(seen, [True])

    def test_conflicting_generations_stop_before_build(self):
        newer = build_package(replace(self.request, options={"flag": True}), home=self.home)
        second = self.root / "second.json"
        write_environment(second, Environment((newer,)))
        commands = []
        with self.assertRaisesRegex(ValueError, "Conflicting artifact references"):
            run_session([["build"]], receipts=[self.receipt, second], env={},
                        run_command=lambda *args: commands.append(args))
        self.assertEqual(commands, [])

    def test_nested_session_does_not_prepare_under_parent_read_lease(self):
        calls = []
        with leased_environment([self.receipt], env={}) as environment:
            run_session([["cmake", "-S", ".", "-B", str(self.root / "build")], ["build"]],
                        env=environment, cwd=self.root,
                        run_command=lambda command, env: calls.append((command, dict(env))))
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("FORGEDEPS_ENVIRONMENTS" in env for _, env in calls))

    def test_failure_releases_read_lease_and_removes_handoff(self):
        def fail(command, environment):
            raise RuntimeError("consumer failed")
        with self.assertRaisesRegex(RuntimeError, "consumer failed"):
            run_session([["build"]], receipts=[self.receipt], env={}, run_command=fail)
        self.assertEqual(list((self.home / "tmp").glob("lease-*")), [])
        build_package(self.request, home=self.home, force=True)

    def test_unrelated_cached_builds_are_not_accidentally_selected(self):
        unrelated = self.root / "build/old-project"
        unrelated.mkdir()
        (unrelated / "CMakeCache.txt").touch()
        (unrelated / "forgedeps-environment.json").write_text("invalid")
        run_session([["cmake", "-S", ".", "-B", str(self.root / "build")]], env={},
                    run_command=lambda command, environment: None)

    def test_default_source_is_shared_but_existing_checkout_is_preserved(self):
        self.assertEqual(source_checkout_path(self.root, name="aria", home=self.home), self.home / "sources/aria")
        legacy = self.root / "build/deps/aria"
        legacy.mkdir(parents=True)
        (legacy / "local-edit.txt").write_text("keep")
        self.assertEqual(source_checkout_path(self.root, name="aria", home=self.home), legacy)
        self.assertEqual((legacy / "local-edit.txt").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
