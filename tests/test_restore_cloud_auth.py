import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from utils.cloud_auth import CloudAuthError
from utils.restore_cloud_auth import restore_archive


def make_archive(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files:
            archive.writestr(name, content)
    return stream.getvalue()


class RestoreArchiveTests(unittest.TestCase):
    def test_restores_only_expected_encrypted_files(self):
        with tempfile.TemporaryDirectory() as directory:
            restore_archive(make_archive([("account.bin", b"opaque encrypted bytes")]), ["account"], directory)
            self.assertEqual((Path(directory) / "account.bin").read_bytes(), b"opaque encrypted bytes")
            self.assertEqual([path.name for path in Path(directory).iterdir()], ["account.bin"])

    def test_rejects_traversal_missing_extra_and_duplicate_files(self):
        for entries in ([('../account.bin', b'x')], [('other.bin', b'x')], [],
                        [('account.bin', b'x'), ('secret.txt', b'x')],
                        [('account.bin', b'x'), ('account.bin', b'y')]):
            with self.subTest(entries=[entry[0] for entry in entries]), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(CloudAuthError):
                    restore_archive(make_archive(entries), ["account"], directory)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_rejects_oversized_expanded_contents(self):
        with tempfile.TemporaryDirectory() as directory, patch('utils.restore_cloud_auth.MAX_ARCHIVE_BYTES', 1024):
            with self.assertRaises(CloudAuthError):
                restore_archive(make_archive([('account.bin', b'x' * 2000)]), ["account"], directory)

    def test_rejects_corrupt_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CloudAuthError):
                restore_archive(b'not a zip', ["account"], directory)


if __name__ == '__main__':
    unittest.main()
