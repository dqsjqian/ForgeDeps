"""Minimal project fixtures for forgedeps mechanism tests.

These recipes exercise the pipeline machinery (resolve/install/verify)
without any project-specific policy. They are intentionally tiny.
"""

from forgedeps import Dependency, ProjectConfig


def _hook_copied(prefix, source, dep):
    target = prefix / "share/licenses" / dep.name / "HOOK.txt"
    target.write_text("hook ran\n", encoding="utf-8")
    return ["HOOK.txt"]


def _hook_build(prefix, source, dep):
    (prefix / "hook-build-marker.txt").write_text(dep.name, encoding="utf-8")


RECIPES = (
    Dependency(
        name="libfoo", version="",
        url="", sha256="",
        license="MIT", license_files=("LICENSE",), root="", kind="cmake",
        options=("-DFOO=ON",),
        artifacts=("include/nlohmann/json.hpp",),
    ),
    Dependency(
        name="libbar", version="",
        url="", sha256="",
        license="MIT", license_files=("LICENSE",), root="", kind="cmake",
        artifacts=("lib/libbar.a",),
        requires=("libfoo",),
        post_install=_hook_copied,
        post_build=_hook_build,
    ),
    Dependency(
        name="libgit", version="",
        url="", sha256="", revision="",
        license="MIT", license_files=("LICENSE",), root="", kind="git",
        options=("-DGIT=ON",),
        artifacts=("lib/libgit.a",),
    ),
)


def make_config(**overrides):
    kwargs = dict(
        name="testproj",
        recipes=RECIPES,
        patches_dir=None,
        patch_versions={},
    )
    kwargs.update(overrides)
    return ProjectConfig(**kwargs)


CONFIG = make_config()
