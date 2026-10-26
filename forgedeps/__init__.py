"""forgedeps: project-agnostic dependency pipeline for C/C++ projects.

Resolve locked dependencies, fetch sources, apply patches, build and
transactionally install them into a prefix. Each project supplies its own
recipes, providers and policy via ProjectConfig; this package only provides
the mechanism.
"""

from .deps_build import (
    Dependency,
    DependencyError,
    Context,
    ProjectConfig,
    cache_key,
    init_project,
    install,
    register_provider,
    resolve,
    read_resolved,
    update_lock,
    verify,
)
from .source_cache import bootstrap_git, fetch_archive, fetch_file

__all__ = [
    "Dependency",
    "DependencyError",
    "Context",
    "ProjectConfig",
    "cache_key",
    "init_project",
    "install",
    "register_provider",
    "resolve",
    "read_resolved",
    "update_lock",
    "verify",
    "bootstrap_git",
    "fetch_archive",
    "fetch_file",
]

# Shared artifact protocol; recipes remain external.
from .artifacts import Artifact, BuildContext, BuildRequest, Environment
from .home import resolve_home, register_workspace
from .store import build_package, ensure_environment, verify_artifact
from .leases import ArtifactLease
from .environments import build_environment, read_environment, write_environment

__all__ += ["Artifact", "BuildContext", "BuildRequest", "Environment", "ArtifactLease",
            "resolve_home", "register_workspace", "build_package", "ensure_environment",
            "verify_artifact", "build_environment", "read_environment", "write_environment"]
