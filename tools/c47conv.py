"""Convert Hitman: Codename 47 scene archives into a New 3DS data package."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib
import sys
from pathlib import Path
from typing import Iterable

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    from PIL import Image
except ImportError:
    Image = None

from tools.c47_phase1 import (
    FormatError,
    _mission_archives,
    _transform,
    decode_ima_adpcm,
    parse_wave,
    read_scene,
    texture_rgba,
)


VERSION = 1
NO_MODEL = 0xFFFFFFFF
NO_TEXTURE = 0xFFFFFFFF
CELL_SIZE = 4096.0


class ConversionCancelled(Exception):
    """Raised when conversion is interrupted by the user."""
    pass
SCENE_HEADER = struct.Struct("<4sHHIIIIIfIIIIII")
SCENE_OBJECT = struct.Struct("<8I16f6f")
SCENE_CELL = struct.Struct("<ii6fII")
MODEL_HEADER = struct.Struct("<4sHHIIII14f")
MODEL_BATCH = struct.Struct("<6I")
MODEL_VERTEX = struct.Struct("<3h4H4B")
SCRIPT_HEADER = struct.Struct("<4sHHIIIII")
INDEX_HEADER = struct.Struct("<4sHHIII")
MISSION_RECORD = struct.Struct("<9I")
TEXTURE_INDEX_HEADER = struct.Struct("<4sHHIII")
TEXTURE_RECORD = struct.Struct("<9I")
COLLISION_HEADER = struct.Struct("<4sHHII")
COLLISION_RECORD = struct.Struct("<II16f6f")
AUDIO_HEADER = struct.Struct("<4sHHII")
AUDIO_RECORD = struct.Struct("<5I")
NAV_HEADER = struct.Struct("<4sHHIIIII")
NAV_RECORD = struct.Struct("<4I")


def _string_table(values: Iterable[str]) -> tuple[bytes, dict[str, int]]:
    table = bytearray()
    offsets: dict[str, int] = {}
    for value in values:
        if value not in offsets:
            offsets[value] = len(table)
            table.extend(value.encode("utf-8") + b"\0")
    return bytes(table), offsets


def _mission_archive(game_root: Path, mission: str) -> Path:
    matches = [
        path
        for path in _mission_archives(game_root)
        if path.stem.casefold() == mission.casefold()
    ]
    if not matches:
        raise FormatError(f"No primary mission archive named {mission!r} under {game_root}")
    if len(matches) > 1:
        raise FormatError(f"Mission name {mission!r} is ambiguous under {game_root}")
    return matches[0]


def _texture_dimensions(width: int, height: int) -> tuple[int, int]:
    def nearest_power_of_two(value: int) -> int:
        value = min(1024, max(8, value))
        lower = 1 << (value.bit_length() - 1)
        upper = min(1024, lower * 2)
        return lower if value - lower <= upper - value else upper

    return nearest_power_of_two(width), nearest_power_of_two(height)


def _texture_encoding(rgba: bytes) -> tuple[str, int]:
    alpha_values = set(rgba[3::4])
    if alpha_values == {255}:
        return "rgb565", 1
    if alpha_values.issubset({0, 255}):
        return "rgba5551", 2
    return "rgba4", 3


def _convert_textures(
    output: Path,
    mission_index: int,
    textures: dict[tuple[int, int], dict[str, object]],
    tex3ds: Path,
    work: Path,
) -> tuple[list[dict[str, object]], int]:
    tex_dir = output / "tex"
    tex_dir.mkdir(parents=True, exist_ok=True)
    texture_records: list[dict[str, object]] = []
    deduplicated: dict[str, tuple[str, int, int, int]] = {}
    total_vram = 0

    for (texture_kind, texture_id), texture in sorted(textures.items()):
        width = int(texture["width"])
        height = int(texture["height"])
        source_encoding = {"PALN": 1, "RGBA": 2}.get(str(texture["format"]))
        if source_encoding is None:
            raise FormatError(
                f"Unsupported source texture format {texture['format']!r} "
                f"for texture {texture_kind}:{texture_id}"
            )
        rgba = texture_rgba(texture)
        if len(rgba) != width * height * 4:
            raise FormatError(
                f"Texture {texture_kind}:{texture_id} has {len(rgba)} RGBA bytes, "
                f"expected {width * height * 4}"
            )
        if set(rgba[3::4]) == {0}:
            # Alpha a 0 partout : c'est l'octet de bourrage de la palette, pas de
            # la transparence. Sinon la texture serait invisible en jeu.
            opaque = bytearray(rgba)
            opaque[3::4] = b"\xff" * (len(opaque) // 4)
            rgba = bytes(opaque)
        target_width, target_height = _texture_dimensions(width, height)
        image = Image.frombytes("RGBA", (width, height), rgba)
        if image.size != (target_width, target_height):
            image = image.resize(
                (target_width, target_height),
                Image.Resampling.LANCZOS,
            )
        resized_rgba = image.tobytes()
        encoding, encoding_id = _texture_encoding(resized_rgba)
        digest = hashlib.sha256(
            struct.pack("<HHB", target_width, target_height, encoding_id)
            + resized_rgba
        ).hexdigest()[:24]
        asset_path = f"{digest}.t3x"
        if digest not in deduplicated:
            png_path = work / f"{digest}.png"
            recipe_path = work / f"{digest}.t3s"
            output_path = tex_dir / asset_path
            image.save(png_path, format="PNG")
            recipe_path.write_text(
                f"-f {encoding} -z auto\n{png_path.name}\n",
                encoding="utf-8",
                newline="\n",
            )
            result = subprocess.run(
                [
                    str(tex3ds),
                    "-i",
                    recipe_path.name,
                    "-o",
                    str(output_path),
                ],
                cwd=work,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0 or not output_path.is_file():
                raise FormatError(
                    f"tex3ds failed for texture {texture_kind}:{texture_id}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
            deduplicated[digest] = (
                asset_path,
                target_width,
                target_height,
                encoding_id,
            )
            total_vram += target_width * target_height * 2
        else:
            asset_path, target_width, target_height, encoding_id = deduplicated[digest]

        texture_records.append(
            {
                "mission_index": mission_index,
                "texture_id": texture_id,
                "texture_kind": texture_kind,
                "width": target_width,
                "height": target_height,
                "encoding": encoding_id,
                "source_format": 1 if texture_kind == 1 else 2,
                "source_encoding": source_encoding,
                "asset_path": asset_path,
                "name": str(texture["name"]),
            }
        )

    return texture_records, total_vram


def _quantize_signed(value: float, origin: float, scale: float) -> int:
    if scale == 0.0:
        return 0
    return max(-32767, min(32767, round((value - origin) / scale)))


def _quantize_unsigned(value: float, origin: float, scale: float) -> int:
    if scale == 0.0:
        return 0
    return max(0, min(65535, round((value - origin) / scale)))


def _mesh_binary(mesh: dict[str, object]) -> tuple[bytes, dict[str, object]]:
    vertices = mesh["vertices"]
    triangles = mesh["triangles"]
    quads = mesh["quads"]
    faces = mesh["ftx_faces"]
    uv_data = mesh["uv_data"]
    light_uv_data = mesh.get("light_uv_data", [])
    if not vertices or len(vertices) > 65535:
        raise FormatError(f"Mesh has unsupported vertex count {len(vertices)}")

    # Seuls les sommets references par une face sont ecrits dans le modele.
    # Les sommets inutilises (souvent des valeurs aberrantes) ne doivent pas
    # agrandir la boite : la quantification 16 bits ecraserait alors tout le
    # vrai modele (un personnage devient une "boule") et les boites de
    # collision seraient enormes.
    used_vertices = {
        encoded_index >> 1
        for encoded_index in (*triangles, *quads)
        if (encoded_index >> 1) < len(vertices)
    }
    bound_vertices = (
        [vertices[index] for index in used_vertices] if used_vertices else vertices
    )
    mins = [min(vertex[axis] for vertex in bound_vertices) for axis in range(3)]
    maxs = [max(vertex[axis] for vertex in bound_vertices) for axis in range(3)]
    position_origin = [(mins[axis] + maxs[axis]) / 2 for axis in range(3)]
    position_scale = [
        (maxs[axis] - mins[axis]) / 65534 if maxs[axis] != mins[axis] else 1.0
        for axis in range(3)
    ]

    grouped_faces: dict[
        tuple[int, int, int],
        list[
            tuple[
                tuple[int, ...],
                tuple[tuple[float, float], ...],
                tuple[tuple[float, float], ...],
            ]
        ],
    ] = {}
    uv_cursor = 0
    light_uv_cursor = 0
    face_index = 0
    sanitized_uv_values = 0
    for indices, shape in ((triangles, 3), (quads, 4)):
        for start in range(0, len(indices), shape):
            face = faces[face_index] if faces else (0, 0, 0, 0, 0, 0)
            face_index += 1
            textured = bool(face[0] & 0x0030)
            texture_id = face[2] if textured else NO_TEXTURE
            lighted = bool(face[0] & 0x00C0)
            light_texture_id = (
                face[3] if lighted and face[3] != 0xFFFF else NO_TEXTURE
            )

            def read_uvs(
                values: list[float],
                cursor: int,
                enabled: bool,
            ) -> tuple[tuple[tuple[float, float], ...], int]:
                nonlocal sanitized_uv_values
                if not enabled:
                    return ((0.0, 0.0),) * shape, cursor
                if cursor + shape * 2 > len(values):
                    raise FormatError("Mesh UV buffer is shorter than its face flags")
                face_uvs = []
                for corner in range(shape):
                    u = values[cursor + corner * 2]
                    v = values[cursor + corner * 2 + 1]
                    if not math.isfinite(u) or abs(u) > 1024.0:
                        u = 0.0
                        sanitized_uv_values += 1
                    if not math.isfinite(v) or abs(v) > 1024.0:
                        v = 0.0
                        sanitized_uv_values += 1
                    face_uvs.append((u, v))
                return tuple(face_uvs), cursor + 8

            face_uvs, uv_cursor = read_uvs(uv_data, uv_cursor, textured)
            face_light_uvs, light_uv_cursor = read_uvs(
                light_uv_data,
                light_uv_cursor,
                lighted,
            )
            vertex_indices = tuple(indices[start : start + shape])
            if len(vertex_indices) != shape:
                raise FormatError("Mesh contains an incomplete face")
            for encoded_index in vertex_indices:
                if encoded_index >> 1 >= len(vertices):
                    raise FormatError(f"Mesh index {encoded_index:#06x} is out of range")
            grouped_faces.setdefault(
                (texture_id, light_texture_id, face[0]),
                [],
            ).append((vertex_indices, face_uvs, face_light_uvs))

    all_uvs = [
        uv
        for grouped in grouped_faces.values()
        for _, face_uvs, _ in grouped
        for uv in face_uvs
    ]
    all_light_uvs = [
        uv
        for grouped in grouped_faces.values()
        for _, _, face_uvs in grouped
        for uv in face_uvs
    ]
    if not all_uvs:
        all_uvs = [(0.0, 0.0)]
    if not all_light_uvs:
        all_light_uvs = [(0.0, 0.0)]
    uv_min = [min(uv[axis] for uv in all_uvs) for axis in range(2)]
    uv_max = [max(uv[axis] for uv in all_uvs) for axis in range(2)]
    light_uv_min = [
        min(uv[axis] for uv in all_light_uvs) for axis in range(2)
    ]
    light_uv_max = [
        max(uv[axis] for uv in all_light_uvs) for axis in range(2)
    ]
    # Les UV sont quantifiees en NON signe (0..65535) : l'origine doit etre le
    # minimum, sinon toutes les valeurs sous le centre sont ecrasees a 0.
    uv_origin = [uv_min[axis] for axis in range(2)]
    uv_scale = [
        (uv_max[axis] - uv_min[axis]) / 65535 if uv_max[axis] != uv_min[axis] else 1.0
        for axis in range(2)
    ]
    light_uv_origin = [light_uv_min[axis] for axis in range(2)]
    light_uv_scale = [
        (light_uv_max[axis] - light_uv_min[axis]) / 65535
        if light_uv_max[axis] != light_uv_min[axis]
        else 1.0
        for axis in range(2)
    ]

    encoded_vertices: list[tuple[int, ...]] = []
    encoded_indices: list[int] = []
    batches: list[tuple[int, int, int, int, int, int]] = []
    triangle_count = 0

    def start_batch() -> tuple[
        dict[tuple[int, int, int], int], list[tuple[int, ...]], list[int]
    ]:
        return {}, [], []

    for (texture_id, light_texture_id, face_flags), grouped in grouped_faces.items():
        vertex_lookup, batch_vertices, batch_indices = start_batch()

        def finish_batch() -> None:
            if not batch_indices:
                return
            first_index = len(encoded_indices)
            first_vertex = len(encoded_vertices)
            encoded_vertices.extend(batch_vertices)
            encoded_indices.extend(index + first_vertex for index in batch_indices)
            batches.append(
                (
                    texture_id,
                    light_texture_id,
                    first_index,
                    len(batch_indices),
                    first_vertex,
                    face_flags,
                )
            )

        for source_indices, face_uvs, face_light_uvs in grouped:
            face_corners = [
                (encoded_index >> 1, uv, light_uv)
                for encoded_index, uv, light_uv in zip(
                    source_indices,
                    face_uvs,
                    face_light_uvs,
                )
            ]
            missing = sum(
                1
                for vertex_index, uv, light_uv in face_corners
                if (
                    vertex_index,
                    *_quantized_uv(uv, uv_origin, uv_scale),
                    *_quantized_uv(light_uv, light_uv_origin, light_uv_scale),
                )
                not in vertex_lookup
            )
            if len(vertex_lookup) + missing > 65535:
                finish_batch()
                vertex_lookup, batch_vertices, batch_indices = start_batch()

            local_indices = []
            for vertex_index, uv, light_uv in face_corners:
                uv_index = _quantized_uv(uv, uv_origin, uv_scale)
                light_uv_index = _quantized_uv(
                    light_uv,
                    light_uv_origin,
                    light_uv_scale,
                )
                key = (vertex_index, *uv_index, *light_uv_index)
                local_index = vertex_lookup.get(key)
                if local_index is None:
                    point = vertices[vertex_index]
                    local_index = len(batch_vertices)
                    vertex_lookup[key] = local_index
                    batch_vertices.append(
                        (
                            *(
                                _quantize_signed(
                                    point[axis],
                                    position_origin[axis],
                                    position_scale[axis],
                                )
                                for axis in range(3)
                            ),
                            *uv_index,
                            *light_uv_index,
                            255,
                            255,
                            255,
                            255,
                        )
                    )
                local_indices.append(local_index)

            if len(local_indices) == 3:
                batch_indices.extend(local_indices)
                triangle_count += 1
            else:
                batch_indices.extend(
                    (
                        local_indices[0],
                        local_indices[1],
                        local_indices[2],
                        local_indices[0],
                        local_indices[2],
                        local_indices[3],
                    )
                )
                triangle_count += 2
        finish_batch()

    header = MODEL_HEADER.pack(
        b"C47M",
        VERSION,
        MODEL_HEADER.size,
        len(encoded_vertices),
        len(encoded_indices),
        len(batches),
        sanitized_uv_values,
        *position_origin,
        *position_scale,
        *uv_origin,
        *uv_scale,
        *light_uv_origin,
        *light_uv_scale,
    )
    batch_data = b"".join(MODEL_BATCH.pack(*batch) for batch in batches)
    vertex_data = b"".join(MODEL_VERTEX.pack(*vertex) for vertex in encoded_vertices)
    index_data = struct.pack(f"<{len(encoded_indices)}H", *encoded_indices)
    model_data = header + batch_data + vertex_data + index_data
    return model_data, {
        "vertices": len(encoded_vertices),
        "indices": len(encoded_indices),
        "triangles": triangle_count,
        "batches": len(batches),
        "bytes": len(model_data),
        "sanitized_uv_values": sanitized_uv_values,
        "bounds": (*mins, *maxs),
    }


def _quantized_uv(
    uv: tuple[float, float],
    origin: list[float],
    scale: list[float],
) -> tuple[int, int]:
    return (
        _quantize_unsigned(uv[0], origin[0], scale[0]),
        _quantize_unsigned(uv[1], origin[1], scale[1]),
    )


def _world_bounds(
    matrix: tuple,
    mesh_info: dict[str, object] | None,
) -> tuple[float, float, float, float, float, float]:
    if mesh_info is None:
        position = matrix[3][:3]
        return (*position, *position)
    bounds = mesh_info["bounds"]
    local_min = bounds[:3]
    local_max = bounds[3:]
    points = [
        _transform(
            (x, y, z),
            matrix,
        )
        for x in (local_min[0], local_max[0])
        for y in (local_min[1], local_max[1])
        for z in (local_min[2], local_max[2])
    ]
    return tuple(
        min(point[axis] for point in points) for axis in range(3)
    ) + tuple(max(point[axis] for point in points) for axis in range(3))


def _write_scene(
    path: Path,
    scene: dict[str, object],
    model_ids: dict[tuple[int, ...], int],
    model_info: list[dict[str, object]],
) -> dict[str, int]:
    objects = scene["objects"]
    cell_objects: dict[tuple[int, int], list[int]] = {}
    cell_bounds: dict[tuple[int, int], list[float]] = {}
    object_rows = []
    clip_object_count = 0
    collision_rows = []

    for object_id, item in enumerate(objects):
        mesh = item["mesh"]
        model_id = NO_MODEL
        mesh_metadata = None
        if mesh is not None:
            mesh_key = tuple(mesh["key"])
            if mesh_key in model_ids:
                model_id = model_ids[mesh_key]
                mesh_metadata = model_info[model_id]

        matrix = item["matrix"]
        bounds = _world_bounds(matrix, mesh_metadata)
        cell_x = math.floor(((bounds[0] + bounds[3]) / 2) / CELL_SIZE)
        cell_z = math.floor(((bounds[2] + bounds[5]) / 2) / CELL_SIZE)
        cell_key = (cell_x, cell_z)
        min_cell_x = math.floor(bounds[0] / CELL_SIZE)
        max_cell_x = math.floor(bounds[3] / CELL_SIZE)
        min_cell_z = math.floor(bounds[2] / CELL_SIZE)
        max_cell_z = math.floor(bounds[5] / CELL_SIZE)
        cell_span = (max_cell_x - min_cell_x + 1) * (max_cell_z - min_cell_z + 1)
        if cell_span > 4096:
            raise FormatError(
                f"Object {item['name']!r} spans {cell_span} visibility cells"
            )
        for object_cell_x in range(min_cell_x, max_cell_x + 1):
            for object_cell_z in range(min_cell_z, max_cell_z + 1):
                object_cell = (object_cell_x, object_cell_z)
                cell_objects.setdefault(object_cell, []).append(object_id)
                current_bounds = cell_bounds.setdefault(
                    object_cell, [math.inf] * 3 + [-math.inf] * 3
                )
                for axis in range(3):
                    current_bounds[axis] = min(current_bounds[axis], bounds[axis])
                    current_bounds[axis + 3] = max(
                        current_bounds[axis + 3], bounds[axis + 3]
                    )

        is_clip = bool(item["path"] and item["path"][0] == "PCLP")
        clip_object_count += int(is_clip)
        scene_flags = int(is_clip)
        object_rows.append(
            {
                "id": object_id,
                "type": int(item["type"]),
                "source_flags": int(item["flags"]),
                "model_id": model_id,
                "cell": cell_key,
                "name": "/".join(item["path"]),
                "scene_flags": scene_flags,
                "color": int(item.get("color", 0xFFFFFFFF)),
                "matrix": matrix,
                "bounds": bounds,
            }
        )
        if model_id != NO_MODEL and not is_clip:
            collision_rows.append((object_id, model_id, matrix, bounds))

    ordered_cells = sorted(cell_objects, key=lambda key: (key[1], key[0]))
    cell_indices = {key: index for index, key in enumerate(ordered_cells)}
    strings, string_offsets = _string_table(item["name"] for item in object_rows)
    cell_refs: list[int] = []
    cell_records = []
    for key in ordered_cells:
        objects_in_cell = cell_objects[key]
        first_ref = len(cell_refs)
        cell_refs.extend(objects_in_cell)
        cell_records.append(
            SCENE_CELL.pack(
                key[0],
                key[1],
                *cell_bounds[key],
                first_ref,
                len(objects_in_cell),
            )
        )

    cell_table = b"".join(cell_records)
    ref_table = struct.pack(f"<{len(cell_refs)}I", *cell_refs)
    object_offset = SCENE_HEADER.size
    cell_offset = object_offset + len(object_rows) * SCENE_OBJECT.size
    ref_offset = cell_offset + len(cell_table)
    string_offset = ref_offset + len(ref_table)
    object_data = b"".join(
        SCENE_OBJECT.pack(
            item["id"],
            item["type"],
            item["source_flags"],
            item["model_id"],
            cell_indices[item["cell"]],
            string_offsets[item["name"]],
            item["scene_flags"],
            item["color"],
            *(value for row in item["matrix"] for value in row),
            *item["bounds"],
        )
        for item in object_rows
    )
    header = SCENE_HEADER.pack(
        b"C47S",
        VERSION,
        SCENE_HEADER.size,
        len(object_rows),
        len(cell_records),
        len(model_info),
        len(collision_rows),
        0,
        CELL_SIZE,
        object_offset,
        cell_offset,
        ref_offset,
        string_offset,
        len(strings),
        0,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + object_data + cell_table + ref_table + strings)
    return {
        "objects": len(object_rows),
        "cells": len(cell_records),
        "cell_references": len(cell_refs),
        "clip_objects": clip_object_count,
        "collision_instances": len(collision_rows),
        "bytes": path.stat().st_size,
        "collision_rows": collision_rows,
    }


def _write_collision(path: Path, rows: list[tuple]) -> int:
    records = b"".join(
        COLLISION_RECORD.pack(
            object_id,
            model_id,
            *(value for row in matrix for value in row),
            *bounds,
        )
        for object_id, model_id, matrix, bounds in rows
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        COLLISION_HEADER.pack(
            b"C47C",
            VERSION,
            COLLISION_RECORD.size,
            len(rows),
            zlib.crc32(records),
        )
        + records
    )
    return path.stat().st_size


def _write_blob_index(
    path: Path,
    signature: bytes,
    records: list[tuple[str, bytes]],
    header: struct.Struct = SCRIPT_HEADER,
) -> int:
    names, offsets = _string_table(name for name, _ in records)
    record_size = 16
    records_offset = header.size
    strings_offset = records_offset + len(records) * record_size
    payload_offset = strings_offset + len(names)
    payloads = bytearray()
    record_data = bytearray()
    for name, payload in records:
        blob_offset = payload_offset + len(payloads)
        record_data.extend(
            struct.pack(
                "<IIII",
                offsets[name],
                blob_offset,
                len(payload),
                zlib.crc32(payload),
            )
        )
        payloads.extend(payload)
    file_header = header.pack(
        signature,
        VERSION,
        header.size,
        len(records),
        records_offset,
        strings_offset,
        len(names),
        payload_offset,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(file_header + record_data + names + payloads)
    return path.stat().st_size


def _write_navigation(path: Path, blobs: list[tuple[str, bytes]]) -> int:
    names, offsets = _string_table(name for name, _ in blobs)
    records_offset = NAV_HEADER.size
    strings_offset = records_offset + len(blobs) * NAV_RECORD.size
    payload_offset = strings_offset + len(names)
    payloads = bytearray()
    records = bytearray()
    for name, data in blobs:
        records.extend(
            NAV_RECORD.pack(
                offsets[name],
                payload_offset + len(payloads),
                len(data),
                zlib.crc32(data),
            )
        )
        payloads.extend(data)
    header = NAV_HEADER.pack(
        b"C47N",
        VERSION,
        NAV_HEADER.size,
        len(blobs),
        records_offset,
        strings_offset,
        len(names),
        payload_offset,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + records + names + payloads)
    return path.stat().st_size


def _write_audio(
    output: Path,
    mission: str,
    archive_path: Path,
    game_root: Path,
) -> tuple[int, int, int]:
    with zipfile.ZipFile(archive_path) as archive:
        from tools.c47_phase1 import read_pack

        pack = read_pack(archive, "WAV", game_root)
    if pack is None:
        pcm_data = b""
        tracks: list[tuple[int, int, int, int, int]] = []
    else:
        pcm = bytearray()
        tracks = []
        for chunk in pack.children:
            info = parse_wave(chunk.main_data)
            decoded = decode_ima_adpcm(chunk.main_data)
            if len(decoded) != int(info["sample_count"]) * 2:
                raise FormatError(f"{mission}: decoded audio sample count mismatch")
            tracks.append(
                (
                    int(info["sample_rate"]),
                    int(info["sample_count"]),
                    len(pcm),
                    len(decoded),
                    zlib.crc32(decoded),
                )
            )
            pcm.extend(decoded)
            while len(pcm) % 4:
                pcm.append(0)
        pcm_data = bytes(pcm)

    audio_dir = output / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    pcm_path = audio_dir / f"{mission}.pcm"
    index_path = audio_dir / f"{mission}.c47a"
    pcm_path.write_bytes(pcm_data)
    index_records = b"".join(AUDIO_RECORD.pack(*track) for track in tracks)
    index_path.write_bytes(
        AUDIO_HEADER.pack(
            b"C47A",
            VERSION,
            1,
            len(tracks),
            AUDIO_RECORD.size,
        )
        + index_records
    )
    return len(pcm_data), index_path.stat().st_size, len(tracks)


def _write_texture_index(path: Path, records: list[dict[str, object]]) -> int:
    names, offsets = _string_table(
        value
        for record in records
        for value in (str(record["asset_path"]), str(record["name"]))
    )
    strings_offset = TEXTURE_INDEX_HEADER.size + len(records) * TEXTURE_RECORD.size
    row_data = b"".join(
        TEXTURE_RECORD.pack(
            int(record["mission_index"]),
            int(record["texture_id"]),
            int(record["width"]),
            int(record["height"]),
            int(record["encoding"]),
            int(record["source_format"]),
            offsets[str(record["asset_path"])],
            offsets[str(record["name"])],
            int(record["source_encoding"]),
        )
        for record in records
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        TEXTURE_INDEX_HEADER.pack(
            b"C47T",
            VERSION,
            0,
            len(records),
            strings_offset,
            len(names),
        )
        + row_data
        + names
    )
    return path.stat().st_size


def _write_package_index(
    path: Path,
    missions: list[dict[str, object]],
    texture_count: int,
) -> int:
    names, offsets = _string_table(str(mission["name"]) for mission in missions)
    strings_offset = INDEX_HEADER.size + len(missions) * MISSION_RECORD.size
    rows = b"".join(
        MISSION_RECORD.pack(
            offsets[str(mission["name"])],
            int(mission["scene_bytes"]),
            int(mission["collision_bytes"]),
            int(mission["navigation_bytes"]),
            int(mission["script_bytes"]),
            int(mission["audio_pcm_bytes"]),
            int(mission["object_count"]),
            int(mission["cell_count"]),
            int(mission["model_count"]),
        )
        for mission in missions
    )
    path.write_bytes(
        INDEX_HEADER.pack(
            b"C47I",
            VERSION,
            len(missions),
            texture_count,
            len(names),
            strings_offset,
        )
        + rows
        + names
    )
    return path.stat().st_size


def _find_tex3ds() -> Path:
    command = shutil.which("tex3ds")
    if command:
        return Path(command)
    devkitpro = Path(r"C:\devkitPro\tools\bin\tex3ds.exe")
    if devkitpro.is_file():
        return devkitpro
    raise FormatError(
        "tex3ds was not found; install devkitPro 3ds-dev or add tex3ds to PATH"
    )


def _mission_texture_sources(
    scene: dict[str, object],
) -> tuple[dict[tuple[int, int], dict[str, object]], list[dict[str, object]]]:
    source_packs = {
        1: scene["textures"],
        2: scene["light_textures"],
    }
    references = {
        (1, face[2])
        for mesh in scene["meshes"]
        for face in mesh["ftx_faces"]
        if face[0] & 0x0030
    }
    references.update(
        (2, face[3])
        for mesh in scene["meshes"]
        for face in mesh["ftx_faces"]
        if face[0] & 0x00C0 and face[3] != 0xFFFF
    )

    textures: dict[tuple[int, int], dict[str, object]] = {}
    unresolved: list[dict[str, object]] = []
    for kind, texture_id in sorted(references):
        source = source_packs[kind].get(texture_id)
        source_kind = kind
        if source is None and kind == 1:
            source = source_packs[2].get(texture_id)
            source_kind = 2
        if source is None:
            unresolved.append(
                {
                    "kind": "base" if kind == 1 else "lightmap",
                    "texture_id": texture_id,
                }
            )
        else:
            textures[(source_kind, texture_id)] = source
    return textures, unresolved


def convert_package(
    game_root: Path,
    output: Path,
    missions: list[str],
    tex3ds: Path | None = None,
    progress: object | None = None,
    cancelled: object | None = None,
) -> dict[str, object]:
    game_root = game_root.resolve()
    output = output.resolve()
    if not game_root.is_dir():
        raise FormatError(f"Game root is not a directory: {game_root}")
    if output == game_root or game_root in output.parents:
        raise FormatError("Output directory must not be inside the game installation")
    if not missions:
        raise FormatError("Select at least one mission to convert")

    tool = tex3ds or _find_tex3ds()
    if not tool.is_file():
        raise FormatError(f"tex3ds was not found: {tool}")
    output.mkdir(parents=True, exist_ok=True)
    (output / "mesh").mkdir(exist_ok=True)
    (output / "scene").mkdir(exist_ok=True)
    (output / "audio").mkdir(exist_ok=True)

    package_missions: list[dict[str, object]] = []
    all_texture_records: list[dict[str, object]] = []
    package_model_bytes = 0
    package_vram_bytes = 0
    package_triangle_count = 0
    package_batch_count = 0

    with tempfile.TemporaryDirectory(prefix="c47conv-", dir=output) as temporary:
        work = Path(temporary)
        for mission_index, mission_name in enumerate(missions):
            if cancelled and callable(cancelled) and cancelled():
                raise ConversionCancelled()
            if progress and callable(progress):
                progress(
                    mission_index / len(missions),
                    f"Conversion de {mission_name} ({mission_index + 1}/{len(missions)})",
                )
            archive_path = _mission_archive(game_root, mission_name)
            mission_name = archive_path.stem
            scene = read_scene(archive_path, game_root)
            mesh_dir = output / "mesh" / mission_name
            mesh_dir.mkdir(parents=True, exist_ok=True)

            model_ids: dict[tuple[int, ...], int] = {}
            model_info: list[dict[str, object]] = []
            for mesh in scene["meshes"]:
                if not mesh["vertices"] or (
                    not mesh["triangles"] and not mesh["quads"]
                ):
                    continue
                model_key = tuple(mesh["key"])
                if model_key in model_ids:
                    continue
                model_id = len(model_ids)
                model_ids[model_key] = model_id
                model_data, info = _mesh_binary(mesh)
                model_path = mesh_dir / f"{model_id:05d}.c47m"
                model_path.write_bytes(model_data)
                model_info.append(info)
                package_model_bytes += len(model_data)
                package_triangle_count += int(info["triangles"])
                package_batch_count += int(info["batches"])

            mission_textures, unresolved_texture_references = (
                _mission_texture_sources(scene)
            )
            texture_records, vram_bytes = _convert_textures(
                output,
                mission_index,
                mission_textures,
                tool,
                work,
            )
            all_texture_records.extend(texture_records)
            package_vram_bytes += vram_bytes

            scene_path = output / "scene" / f"{mission_name}.c47s"
            scene_info = _write_scene(scene_path, scene, model_ids, model_info)
            collision_path = output / "scene" / f"{mission_name}.c47c"
            collision_size = _write_collision(
                collision_path,
                scene_info.pop("collision_rows"),
            )
            navigation_path = output / "scene" / f"{mission_name}.c47n"
            navigation_size = _write_navigation(
                navigation_path,
                scene["pathfinder_blobs"],
            )
            scripts = []
            with zipfile.ZipFile(archive_path) as archive:
                for info in archive.infolist():
                    if not info.is_dir() and info.filename.lower().endswith(".sdl"):
                        scripts.append(
                            (
                                info.filename.replace("\\", "/"),
                                archive.read(info.filename),
                            )
                        )
            scripts_path = output / "scene" / f"{mission_name}.c47sc"
            scripts_size = _write_blob_index(scripts_path, b"C47B", scripts)
            audio_bytes, audio_index_size, track_count = _write_audio(
                output,
                mission_name,
                archive_path,
                game_root,
            )
            package_missions.append(
                {
                    "name": mission_name,
                    "archive": str(archive_path.relative_to(game_root)),
                    "object_count": scene_info["objects"],
                    "clip_object_count": scene_info["clip_objects"],
                    "cell_count": scene_info["cells"],
                    "cell_reference_count": scene_info["cell_references"],
                    "model_count": len(model_ids),
                    "texture_count": len(texture_records),
                    "unresolved_texture_references": unresolved_texture_references,
                    "triangle_count": sum(
                        int(model["triangles"]) for model in model_info
                    ),
                    "scene_triangles": sum(
                        int(
                            model_info[
                                model_ids[tuple(item["mesh"]["key"])]
                            ]["triangles"]
                        )
                        for item in scene["objects"]
                        if item["mesh"] is not None
                        and tuple(item["mesh"]["key"]) in model_ids
                    ),
                    "draw_batches": sum(
                        int(model["batches"]) for model in model_info
                    ),
                    "collision_instances": scene_info["collision_instances"],
                    "pathfinders": len(scene["pathfinder_blobs"]),
                    "script_count": len(scripts),
                    "audio_track_count": track_count,
                    "audio_duration_seconds": round(
                        float(scene["audio"]["duration_seconds"]), 3
                    ),
                    "scene_bytes": scene_info["bytes"],
                    "collision_bytes": collision_size,
                    "navigation_bytes": navigation_size,
                    "script_bytes": scripts_size,
                    "audio_pcm_bytes": audio_bytes,
                    "audio_index_bytes": audio_index_size,
                    "model_bytes": sum(int(model["bytes"]) for model in model_info),
                    "texture_t3x_bytes": sum(
                        (output / "tex" / asset).stat().st_size
                        for asset in {
                            str(record["asset_path"]) for record in texture_records
                        }
                    ),
                    "texture_vram_estimate_bytes": vram_bytes,
                    "model_triangles": sum(
                        int(model["triangles"]) for model in model_info
                    ),
                    "sanitized_uv_values": sum(
                        int(model["sanitized_uv_values"]) for model in model_info
                    ),
                    "model_details": model_info,
                }
            )

    texture_index_bytes = _write_texture_index(
        output / "tex" / "index.bin",
        all_texture_records,
    )
    index_bytes = _write_package_index(
        output / "index.bin",
        package_missions,
        len(all_texture_records),
    )
    totals = {
        "mission_count": len(package_missions),
        "texture_count": len(all_texture_records),
        "model_count": sum(int(mission["model_count"]) for mission in package_missions),
        "triangle_count": sum(int(mission["model_triangles"]) for mission in package_missions),
        "draw_batches": sum(int(mission["draw_batches"]) for mission in package_missions),
        "model_bytes": package_model_bytes,
        "texture_vram_estimate_bytes": package_vram_bytes,
        "index_bytes": index_bytes,
        "texture_index_bytes": texture_index_bytes,
    }
    report = {
        "format_version": VERSION,
        "game_root": str(game_root),
        "output": str(output),
        "tool_tex3ds": str(tool),
        "missions": package_missions,
        "totals": totals,
    }
    report_path = output / "report.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    validate_package(output)
    if progress and callable(progress):
        progress(1.0, "Conversion terminée")
    return report


def validate_package(output: Path) -> None:
    data = (output / "index.bin").read_bytes()
    if len(data) < INDEX_HEADER.size:
        raise FormatError("Package index is truncated")
    signature, version, mission_count, texture_count, strings_size, strings_offset = (
        INDEX_HEADER.unpack_from(data)
    )
    if signature != b"C47I" or version != VERSION:
        raise FormatError("Package index signature or version is invalid")
    records_end = INDEX_HEADER.size + mission_count * MISSION_RECORD.size
    if strings_offset != records_end or strings_offset + strings_size != len(data):
        raise FormatError("Package index offsets or string table size are invalid")
    strings = data[strings_offset:]
    mission_records = [
        MISSION_RECORD.unpack_from(data, INDEX_HEADER.size + i * MISSION_RECORD.size)
        for i in range(mission_count)
    ]
    mission_names = []
    for mission_record in mission_records:
        name_offset = mission_record[0]
        if name_offset >= len(strings) or b"\0" not in strings[name_offset:]:
            raise FormatError("Package index has an invalid mission name offset")
        name = strings[name_offset : strings.index(b"\0", name_offset)].decode("utf-8")
        mission_names.append(name)
        for extension in (".c47s", ".c47c", ".c47n", ".c47sc"):
            path = output / "scene" / f"{name}{extension}"
            if not path.is_file():
                raise FormatError(f"Package is missing {path}")
        if not (output / "audio" / f"{name}.pcm").is_file():
            raise FormatError(f"Package is missing audio for {name}")
        _validate_scene(output, name, mission_record)
        _validate_collision(output, name, mission_record)
        _validate_navigation(output, name)
        _validate_scripts(output, name)
        _validate_audio(output, name, mission_record[5])

    texture_data = (output / "tex" / "index.bin").read_bytes()
    if len(texture_data) < TEXTURE_INDEX_HEADER.size:
        raise FormatError("Texture index is truncated")
    (
    texture_signature,
    texture_version,
    _,
    actual_textures,
    texture_strings_offset,
    texture_strings_size,
    ) = TEXTURE_INDEX_HEADER.unpack_from(texture_data)
    if texture_signature != b"C47T" or texture_version != VERSION:
        raise FormatError("Texture index signature or version is invalid")
    expected_string_offset = (
        TEXTURE_INDEX_HEADER.size + actual_textures * TEXTURE_RECORD.size
    )
    if (
        actual_textures != texture_count
        or texture_strings_offset != expected_string_offset
        or texture_strings_offset + texture_strings_size != len(texture_data)
    ):
        raise FormatError("Texture index counts or offsets are invalid")
    texture_strings = texture_data[texture_strings_offset:]
    for texture_index in range(actual_textures):
        row_offset = TEXTURE_INDEX_HEADER.size + texture_index * TEXTURE_RECORD.size
        record = TEXTURE_RECORD.unpack_from(texture_data, row_offset)
        asset_offset = record[6]
        name_offset = record[7]
        asset_name = _read_table_string(texture_strings, asset_offset, "texture asset path")
        _read_table_string(texture_strings, name_offset, "texture source name")
        if Path(asset_name).name != asset_name or not asset_name.endswith(".t3x"):
            raise FormatError("Texture index has an unsafe or invalid asset path")
        if (
            record[0] >= mission_count
            or record[4] not in (1, 2, 3)
            or record[5] not in (1, 2)
            or record[8] not in (1, 2)
        ):
            raise FormatError("Texture index contains an invalid mission or encoding ID")
        if (
            record[2] < 8
            or record[3] < 8
            or record[2] > 1024
            or record[3] > 1024
            or record[2] & (record[2] - 1)
            or record[3] & (record[3] - 1)
        ):
            raise FormatError("Texture index dimensions are not valid 3DS power-of-two sizes")
        asset_path = output / "tex" / asset_name
        if not asset_path.is_file() or asset_path.stat().st_size == 0:
            raise FormatError("Texture index references a missing or empty T3X asset")


def _read_table_string(data: bytes, offset: int, label: str) -> str:
    if offset < 0 or offset >= len(data):
        raise FormatError(f"{label} offset is out of range")
    end = data.find(b"\0", offset)
    if end < 0:
        raise FormatError(f"{label} is not NUL-terminated")
    return data[offset:end].decode("utf-8")


def _validate_scene(output: Path, mission: str, index_record: tuple[int, ...]) -> None:
    path = output / "scene" / f"{mission}.c47s"
    data = path.read_bytes()
    if len(data) < SCENE_HEADER.size:
        raise FormatError(f"{path}: scene header is truncated")
    (
        signature,
        version,
        header_size,
        object_count,
        cell_count,
        model_count,
        _,
        _,
        cell_size,
        object_offset,
        cell_offset,
        refs_offset,
        strings_offset,
        strings_size,
        _,
    ) = SCENE_HEADER.unpack_from(data)
    if signature != b"C47S" or version != VERSION or header_size != SCENE_HEADER.size:
        raise FormatError(f"{path}: invalid scene signature or version")
    if not math.isfinite(cell_size) or cell_size != CELL_SIZE:
        raise FormatError(f"{path}: invalid visibility cell size")
    if (
        object_offset != SCENE_HEADER.size
        or cell_offset != object_offset + object_count * SCENE_OBJECT.size
        or refs_offset != cell_offset + cell_count * SCENE_CELL.size
        or refs_offset > strings_offset
        or strings_offset + strings_size != len(data)
    ):
        raise FormatError(f"{path}: invalid scene section offsets")

    cells = [
        SCENE_CELL.unpack_from(data, cell_offset + index * SCENE_CELL.size)
        for index in range(cell_count)
    ]
    ref_count = (strings_offset - refs_offset) // 4
    if refs_offset + ref_count * 4 != strings_offset:
        raise FormatError(f"{path}: cell reference table is misaligned")
    refs = struct.unpack_from(f"<{ref_count}I", data, refs_offset) if ref_count else ()
    for cell in cells:
        first_ref, count = cell[-2:]
        if first_ref + count > len(refs):
            raise FormatError(f"{path}: cell references exceed the reference table")
        if not all(math.isfinite(value) for value in cell[2:8]):
            raise FormatError(f"{path}: cell bounds contain non-finite values")
        if any(object_id >= object_count for object_id in refs[first_ref : first_ref + count]):
            raise FormatError(f"{path}: cell references an unknown object")

    scene_strings = data[strings_offset:]
    for index in range(object_count):
        record = SCENE_OBJECT.unpack_from(
            data,
            object_offset + index * SCENE_OBJECT.size,
        )
        model_id = record[3]
        cell_id = record[4]
        name_offset = record[5]
        matrix = record[8:24]
        bounds = record[24:30]
        if cell_id >= cell_count:
            raise FormatError(f"{path}: object references an unknown cell")
        if model_id != NO_MODEL and model_id >= model_count:
            raise FormatError(f"{path}: object references an unknown model")
        _read_table_string(scene_strings, name_offset, "scene object name")
        if not all(math.isfinite(value) for value in (*matrix, *bounds)):
            raise FormatError(f"{path}: object transform or bounds are non-finite")
        if model_id != NO_MODEL:
            _validate_model(output / "mesh" / mission / f"{model_id:05d}.c47m")

    if (
        object_count != index_record[6]
        or cell_count != index_record[7]
        or model_count != index_record[8]
        or path.stat().st_size != index_record[1]
    ):
        raise FormatError(f"{path}: counts or size do not match package index")


def _validate_model(path: Path) -> None:
    if not path.is_file():
        raise FormatError(f"Package is missing model {path}")
    data = path.read_bytes()
    if len(data) < MODEL_HEADER.size:
        raise FormatError(f"{path}: model header is truncated")
    (
        signature,
        version,
        header_size,
        vertex_count,
        index_count,
        batch_count,
        _,
        *ranges,
    ) = MODEL_HEADER.unpack_from(data)
    if signature != b"C47M" or version != VERSION or header_size != MODEL_HEADER.size:
        raise FormatError(f"{path}: invalid model signature or version")
    if not all(math.isfinite(value) for value in ranges):
        raise FormatError(f"{path}: model quantization range is not finite")
    batch_offset = MODEL_HEADER.size
    vertex_offset = batch_offset + batch_count * MODEL_BATCH.size
    index_offset = vertex_offset + vertex_count * MODEL_VERTEX.size
    if index_offset + index_count * 2 != len(data):
        raise FormatError(f"{path}: invalid model section sizes")
    indices = (
        struct.unpack_from(f"<{index_count}H", data, index_offset)
        if index_count
        else ()
    )
    for batch_index in range(batch_count):
        (
            texture_id,
            light_texture_id,
            first_index,
            count,
            first_vertex,
            face_flags,
        ) = MODEL_BATCH.unpack_from(
            data,
            batch_offset + batch_index * MODEL_BATCH.size,
        )
        batch_vertex_end = (
            MODEL_BATCH.unpack_from(
                data,
                batch_offset + (batch_index + 1) * MODEL_BATCH.size,
            )[4]
            if batch_index + 1 < batch_count
            else vertex_count
        )
        if (
            (texture_id != NO_TEXTURE and texture_id > 0xFFFF)
            or (light_texture_id != NO_TEXTURE and light_texture_id > 0xFFFF)
            or face_flags > 0xFFFF
            or first_index + count > index_count
            or first_vertex >= batch_vertex_end
            or count % 3
            or count == 0
        ):
            raise FormatError(f"{path}: draw batch has invalid index bounds")
        if any(
            index < first_vertex
            or index >= batch_vertex_end
            for index in indices[first_index : first_index + count]
        ):
            raise FormatError(f"{path}: draw batch contains an out-of-range vertex index")


def _validate_collision(
    output: Path,
    mission: str,
    index_record: tuple[int, ...],
) -> None:
    path = output / "scene" / f"{mission}.c47c"
    data = path.read_bytes()
    if len(data) < COLLISION_HEADER.size:
        raise FormatError(f"{path}: collision header is truncated")
    signature, version, record_size, count, crc = COLLISION_HEADER.unpack_from(data)
    records = data[COLLISION_HEADER.size :]
    if (
        signature != b"C47C"
        or version != VERSION
        or record_size != COLLISION_RECORD.size
        or len(records) != count * record_size
        or zlib.crc32(records) != crc
        or len(data) != index_record[2]
    ):
        raise FormatError(f"{path}: invalid collision records or checksum")
    for index in range(count):
        record = COLLISION_RECORD.unpack_from(records, index * record_size)
        model_id = record[1]
        _validate_model(output / "mesh" / mission / f"{model_id:05d}.c47m")


def _validate_blob_file(path: Path, expected_signature: bytes) -> int:
    data = path.read_bytes()
    if len(data) < SCRIPT_HEADER.size:
        raise FormatError(f"{path}: indexed blob header is truncated")
    signature, version, header_size, count, records_offset, strings_offset, strings_size, payload_offset = (
        SCRIPT_HEADER.unpack_from(data)
    )
    records_end = records_offset + count * 16
    if (
        signature != expected_signature
        or version != VERSION
        or header_size != SCRIPT_HEADER.size
        or records_offset != SCRIPT_HEADER.size
        or strings_offset != records_end
        or strings_offset + strings_size != payload_offset
        or payload_offset > len(data)
    ):
        raise FormatError(f"{path}: invalid indexed blob table")
    strings = data[strings_offset:payload_offset]
    for index in range(count):
        name_offset, blob_offset, blob_size, crc = struct.unpack_from(
            "<IIII",
            data,
            records_offset + index * 16,
        )
        _read_table_string(strings, name_offset, f"{path} record name")
        if (
            blob_offset < payload_offset
            or blob_offset + blob_size > len(data)
            or zlib.crc32(data[blob_offset : blob_offset + blob_size]) != crc
        ):
            raise FormatError(f"{path}: invalid blob payload or checksum")
    return count


def _validate_scripts(output: Path, mission: str) -> None:
    _validate_blob_file(output / "scene" / f"{mission}.c47sc", b"C47B")


def _validate_navigation(output: Path, mission: str) -> None:
    path = output / "scene" / f"{mission}.c47n"
    data = path.read_bytes()
    if len(data) < NAV_HEADER.size:
        raise FormatError(f"{path}: navigation header is truncated")
    signature, version, header_size, count, records_offset, strings_offset, strings_size, payload_offset = (
        NAV_HEADER.unpack_from(data)
    )
    records_end = records_offset + count * NAV_RECORD.size
    if (
        signature != b"C47N"
        or version != VERSION
        or header_size != NAV_HEADER.size
        or records_offset != NAV_HEADER.size
        or strings_offset != records_end
        or strings_offset + strings_size != payload_offset
        or payload_offset > len(data)
    ):
        raise FormatError(f"{path}: invalid navigation table")
    strings = data[strings_offset:payload_offset]
    for index in range(count):
        name_offset, blob_offset, blob_size, crc = NAV_RECORD.unpack_from(
            data,
            records_offset + index * NAV_RECORD.size,
        )
        _read_table_string(strings, name_offset, "navigation object name")
        if (
            blob_offset < payload_offset
            or blob_offset + blob_size > len(data)
            or zlib.crc32(data[blob_offset : blob_offset + blob_size]) != crc
        ):
            raise FormatError(f"{path}: invalid navigation payload or checksum")


def _validate_audio(output: Path, mission: str, expected_size: int) -> None:
    index_path = output / "audio" / f"{mission}.c47a"
    data = index_path.read_bytes()
    if len(data) < AUDIO_HEADER.size:
        raise FormatError(f"{index_path}: audio header is truncated")
    signature, version, channels, count, record_size = AUDIO_HEADER.unpack_from(data)
    if (
        signature != b"C47A"
        or version != VERSION
        or channels != 1
        or record_size != AUDIO_RECORD.size
        or len(data) != AUDIO_HEADER.size + count * record_size
    ):
        raise FormatError(f"{index_path}: invalid audio index")
    pcm = (output / "audio" / f"{mission}.pcm").read_bytes()
    if len(pcm) != expected_size:
        raise FormatError(f"{mission}: PCM size does not match package index")
    for index in range(count):
        rate, samples, offset, length, crc = AUDIO_RECORD.unpack_from(
            data,
            AUDIO_HEADER.size + index * record_size,
        )
        if (
            rate not in (11025, 22050)
            or length != samples * 2
            or offset + length > len(pcm)
            or zlib.crc32(pcm[offset : offset + length]) != crc
        ):
            raise FormatError(f"{index_path}: invalid PCM track record or checksum")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert Hitman: Codename 47 mission assets into New 3DS data files."
    )
    parser.add_argument("game_root", type=Path, help="path to the user's game installation")
    parser.add_argument("output", type=Path, help="output directory for the SD-card package")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--mission",
        action="append",
        help="primary mission to convert (repeatable; default: C1_1)",
    )
    selection.add_argument(
        "--all",
        action="store_true",
        help="convert all primary C<number>_<number> missions",
    )
    parser.add_argument("--tex3ds", type=Path, help="path to tex3ds executable")
    arguments = parser.parse_args()
    try:
        game_root = arguments.game_root.resolve()
        if arguments.all:
            missions = [path.stem for path in _mission_archives(game_root)]
        elif arguments.mission:
            missions = arguments.mission
        else:
            missions = ["C1_1"]
        report = convert_package(
            game_root,
            arguments.output,
            missions,
            arguments.tex3ds.resolve() if arguments.tex3ds else None,
        )
        for mission in report["missions"]:
            print(
                f"{mission['name']}: {mission['object_count']} objects, "
                f"{mission['model_count']} models, {mission['texture_count']} textures, "
                f"{mission['scene_bytes'] + mission['collision_bytes'] + mission['navigation_bytes'] + mission['script_bytes']} "
                "scene/navigation/script bytes, "
                f"{mission['audio_pcm_bytes']} PCM bytes"
            )
        print(f"Package report: {arguments.output.resolve() / 'report.json'}")
    except (FormatError, OSError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
