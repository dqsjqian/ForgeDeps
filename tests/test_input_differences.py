import json, tempfile, unittest
from pathlib import Path
from forgedeps.store import _input_differences


class InputDifferenceTests(unittest.TestCase):
    def test_names_changed_scalars_and_list_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp)
            (prefix / "manifest.json").write_text(json.dumps({"inputs": {
                "target": "windows-x64-gcc-release",
                "options": ["-DCMAKE_BUILD_TYPE=Release"],
                "environment": {"CMAKE_CXX_FLAGS": ""},
            }}), encoding="utf-8")
            report = _input_differences(prefix, {
                "target": "windows-x64-gcc-release",
                "options": ["-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_DEBUG_POSTFIX="],
                "environment": {"CMAKE_CXX_FLAGS": "/O2"},
            })
        self.assertIn("added ['-DCMAKE_DEBUG_POSTFIX=']", report)
        self.assertIn("environment.CMAKE_CXX_FLAGS", report)
        self.assertNotIn("target", report)

    def test_missing_manifest_is_silent(self):
        self.assertEqual(_input_differences(Path("/nonexistent"), {"a": 1}), "")


if __name__ == "__main__":
    unittest.main()
