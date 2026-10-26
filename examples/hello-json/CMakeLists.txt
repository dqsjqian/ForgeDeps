cmake_minimum_required(VERSION 3.21)
project(hello_json LANGUAGES CXX)

find_package(Python3 3.10 REQUIRED COMPONENTS Interpreter)
execute_process(COMMAND "${Python3_EXECUTABLE}" -m forgedeps cmake-dir
    RESULT_VARIABLE result OUTPUT_VARIABLE modules ERROR_VARIABLE error
    OUTPUT_STRIP_TRAILING_WHITESPACE)
if(NOT result EQUAL 0)
    message(FATAL_ERROR "Install forgedeps into ${Python3_EXECUTABLE}: ${error}")
endif()
set(ForgeDeps_PYTHON_EXECUTABLE "${Python3_EXECUTABLE}")
include("${modules}/ForgeDeps.cmake")

option(EXAMPLE_OFFLINE "Reuse only locked metadata and verified cached sources" OFF)
set(offline)
if(EXAMPLE_OFFLINE)
    list(APPEND offline OFFLINE)
endif()
forgedeps_resolve(NAME json FILE "${CMAKE_CURRENT_SOURCE_DIR}/dependencies.json"
    VERSION version URL url SHA256 sha256 ${offline})
forgedeps_fetch_archive(NAME json VERSION "${version}" URL "${url}" SHA256 "${sha256}"
    CACHE "${CMAKE_CURRENT_BINARY_DIR}/deps/cache" SOURCE_DIR json_source ${offline})
# Keep generated CMake files outside the immutable source cache.
set(JSON_BuildTests OFF CACHE BOOL "" FORCE)
add_subdirectory("${json_source}" "${CMAKE_CURRENT_BINARY_DIR}/json")
add_executable(hello_json main.cpp)
target_compile_features(hello_json PRIVATE cxx_std_17)
target_link_libraries(hello_json PRIVATE nlohmann_json::nlohmann_json)
enable_testing()
add_test(NAME hello_json COMMAND hello_json)
set_tests_properties(hello_json PROPERTIES PASS_REGULAR_EXPRESSION "hello from ForgeDeps")
