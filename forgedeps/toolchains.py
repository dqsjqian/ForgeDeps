"""Probe a real CMake toolchain without running target executables.

The returned JSON-compatible mapping is an artifact target, including its human
label. Temporary project paths, consumer cwd and Python installation do not form
part of its identity. Compiler patches and effective flags remain in metadata;
the store, not this module, decides how to compare artifact identities.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile

from .home import prepare_home
from .process_environment import merge_environments, select_environment
from .store import BUILD_ENVIRONMENT_KEYS


# Only build inputs belong in the identity. PATH is used for discovery, but the
# selected tools themselves are recorded. PWD, Python and venv paths never are.
_BUILD_ENV = frozenset(BUILD_ENVIRONMENT_KEYS)

_VARIABLES = """
CMAKE_SYSTEM_NAME CMAKE_SYSTEM_VERSION CMAKE_SYSTEM_PROCESSOR CMAKE_CROSSCOMPILING
CMAKE_SYSROOT CMAKE_SYSROOT_COMPILE CMAKE_SYSROOT_LINK CMAKE_OSX_SYSROOT
CMAKE_OSX_ARCHITECTURES CMAKE_OSX_DEPLOYMENT_TARGET CMAKE_LIBRARY_ARCHITECTURE
CMAKE_ANDROID_NDK CMAKE_ANDROID_NDK_VERSION CMAKE_ANDROID_ARCH_ABI
CMAKE_ANDROID_API CMAKE_ANDROID_STL_TYPE CMAKE_ANDROID_NDK_TOOLCHAIN_VERSION
ANDROID_PLATFORM ANDROID_ABI ANDROID_STL
CMAKE_SIZEOF_VOID_P BUILD_SHARED_LIBS CMAKE_POSITION_INDEPENDENT_CODE CMAKE_INTERPROCEDURAL_OPTIMIZATION
CMAKE_C_VISIBILITY_PRESET CMAKE_CXX_VISIBILITY_PRESET CMAKE_VISIBILITY_INLINES_HIDDEN
CMAKE_MSVC_RUNTIME_LIBRARY CMAKE_MSVC_DEBUG_INFORMATION_FORMAT
CMAKE_C_STANDARD CMAKE_C_STANDARD_REQUIRED CMAKE_C_EXTENSIONS
CMAKE_CXX_STANDARD CMAKE_CXX_STANDARD_REQUIRED CMAKE_CXX_EXTENSIONS
CMAKE_EXE_LINKER_FLAGS CMAKE_SHARED_LINKER_FLAGS CMAKE_MODULE_LINKER_FLAGS
CMAKE_STATIC_LINKER_FLAGS CMAKE_TOOLCHAIN_FILE CMAKE_GENERATOR
CMAKE_GENERATOR_PLATFORM CMAKE_GENERATOR_TOOLSET CMAKE_GENERATOR_INSTANCE
CMAKE_VS_PLATFORM_TOOLSET CMAKE_VS_WINDOWS_TARGET_PLATFORM_VERSION
CMAKE_VS_WINDOWS_TARGET_PLATFORM_VERSION_MAXIMUM
CMAKE_MAKE_PROGRAM CMAKE_AR CMAKE_RANLIB CMAKE_LINKER CMAKE_RC_COMPILER
CMAKE_C_COMPILER CMAKE_C_COMPILER_ID CMAKE_C_COMPILER_VERSION CMAKE_C_COMPILER_ARG1
CMAKE_C_COMPILER_TARGET CMAKE_C_COMPILER_EXTERNAL_TOOLCHAIN CMAKE_C_COMPILER_LAUNCHER
CMAKE_C_COMPILER_FRONTEND_VARIANT CMAKE_C_SIMULATE_ID CMAKE_C_SIMULATE_VERSION
CMAKE_C_COMPILER_ARCHITECTURE_ID CMAKE_C_BYTE_ORDER CMAKE_C_FLAGS
CMAKE_CXX_COMPILER CMAKE_CXX_COMPILER_ID CMAKE_CXX_COMPILER_VERSION CMAKE_CXX_COMPILER_ARG1
CMAKE_CXX_COMPILER_TARGET CMAKE_CXX_COMPILER_EXTERNAL_TOOLCHAIN CMAKE_CXX_COMPILER_LAUNCHER
CMAKE_CXX_COMPILER_FRONTEND_VARIANT CMAKE_CXX_SIMULATE_ID CMAKE_CXX_SIMULATE_VERSION
CMAKE_CXX_COMPILER_ARCHITECTURE_ID CMAKE_CXX_BYTE_ORDER CMAKE_CXX_FLAGS
""".split()

_SOURCE = r'''
#include <stdlib.h>
#ifdef __cplusplus
#include <string>
#endif
#define AD_STRING_INNER(x) #x
#define AD_STRING(x) AD_STRING_INNER(x)
#if defined(_M_ARM64EC)
#define AD_ARCH "arm64ec"
#elif defined(__aarch64__) || defined(_M_ARM64)
#define AD_ARCH "arm64"
#elif defined(__x86_64__) || defined(_M_X64)
#define AD_ARCH "x86_64"
#elif defined(__i386__) || defined(_M_IX86)
#define AD_ARCH "x86"
#elif defined(__arm__) || defined(_M_ARM)
#define AD_ARCH "arm"
#elif defined(__riscv) && __riscv_xlen == 64
#define AD_ARCH "riscv64"
#elif defined(__powerpc64__)
#define AD_ARCH "ppc64"
#elif defined(__s390x__)
#define AD_ARCH "s390x"
#elif defined(__wasm64__)
#define AD_ARCH "wasm64"
#elif defined(__wasm32__)
#define AD_ARCH "wasm32"
#else
#define AD_ARCH "unknown"
#endif
#if defined(__ANDROID__)
#define AD_PLATFORM "android"
#elif defined(__APPLE__)
#include <TargetConditionals.h>
#if TARGET_OS_WATCH
#define AD_APPLE_PLATFORM "watchos"
#elif TARGET_OS_TV
#define AD_APPLE_PLATFORM "tvos"
#elif defined(TARGET_OS_VISION) && TARGET_OS_VISION
#define AD_APPLE_PLATFORM "visionos"
#elif TARGET_OS_IPHONE
#define AD_APPLE_PLATFORM "ios"
#else
#define AD_APPLE_PLATFORM "macos"
#endif
#if TARGET_OS_SIMULATOR
#define AD_PLATFORM AD_APPLE_PLATFORM "simulator"
#elif defined(TARGET_OS_MACCATALYST) && TARGET_OS_MACCATALYST
#define AD_PLATFORM "maccatalyst"
#else
#define AD_PLATFORM AD_APPLE_PLATFORM
#endif
#elif defined(_WIN32)
#define AD_PLATFORM "windows"
#elif defined(__linux__)
#define AD_PLATFORM "linux"
#elif defined(__FreeBSD__)
#define AD_PLATFORM "freebsd"
#elif defined(__EMSCRIPTEN__)
#define AD_PLATFORM "emscripten"
#else
#define AD_PLATFORM "unknown"
#endif
#ifdef __cplusplus
#define AD_LANGUAGE "CXX"
#define AD_SYMBOL forgedeps_probe_cxx
#else
#define AD_LANGUAGE "C"
#define AD_SYMBOL forgedeps_probe_c
#endif
char AD_SYMBOL[] = "FORGEDEPS_PROBE:" AD_LANGUAGE ":"
    "arch=" AD_ARCH ";platform=" AD_PLATFORM ";"
    "cplusplus=" AD_STRING(__cplusplus) ";"
    "msvc=" AD_STRING(_MSC_VER) ";"
    "msvc_stl=" AD_STRING(_MSVC_STL_UPDATE) ";"
    "iterator_debug=" AD_STRING(_ITERATOR_DEBUG_LEVEL) ";"
    "msvc_multithread=" AD_STRING(_MT) ";"
    "msvc_dll_runtime=" AD_STRING(_DLL) ";"
    "debug=" AD_STRING(_DEBUG) ";"
    "exceptions=" AD_STRING(__EXCEPTIONS) ";"
    "msvc_exceptions=" AD_STRING(_CPPUNWIND) ";"
    "rtti=" AD_STRING(__GXX_RTTI) ";"
    "msvc_rtti=" AD_STRING(_CPPRTTI) ";"
    "glibc=" AD_STRING(__GLIBC__) "." AD_STRING(__GLIBC_MINOR__) ";"
    "glibcxx=" AD_STRING(__GLIBCXX__) ";"
    "glibcxx_cxx11_abi=" AD_STRING(_GLIBCXX_USE_CXX11_ABI) ";"
    "libcpp=" AD_STRING(_LIBCPP_VERSION) ";"
    "libcpp_abi=" AD_STRING(_LIBCPP_ABI_VERSION) ";"
    "android_api=" AD_STRING(__ANDROID_API__) ";END_FORGEDEPS_PROBE";
'''


def _project(configuration: str) -> str:
    variables = list(_VARIABLES)
    for prefix in ("C", "CXX", "EXE_LINKER", "SHARED_LINKER", "MODULE_LINKER", "STATIC_LINKER"):
        variables.append(f"CMAKE_{prefix}_FLAGS_{configuration.upper()}")
    return r'''cmake_minimum_required(VERSION 3.21)
project(ForgeDepsToolchainProbe LANGUAGES C CXX)
# Each isolated probe builds one selected SDK. Xcode's optional platform
# suffix leaks an unresolved EFFECTIVE_PLATFORM_NAME into TARGET_FILE, so
# suppress that suffix rather than guessing the actual archive location.
set_property(GLOBAL PROPERTY XCODE_EMIT_EFFECTIVE_PLATFORM_NAME OFF)
add_library(forgedeps_probe STATIC probe.c probe.cpp)
# The first target checks the complete requested flags, including LTO. A second
# object carrier disables LTO only, so ABI markers remain readable without a
# target-specific disassembler or running a cross-compiled program.
add_library(forgedeps_probe_abi STATIC probe.c probe.cpp)
set_property(TARGET forgedeps_probe_abi PROPERTY INTERPROCEDURAL_OPTIMIZATION FALSE)
foreach(language IN ITEMS C CXX)
  if(CMAKE_${language}_COMPILER_ID STREQUAL "MSVC")
    target_compile_options(forgedeps_probe_abi PRIVATE "$<$<COMPILE_LANGUAGE:${language}>:/GL->")
  elseif(CMAKE_${language}_COMPILER_ID MATCHES "^(GNU|Clang|AppleClang)$")
    if(CMAKE_${language}_COMPILER_FRONTEND_VARIANT STREQUAL "MSVC")
      target_compile_options(forgedeps_probe_abi PRIVATE "$<$<COMPILE_LANGUAGE:${language}>:/clang:-fno-lto>")
    else()
      target_compile_options(forgedeps_probe_abi PRIVATE "$<$<COMPILE_LANGUAGE:${language}>:-fno-lto>")
    endif()
  endif()
endforeach()
function(ad_quote output value)
  string(REPLACE "\\" "\\\\" value "${value}")
  string(REPLACE "\"" "\\\"" value "${value}")
  string(REPLACE "\n" "\\n" value "${value}")
  string(REPLACE "\r" "\\r" value "${value}")
  string(REPLACE "\t" "\\t" value "${value}")
  set(${output} "\"${value}\"" PARENT_SCOPE)
endfunction()
set(record "{")
foreach(name IN ITEMS ''' + " ".join(variables) + r''')
  ad_quote(value "${${name}}")
  string(APPEND record "\"${name}\":${value},")
endforeach()
string(APPEND record "\"archive\":\"$<TARGET_FILE:forgedeps_probe_abi>\",")
string(APPEND record "\"runtime\":\"$<TARGET_GENEX_EVAL:forgedeps_probe,$<TARGET_PROPERTY:forgedeps_probe,MSVC_RUNTIME_LIBRARY>>\"}")
file(GENERATE OUTPUT "${CMAKE_BINARY_DIR}/probe-$<CONFIG>.json" CONTENT "${record}")
'''


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _executable(value: str, env: dict) -> dict:
    path = shutil.which(value, path=env.get("PATH")) if value else None
    if not path:
        raise ValueError(f"Toolchain executable is unavailable: {value!r}")
    # Preserve argv[0]: clang++ and clang may be symlinks to the same binary,
    # but invoking their resolved path changes default C++ link behaviour.
    absolute = Path(path).absolute()
    return {"path": str(absolute), "real_path": str(absolute.resolve()),
            "binary_sha256": _sha256(absolute)}


def _split(value: str) -> list[str]:
    parts = shlex.split(value, posix=os.name != "nt")
    return [part.strip('"') for part in parts] if os.name == "nt" else parts


def _compiler_options(language: str, command) -> list[str]:
    if command is None:
        return []
    text = os.fspath(command)
    parts = [str(Path(text).absolute())] if Path(text).is_file() else _split(text)
    if not parts:
        raise ValueError(f"Empty {language} compiler")
    if Path(parts[0]).is_file():
        parts[0] = str(Path(parts[0]).absolute())
    result = [f"-DCMAKE_{language}_COMPILER:FILEPATH={parts[0]}"]
    if len(parts) > 1:
        arguments = subprocess.list2cmdline(parts[1:]) if os.name == "nt" else shlex.join(parts[1:])
        result.append(f"-DCMAKE_{language}_COMPILER_ARG1:STRING={arguments}")
    return result


def _run(command: list[str], *, cwd: Path, env: dict) -> None:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise ValueError("CMake toolchain probe failed:\n" + result.stdout + result.stderr)


def _read_reply(build: Path) -> tuple[dict, dict]:
    reply = build / ".cmake/api/v1/reply"
    index = json.loads(sorted(reply.glob("index-*.json"))[-1].read_text(encoding="utf-8"))
    objects = {item["kind"]: json.loads((reply / item["jsonFile"]).read_text(encoding="utf-8"))
               for item in index["objects"]}
    return index, objects


def _markers(archive: Path) -> dict:
    found = {"C": [], "CXX": []}
    for language, payload in re.findall(
            rb"FORGEDEPS_PROBE:(C|CXX):([^\x00]*?);END_FORGEDEPS_PROBE", archive.read_bytes()):
        values = dict(field.split("=", 1) for field in payload.decode("ascii").split(";"))
        if values not in found[language.decode()]:
            found[language.decode()].append(values)
    for language, values in found.items():
        if not values or any(item["arch"] == "unknown" or item["platform"] == "unknown" for item in values):
            raise ValueError(f"Cannot determine actual {language} target platform/architecture from compiled objects")
        values.sort(key=lambda item: (item["arch"], item["platform"]))
    if {(x["arch"], x["platform"]) for x in found["C"]} != {
            (x["arch"], x["platform"]) for x in found["CXX"]}:
        raise ValueError("C and C++ compilers target different platforms/architectures")
    return found


def _label(record: dict, markers: dict, configuration: str) -> tuple[str, str, str]:
    platforms = {item["platform"] for item in markers["C"]}
    if len(platforms) != 1:
        raise ValueError("A toolchain cannot combine different target platforms")
    platform = platforms.pop()
    systems = {"windows": "Windows", "linux": "Linux", "android": "Android",
               "macos": "Darwin", "ios": "iOS", "iossimulator": "iOS",
               "tvos": "tvOS", "tvossimulator": "tvOS", "watchos": "watchOS",
               "watchossimulator": "watchOS", "visionos": "visionOS",
               "visionossimulator": "visionOS", "maccatalyst": "Darwin",
               "freebsd": "FreeBSD", "emscripten": "Emscripten"}
    actual_system = systems[platform]
    configured = record["CMAKE_SYSTEM_NAME"]
    if configured != actual_system:
        raise ValueError(f"CMake system {configured!r} disagrees with compiled target {actual_system!r}")
    machine = "+".join(sorted({item["arch"] for item in markers["C"]}))
    architecture = machine.replace("x86_64", "x64").replace("+", "-")
    compiler = record["CMAKE_CXX_COMPILER_ID"].lower()
    compiler = {"gnu": "gcc"}.get(compiler, compiler)
    if compiler == "msvc":
        major, minor, *_ = map(int, record["CMAKE_CXX_COMPILER_VERSION"].split("."))
        if major == 19:
            compiler += "2026" if minor >= 50 else "2022" if minor >= 30 else "2019" if minor >= 20 else "2017" if minor >= 10 else "2015"
    if platform == "android":
        apis = {item["android_api"] for item in markers["C"]}
        if len(apis) != 1 or not next(iter(apis)).isdigit():
            raise ValueError("Android toolchain did not provide an actual API level")
        platform += "-api" + apis.pop()
    return f"{platform}-{architecture}-{compiler}-{configuration.lower()}", actual_system, machine


def _sdk(record: dict, compilers: dict, env: dict) -> dict:
    roots = {record.get(key, "") for key in (
        "CMAKE_SYSROOT", "CMAKE_SYSROOT_COMPILE", "CMAKE_SYSROOT_LINK",
        "CMAKE_OSX_SYSROOT", "CMAKE_ANDROID_NDK")}
    # AppleClang can select the SDK implicitly; its actual include search path
    # then carries the SDK even when CMAKE_OSX_SYSROOT is empty.
    for compiler in compilers.values():
        for include in compiler.get("implicit", {}).get("includeDirectories", []):
            match = re.match(r"(.+?\.sdk)(?:/|$)", include)
            if match:
                roots.add(match.group(1))
    roots.update(env.get(key, "") for key in ("WindowsSdkDir", "UniversalCRTSdkDir"))
    result = []
    for value in sorted(roots - {""}):
        root = Path(value)
        if not root.is_dir():
            continue  # An SDK name is still retained in the CMake record.
        root = root.resolve()
        files = []
        for relative in ("SDKSettings.json", "SDKSettings.plist", "source.properties",
                         "SDKManifest.xml", "System/Library/CoreServices/SystemVersion.plist",
                         "usr/include/features.h", "usr/include/TargetConditionals.h"):
            file = root / relative
            if file.is_file():
                files.append({"path": relative, "sha256": _sha256(file)})
        result.append({"path": str(root), "metadata": files})
    return {"roots": result}


def probe_toolchain(*, configuration="Release", c=None, cxx=None, toolchain=None,
                    cmake_options=(), environment=None, home=None) -> dict:
    """Compile C/C++ probes and return the actual target's complete build inputs.

    ``environment`` overlays subprocess environment; only explicit build-related
    variables are recorded. CMake options must be configure options, not -S/-B,
    --build or presets (which could replace this independent probe project).
    Cross targets are compiled, never executed. Unrecognized targets fail closed.
    """
    if not isinstance(configuration, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", configuration):
        raise ValueError("Invalid CMake configuration")
    options = [os.fspath(option) for option in cmake_options]
    for option in options:
        if option.startswith(("-S", "-B", "--build", "--install", "--preset", "-P")):
            raise ValueError(f"Option cannot replace the toolchain probe project: {option}")
    env = merge_environments(os.environ, environment)
    cmake = _executable("cmake", env)
    generator = []
    if not env.get("CMAKE_GENERATOR") and not any(x == "-G" or x.startswith("-G") for x in options):
        if shutil.which("ninja", path=env.get("PATH")):
            generator = ["-G", "Ninja"]
    selected_toolchain = toolchain or env.get("CMAKE_TOOLCHAIN_FILE")
    if selected_toolchain:
        selected_toolchain = str(Path(selected_toolchain).expanduser().resolve(strict=True))
    workspace = prepare_home(home)
    with tempfile.TemporaryDirectory(prefix="toolchain-", dir=workspace / "tmp") as temporary:
        root = Path(temporary)
        source, build = root / "source", root / "build"
        source.mkdir()
        query = build / ".cmake/api/v1/query"
        query.mkdir(parents=True)
        for name in ("codemodel-v2", "toolchains-v1", "cmakeFiles-v1", "cache-v2"):
            (query / name).touch()
        (source / "CMakeLists.txt").write_text(_project(configuration), encoding="utf-8")
        (source / "probe.c").write_text(_SOURCE, encoding="utf-8")
        (source / "probe.cpp").write_text(_SOURCE, encoding="utf-8")
        command = [cmake["path"], "-S", str(source), "-B", str(build), *generator,
                   f"-DCMAKE_BUILD_TYPE:STRING={configuration}",
                   f"-DCMAKE_TRY_COMPILE_CONFIGURATION:STRING={configuration}",
                   "-DCMAKE_POSITION_INDEPENDENT_CODE=ON", "-DBUILD_SHARED_LIBS=OFF", *options,
                   *_compiler_options("C", c), *_compiler_options("CXX", cxx)]
        if selected_toolchain:
            command.append("-DCMAKE_TOOLCHAIN_FILE:FILEPATH=" + selected_toolchain)
        _run(command, cwd=root, env=env)
        _run([cmake["path"], "--build", str(build), "--config", configuration,
              "--target", "forgedeps_probe", "forgedeps_probe_abi"], cwd=root, env=env)
        record = json.loads((build / f"probe-{configuration}.json").read_text(encoding="utf-8"))
        markers = _markers(Path(record.pop("archive")))
        label, platform, machine = _label(record, markers, configuration)
        index, replies = _read_reply(build)
        config = next(item for item in replies["codemodel"]["configurations"]
                      if item["name"].lower() == configuration.lower())
        target = next(item for item in config["targets"] if item["name"] == "forgedeps_probe")
        target = json.loads((build / ".cmake/api/v1/reply" / target["jsonFile"]).read_text(encoding="utf-8"))
        groups = {group["language"]: group for group in target["compileGroups"]}
        actual_compilers = {item["language"]: item["compiler"] for item in replies["toolchains"]["toolchains"]}
        compilers = {}
        for language, key in (("C", "c"), ("CXX", "cxx")):
            prefix = "CMAKE_" + language + "_"
            info = _executable(record[prefix + "COMPILER"], env)
            info.update({"id": record[prefix + "COMPILER_ID"], "version": record[prefix + "COMPILER_VERSION"],
                         "arguments": _split(record[prefix + "COMPILER_ARG1"]),
                         "target": record[prefix + "COMPILER_TARGET"],
                         "frontend": record[prefix + "COMPILER_FRONTEND_VARIANT"],
                         "implicit": actual_compilers[language].get("implicit", {}),
                         "compile": {name: groups[language].get(name, []) for name in
                                     ("compileCommandFragments", "defines", "includes", "languageStandard")},
                         "abi": markers[language]})
            if info["id"] == "AppleClang" and Path(info["path"]).parent == Path("/usr/bin"):
                # /usr/bin/cc can be an xcselect shim whose bytes do not change
                # when Xcode does. Hash the selected compiler behind it too.
                xcrun = _executable("xcrun", env)
                selected = subprocess.run([xcrun["path"], "--find", Path(info["path"]).name],
                                          cwd=root, env=env, capture_output=True, text=True,
                                          encoding="utf-8", errors="replace")
                if selected.returncode:
                    raise ValueError("Cannot locate actual AppleClang driver: " + selected.stderr)
                info["driver"] = _executable(selected.stdout.strip(), env)
            launcher = record[prefix + "COMPILER_LAUNCHER"].split(";")
            if launcher[0]:
                info["launcher"] = {**_executable(launcher[0], env), "arguments": launcher[1:]}
            compilers[key] = info
        inputs = {}
        for item in replies["cmakeFiles"]["inputs"]:
            path = Path(item["path"])
            if not path.is_absolute():
                path = source / path
            path = path.resolve()
            if not path.is_relative_to(root) and path.is_file():
                inputs[str(path)] = _sha256(path)
        tools = {"cmake": {**cmake, "version": index["cmake"]["version"]["string"]}}
        for variable in ("CMAKE_MAKE_PROGRAM", "CMAKE_AR", "CMAKE_RANLIB", "CMAKE_LINKER", "CMAKE_RC_COMPILER"):
            if record[variable] and not (variable == "CMAKE_RANLIB" and record[variable] == ":"):
                # MSVC archives do not use ranlib; CMake records the no-op ':'
                # rather than a binary. Keep that value in cmake, not tools.
                tools[variable.removeprefix("CMAKE_").lower()] = _executable(record[variable], env)
        result = {"label": label, "platform": platform, "machine": machine,
                  "configuration": configuration, **compilers, "cmake": record,
                  # Toolchains may prepend their flags to cache inputs. Keep
                  # those inputs distinct from the effective normal variables
                  # above, so replay does not prepend the same flags twice.
                  "cmake_cache": {item["name"]: item["value"] for item in replies["cache"]["entries"]
                                  if item["name"] in record},
                  "system": {key: value for key, value in record.items() if any(
                      part in key for part in ("SYSTEM", "SYSROOT", "OSX_", "ANDROID", "CROSSCOMPILING"))},
                  "abi": {"pointer_size": record["CMAKE_SIZEOF_VOID_P"], "msvc_runtime": record["runtime"],
                          "c": markers["C"], "cxx": markers["CXX"]},
                  "tools": tools, "toolchain": {"path": record["CMAKE_TOOLCHAIN_FILE"],
                                                "inputs": [{"path": path, "sha256": digest}
                                                           for path, digest in sorted(inputs.items())]},
                  "sdk": _sdk(record, actual_compilers, env),
                  "environment": select_environment(_BUILD_ENV, env),
                  "cmake_options": options}
        quote = subprocess.list2cmdline if os.name == "nt" else shlex.join
        for variable, key in (("CC", "c"), ("CXX", "cxx")):
            result["environment"][variable] = quote([
                compilers[key]["path"], *compilers[key].get("arguments", [])])
        # File API backtraces are indexes into this temporary project, not ABI.
        def stable(value):
            if isinstance(value, dict):
                return {key: stable(item) for key, item in value.items() if key != "backtrace"}
            if isinstance(value, list):
                return [stable(item) for item in value]
            if isinstance(value, str):
                return value.replace(str(root), "<probe>").replace(root.as_posix(), "<probe>")
            return value
        return stable(result)
