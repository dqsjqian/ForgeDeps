"""Filesystem removal must survive the states CI leaves behind."""
import os
import stat
import tempfile
import unittest
from pathlib import Path

from forgedeps import fsops
from forgedeps.store import BUILD_ENVIRONMENT_KEYS


class RemoveTreeTests(unittest.TestCase):
    def test_read_only_file_does_not_block_removal(self):
        # Windows refuses to delete a read-only file, which is how extraction
        # and git leave object files; the helper clears the attribute.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tree"
            (root / "nested").mkdir(parents=True)
            target = root / "nested" / "libcrypto.pc"
            target.write_text("prefix=/usr\n", encoding="utf-8")
            os.chmod(target, stat.S_IREAD)
            self.addCleanup(lambda: os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
                            if target.exists() else None)
            fsops.remove_tree(root)
            self.assertFalse(root.exists())

    def test_missing_tree_is_not_an_error(self):
        fsops.remove_tree(Path(tempfile.gettempdir()) / "forgedeps-does-not-exist")


class BuildEnvironmentIdentityTests(unittest.TestCase):
    def test_resource_compiler_names_are_not_inherited(self):
        # CMake resolves the resource compiler itself and the probe records it;
        # an inherited name made the identity depend on the launching shell.
        self.assertNotIn("RC", BUILD_ENVIRONMENT_KEYS)
        self.assertNotIn("WINDRES", BUILD_ENVIRONMENT_KEYS)


if __name__ == "__main__":
    unittest.main()
