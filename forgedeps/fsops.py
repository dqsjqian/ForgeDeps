"""Filesystem helpers whose behaviour has to be identical on every platform.

Windows refuses to delete a read-only file, and a directory tree removed while a
compiler or a virus scanner still holds a handle fails with a sharing violation.
Both conditions have been observed on CI runners right after a dependency build,
so every tree removal goes through one implementation instead of a plain
``shutil.rmtree`` scattered across modules.
"""
from __future__ import annotations

import os
import shutil
import stat
import time


def _clear_readonly(function, name, exc_info):
    error = exc_info[1]
    if os.name != "nt" or not isinstance(error, PermissionError):
        raise error
    # Git object files and extracted build artifacts can carry the Windows
    # read-only attribute. A delay does not clear it; make the failed path
    # writable, then retry the exact filesystem operation.
    os.chmod(name, stat.S_IREAD | stat.S_IWRITE)
    function(name)


def remove_tree(path, *, attempts: int = 4) -> None:
    """Remove a tree, tolerating Windows read-only files and brief locks.

    ``ignore_errors`` is deliberately not used: these deletions exist to stop
    leftovers from being read as state, so a failure has to surface.
    """
    for attempt in range(attempts):
        try:
            if os.name == "nt":
                shutil.rmtree(path, onerror=_clear_readonly)
            else:
                shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1 * (attempt + 1))
