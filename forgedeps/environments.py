"""Connect injected recipes and locked sources to the shared artifact protocol."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess

from .artifacts import BuildRequest, Environment, Artifact
from . import deps_build as deps
from .home import prepare_home, resolve_home
from .process_environment import merge_environments
from .store import build_package
from .toolchains import probe_toolchain
from .workspace_sources import prepare_source, verify_source


def _ordered(recipes, only):
    by_name = {recipe.name: recipe for recipe in recipes}
    if len(by_name) != len(recipes):
        raise ValueError('Dependency recipe names must be unique')
    selected = deps.dependency_selection(only, recipes)
    result, active, done = [], set(), set()
    def visit(name):
        if name in active:
            raise ValueError(f'Cyclic dependency recipes: {name}')
        if name in done:
            return
        active.add(name)
        for requirement in by_name[name].requires:
            visit(requirement)
        active.remove(name)
        done.add(name)
        result.append(by_name[name])
    for recipe in recipes:
        if recipe.name in selected:
            visit(recipe.name)
    return result




def recipe_flag_values(options):
    """Flag variables a recipe set through -D options, by name."""
    values = {}
    for option in options:
        if not option.startswith('-D'):
            continue
        name, _, value = option[2:].partition('=')
        name = name.split(':', 1)[0]
        if '_FLAGS' in name and value != '':
            values[name] = value
    return values


def merge_flag_value(recipe_value, replay_value):
    """Keep recipe defines and append the probed flags behind them."""
    recipe_value = (recipe_value or '').strip()
    replay_value = (replay_value or '').strip()
    if not recipe_value:
        return replay_value
    if not replay_value or replay_value in recipe_value:
        return recipe_value
    return recipe_value + ' ' + replay_value

def _setting_tokens(name, value):
    """Compare flag variables by token, every other setting exactly."""
    text = str(value)
    return tuple(text.split()) if '_FLAGS' in name else text

def _cmake_build(ctx):
    target = ctx.target
    options = list(ctx.options['cmake_options'])
    settings = target.get('cmake', {})
    cmake = target.get('tools', {}).get('cmake', {}).get('path', 'cmake')
    command = [cmake, '-S', str(ctx.source_snapshot), '-B', str(ctx.build_dir)]
    # Installation location and dependency search are protocol-controlled.
    # A recipe can add libraries through requires, never another mutable SDK.
    for option in options:
        name = option.removeprefix('-D').split('=', 1)[0].split(':', 1)[0]
        if name in ('CMAKE_INSTALL_PREFIX', 'CMAKE_PREFIX_PATH'):
            raise ValueError(f'{name} is managed by ForgeDeps; inject dependency recipes instead')
    # Artifact names are part of a recipe's contract and must not depend on
    # the configuration: projects such as zlib (libzsd.a) and curl
    # (libcurl-d.a) add a Debug postfix unless it is cleared explicitly.
    command += ['-DCMAKE_POSITION_INDEPENDENT_CODE=ON', '-DBUILD_SHARED_LIBS=OFF',
                '-DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF',
                '-DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=OFF',
                '-DCMAKE_DEBUG_POSTFIX=',
                *options]
    # Pin the configuration that was actually probed. In particular, normal
    # variables set by toolchain files and environment-initialized flags are
    # not necessarily cache entries that a later project can reconstruct.
    replay = {
        'BUILD_SHARED_LIBS', 'CMAKE_POSITION_INDEPENDENT_CODE',
        'CMAKE_C_VISIBILITY_PRESET', 'CMAKE_CXX_VISIBILITY_PRESET', 'CMAKE_VISIBILITY_INLINES_HIDDEN',
        'CMAKE_INTERPROCEDURAL_OPTIMIZATION', 'CMAKE_MSVC_RUNTIME_LIBRARY',
        'CMAKE_MSVC_DEBUG_INFORMATION_FORMAT', 'CMAKE_SYSROOT', 'CMAKE_SYSROOT_COMPILE',
        'CMAKE_SYSROOT_LINK', 'CMAKE_OSX_SYSROOT', 'CMAKE_OSX_ARCHITECTURES',
        'CMAKE_OSX_DEPLOYMENT_TARGET', 'CMAKE_ANDROID_NDK', 'CMAKE_ANDROID_ARCH_ABI',
        'CMAKE_ANDROID_API', 'CMAKE_ANDROID_STL_TYPE', 'CMAKE_ANDROID_NDK_TOOLCHAIN_VERSION',
        'CMAKE_MAKE_PROGRAM', 'CMAKE_AR', 'CMAKE_RANLIB', 'CMAKE_LINKER',
        'CMAKE_RC_COMPILER', 'CMAKE_GENERATOR_INSTANCE', 'CMAKE_TOOLCHAIN_FILE',
        'CMAKE_VS_PLATFORM_TOOLSET', 'CMAKE_VS_WINDOWS_TARGET_PLATFORM_VERSION',
    }
    for language in ('C', 'CXX'):
        replay.update('CMAKE_' + language + '_' + suffix for suffix in (
            'COMPILER_TARGET', 'COMPILER_EXTERNAL_TOOLCHAIN', 'COMPILER_LAUNCHER',
            'STANDARD', 'STANDARD_REQUIRED', 'EXTENSIONS'))
    if settings.get('CMAKE_CROSSCOMPILING') == 'TRUE':
        replay.update(('CMAKE_SYSTEM_NAME', 'CMAKE_SYSTEM_VERSION', 'CMAKE_SYSTEM_PROCESSOR'))
    added_flags = recipe_flag_values(options)
    for key, value in sorted(settings.items()):
        is_flags = key.startswith('CMAKE_') and '_FLAGS' in key
        # Empty flags must also be pinned, rather than reinitialized from a
        # different environment. Empty optional CMake properties mean unset.
        if is_flags or (key in replay and value):
            if is_flags:
                # NDK and other toolchains prepend platform flags to these
                # cache inputs. Replaying the final normal value doubles them.
                value = target.get('cmake_cache', {}).get(key, value)
                # A recipe that puts its own defines in a flag variable (curl
                # needs NGTCP2_STATICLIB for static QUIC backends) must keep
                # them: emitting the probed value alone overwrote them, and the
                # consumer then linked dllimport stubs against a static library.
                if key in added_flags:
                    value = merge_flag_value(added_flags[key], value)
            command.append(f'-D{key}:STRING={value}')
    generator = settings.get('CMAKE_GENERATOR')
    if generator:
        command += ['-G', generator]
    for key, argument in (('CMAKE_GENERATOR_PLATFORM', '-A'), ('CMAKE_GENERATOR_TOOLSET', '-T')):
        if settings.get(key):
            command += [argument, settings[key]]
    for language, key in (('C', 'c'), ('CXX', 'cxx')):
        compiler = target.get(key)
        if compiler:
            command.append(f'-DCMAKE_{language}_COMPILER:FILEPATH=' + compiler['path'])
            quote = subprocess.list2cmdline if os.name == 'nt' else shlex.join
            command.append(f'-DCMAKE_{language}_COMPILER_ARG1:STRING=' + quote(compiler.get('arguments', [])))
    # A header-only recipe carries the synthetic "Headers" identity. Real CMake
    # configurations are required for the build itself: multi-config generators
    # (Visual Studio, Xcode) know only their own names, and MSBuild rejects the
    # synthetic one in a try-compile with MSB8013.
    configuration = 'Release' if ctx.configuration == 'Headers' else ctx.configuration
    command += [f'-DCMAKE_BUILD_TYPE={configuration}',
                f'-DCMAKE_TRY_COMPILE_CONFIGURATION={configuration}',
                '-DCMAKE_PREFIX_PATH=' + ';'.join(str(path) for path in ctx.dependency_prefixes),
                f'-DCMAKE_INSTALL_PREFIX={ctx.prefix}']
    ctx.run(command)
    ctx.run([cmake, '--build', ctx.build_dir, '--parallel', str(ctx.jobs), '--config', configuration])
    ctx.run([cmake, '--install', ctx.build_dir, '--config', configuration])


def build_cmake(ctx):
    """Run the generic CMake adapter from an externally injected builder."""
    return _cmake_build(ctx)


def _builder_for(dependency, config):
    record = asdict(dependency)
    post_build = record.pop('post_build')
    post_install = record.pop('post_install')
    record.pop('builder_inputs')
    builder = config.builders.get(dependency.kind)
    if builder is None and dependency.kind not in ('cmake', 'generated', 'git'):
        raise ValueError(f'No external builder registered for recipe kind: {dependency.kind}')
    assets = {}
    for name in (dependency.patch, *dependency.extra_files):
        if name:
            if config.patches_dir is None:
                raise ValueError(f'Missing external recipe assets directory: {name}')
            assets[name] = (Path(config.patches_dir) / name).read_bytes().hex()

    def build(ctx):
        # Capture content and callbacks, never config objects, consumer paths or
        # package installation paths. Two consumers obtain the same identity.
        dep = deps.Dependency(**record)
        if dep.patch:
            patch = ctx.build_dir / 'recipe.patch'
            patch.write_bytes(bytes.fromhex(assets[dep.patch]))
            deps.apply_patch(ctx.source_snapshot, patch)
        for name in dep.extra_files:
            (ctx.source_snapshot / name).write_bytes(bytes.fromhex(assets[name]))
        if dep.cmake_lists:
            (ctx.source_snapshot / 'CMakeLists.txt').write_text(dep.cmake_lists, encoding='utf-8')
        if builder is not None:
            builder(ctx)
        else:
            _cmake_build(ctx)
        licenses = ctx.prefix / 'share/licenses' / dep.name
        licenses.mkdir(parents=True, exist_ok=True)
        for relative in (*dep.license_files, 'NOTICE', 'NOTICE.txt'):
            origin = ctx.source_snapshot / relative
            if not origin.is_file():
                if relative in dep.license_files:
                    raise ValueError(f'Required license missing: {dep.name}/{relative}')
                continue
            shutil.copyfile(origin, licenses / origin.name)
        if post_install is not None:
            post_install(ctx.prefix, ctx.source_snapshot, dep)
        if post_build is not None:
            post_build(ctx.prefix, ctx.source_snapshot, dep)
        missing = [path for path in dep.artifacts if not deps.artifact_present(ctx.prefix, path)]
        if missing:
            raise ValueError(f'{dep.name} missing expected artifacts: {missing}')
    return build


def build_environment(file: Path, *, config: deps.ProjectConfig, home=None,
                      profile='tests', tls_backend='auto', only='', offline=False,
                      c=None, cxx=None, toolchain=None, configuration='Release',
                      cmake_options=(), environment=None, source_overrides=None,
                      jobs=None, force=False, expected_settings=None, _verify=None) -> Environment:
    """Build a locked, injected dependency graph into one shared workspace.

    No lock update or discovery is implicit. Run resolve to change a selection.
    Source overrides are explicit editable inputs and still get content hashed.
    """
    root = prepare_home(home) if _verify is None else resolve_home(home)
    selected = config.effective_configure()(config.recipes, profile, tls_backend)
    recipes = _ordered(list(selected), only)
    resolution = deps.read_resolved(Path(file), [dep.name for dep in recipes])
    recipes = deps.locked_recipes(resolution, recipes, config)
    expected = {} if _verify is None else {artifact.name: artifact for artifact in _verify.artifacts}
    if _verify is not None and (len(expected) != len(_verify.artifacts)
                               or set(expected) != {dep.name for dep in recipes}):
        raise ValueError('Dependency environment selection differs from the requested recipe graph')
    overrides = dict(source_overrides or {})
    if set(overrides) - {dep.name for dep in recipes}:
        raise ValueError('Source overrides must name a selected dependency')
    artifacts, targets = {}, {}
    for dep in recipes:
        expected_source = None
        if dep.name in overrides:
            source = Path(overrides[dep.name]).resolve(strict=True)
        elif _verify is not None:
            source = verify_source(dep, home=root)
        else:
            source, expected_source = prepare_source(dep, home=root, offline=offline, with_identity=True)
        options = [] if dep.header_only else list(cmake_options)
        if dep.uses_libdir:
            options.append('-DCMAKE_INSTALL_LIBDIR=lib')
        options.extend(dep.options)
        # Recipe flags are real build inputs too. Probe the effective recipe
        # settings; never overwrite a recipe's ABI flags with a global probe.
        target = {}
        if not dep.header_only:
            key = tuple(options)
            if key not in targets:
                targets[key] = probe_toolchain(
                    configuration=configuration, c=c, cxx=cxx, toolchain=toolchain,
                    cmake_options=key, environment=environment, home=root)
            target = targets[key]
            for name, value in (expected_settings or {}).items():
                actual = str(target.get('cmake', {}).get(name, ''))
                # Flag strings are whitespace-separated tokens. CMake keeps a
                # trailing space on some MSVC defaults (``/Zi /Ob0 /Od ``) in a
                # configured project but not in the probe, which is the same ABI.
                if _setting_tokens(name, actual) != _setting_tokens(name, value):
                    raise ValueError(f'Consumer {name} differs from the dependency toolchain probe '
                                     f'(consumer {value!r}, probe {actual!r}); express additional ABI '
                                     'settings through CMake cache options or an explicit toolchain '
                                     'input, then rebuild the environment')
        build_env = merge_environments(target.get('environment', {}), environment or {})
        # Ambient tool locations are defaults only; an explicit value wins and
        # never joins a case-variant duplicate inherited from the process.
        ambient = {name: os.environ[name] for name in ('MSYS2_ROOT', 'PERL', 'MAKE')
                   if name in os.environ}
        build_env = merge_environments(ambient, build_env)
        inputs = {str(index) + '/' + Path(path).name: Path(path)
                  for index, path in enumerate(dep.builder_inputs)}
        request = BuildRequest(
            name=dep.name, source=source, builder=_builder_for(dep, config),
            recipe_input_files=inputs, target={} if dep.header_only else target,
            options={'cmake_options': options}, environment={} if dep.header_only else build_env,
            requires=tuple(artifacts[name] for name in dep.requires),
            header_only=dep.header_only, configuration='Headers' if dep.header_only else configuration,
            jobs=jobs or min(os.cpu_count() or 1, 8), expected_source_content=expected_source)
        if _verify is not None:
            from .store import verify_request
            artifacts[dep.name] = verify_request(request, expected[dep.name], home=root)
        else:
            artifacts[dep.name] = build_package(request, home=root, force=force)
    result = Environment(tuple(artifacts.values()))
    if _verify is None:
        with result.lease():
            pass
    return result


def write_environment(path: Path, environment: Environment):
    deps.atomic_json(Path(path), {'schema': 1, **environment.to_dict()})


def read_environment(path: Path) -> Environment:
    record = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(record, dict) or record.get('schema') != 1:
        raise ValueError('Invalid dependency environment')
    return Environment(tuple(Artifact.from_dict(item) for item in record['refs']))


def verify_environment(file: Path, receipt: Path | Environment, **kwargs) -> Environment:
    """Verify current lock, recipes, source contents, toolchain and artifact bytes.

    This never builds, replaces or fetches a dependency. A real toolchain probe
    may create temporary files under the workspace's tmp directory.
    """
    expected = receipt if isinstance(receipt, Environment) else read_environment(receipt)
    with expected.lease():
        result = build_environment(file, _verify=expected, **kwargs)
        # Optional build-driver context also prevents an uncovered child
        # configure from trying to write while its parent holds reader leases.
        from .build_kit.session import assert_parent_covers_environment
        assert_parent_covers_environment(result)
        return result
