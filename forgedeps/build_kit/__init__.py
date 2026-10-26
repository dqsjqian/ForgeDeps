"""Shared build pipeline kit for C++ projects.

This subpackage provides the mechanics of a complete build pipeline
(deps -> build -> test -> bench -> package). Each project supplies its
own configuration (dependencies, CMake flags, platform quirks).

Public API:
    Pipeline: Main entry point. Configure with project-specific settings,
              then call .run().
    QtSupport: Feature mixin for Qt6-based projects.
    DllDeploy: Post-build step for Windows DLL deployment.

Example:
    from forgedeps.build_kit import Pipeline

    Pipeline(
        name="my-project",
        deps=[("third-party", ["python", "tools/fetch_deps.py"])],
        cmake_flags={"MY_OPTION": "ON"},
    ).run()
"""

from .pipeline import Pipeline
from .env import find_qt_prefix, find_msys2_bin, cpu_count, is_gcc
from .package import create_package, detect_version
from .qt import deploy_qt_dlls

__all__ = [
    "Pipeline", "find_qt_prefix", "find_msys2_bin", "cpu_count", "is_gcc",
    "create_package", "detect_version", "deploy_qt_dlls",
]
