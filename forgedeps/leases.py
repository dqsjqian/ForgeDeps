"""Process-owned leases for stable artifact slots, released on process exit."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import errno
import hashlib
import os
from pathlib import Path
import re
import threading
import time

from .artifacts import Artifact

_HELD = threading.local()


@contextmanager
def _lock(prefix: Path, timeout: float | None = None, *, shared=False):
    prefix = Path(prefix).absolute()
    held = getattr(_HELD, "slots", None)
    if held is None:
        held = _HELD.slots = {}
    lock_identity = os.path.normcase(str(prefix))
    key = (os.getpid(), lock_identity)
    readers, writers = held.get(key, (0, 0))
    if readers or writers:
        if not shared:
            raise ValueError(f"Cannot replace an artifact inside this thread's active lease: {prefix}")
        if writers:
            yield  # The current thread already owns the stronger OS lock.
            return
    home = prefix.parent.parent.parent
    directory = home / "state/locks"
    if directory.parent.is_symlink():
        raise ValueError(f"Unsafe artifact state directory: {directory.parent}")
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ValueError(f"Unsafe artifact lock directory: {directory}")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(lock_identity.encode()).hexdigest() + ".lock")
    if path.is_symlink():
        raise ValueError(f"Unsafe artifact lock: {path}")
    deadline = None if timeout is None else time.monotonic() + timeout
    with path.open("a+b") as stream:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            import msvcrt
            class Overlapped(ctypes.Structure):
                _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                            ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                            ("hEvent", wintypes.HANDLE)]
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
            api.LockFileEx.restype = wintypes.BOOL
            api.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                        wintypes.DWORD, ctypes.POINTER(Overlapped)]
            api.UnlockFileEx.restype = wintypes.BOOL
            handle = msvcrt.get_osfhandle(stream.fileno())
            overlapped = Overlapped()
            def acquire():
                # LOCKFILE_FAIL_IMMEDIATELY plus optional EXCLUSIVE_LOCK.
                if not api.LockFileEx(handle, 1 | (0 if shared else 2), 0, 1, 0,
                                      ctypes.byref(overlapped)):
                    raise ctypes.WinError(ctypes.get_last_error())
            def release():
                if not api.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
                    raise ctypes.WinError(ctypes.get_last_error())
        else:
            import fcntl
            def acquire():
                fcntl.flock(stream, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            def release():
                fcntl.flock(stream, fcntl.LOCK_UN)
        while True:
            try:
                acquire()
                break
            except OSError as error:
                if (error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK)
                        and getattr(error, "winerror", None) != 33):
                    raise
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for artifact lease: {prefix}") from error
                time.sleep(0.05)
        try:
            readers, writers = held.get(key, (0, 0))
            held[key] = (readers + int(shared), writers + int(not shared))
            yield
        finally:
            readers, writers = held[key]
            held[key] = (readers - int(shared), writers - int(not shared))
            if held[key] == (0, 0):
                del held[key]
            release()


def artifact_graph(artifacts):
    result = {}
    active = set()
    def visit(artifact):
        if not isinstance(artifact, Artifact):
            raise ValueError("Artifact leases require complete Artifact references")
        path = Path(artifact.prefix).absolute()
        if (Path(artifact.prefix) != path or path != path.resolve()
                or path.name != artifact.target_label or path.parent.name != artifact.name
                or path.parent.parent.name != "build"
                or not all(isinstance(value, str) and re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", value)
                           for value in (artifact.name, artifact.target_label))):
            raise ValueError(f"Invalid artifact reference location: {artifact.prefix}")
        if path in active:
            raise ValueError("Artifact dependency graph contains a cycle")
        if path in result:
            if result[path] != artifact:
                raise ValueError(f"Conflicting artifact references for {path}")
            return
        active.add(path)
        for dependency in artifact.requires:
            visit(dependency)
        active.remove(path)
        result[path] = artifact
    for artifact in artifacts:
        visit(artifact)
    return result


@contextmanager
def slot_locks(prefixes, *, timeout: float | None = None, shared=False, write_prefixes=None):
    # Every writer and consumer acquires the complete graph in this one order.
    with ExitStack() as stack:
        writers = None if write_prefixes is None else {Path(path).absolute() for path in write_prefixes}
        for prefix in sorted({Path(path).absolute() for path in prefixes}, key=str):
            read_only = prefix not in writers if writers is not None else shared
            stack.enter_context(_lock(prefix, timeout, shared=read_only))
        yield


class ArtifactLease:
    """Hold across configure, build, test and packaging when using slot paths."""
    def __init__(self, artifacts, *, timeout: float | None = None):
        self.artifacts = (artifacts,) if isinstance(artifacts, Artifact) else tuple(artifacts)
        self.timeout = timeout
        self._stack = None

    def __enter__(self):
        from .store import verify_artifact
        graph = artifact_graph(self.artifacts)
        self._stack = ExitStack()
        try:
            self._stack.enter_context(slot_locks(graph, timeout=self.timeout, shared=True))
            for artifact in graph.values():
                verify_artifact(artifact)
            return self
        except BaseException:
            self._stack.close()
            self._stack = None
            raise

    def __exit__(self, *exc):
        if self._stack is not None:
            return self._stack.__exit__(*exc)
