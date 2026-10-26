import ast
import json
import sys
import tempfile
import unittest
from importlib import metadata
from pathlib import Path

from forgedeps import bootstrap

SOURCE = Path(bootstrap.__file__)


class BootstrapContractTests(unittest.TestCase):
    def test_runs_before_forgedeps_is_installed(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                modules.add(node.module.split(".")[0])
        modules.discard("__future__")
        self.assertNotIn("forgedeps", modules)
        self.assertLessEqual(modules, set(sys.stdlib_module_names))

    def test_pins_accept_tagged_urls_and_exact_versions(self):
        with tempfile.TemporaryDirectory() as temporary:
            requirements = Path(temporary) / "requirements-build.txt"
            requirements.write_text(
                "# comment\n"
                "forgedeps @ git+https://github.com/dqsjqian/ForgeDeps.git@v0.4.4\n"
                "Forge_Recipes==0.1.6  # trailing\n", encoding="utf-8")
            self.assertEqual(bootstrap.pinned_versions(requirements),
                             {"forgedeps": "0.4.4", "forge-recipes": "0.1.6"})

    def test_unpinned_requirements_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            requirements = Path(temporary) / "requirements-build.txt"
            for line in ("forgedeps>=0.4", "forgedeps @ git+https://example.invalid/x.git@main"):
                requirements.write_text(line + "\n", encoding="utf-8")
                with self.assertRaises(SystemExit):
                    bootstrap.pinned_versions(requirements)

    def test_satisfied_requires_the_exact_installed_version(self):
        installed = metadata.version("forgedeps")
        self.assertTrue(bootstrap.satisfied({"forgedeps": installed}))
        self.assertFalse(bootstrap.satisfied({"forgedeps": installed + ".1"}))
        self.assertFalse(bootstrap.satisfied({"definitely-not-installed": "1.0"}))

    def test_workspace_follows_the_forgedeps_resolution_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            explicit = root / "explicit"
            self.assertEqual(bootstrap.workspace_home({"FORGEDEPS_HOME": str(explicit)}), (explicit, False))
            config = root / "config"
            (config / "forgedeps").mkdir(parents=True)
            registered = root / "registered"
            (config / "forgedeps/workspace.json").write_text(
                json.dumps({"schema": 1, "workspace": str(registered)}), encoding="utf-8")
            key = "APPDATA" if sys.platform == "win32" else "XDG_CONFIG_HOME"
            self.assertEqual(bootstrap.workspace_home({key: str(config)}), (registered, False))
            home, unregistered = bootstrap.workspace_home({key: str(root / "empty")})
            self.assertEqual(home.name, ".forgedeps")
            self.assertTrue(unregistered)

    def test_one_environment_per_pinned_tooling(self):
        home = Path("/workspace")
        first = bootstrap.tools_environment(home, "forgedeps @ x@v0.4.4\n")
        self.assertEqual(first, bootstrap.tools_environment(home, "forgedeps @ x@v0.4.4"))
        self.assertNotEqual(first, bootstrap.tools_environment(home, "forgedeps @ x@v0.4.5\n"))
        self.assertEqual(first.parent, home / "tools")

    def test_disabled_bootstrap_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "absent.txt"
            original = bootstrap.os.environ.get("FORGEDEPS_BOOTSTRAP")
            bootstrap.os.environ["FORGEDEPS_BOOTSTRAP"] = "0"
            try:
                self.assertIsNone(bootstrap.ensure(missing))
            finally:
                if original is None:
                    del bootstrap.os.environ["FORGEDEPS_BOOTSTRAP"]
                else:
                    bootstrap.os.environ["FORGEDEPS_BOOTSTRAP"] = original


if __name__ == "__main__":
    unittest.main()
