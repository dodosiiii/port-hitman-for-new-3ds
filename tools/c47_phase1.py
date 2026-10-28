#!/usr/bin/env python3
"""Independent, read-only reader and OBJ/PNG exporter for C47 scene archives."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import struct
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    Image = None


class FormatError(ValueError):
    """Raised when a scene file contains an unsupported or invalid structure."""


@dataclass
class Chunk:
    tag: int
    size: int
    main_data: bytes = b""
    multi_data: list[bytes] = field(default_factory=list)
    children: list["Chunk"] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.tag.to_bytes(4, "big").lstrip(b"\0").decode("ascii", errors="replace")

    def child(self, name: str) -> "Chunk | None":
        expected = int.from_bytes(name.encode("ascii"), "big")
        return next((item for item in self.children if item.tag == expected), None)


class BinaryReader:
    def __init__(self, data: bytes | bytearray | memoryview, label: str):
        self.data = memoryview(data)
        self.label = label

    def require(self, offset: int, size: int, limit: int | None = None) -> None:
        end = len(self.data) if limit is None else min(limit, len(self.data))
        if offset < 0 or size < 0 or offset > end or size > end - offset:
            raise FormatError(
                f"{self.label}: read at 0x{offset:X} for 0x{size:X} bytes "
                f"exceeds available data (0x{end:X})"
            )

    def u16(self, offset: int, limit: int | None = None) -> int:
        self.require(offset, 2, limit)
        return struct.unpack_from("<H", self.data, offset)[0]

    def u32(self, offset: int, limit: int | None = None) -> int:
        self.require(offset, 4, limit)
        return struct.unpack_from("<I", self.data, offset)[0]

    def i32(self, offset: int, limit: int | None = None) -> int:
        self.require(offset, 4, limit)
        return struct.unpack_from("<i", self.data, offset)[0]

    def f32(self, offset: int, limit: int | None = None) -> float:
        self.require(offset, 4, limit)
        return struct.unpack_from("<f", self.data, offset)[0]


def parse_chunk(data: bytes, offset: int = 0, parent_end: int | None = None) -> Chunk:
    reader = BinaryReader(data, "chunk")
    if parent_end is None:
        parent_end = len(data)
    reader.require(offset, 8, parent_end)

    tag = reader.u32(offset, parent_end)
    info = reader.u32(offset + 4, parent_end)
    chunk_size = info & 0x3FFFFFFF
    has_subchunks = bool(info & 0x80000000)
    has_multi_data = bool(info & 0x40000000)
    if chunk_size < 8:
        raise FormatError(f"{reader.label}: chunk at 0x{offset:X} is smaller than its header")
    chunk_end = offset + chunk_size
    reader.require(offset, chunk_size, parent_end)

    cursor = offset + 8
    if has_subchunks or has_multi_data:
        data_offset = reader.u32(cursor, chunk_end)
        cursor += 4
        if data_offset < cursor - offset or data_offset > chunk_size:
            raise FormatError(f"{reader.label}: invalid data offset in chunk {tag:08X}")
    else:
        data_offset = 8

    child_count = reader.u32(cursor, chunk_end) if has_subchunks else 0
    if has_subchunks:
        cursor += 4

    lengths: list[int] = []
    if has_multi_data:
        item_count = reader.u32(cursor, chunk_end)
        cursor += 4
        if item_count > (chunk_size // 4):
            raise FormatError(f"{reader.label}: impossible multidata count in chunk {tag:08X}")
        reader.require(cursor, item_count * 4, chunk_end)
        lengths = [reader.u32(cursor + i * 4, chunk_end) for i in range(item_count)]
        cursor += item_count * 4

    data_start = offset + data_offset
    if cursor > data_start:
        raise FormatError(f"{reader.label}: chunk metadata overlaps its data")

    children: list[Chunk] = []
    child_cursor = cursor
    for _ in range(child_count):
        child = parse_chunk(data, child_cursor, data_start)
        children.append(child)
        child_cursor += child.size
    if child_cursor > data_start:
        raise FormatError(f"{reader.label}: child chunks exceed their parent data offset")

    if has_multi_data:
        multi_data: list[bytes] = []
        position = data_start
        for length in lengths:
            reader.require(position, length, chunk_end)
            multi_data.append(bytes(reader.data[position : position + length]))
            position += length
        main_data = b""
    else:
        multi_data = []
        main_data = bytes(reader.data[data_start:chunk_end])

    return Chunk(tag, chunk_size, main_data, multi_data, children)


@dataclass
class PackNode:
    tag: int
    info: int
    size: int
    data_offset: int
    lengths: list[int]
    children: list["PackNode"] = field(default_factory=list)
    main_data: bytes = b""
    multi_data: list[bytes] = field(default_factory=list)


def reconstruct_pack(pack_repeat: bytes, repeat: bytes, label: str = "PackRepeat") -> Chunk:
    reader = BinaryReader(pack_repeat, label)
    if len(reader.data) < 16:
        raise FormatError(f"{label}: truncated pack-repeat header")

    reconstruction_offset = reader.u32(4)
    cursor = 8
    destinations: dict[int, tuple[PackNode, int]] = {}

    def parse_node(base_offset: int) -> PackNode:
        nonlocal cursor
        node_start = cursor
        reader.require(cursor, 8)
        tag = reader.u32(cursor)
        info = reader.u32(cursor + 4)
        cursor += 8
        size = info & 0x3FFFFFFF
        has_children = bool(info & 0x80000000)
        has_multiple = bool(info & 0x40000000)
        if size < 8:
            raise FormatError(f"{label}: invalid chunk size {size}")

        if has_children or has_multiple:
            data_offset = reader.u32(cursor)
            cursor += 4
        else:
            data_offset = 8

        child_count = reader.u32(cursor) if has_children else 0
        if has_children:
            cursor += 4

        lengths: list[int] = []
        if has_multiple:
            count = reader.u32(cursor)
            cursor += 4
            if count > size // 4:
                raise FormatError(f"{label}: impossible multidata count {count}")
            reader.require(cursor, count * 4)
            lengths = [reader.u32(cursor + i * 4) for i in range(count)]
            cursor += count * 4

        node = PackNode(tag, info, size, data_offset, lengths)
        if data_offset < cursor - node_start or data_offset > size:
            raise FormatError(f"{label}: invalid data offset for chunk {tag:08X}")

        data_position = base_offset + data_offset
        if has_multiple:
            for index, length in enumerate(lengths):
                if data_position in destinations:
                    raise FormatError(f"{label}: duplicate reconstruction target 0x{data_position:X}")
                destinations[data_position] = (node, index)
                data_position += length
        else:
            if data_position in destinations:
                raise FormatError(f"{label}: duplicate reconstruction target 0x{data_position:X}")
            destinations[data_position] = (node, -1)

        if has_children:
            virtual_child_offset = base_offset + 16 + (4 + len(lengths) * 4 if has_multiple else 0)
            for _ in range(child_count):
                child = parse_node(virtual_child_offset)
                node.children.append(child)
                virtual_child_offset += child.size
            if virtual_child_offset > base_offset + data_offset:
                raise FormatError(f"{label}: reconstructed child chunks overlap data")
        return node

    root = parse_node(0)
    table_start = 8 + reconstruction_offset
    if table_start < cursor or table_start > len(pack_repeat):
        raise FormatError(f"{label}: invalid reconstruction table offset 0x{table_start:X}")
    table_size = len(pack_repeat) - table_start
    if table_size % 16:
        raise FormatError(f"{label}: reconstruction table has {table_size % 16} trailing bytes")

    repeat_reader = BinaryReader(repeat, f"{label} repeat data")
    for position in range(table_start, len(pack_repeat), 16):
        repeat_offset, destination, data_size, patched_word = struct.unpack_from(
            "<IIII", pack_repeat, position
        )
        target = destinations.get(destination)
        if target is None:
            raise FormatError(f"{label}: no data buffer at reconstruction offset 0x{destination:X}")
        node, item_index = target
        if item_index < 0:
            target_length = data_size
        else:
            target_length = node.lengths[item_index]
            if target_length != data_size:
                raise FormatError(
                    f"{label}: multidata length mismatch ({target_length} != {data_size})"
                )
        repeat_reader.require(repeat_offset, data_size)
        payload = bytearray(repeat[repeat_offset : repeat_offset + data_size])
        if len(payload) < 4:
            raise FormatError(f"{label}: reconstructed data is too small for its patch word")
        struct.pack_into("<I", payload, 0, patched_word)
        if item_index < 0:
            node.main_data = bytes(payload)
        else:
            if not node.multi_data:
                node.multi_data = [b""] * len(node.lengths)
            node.multi_data[item_index] = bytes(payload)
        if len(payload) != target_length:
            raise FormatError(f"{label}: reconstructed data has an invalid size")

    return Chunk(
        root.tag,
        root.size,
        root.main_data,
        root.multi_data,
        [
            Chunk(c.tag, c.size, c.main_data, c.multi_data, _materialize_children(c))
            for c in root.children
        ],
    )


def _materialize_children(node: PackNode) -> list[Chunk]:
    return [
        Chunk(
            child.tag,
            child.size,
            child.main_data,
            child.multi_data,
            _materialize_children(child),
        )
        for child in node.children
    ]


def read_pack(archive: zipfile.ZipFile, extension: str, game_root: Path) -> Chunk | None:
    repeat_pack_name = f"PackRepeat.{extension}"
    direct_pack_name = f"Pack.{extension}"
    names = set(archive.namelist())

    if repeat_pack_name in names:
        repeat_file = game_root / f"Repeat.{extension}"
        if not repeat_file.is_file():
            raise FormatError(
                f"{archive.filename}: {repeat_pack_name} requires {repeat_file}"
            )
        return reconstruct_pack(
            archive.read(repeat_pack_name),
            repeat_file.read_bytes(),
            f"{archive.filename}:{repeat_pack_name}",
        )
    if direct_pack_name in names:
        return parse_chunk(archive.read(direct_pack_name))
    return None


def require_chunk(parent: Chunk, name: str) -> Chunk:
    result = parent.child(name)
    if result is None:
        raise FormatError(f"{parent.name}: required subchunk {name} was not found")
    return result


def read_c_string(data: bytes, offset: int, label: str) -> str:
    if offset < 0 or offset >= len(data):
        raise FormatError(f"{label}: string offset 0x{offset:X} is out of range")
    end = data.find(b"\0", offset)
    if end < 0:
        raise FormatError(f"{label}: unterminated string at 0x{offset:X}")
    return data[offset:end].decode("latin-1")


def parse_texture(chunk: Chunk) -> dict[str, object]:
    data = chunk.main_data
    reader = BinaryReader(data, f"{chunk.name} texture")
    reader.require(0, 21)
    texture_id = reader.u32(0)
    height = reader.u16(4)
    width = reader.u16(6)
    mip_count = reader.u32(8)
    if not width or not height or not mip_count:
        raise FormatError(f"{chunk.name}: invalid texture dimensions or mip count")

    name = read_c_string(data, 20, f"{chunk.name} texture")
    cursor = 20 + len(name.encode("latin-1")) + 1
    mips: list[bytes] = []
    for mip in range(mip_count):
        size = reader.u32(cursor)
        cursor += 4
        reader.require(cursor, size)
        mips.append(bytes(data[cursor : cursor + size]))
        cursor += size
        expected_width = max(1, width >> mip)
        expected_height = max(1, height >> mip)
        if chunk.name == "RGBA":
            expected_bytes = expected_width * expected_height * 4
        elif chunk.name == "PALN":
            expected_bytes = expected_width * expected_height
        elif chunk.name in ("DXT1", "DXT3"):
            block_size = 8 if chunk.name == "DXT1" else 16
            expected_bytes = (
                (expected_width + 3) // 4
                * ((expected_height + 3) // 4)
                * block_size
            )
        else:
            raise FormatError(f"Unsupported texture chunk type {chunk.name!r}")
        if size != expected_bytes:
            raise FormatError(
                f"{chunk.name} texture {name!r} mip {mip}: {size} bytes, "
                f"expected {expected_bytes} for {expected_width}x{expected_height}"
            )

    palette: bytes | None = None
    if chunk.name == "PALN":
        palette_size = reader.u32(cursor)
        cursor += 4
        if palette_size > 256:
            raise FormatError(f"PALN texture {name!r} has {palette_size} palette entries")
        palette_bytes = palette_size * 4
        reader.require(cursor, palette_bytes)
        palette = bytes(data[cursor : cursor + palette_bytes])
        cursor += palette_bytes
        if palette_size == 0:
            raise FormatError(f"PALN texture {name!r} has an empty palette")
    elif chunk.name not in ("RGBA", "DXT1", "DXT3"):
        raise FormatError(f"Unsupported texture chunk type {chunk.name!r}")

    if cursor != len(data):
        raise FormatError(
            f"{chunk.name} texture {name!r} has {len(data) - cursor} unexplained trailing bytes"
        )
    return {
        "id": texture_id,
        "name": name,
        "width": width,
        "height": height,
        "mip_count": mip_count,
        "format": chunk.name,
        "mips": mips,
        "palette": palette,
    }


def texture_rgba(texture: dict[str, object]) -> bytes:
    pixels = texture["mips"][0]
    if texture["format"] == "RGBA":
        return pixels
    palette = texture["palette"]
    assert isinstance(pixels, bytes) and isinstance(palette, bytes)
    return b"".join(
        palette[index * 4 : index * 4 + 4]
        if index < len(palette) // 4
        else b"\0\0\0\0"
        for index in pixels
    )


def _color_565(value: int) -> tuple[int, int, int, int]:
    red = (value >> 11) & 0x1F
    green = (value >> 5) & 0x3F
    blue = value & 0x1F
    return (
        (red << 3) | (red >> 2),
        (green << 2) | (green >> 4),
        (blue << 3) | (blue >> 2),
        255,
    )


def _decode_dxt_block(data: bytes, dxt_format: str) -> list[tuple[int, int, int, int]]:
    alpha_values: list[int] | None = None
    if dxt_format == "DXT3":
        alpha_values = [
            ((data[index // 2] >> (4 * (index % 2))) & 0x0F) * 17
            for index in range(16)
        ]
        data = data[8:]

    color_0, color_1, color_indices = struct.unpack("<HHI", data)
    first = _color_565(color_0)
    second = _color_565(color_1)
    if color_0 > color_1 or dxt_format == "DXT3":
        colors = (
            first,
            second,
            tuple((2 * first[channel] + second[channel]) // 3 for channel in range(3))
            + (255,),
            tuple((first[channel] + 2 * second[channel]) // 3 for channel in range(3))
            + (255,),
        )
    else:
        colors = (
            first,
            second,
            tuple((first[channel] + second[channel]) // 2 for channel in range(3)) + (255,),
            (0, 0, 0, 0),
        )

    pixels = []
    for index in range(16):
        color = colors[(color_indices >> (index * 2)) & 0x03]
        alpha = alpha_values[index] if alpha_values is not None else color[3]
        pixels.append(color[:3] + (alpha,))
    return pixels


def texture_dxt_rgba(texture: dict[str, object]) -> bytes:
    dxt_format = str(texture["format"])
    if dxt_format not in ("DXT1", "DXT3"):
        raise FormatError(f"Cannot DXT-decode texture format {dxt_format!r}")

    width = int(texture["width"])
    height = int(texture["height"])
    mip_data = texture["mips"][0]
    assert isinstance(mip_data, bytes)
    block_size = 8 if dxt_format == "DXT1" else 16
    output = bytearray(width * height * 4)
    cursor = 0
    for block_y in range((height + 3) // 4):
        for block_x in range((width + 3) // 4):
            pixels = _decode_dxt_block(
                mip_data[cursor : cursor + block_size], dxt_format
            )
            cursor += block_size
            for index, pixel in enumerate(pixels):
                x = block_x * 4 + index % 4
                y = block_y * 4 + index // 4
                if x < width and y < height:
                    pixel_offset = (y * width + x) * 4
                    output[pixel_offset : pixel_offset + 4] = bytes(pixel)
    return bytes(output)


def parse_pathfinder(data: bytes) -> dict[str, object]:
    reader = BinaryReader(data, "Pathfinder data")
    name_buffer_size = reader.u32(0)
    cursor = 4
    if name_buffer_size == 0x12312312:
        name_buffer_size = reader.u32(cursor)
        cursor += 4
    reader.require(cursor, name_buffer_size)
    name_buffer = data[cursor : cursor + name_buffer_size]
    cursor += name_buffer_size

    def count(record_size: int = 1) -> int:
        nonlocal cursor
        value = reader.u32(cursor)
        cursor += 4
        reader.require(cursor, value * record_size)
        return value

    def read_string(offset: int, label: str) -> str:
        return read_c_string(name_buffer, offset, f"Pathfinder {label}")

    room_count = count(4)
    rooms: list[dict[str, int]] = []
    total_leaf_nodes = 0
    total_nodes = 0
    total_layers = 0
    total_leaf_edges = 0
    total_doors = 0
    total_kongs = 0
    for room_index in range(room_count):
        leaf_count = count(24)
        cursor += leaf_count * 24
        node_count = count(11)
        cursor += node_count * 11
        layer_count = count(4)
        cursor += layer_count * 4
        leaf_edge_count = count(8)
        cursor += leaf_edge_count * 8
        reader.require(cursor, 36)
        bounds = struct.unpack_from("<9f", data, cursor)
        if not all(math.isfinite(value) for value in bounds):
            raise FormatError(f"Pathfinder room {room_index} has non-finite bounds")
        cursor += 36

        door_count = count(16)
        cursor += door_count * 16
        door_edge_count = door_count * (door_count - 1) // 2
        reader.require(cursor, door_edge_count * 12 + door_count * 12)
        cursor += door_edge_count * 12 + door_count * 12
        kong_count = count(12)
        cursor += kong_count * 12
        string_offset = reader.u32(cursor)
        cursor += 4
        read_string(string_offset, f"room {room_index} name")

        rooms.append(
            {
                "leaf_nodes": leaf_count,
                "nodes": node_count,
                "layers": layer_count,
                "leaf_edges": leaf_edge_count,
                "doors": door_count,
                "kongs": kong_count,
            }
        )
        total_leaf_nodes += leaf_count
        total_nodes += node_count
        total_layers += layer_count
        total_leaf_edges += leaf_edge_count
        total_doors += door_count
        total_kongs += kong_count

    room_instance_count = count(8)
    total_instance_door_refs = 0
    for instance_index in range(room_instance_count):
        string_offset = reader.u32(cursor)
        room_index = reader.i32(cursor + 4)
        reader.require(cursor, 8)
        cursor += 8
        if room_index < 0 or room_index >= room_count:
            raise FormatError(
                f"Pathfinder room instance {instance_index} has invalid room index {room_index}"
            )
        read_string(string_offset, f"room instance {instance_index} name")
        door_refs = rooms[room_index]["doors"]
        reader.require(cursor, door_refs * 4)
        cursor += door_refs * 4
        total_instance_door_refs += door_refs

    door_instance_count = count(16)
    cursor += door_instance_count * 16
    last_value = reader.u32(cursor)
    cursor += 4
    if cursor != len(data):
        raise FormatError(
            f"Pathfinder data has {len(data) - cursor} unexplained trailing bytes"
        )

    return {
        "rooms": rooms,
        "room_count": room_count,
        "room_instances": room_instance_count,
        "door_instances": door_instance_count,
        "last_value": last_value,
        "leaf_nodes": total_leaf_nodes,
        "nodes": total_nodes,
        "layers": total_layers,
        "leaf_edges": total_leaf_edges,
        "doors": total_doors,
        "kongs": total_kongs,
        "room_instance_door_refs": total_instance_door_refs,
    }


def _pathfinder_data(spk: dict[str, Chunk], record: tuple[int, ...]) -> bytes:
    dbl = BinaryReader(spk["PDBL"].main_data, "SPK object properties")
    list_offset = record[0]
    list_size = dbl.u32(list_offset) & 0x00FFFFFF
    if list_size < 5:
        raise FormatError("SPK object property list is too small")
    list_end = list_offset + list_size
    dbl.require(list_offset, list_size)
    cursor = list_offset + 4
    entry_index = 0
    while cursor < list_end:
        code = dbl.data[cursor]
        if code == 0xFF:
            break
        value_type = code & 0x3F
        cursor += 1
        if value_type in (0, 6):
            size = 0
        elif value_type == 1:
            size = 8
        elif value_type in (2, 3, 8, 10, 11):
            size = 4
        elif value_type in (4, 5):
            end = dbl.data[cursor:list_end].tobytes().find(b"\0")
            if end < 0:
                raise FormatError("SPK object property has an unterminated string")
            size = end + 1
        elif value_type in (7, 9, 12):
            size = dbl.u32(cursor)
            if size < 4:
                raise FormatError("SPK object property has an invalid data length")
            if value_type == 7 and entry_index == 14:
                dbl.require(cursor, size, list_end)
                return bytes(dbl.data[cursor + 4 : cursor + size])
        else:
            raise FormatError(f"SPK object property has unsupported type {value_type}")
        dbl.require(cursor, size, list_end)
        cursor += size
        entry_index += 1
    raise FormatError("Pathfinder object has no binary-data property at entry 14")


def _read_wave_chunks(data: bytes) -> dict[bytes, bytes]:
    reader = BinaryReader(data, "WPCM RIFF/WAVE")
    reader.require(0, 12)
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise FormatError("WPCM entry is not a RIFF/WAVE file")
    riff_end = reader.u32(4) + 8
    if riff_end != len(data):
        raise FormatError(
            f"WPCM RIFF size ends at 0x{riff_end:X}, but entry ends at 0x{len(data):X}"
        )

    chunks: dict[bytes, bytes] = {}
    cursor = 12
    while cursor < riff_end:
        reader.require(cursor, 8, riff_end)
        tag = bytes(reader.data[cursor : cursor + 4])
        size = reader.u32(cursor + 4, riff_end)
        payload_start = cursor + 8
        reader.require(payload_start, size, riff_end)
        if tag in chunks:
            raise FormatError(f"WPCM RIFF contains duplicate {tag!r} chunks")
        chunks[tag] = bytes(reader.data[payload_start : payload_start + size])
        cursor = payload_start + size + (size & 1)
    if cursor != riff_end:
        raise FormatError("WPCM RIFF has an invalid odd-sized chunk boundary")
    return chunks


def parse_wave(data: bytes) -> dict[str, int | float]:
    chunks = _read_wave_chunks(data)
    fmt = chunks.get(b"fmt ")
    audio_data = chunks.get(b"data")
    if fmt is None or audio_data is None or len(fmt) < 16:
        raise FormatError("WPCM RIFF is missing a valid fmt or data chunk")
    format_tag, channels, sample_rate, byte_rate, block_align, bits_per_sample = (
        struct.unpack_from("<HHIIHH", fmt)
    )
    if not channels or not sample_rate or not block_align:
        raise FormatError("WPCM RIFF has invalid channel, rate, or block-alignment values")

    samples_per_block = 0
    sample_count = 0
    if format_tag == 0x0011:
        if len(fmt) < 20 or struct.unpack_from("<H", fmt, 16)[0] < 2:
            raise FormatError("IMA ADPCM fmt chunk is missing its extension")
        samples_per_block = struct.unpack_from("<H", fmt, 18)[0]
        fact = chunks.get(b"fact")
        if fact is None or len(fact) < 4:
            raise FormatError("IMA ADPCM RIFF is missing its sample-count fact chunk")
        sample_count = struct.unpack_from("<I", fact)[0]
        if bits_per_sample != 4 or block_align < 4 * channels:
            raise FormatError("IMA ADPCM fmt block size and samples-per-block disagree")
        expected_samples = 1 + (
            (block_align - 4 * channels) * 8 // (bits_per_sample * channels)
        )
        if samples_per_block != expected_samples:
            raise FormatError("IMA ADPCM fmt block size and samples-per-block disagree")
        if len(audio_data) % block_align:
            raise FormatError("IMA ADPCM data size is not a whole number of blocks")
    return {
        "format_tag": format_tag,
        "channels": channels,
        "sample_rate": sample_rate,
        "byte_rate": byte_rate,
        "block_align": block_align,
        "bits_per_sample": bits_per_sample,
        "samples_per_block": samples_per_block,
        "sample_count": sample_count,
        "data_bytes": len(audio_data),
        "duration_seconds": sample_count / sample_rate if sample_count else 0.0,
    }


def decode_ima_adpcm(data: bytes) -> bytes:
    wave = parse_wave(data)
    if wave["format_tag"] != 0x0011:
        raise FormatError(f"Cannot decode WAVE format tag {wave['format_tag']}")
    if wave["channels"] != 1:
        raise FormatError(
            f"IMA ADPCM decoder requires mono audio, got {wave['channels']} channels"
        )

    encoded = _read_wave_chunks(data)[b"data"]
    block_align = int(wave["block_align"])
    sample_count = int(wave["sample_count"])
    decoded_samples: list[int] = []
    step_table = (
        7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31,
        34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143,
        157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544,
        598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707,
        1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871,
        5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635,
        13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
        32767,
    )
    index_adjust = (-1, -1, -1, -1, 2, 4, 6, 8)

    for block_offset in range(0, len(encoded), block_align):
        block = encoded[block_offset : block_offset + block_align]
        if len(block) < 4:
            raise FormatError("IMA ADPCM block is missing its predictor header")
        predictor, step_index = struct.unpack_from("<hB", block)
        if step_index > 88:
            raise FormatError(f"IMA ADPCM block has invalid step index {step_index}")
        decoded_samples.append(predictor)
        for packed_nibbles in block[4:]:
            for code in (packed_nibbles & 0x0F, packed_nibbles >> 4):
                step = step_table[step_index]
                difference = ((code & 0x07) * 2 + 1) * step >> 3
                predictor += -difference if code & 8 else difference
                predictor = max(-32768, min(32767, predictor))
                step_index = max(0, min(88, step_index + index_adjust[code & 0x07]))
                decoded_samples.append(predictor)

    if sample_count > len(decoded_samples):
        raise FormatError(
            f"IMA ADPCM fact count {sample_count} exceeds decoded capacity "
            f"{len(decoded_samples)}"
        )
    del decoded_samples[sample_count:]
    return struct.pack(f"<{len(decoded_samples)}h", *decoded_samples)


def summarize_audio(pack: Chunk | None) -> dict[str, object]:
    if pack is None:
        return {
            "chunks": 0,
            "formats": {},
            "sample_rates": [],
            "audio_data_bytes": 0,
            "duration_seconds": 0.0,
        }

    formats: dict[int, int] = {}
    sample_rates: set[int] = set()
    audio_data_bytes = 0
    duration_seconds = 0.0
    for chunk in pack.children:
        if chunk.name != "WPCM":
            raise FormatError(f"WAV pack contains unsupported entry type {chunk.name!r}")
        audio = parse_wave(chunk.main_data)
        format_tag = int(audio["format_tag"])
        formats[format_tag] = formats.get(format_tag, 0) + 1
        sample_rates.add(int(audio["sample_rate"]))
        audio_data_bytes += int(audio["data_bytes"])
        duration_seconds += float(audio["duration_seconds"])
    return {
        "chunks": len(pack.children),
        "formats": formats,
        "sample_rates": sorted(sample_rates),
        "audio_data_bytes": audio_data_bytes,
        "duration_seconds": duration_seconds,
    }


def audit_dxt(archive_path: Path, game_root: Path) -> dict[str, object]:
    with zipfile.ZipFile(archive_path) as archive:
        pal_pack = read_pack(archive, "PAL", game_root)
        dxt_pack = read_pack(archive, "DXT", game_root)
    if pal_pack is None or dxt_pack is None:
        raise FormatError(f"{archive_path}: both PAL and DXT packs are required")
    if len(pal_pack.children) != len(dxt_pack.children):
        raise FormatError(
            f"{archive_path}: PAL/DXT chunk count mismatch "
            f"({len(pal_pack.children)} != {len(dxt_pack.children)})"
        )

    formats: dict[str, int] = {}
    resolution_mismatches: list[dict[str, object]] = []
    visible_rgb_error = 0
    visible_rgb_channels = 0
    alpha_error = 0
    alpha_pixels = 0
    decoded_textures = 0
    for pal_chunk, dxt_chunk in zip(pal_pack.children, dxt_pack.children):
        pal_texture = parse_texture(pal_chunk)
        dxt_texture = parse_texture(dxt_chunk)
        if pal_texture["id"] != dxt_texture["id"]:
            raise FormatError(f"{archive_path}: PAL/DXT texture IDs are not aligned")
        formats[dxt_chunk.name] = formats.get(dxt_chunk.name, 0) + 1
        if (pal_texture["width"], pal_texture["height"]) != (
            dxt_texture["width"],
            dxt_texture["height"],
        ):
            resolution_mismatches.append(
                {
                    "id": pal_texture["id"],
                    "name": pal_texture["name"],
                    "pal": [pal_texture["width"], pal_texture["height"]],
                    "dxt": [dxt_texture["width"], dxt_texture["height"]],
                }
            )
            continue
        if dxt_chunk.name not in ("DXT1", "DXT3"):
            continue

        decoded = texture_dxt_rgba(dxt_texture)
        reference = texture_rgba(pal_texture)
        for offset in range(0, len(decoded), 4):
            alpha_error += abs(decoded[offset + 3] - reference[offset + 3])
            alpha_pixels += 1
            if reference[offset + 3] > 0:
                visible_rgb_error += sum(
                    abs(decoded[offset + channel] - reference[offset + channel])
                    for channel in range(3)
                )
                visible_rgb_channels += 3
        decoded_textures += 1

    if not decoded_textures or not alpha_pixels or not visible_rgb_channels:
        raise FormatError(f"{archive_path}: no matching DXT1/DXT3 images to compare")
    return {
        "mission": archive_path.stem,
        "textures_by_format": formats,
        "decoded_textures": decoded_textures,
        "resolution_mismatches": resolution_mismatches,
        "visible_rgb_mae": visible_rgb_error / visible_rgb_channels,
        "alpha_mae": alpha_error / alpha_pixels,
        "compared_pixels": alpha_pixels,
    }


def extract_textures(pack: Chunk | None) -> dict[int, dict[str, object]]:
    if pack is None:
        return {}
    textures: dict[int, dict[str, object]] = {}
    for chunk in pack.children:
        texture = parse_texture(chunk)
        texture_id = int(texture["id"])
        if texture_id in textures:
            raise FormatError(f"{pack.name}: duplicate texture ID {texture_id}")
        textures[texture_id] = texture
    return textures


def _read_float_array(
    data: bytes,
    float_offset: int,
    count: int,
    label: str,
    allow_non_finite: bool = False,
) -> list[float]:
    byte_offset = float_offset * 4
    size = count * 4
    reader = BinaryReader(data, label)
    reader.require(byte_offset, size)
    values = list(struct.unpack_from(f"<{count}f", data, byte_offset))
    for index, value in enumerate(values):
        if not math.isfinite(value) and not allow_non_finite:
            raise FormatError(
                f"{label}: non-finite float at array index {index} "
                f"(byte offset 0x{byte_offset + index * 4:X})"
            )
    return values


def _matrix_multiply(a: tuple[tuple[float, ...], ...], b: tuple[tuple[float, ...], ...]):
    return tuple(
        tuple(sum(a[row][k] * b[k][column] for k in range(4)) for column in range(4))
        for row in range(4)
    )


def _object_matrix(record: tuple[int, ...], positions: bytes, matrices: bytes) -> tuple:
    reader = BinaryReader(positions, "SPK positions")
    reader.require(record[4], 12)
    position = struct.unpack_from("<3f", positions, record[4])
    if not all(math.isfinite(value) for value in position):
        raise FormatError("SPK object position contains a non-finite float")

    matrix_offset = record[3] * 16
    matrix_reader = BinaryReader(matrices, "SPK rotations")
    values = [matrix_reader.i32(matrix_offset + i * 4) / 1073741824.0 for i in range(4)]
    z2 = max(0.0, 1.0 - values[0] * values[0] - values[1] * values[1])
    y2 = max(0.0, 1.0 - values[2] * values[2] - values[3] * values[3])
    row_z = [values[0], values[1], math.sqrt(z2)]
    row_y = [values[2], values[3], math.sqrt(y2)]
    if matrix_reader.i32(matrix_offset) & 1:
        row_z[2] = -row_z[2]
    if matrix_reader.i32(matrix_offset + 8) & 1:
        row_y[2] = -row_y[2]
    row_x = [
        row_y[1] * row_z[2] - row_y[2] * row_z[1],
        row_y[2] * row_z[0] - row_y[0] * row_z[2],
        row_y[0] * row_z[1] - row_y[1] * row_z[0],
    ]

    return (
        tuple(row_x) + (0.0,),
        tuple(row_y) + (0.0,),
        tuple(row_z) + (0.0,),
        tuple(position) + (1.0,),
    )


def _transform(point: tuple[float, float, float], matrix: tuple) -> tuple[float, float, float]:
    x, y, z = point
    return tuple(
        x * matrix[0][axis]
        + y * matrix[1][axis]
        + z * matrix[2][axis]
        + matrix[3][axis]
        for axis in range(3)
    )


def _decode_mesh(record: tuple[int, ...], spk: dict[str, Chunk]) -> dict[str, object]:
    vertices = _read_float_array(spk["PVER"].main_data, record[6], record[10] * 3, "SPK vertices")
    face_data = spk["PFAC"].main_data
    face_reader = BinaryReader(face_data, "SPK indices")
    quad_offset = record[7] * 2
    tri_offset = record[8] * 2
    quad_count = record[11]
    tri_count = record[12]
    face_reader.require(quad_offset, quad_count * 8)
    face_reader.require(tri_offset, tri_count * 6)
    quads = (
        list(struct.unpack_from(f"<{quad_count * 4}H", face_data, quad_offset))
        if quad_count
        else []
    )
    triangles = (
        list(struct.unpack_from(f"<{tri_count * 3}H", face_data, tri_offset))
        if tri_count
        else []
    )

    ftx_offset = record[9]
    if ftx_offset & 0x80000000:
        dat_reader = BinaryReader(spk["PDAT"].main_data, "SPK object data")
        dat_offset = ftx_offset & 0x7FFFFFFF
        ftx_offset = dat_reader.u32(dat_offset)

    ftx_faces: list[tuple[int, ...]] = []
    uv_data: list[float] = []
    light_uv_data: list[float] = []
    if ftx_offset:
        ftx_reader = BinaryReader(spk["PFTX"].main_data, "SPK face texture records")
        ftx_start = ftx_offset - 1
        uv1_offset = ftx_reader.u32(ftx_start)
        uv2_offset = ftx_reader.u32(ftx_start + 4)
        ftx_count = ftx_reader.u32(ftx_start + 8)
        expected_faces = len(triangles) // 3 + len(quads) // 4
        if ftx_count != expected_faces:
            raise FormatError(
                f"SPK mesh: FTX face count {ftx_count} does not match {expected_faces}"
            )
        ftx_reader.require(ftx_start + 12, ftx_count * 12)
        ftx_faces = [
            struct.unpack_from("<6H", spk["PFTX"].main_data, ftx_start + 12 + index * 12)
            for index in range(ftx_count)
        ]
        textured_faces = sum(1 for face in ftx_faces if face[0] & 0x0030)
        lighted_faces = sum(1 for face in ftx_faces if face[0] & 0x00C0)
        uv_data = _read_float_array(
            spk["PUVC"].main_data,
            uv1_offset,
            textured_faces * 8,
            f"SPK UVs for mesh {tuple(record[index] for index in (6, 7, 8, 9, 10, 11, 12, 14))}",
            allow_non_finite=True,
        )
        light_uv_data = _read_float_array(
            spk["PUVC"].main_data,
            uv2_offset,
            lighted_faces * 8,
            f"SPK lightmap UVs for mesh {tuple(record[index] for index in (6, 7, 8, 9, 10, 11, 12, 14))}",
            allow_non_finite=True,
        )

    return {
        "vertices": [tuple(vertices[i : i + 3]) for i in range(0, len(vertices), 3)],
        "triangles": triangles,
        "quads": quads,
        "ftx_faces": ftx_faces,
        "uv_data": uv_data,
        "light_uv_data": light_uv_data,
        "invalid_uv_values": sum(
            not math.isfinite(value) for value in (*uv_data, *light_uv_data)
        ),
        "key": tuple(record[index] for index in (6, 7, 8, 9, 10, 11, 12, 14)),
    }


def read_scene(archive_path: Path, game_root: Path) -> dict[str, object]:
    with zipfile.ZipFile(archive_path) as archive:
        spk_chunk = parse_chunk(archive.read("Pack.SPK"))
        pal_pack = read_pack(archive, "PAL", game_root)
        dxt_pack = read_pack(archive, "DXT", game_root)
        lgt_pack = read_pack(archive, "LGT", game_root)
        wav_pack = read_pack(archive, "WAV", game_root)
        anm_pack = read_pack(archive, "ANM", game_root)
        dxt_formats: dict[str, int] = {}
        dxt_resolution_mismatches = 0
        if pal_pack is not None and dxt_pack is not None:
            if len(pal_pack.children) != len(dxt_pack.children):
                raise FormatError(
                    f"{archive_path}: PAL/DXT chunk count mismatch "
                    f"({len(pal_pack.children)} != {len(dxt_pack.children)})"
                )
            for pal, dxt in zip(pal_pack.children, dxt_pack.children):
                if len(pal.main_data) < 4 or len(dxt.main_data) < 4:
                    raise FormatError(f"{archive_path}: truncated paired PAL/DXT texture")
                if pal.main_data[:4] != dxt.main_data[:4]:
                    raise FormatError(f"{archive_path}: PAL/DXT texture IDs are not aligned")
                pal_texture = parse_texture(pal)
                dxt_texture = parse_texture(dxt)
                dxt_formats[dxt.name] = dxt_formats.get(dxt.name, 0) + 1
                if (pal_texture["width"], pal_texture["height"]) != (
                    dxt_texture["width"],
                    dxt_texture["height"],
                ):
                    dxt_resolution_mismatches += 1

        textures = extract_textures(pal_pack)
        light_textures = extract_textures(lgt_pack)
        audio_summary = summarize_audio(wav_pack)
        spk = {
            name: require_chunk(spk_chunk, name)
            for name in (
                "TORP",
                "PLCP",
                "AEHP",
                "MANP",
                "SOPP",
                "XTMP",
                "REVP",
                "CAFP",
                "XTFP",
                "CVUP",
                "TADP",
                "LBDP",
                "CXEP",
            )
        }
        spk.update(
            {
                "PROT": spk["TORP"],
                "PCLP": spk["PLCP"],
                "PHEA": spk["AEHP"],
                "PNAM": spk["MANP"],
                "PPOS": spk["SOPP"],
                "PMTX": spk["XTMP"],
                "PVER": spk["REVP"],
                "PFAC": spk["CAFP"],
                "PFTX": spk["XTFP"],
                "PUVC": spk["CVUP"],
                "PDAT": spk["TADP"],
                "PDBL": spk["LBDP"],
                "PEXC": spk["CXEP"],
            }
        )
        objects: list[dict[str, object]] = []
        mesh_cache: dict[tuple[int, ...], dict[str, object]] = {}
        pathfinders: list[dict[str, object]] = []
        pathfinder_blobs: list[tuple[str, bytes]] = []
        records_reader = BinaryReader(spk["PHEA"].main_data, "SPK object records")

        def walk(node: Chunk, parent_matrix: tuple, path: tuple[str, ...]) -> None:
            record_offset = node.tag & 0xFFFFFF
            records_reader.require(record_offset, 24)
            available_words = min(15, (len(spk["PHEA"].main_data) - record_offset) // 4)
            record = struct.unpack_from(
                f"<{available_words}I", spk["PHEA"].main_data, record_offset
            ) + (0,) * (15 - available_words)
            object_type = record[5] & 0xFFFF
            flags = record[5] >> 16
            name = read_c_string(spk["PNAM"].main_data, record[2], "SPK object names")
            if object_type == 111:
                pathfinder_data = _pathfinder_data(spk, record)
                pathfinder_blobs.append((name, pathfinder_data))
                pathfinders.append(
                    {
                        "name": name,
                        **parse_pathfinder(pathfinder_data),
                    }
                )
            local_matrix = _object_matrix(record, spk["PPOS"].main_data, spk["PMTX"].main_data)
            global_matrix = _matrix_multiply(local_matrix, parent_matrix)
            object_path = path + (name,)

            mesh = None
            if flags & 0x0020:
                records_reader.require(record_offset, 60)
                key = tuple(record[index] for index in (6, 7, 8, 9, 10, 11, 12, 14))
                mesh = mesh_cache.get(key)
                if mesh is None:
                    mesh = _decode_mesh(record, spk)
                    mesh_cache[key] = mesh
            objects.append(
                {
                    "name": name,
                    "path": object_path,
                    "type": object_type,
                    "flags": flags,
                    "color": record[13] if available_words > 13 else 0xFFFFFFFF,
                    "matrix": global_matrix,
                    "mesh": mesh,
                    "chunk_state": (node.tag >> 24) & 0xFF,
                }
            )
            for child in node.children:
                walk(child, global_matrix, object_path)

        identity = (
            (1.0, 0.0, 0.0, 0.0),
            (0.0, 1.0, 0.0, 0.0),
            (0.0, 0.0, 1.0, 0.0),
            (0.0, 0.0, 0.0, 1.0),
        )
        for root_name in ("PROT", "PCLP"):
            root_chunk = spk[root_name]
            for child in root_chunk.children:
                walk(child, identity, (root_name,))

        with zipfile.ZipFile(archive_path) as archive:
            file_entries = [
                entry
                for entry in archive.infolist()
                if not entry.is_dir() and Path(entry.filename).suffix.lower() == ".sdl"
            ]
        return {
            "archive": archive_path.name,
            "objects": objects,
            "meshes": list(mesh_cache.values()),
            "pathfinders": pathfinders,
            "pathfinder_blobs": pathfinder_blobs,
            "audio": audio_summary,
            "dxt_formats": dxt_formats,
            "dxt_resolution_mismatches": dxt_resolution_mismatches,
            "textures": textures,
            "light_textures": light_textures,
            "scripts": len(file_entries),
            "pack_sizes": {
                "SPK": spk_chunk.size,
                "PAL": pal_pack.size if pal_pack else 0,
                "DXT": dxt_pack.size if dxt_pack else 0,
                "LGT": lgt_pack.size if lgt_pack else 0,
                "WAV": wav_pack.size if wav_pack else 0,
                "ANM": anm_pack.size if anm_pack else 0,
            },
            "texture_chunk_counts": {
                "PAL": len(pal_pack.children) if pal_pack else 0,
                "LGT": len(lgt_pack.children) if lgt_pack else 0,
            },
        }


def _safe_name(text: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_.")
    return clean[:80] or "unnamed"


def _write_obj(scene: dict[str, object], output: Path) -> dict[str, int]:
    objects = scene["objects"]
    textures = scene["textures"]
    light_textures = scene["light_textures"]
    texture_table = {**light_textures, **textures}
    output.mkdir(parents=True, exist_ok=True)
    texture_dir = output / "textures"
    texture_dir.mkdir(parents=True, exist_ok=True)
    mtl_path = output / "C1_1.mtl"
    obj_path = output / "C1_1.obj"

    material_ids: set[int] = set()
    for item in objects:
        mesh = item["mesh"]
        if mesh is None:
            continue
        for face in mesh["ftx_faces"]:
            if face[0] & 0x0030:
                material_ids.add(face[2])

    image_ids: set[int] = set()
    for texture_id in sorted(material_ids):
        texture = texture_table.get(texture_id)
        if texture is None:
            raise FormatError(f"Referenced texture ID {texture_id} has no PAL/LGT image")
        rgba = texture_rgba(texture)
        width = int(texture["width"])
        height = int(texture["height"])
        image = Image.frombytes("RGBA", (width, height), rgba)
        image.save(texture_dir / f"texture_{texture_id}.png")
        image_ids.add(texture_id)

    for existing in texture_dir.glob("texture_*.png"):
        match = re.fullmatch(r"texture_(\d+)\.png", existing.name)
        if match and int(match.group(1)) not in image_ids:
            existing.unlink()

    with mtl_path.open("w", encoding="utf-8", newline="\n") as mtl:
        for texture_id in sorted(material_ids):
            texture = texture_table.get(texture_id)
            if texture is None:
                raise FormatError(f"Referenced texture ID {texture_id} has no PAL/LGT image")
            mtl.write(f"newmtl texture_{texture_id}\n")
            mtl.write("Kd 1.0 1.0 1.0\n")
            mtl.write(f"map_Kd textures/texture_{texture_id}.png\n\n")
        mtl.write("newmtl untextured\nKd 0.8 0.8 0.8\n\n")

    vertex_offset = 1
    uv_offset = 1
    triangle_count = 0
    quad_count = 0
    mesh_object_count = 0
    used_texture_ids: set[int] = set()
    sanitized_uv_values = 0
    with obj_path.open("w", encoding="utf-8", newline="\n") as obj:
        obj.write("# Generated locally from the user's own game installation.\n")
        obj.write(f"mtllib {mtl_path.name}\n")
        for object_index, item in enumerate(objects):
            mesh = item["mesh"]
            if mesh is None:
                continue
            mesh_object_count += 1
            obj.write(f"\no {_safe_name('/'.join(item['path']))}_{object_index}\n")

            vertices = mesh["vertices"]
            for vertex in vertices:
                point = _transform(vertex, item["matrix"])
                obj.write(f"v {point[0]:.9g} {point[1]:.9g} {point[2]:.9g}\n")

            triangles = mesh["triangles"]
            quads = mesh["quads"]
            ftx_faces = mesh["ftx_faces"]
            uv_data = mesh["uv_data"]
            face_index = 0
            uv_cursor = 0
            for indices, shape in ((triangles, 3), (quads, 4)):
                for start in range(0, len(indices), shape):
                    face = ftx_faces[face_index] if ftx_faces else (0, 0, 0, 0, 0, 0)
                    face_index += 1
                    textured = bool(face[0] & 0x0030)
                    if textured:
                        texture_id = face[2]
                        if texture_id not in image_ids:
                            raise FormatError(
                                f"Mesh {item['name']!r} references missing texture ID {texture_id}"
                            )
                        used_texture_ids.add(texture_id)
                        obj.write(f"usemtl texture_{texture_id}\n")
                    else:
                        obj.write("usemtl untextured\n")

                    face_uv_indices: list[int] = []
                    if textured:
                        for corner in range(shape):
                            u = uv_data[uv_cursor + corner * 2]
                            v = uv_data[uv_cursor + corner * 2 + 1]
                            if not math.isfinite(u):
                                u = 0.0
                                sanitized_uv_values += 1
                            if not math.isfinite(v):
                                v = 0.0
                                sanitized_uv_values += 1
                            obj.write(f"vt {u:.9g} {v:.9g}\n")
                            face_uv_indices.append(uv_offset)
                            uv_offset += 1
                        uv_cursor += 8

                    face_vertices: list[int] = []
                    for encoded_index in indices[start : start + shape]:
                        vertex_index = encoded_index >> 1
                        if vertex_index >= len(vertices):
                            raise FormatError(
                                f"Mesh {item['name']!r} index {encoded_index:#06x} "
                                f"selects missing vertex {vertex_index}"
                            )
                        face_vertices.append(vertex_offset + vertex_index)

                    fan_triangles = [(0, 1, 2)] if shape == 3 else [(0, 1, 2), (0, 2, 3)]
                    for a, b, c in fan_triangles:
                        refs = []
                        for corner in (a, b, c):
                            if textured:
                                refs.append(f"{face_vertices[corner]}/{face_uv_indices[corner]}")
                            else:
                                refs.append(str(face_vertices[corner]))
                        obj.write(f"f {' '.join(refs)}\n")
                    triangle_count += len(fan_triangles)
                    if shape == 4:
                        quad_count += 1
            vertex_offset += len(vertices)

        if not mesh_object_count:
            raise FormatError("Scene contains no renderable mesh objects")
        if len(used_texture_ids) != len(material_ids):
            material_ids.intersection_update(used_texture_ids)

    return {
        "mesh_objects": mesh_object_count,
        "unique_meshes": len(scene["meshes"]),
        "vertices_per_instance": sum(len(mesh["vertices"]) for mesh in scene["meshes"]),
        "unique_triangles": sum(
            len(mesh["triangles"]) // 3 + 2 * (len(mesh["quads"]) // 4)
            for mesh in scene["meshes"]
        ),
        "triangles_in_obj": triangle_count,
        "quads_in_obj": quad_count,
        "textures_in_obj": len(used_texture_ids),
        "images_written": len(image_ids),
        "invalid_uv_values_sanitized": sanitized_uv_values,
    }


def _mission_archives(game_root: Path) -> list[Path]:
    pattern = re.compile(r"^C\d+_\d+\.zip$", re.IGNORECASE)
    return sorted(
        (path for path in game_root.rglob("*.zip") if pattern.match(path.name)),
        key=lambda path: str(path).lower(),
    )


def build_budget(game_root: Path, output_path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for archive_path in _mission_archives(game_root):
        scene = read_scene(archive_path, game_root)
        objects = scene["objects"]
        meshes = scene["meshes"]
        vertices = sum(len(mesh["vertices"]) for mesh in meshes)
        triangles = sum(
            len(mesh["triangles"]) // 3 + 2 * (len(mesh["quads"]) // 4) for mesh in meshes
        )
        rows.append(
            {
                "mission": archive_path.stem,
                "archive": str(archive_path.relative_to(game_root)),
                "scripts": scene["scripts"],
                "objects": len(objects),
                "mesh_objects": sum(1 for item in objects if item["mesh"] is not None),
                "unique_meshes": len(meshes),
                "unique_vertices": vertices,
                "unique_triangles": triangles,
                "scene_triangles": sum(
                    len(item["mesh"]["triangles"]) // 3
                    + 2 * (len(item["mesh"]["quads"]) // 4)
                    for item in objects
                    if item["mesh"] is not None
                ),
                "pal_textures": scene["texture_chunk_counts"]["PAL"],
                "light_textures": scene["texture_chunk_counts"]["LGT"],
                "invalid_uv_values": sum(
                    mesh["invalid_uv_values"] for mesh in meshes
                ),
                "texture_rgba_bytes": sum(
                    int(texture["width"]) * int(texture["height"]) * 4
                    for texture in (*scene["textures"].values(), *scene["light_textures"].values())
                ),
                "spk_bytes": scene["pack_sizes"]["SPK"],
                "pal_bytes": scene["pack_sizes"]["PAL"],
                "dxt_bytes": scene["pack_sizes"]["DXT"],
                "lgt_bytes": scene["pack_sizes"]["LGT"],
                "wav_bytes": scene["pack_sizes"]["WAV"],
                "anm_bytes": scene["pack_sizes"]["ANM"],
                "dxt1_textures": scene["dxt_formats"].get("DXT1", 0),
                "dxt3_textures": scene["dxt_formats"].get("DXT3", 0),
                "dxt_other_textures": sum(
                    count
                    for format_name, count in scene["dxt_formats"].items()
                    if format_name not in ("DXT1", "DXT3")
                ),
                "dxt_resolution_mismatches": scene["dxt_resolution_mismatches"],
                "wav_chunks": scene["audio"]["chunks"],
                "wav_ima_adpcm_chunks": scene["audio"]["formats"].get(0x0011, 0),
                "wav_sample_rates_hz": ";".join(
                    str(rate) for rate in scene["audio"]["sample_rates"]
                ),
                "wav_audio_data_bytes": scene["audio"]["audio_data_bytes"],
                "wav_duration_seconds": round(scene["audio"]["duration_seconds"], 3),
                "pathfinder_objects": len(scene["pathfinders"]),
                "pathfinder_rooms": sum(
                    pathfinder["room_count"] for pathfinder in scene["pathfinders"]
                ),
                "pathfinder_nodes": sum(
                    pathfinder["nodes"] for pathfinder in scene["pathfinders"]
                ),
                "pathfinder_leaf_nodes": sum(
                    pathfinder["leaf_nodes"] for pathfinder in scene["pathfinders"]
                ),
            }
        )

    if not rows:
        raise FormatError(f"No mission archives named C<number>_<number>.zip under {game_root}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return rows


def export_mission(archive_path: Path, game_root: Path, output_dir: Path) -> dict[str, object]:
    scene = read_scene(archive_path, game_root)
    stats = _write_obj(scene, output_dir)
    stats.update(
        {
            "mission": archive_path.stem,
            "archive": str(archive_path),
            "objects": len(scene["objects"]),
            "scripts": scene["scripts"],
            "invalid_uv_values": sum(
                mesh["invalid_uv_values"] for mesh in scene["meshes"]
            ),
            "pal_textures": len(scene["textures"]),
            "light_textures": len(scene["light_textures"]),
            "pathfinders": scene["pathfinders"],
            "audio": scene["audio"],
            "dxt_formats": scene["dxt_formats"],
            "dxt_resolution_mismatches": scene["dxt_resolution_mismatches"],
            "pack_sizes": scene["pack_sizes"],
            "texture_dimensions": [
                {
                    "id": int(texture["id"]),
                    "name": str(texture["name"]),
                    "width": int(texture["width"]),
                    "height": int(texture["height"]),
                    "format": str(texture["format"]),
                    "mip_count": int(texture["mip_count"]),
                }
                for texture in sorted(
                    (*scene["textures"].values(), *scene["light_textures"].values()),
                    key=lambda texture: int(texture["id"]),
                )
            ],
        }
    )
    (output_dir / "scene-stats.json").write_text(
        json.dumps(stats, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="export one mission as OBJ, MTL and PNG")
    export_parser.add_argument("archive", type=Path, help="mission ZIP archive")
    export_parser.add_argument("--game-root", type=Path, required=True, help="game install root")
    export_parser.add_argument("--output", type=Path, required=True, help="output directory")

    budget_parser = subparsers.add_parser("budget", help="measure all C<number>_<number> missions")
    budget_parser.add_argument("--game-root", type=Path, required=True, help="game install root")
    budget_parser.add_argument("--output", type=Path, required=True, help="output CSV path")

    texture_parser = subparsers.add_parser(
        "texture-audit", help="compare decoded DXT images with PAL reference textures"
    )
    texture_parser.add_argument("archive", type=Path, help="mission ZIP archive")
    texture_parser.add_argument("--game-root", type=Path, required=True, help="game install root")

    arguments = parser.parse_args()
    try:
        if arguments.command == "export":
            stats = export_mission(
                arguments.archive.resolve(),
                arguments.game_root.resolve(),
                arguments.output.resolve(),
            )
            if stats["invalid_uv_values_sanitized"]:
                print(
                    f"warning: replaced {stats['invalid_uv_values_sanitized']} "
                    "non-finite texture-coordinate values with 0 in the OBJ export",
                    file=sys.stderr,
                )
            print(json.dumps(stats, indent=2, ensure_ascii=False))
        elif arguments.command == "texture-audit":
            result = audit_dxt(arguments.archive.resolve(), arguments.game_root.resolve())
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            rows = build_budget(arguments.game_root.resolve(), arguments.output.resolve())
            print(f"Measured {len(rows)} missions; report: {arguments.output.resolve()}")
            for row in rows:
                print(
                    f"{row['mission']}: {row['objects']} objects, "
                    f"{row['unique_triangles']} triangles, "
                    f"{row['pal_textures']} textures, {row['scripts']} scripts"
                )
    except (FormatError, OSError, zipfile.BadZipFile) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
