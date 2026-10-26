"""Case-insensitive environment merging for child processes.

Windows environment blocks are case-insensitive: ``WindowsSDKLibVersion`` and
``WINDOWSSDKLIBVERSION`` name the same variable. ``os.environ`` normalizes the
keys it owns, but a mapping merged from a recipe, a toolchain probe or an
explicit overlay can still hold both spellings at once. CPython before 3.11
forwards both entries to ``CreateProcess`` unchanged, and MSBuild rejects such
a block with ``Item has already been added``. Normalize on every merge so the
child environment never depends on the interpreter version.
"""
from __future__ import annotations

import os
from typing import Iterable, Mapping


def case_insensitive_keys(case_insensitive: bool | None = None) -> bool:
    """Report whether variable names collide ignoring case (Windows by default)."""
    return os.name == "nt" if case_insensitive is None else bool(case_insensitive)


def merge_environments(*layers: Mapping[str, str] | None,
                       case_insensitive: bool | None = None) -> dict[str, str]:
    """Merge environment layers, later layers winning.

    On a case-insensitive platform a later layer replaces the earlier entry
    regardless of spelling, and the surviving key keeps the spelling of the
    layer that supplied the value.
    """
    fold = case_insensitive_keys(case_insensitive)
    merged: dict[str, str] = {}
    owners: dict[str, str] = {}
    for layer in layers:
        if layer is None:
            continue
        for raw_name, raw_value in layer.items():
            name, value = str(raw_name), str(raw_value)
            key = name.casefold() if fold else name
            previous = owners.get(key)
            if previous is not None and previous != name:
                merged.pop(previous, None)
            owners[key] = name
            merged[name] = value
    return merged


def select_environment(names: Iterable[str], source: Mapping[str, str],
                       case_insensitive: bool | None = None) -> dict[str, str]:
    """Select ``names`` from ``source``, keeping the requested spelling.

    Empty values are dropped. On a case-insensitive platform a source key such
    as ``WINDOWSSDKLIBVERSION`` satisfies the requested ``WindowsSDKLibVersion``.
    """
    fold = case_insensitive_keys(case_insensitive)
    index = ({str(name).casefold(): str(value) for name, value in source.items()}
             if fold else {str(name): str(value) for name, value in source.items()})
    selected: dict[str, str] = {}
    for name in sorted(names):
        value = index.get(name.casefold() if fold else name)
        if value:
            selected[name] = value
    return selected
