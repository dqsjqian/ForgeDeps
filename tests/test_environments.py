"""Real consumer-independent builds through an externally injected recipe."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace

from forgedeps import Dependency, ProjectConfig
from forgedeps import deps_build as deps
from forgedeps.environments import build_environment, verify_environment, write_environment


@unittest.skipUnless(shutil.which('cmake'), 'CMake unavailable')
class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='forgedeps-environment-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'shared'
        self.home.mkdir()
        self.source = self.root / 'vendor'
        self.source.mkdir()
        (self.source / 'LICENSE').write_text('Fixture license\n')
        (self.source / 'fixture.h').write_text('int fixture(void);\n')
        (self.source / 'fixture.c').write_text('int fixture(void) { return 42; }\n')
        (self.source / 'CMakeLists.txt').write_text('''cmake_minimum_required(VERSION 3.21)
project(Fixture C)
add_library(fixture STATIC fixture.c)
install(TARGETS fixture ARCHIVE DESTINATION lib)
install(FILES fixture.h DESTINATION include)
''')
        self.recipe = Dependency(name='fixture', version='', url='', sha256='',
                                 license='MIT', license_files=('LICENSE',), root='', kind='cmake',
                                 artifacts=('include/fixture.h', 'lib/libfixture.a'))
        spec = {'provider': 'github', 'repo': 'example/fixture', 'artifact': 'archive'}
        record = {'requested': 'latest', 'version': '1.0', 'revision': 'a' * 40,
                  'tag': 'v1.0', 'url': 'https://example.invalid/fixture.tar.gz',
                  'sha256': 'b' * 64, 'request_hash': deps.request_hash(spec)}
        self.lock = self.root / 'project-one/dependencies.json'
        self.lock.parent.mkdir()
        self.lock.write_text(json.dumps({'schema': 2, 'dependencies': {
            'fixture': {**spec, 'resolved': record}}}))
        self.kwargs = dict(home=self.home, source_overrides={'fixture': self.source}, jobs=1)

    def test_real_build_reused_between_projects_and_changed_source_rejected(self):
        first_config = ProjectConfig(name='first-consumer', recipes=(self.recipe,))
        first = build_environment(self.lock, config=first_config, **self.kwargs)
        prefix = first.prefixes[0]
        manifest = (prefix / 'manifest.json').read_bytes()
        receipt = self.root / 'first.json'
        write_environment(receipt, first)
        other = self.root / 'other-consumer/dependencies.json'
        other.parent.mkdir()
        shutil.copyfile(self.lock, other)
        second = build_environment(other, config=replace(first_config, name='other-consumer'), **self.kwargs)
        self.assertEqual(first, second)
        self.assertEqual(manifest, (prefix / 'manifest.json').read_bytes())
        self.assertEqual(len(list((self.home / 'build/fixture').iterdir())), 1)
        # Use the actual static archive from the shared prefix in another CMake project.
        consumer = self.root / 'consumer'
        consumer.mkdir()
        (consumer / 'main.c').write_text('#include "fixture.h"\nint main(void) { return fixture() != 42; }\n')
        (consumer / 'CMakeLists.txt').write_text('''cmake_minimum_required(VERSION 3.21)
project(Consumer C)
find_path(FIXTURE_INCLUDE fixture.h REQUIRED)
find_library(FIXTURE_LIBRARY NAMES fixture REQUIRED)
add_executable(consumer main.c)
target_include_directories(consumer PRIVATE "${FIXTURE_INCLUDE}")
target_link_libraries(consumer PRIVATE "${FIXTURE_LIBRARY}")
enable_testing()
add_test(NAME actual-shared-library COMMAND consumer)
''')
        with first.lease():
            subprocess.run(['cmake', '-S', str(consumer), '-B', str(consumer / 'build'),
                            '-DCMAKE_PREFIX_PATH=' + str(prefix)], check=True, capture_output=True)
            subprocess.run(['cmake', '--build', str(consumer / 'build'), '--config', 'Release'],
                           check=True, capture_output=True)
            subprocess.run(['ctest', '--test-dir', str(consumer / 'build'), '-C', 'Release',
                            '--output-on-failure'], check=True, capture_output=True)
        verify_environment(self.lock, receipt, config=first_config, **self.kwargs)
        source = self.source / 'fixture.c'
        timestamp = source.stat().st_mtime_ns
        source.write_text('int fixture(void) { return 43; }\n')
        os.utime(source, ns=(timestamp, timestamp))
        with self.assertRaisesRegex(ValueError, 'inputs changed'):
            verify_environment(self.lock, receipt, config=first_config, **self.kwargs)
        self.assertEqual(manifest, (prefix / 'manifest.json').read_bytes())
        changed = build_environment(self.lock, config=first_config, **self.kwargs)
        self.assertEqual(changed.prefixes, first.prefixes)
        self.assertNotEqual(changed.artifacts[0].fingerprint, first.artifacts[0].fingerprint)
        with self.assertRaisesRegex(ValueError, 'generation changed'):
            with first.lease():
                pass

    def test_cycles_fail_before_source_or_toolchain_work(self):
        cyclic = replace(self.recipe, requires=('fixture',))
        with self.assertRaisesRegex(ValueError, 'Cyclic'):
            build_environment(self.lock, config=ProjectConfig(name='cycle', recipes=(cyclic,)), **self.kwargs)


if __name__ == '__main__':
    unittest.main()
