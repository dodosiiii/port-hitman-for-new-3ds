"""Pack a converted Hitman 47 data directory into one SD-card file."""

from __future__ import annotations

import argparse
import os
import struct
import sys
import tempfile
from pathlib import Path


VERSION = 1
HEADER = struct.Struct("<4sHHIIII")
ENTRY = struct.Struct("<III")
MAX_ARCHIVE_SIZE = 0x7FFFFFFF
MAX_STRING_TABLE_SIZE = 0xFFFFFFFF


def _archive_entries(source: Path) -> list[tuple[str, Path, int]]:
    if not source.is_dir():
        raise ValueError(f"package directory does not exist: {source}")

    files: list[tuple[str, Path, int]] = []
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        relative_path = path.relative_to(source).as_posix()
        if relative_path == "report.json":
            continue
        if (
            relative_path.startswith("/")
            or "\\" in relative_path
            or any(part in ("", ".", "..") for part in relative_path.split("/"))
        ):
            raise ValueError(f"invalid package path: {relative_path!r}")
        files.append((relative_path, path, path.stat().st_size))

    files.sort(key=lambda item: item[0])
    names = {name for name, _, _ in files}
    for required in ("index.bin", "tex/index.bin"):
        if required not in names:
            raise ValueError(f"package is missing required file: {required}")
    return files


def build_archive(source: Path, output: Path) -> tuple[int, int]:
    source = source.resolve()
    output = output.resolve()
    try:
        output.relative_to(source)
    except ValueError:
        pass
    else:
        raise ValueError("archive output must be outside the package directory")

    files = _archive_entries(source)
    if len(files) > 0xFFFFFFFF:
        raise ValueError("package has too many files")

    strings = bytearray()
    records: list[tuple[int, int, int]] = []
    data_offset = HEADER.size + len(files) * ENTRY.size
    strings_offset = data_offset
    for name, _, size in files:
        encoded_name = name.encode("utf-8") + b"\0"
        if len(strings) + len(encoded_name) > MAX_STRING_TABLE_SIZE:
            raise ValueError("package path table is too large")
        records.append((len(strings), 0, size))
        strings.extend(encoded_name)
    strings_offset += len(strings)
    data_offset = strings_offset

    for index, (name, path, size) in enumerate(files):
        path_offset, _, _ = records[index]
        if data_offset > MAX_ARCHIVE_SIZE or size > MAX_ARCHIVE_SIZE - data_offset:
            raise ValueError("archive exceeds the 3DS file-offset limit (2 GiB)")
        records[index] = (path_offset, data_offset, size)
        data_offset += size

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=output.parent,
            delete=False,
        ) as archive:
            temporary_path = Path(archive.name)
            archive.write(
                HEADER.pack(
                    b"C47P",
                    VERSION,
                    0,
                    len(files),
                    HEADER.size,
                    HEADER.size + len(files) * ENTRY.size,
                    len(strings),
                )
            )
            for record in records:
                archive.write(ENTRY.pack(*record))
            archive.write(strings)

            for (_, path, expected_size), (_, expected_offset, _) in zip(files, records):
                if archive.tell() != expected_offset:
                    raise OSError(f"unexpected archive offset while writing {path}")
                copied = 0
                with path.open("rb") as source_file:
                    while True:
                        chunk = source_file.read(1024 * 1024)
                        if not chunk:
                            break
                        archive.write(chunk)
                        copied += len(chunk)
                if copied != expected_size:
                    raise OSError(f"file changed while packing: {path}")

        os.replace(temporary_path, output)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return len(files), output.stat().st_size


def validate_archive(archive_path: Path) -> tuple[int, int]:
    with archive_path.open("rb") as archive:
        archive.seek(0, os.SEEK_END)
        archive_size = archive.tell()
        archive.seek(0)
        header_data = archive.read(HEADER.size)
        if len(header_data) != HEADER.size:
            raise ValueError("archive header is truncated")
        signature, version, reserved, count, records_offset, strings_offset, strings_size = (
            HEADER.unpack(header_data)
        )
        records_end = HEADER.size + count * ENTRY.size
        if (
            signature != b"C47P"
            or version != VERSION
            or reserved != 0
            or records_offset != HEADER.size
            or strings_offset != records_end
            or strings_offset + strings_size > archive_size
        ):
            raise ValueError("invalid archive header or table offsets")

        records_data = archive.read(count * ENTRY.size)
        strings = archive.read(strings_size)
        if len(records_data) != count * ENTRY.size or len(strings) != strings_size:
            raise ValueError("archive index is truncated")

        previous_name: str | None = None
        data_start = strings_offset + strings_size
        for index in range(count):
            path_offset, data_offset, data_size = ENTRY.unpack_from(
                records_data, index * ENTRY.size
            )
            if path_offset >= strings_size or data_offset < data_start or (
                data_offset > archive_size or data_size > archive_size - data_offset
            ):
                raise ValueError(f"invalid archive entry bounds at record {index}")
            terminator = strings.find(b"\0", path_offset)
            if terminator < 0:
                raise ValueError(f"unterminated path in archive record {index}")
            try:
                name = strings[path_offset:terminator].decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError(f"invalid UTF-8 path in archive record {index}") from error
            if (
                not name
                or name.startswith("/")
                or "\\" in name
                or any(part in ("", ".", "..") for part in name.split("/"))
                or (previous_name is not None and name <= previous_name)
            ):
                raise ValueError(f"invalid or unsorted path in archive record {index}")
            previous_name = name

    return count, archive_size


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pack a converted phase-2 directory into one New 3DS archive."
    )
    parser.add_argument("package", type=Path, help="phase-2 package directory")
    parser.add_argument(
        "output",
        type=Path,
        nargs="?",
        help="output .c47p file (default: next to the package directory)",
    )
    arguments = parser.parse_args()
    output = arguments.output or arguments.package.with_suffix(".c47p")
    try:
        count, size = build_archive(arguments.package, output)
        validated_count, validated_size = validate_archive(output)
        if (count, size) != (validated_count, validated_size):
            raise ValueError("archive verification returned inconsistent size or file count")
    except (OSError, ValueError) as error:
        print(f"c47pack: {error}", file=sys.stderr)
        return 1

    print(f"Archive verified: {output} ({count} files, {size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
