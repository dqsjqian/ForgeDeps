"""Unicode build paths must not fail because stdout is a legacy code page."""

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from forgedeps._console import safe_print
from forgedeps import deps_build, deps_sources


class ConsoleTests(unittest.TestCase):
    def test_legacy_encoding_escapes_only_unrepresentable_text(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", newline="\n")
        safe_print("café", "目录", file=stream, sep=" / ", flush=True)
        self.assertEqual(raw.getvalue().decode("cp1252"), "café / \\u76ee\\u5f55\n")
        self.assertEqual(stream.errors, "strict")

    def test_utf8_and_in_memory_streams_preserve_unicode(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            safe_print("目录", end="!")
        self.assertEqual(stream.getvalue(), "目录!")
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="utf-8", newline="\n")
        safe_print("目录", file=stream, flush=True)
        self.assertEqual(raw.getvalue().decode("utf-8"), "目录\n")

    def test_build_command_arguments_are_not_escaped(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
        command = ["cmake", "-S", "目录"]
        with redirect_stdout(stream), patch.object(deps_build.subprocess, "run") as run:
            deps_build.run(command)
        self.assertEqual(run.call_args.args[0], command)
        self.assertIn(b"\\u76ee\\u5f55", raw.getvalue())
        self.assertIs(deps_sources.print, safe_print)


if __name__ == "__main__":
    unittest.main()
