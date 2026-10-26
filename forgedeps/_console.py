"""Diagnostics that remain usable with redirected legacy console encodings."""

import builtins
import sys


def safe_print(*values, sep=" ", end="\n", file=None, flush=False):
    """Escape unrepresentable characters without changing process-wide streams.

    Paths passed to tools and written to metadata retain their original Unicode
    spelling. Only diagnostic rendering uses escapes on a limited output stream.
    """
    stream = sys.stdout if file is None else file
    message = (" " if sep is None else sep).join(map(str, values))
    message += "\n" if end is None else end
    encoding = getattr(stream, "encoding", None)
    if encoding:
        message = message.encode(encoding, errors="backslashreplace").decode(encoding)
    builtins.print(message, end="", file=stream, flush=flush)
