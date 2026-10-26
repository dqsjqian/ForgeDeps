"""Archive rejection must be portable and leave the destination untouched."""
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile

from forgedeps.deps_build import extract


class ArchiveTests(unittest.TestCase):
    def test_tar_rejects_unsafe_names_and_types_before_any_write(self):
        members = [
            ('../escape', tarfile.REGTYPE), ('/escape', tarfile.REGTYPE),
            ('other/file', tarfile.REGTYPE), ('package/file:stream', tarfile.REGTYPE),
            ('package/CON', tarfile.REGTYPE), ('package/nul.txt', tarfile.REGTYPE),
            ('package/LPT1.txt', tarfile.REGTYPE), ('package/file.', tarfile.REGTYPE),
            ('package/file ', tarfile.REGTYPE), ('package\\file', tarfile.REGTYPE),
            ('package/link', tarfile.SYMTYPE), ('package/link', tarfile.LNKTYPE),
            ('package/pipe', tarfile.FIFOTYPE), ('package/safe', tarfile.REGTYPE),
        ]
        for name, kind in members:
            with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive, output = root / 'source.tar.gz', root / 'out'
                output.mkdir()
                with tarfile.open(archive, 'w:gz') as package:
                    safe = tarfile.TarInfo('package/safe')
                    safe.size = 1
                    package.addfile(safe, io.BytesIO(b'x'))
                    bad = tarfile.TarInfo(name)
                    bad.type = kind
                    bad.linkname = '../escape'
                    package.addfile(bad)
                with self.assertRaises(ValueError):
                    extract(archive, output, 'package')
                self.assertEqual(list(output.iterdir()), [])

    def test_zip_rejects_symlinks_before_any_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, output = root / 'source.zip', root / 'out'
            output.mkdir()
            with zipfile.ZipFile(archive, 'w') as package:
                package.writestr('package/safe', 'safe')
                link = zipfile.ZipInfo('package/link')
                link.create_system = 3
                link.external_attr = 0o120777 << 16
                package.writestr(link, '../escape')
            with self.assertRaises(ValueError):
                extract(archive, output, 'package')
            self.assertEqual(list(output.iterdir()), [])
