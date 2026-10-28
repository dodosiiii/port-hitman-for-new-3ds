import struct
import tempfile
import unittest
from pathlib import Path

from tools.c47pack import ENTRY, HEADER, build_archive, validate_archive


class C47PackTests(unittest.TestCase):
    def test_builds_single_archive_with_sorted_seekable_entries(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            package = root / "package"
            (package / "tex").mkdir(parents=True)
            (package / "mesh" / "C1_1").mkdir(parents=True)
            (package / "index.bin").write_bytes(b"C47I-index")
            (package / "tex" / "index.bin").write_bytes(b"C47T-index")
            (package / "mesh" / "C1_1" / "00000.c47m").write_bytes(b"C47M-model")
            (package / "report.json").write_text("local report", encoding="utf-8")
            archive_path = root / "package.c47p"

            count, size = build_archive(package, archive_path)

            self.assertEqual(validate_archive(archive_path), (count, size))
            with archive_path.open("rb") as archive:
                signature, version, reserved, entry_count, records_offset, strings_offset, strings_size = (
                    HEADER.unpack(archive.read(HEADER.size))
                )
                self.assertEqual((signature, version, reserved), (b"C47P", 1, 0))
                self.assertEqual(entry_count, 3)
                self.assertEqual(records_offset, HEADER.size)
                self.assertEqual(strings_offset, HEADER.size + entry_count * ENTRY.size)
                records = [
                    ENTRY.unpack(archive.read(ENTRY.size))
                    for _ in range(entry_count)
                ]
                strings = archive.read(strings_size)
                contents = {}
                for path_offset, data_offset, data_size in records:
                    end = strings.index(b"\0", path_offset)
                    name = strings[path_offset:end].decode("utf-8")
                    archive.seek(data_offset)
                    contents[name] = archive.read(data_size)

            self.assertEqual(
                contents,
                {
                    "index.bin": b"C47I-index",
                    "mesh/C1_1/00000.c47m": b"C47M-model",
                    "tex/index.bin": b"C47T-index",
                },
            )

    def test_rejects_output_inside_package_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            package = Path(temporary_directory) / "package"
            (package / "tex").mkdir(parents=True)
            (package / "index.bin").write_bytes(b"index")
            (package / "tex" / "index.bin").write_bytes(b"texture index")

            with self.assertRaisesRegex(ValueError, "outside the package directory"):
                build_archive(package, package / "data.c47p")

    def test_rejects_truncated_archive(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            archive_path = Path(temporary_directory) / "broken.c47p"
            archive_path.write_bytes(struct.pack("<4sHH", b"C47P", 1, 0))

            with self.assertRaisesRegex(ValueError, "header is truncated"):
                validate_archive(archive_path)


if __name__ == "__main__":
    unittest.main()
