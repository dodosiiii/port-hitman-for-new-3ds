import sys
import struct
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.c47_phase1 import (
    Chunk,
    FormatError,
    _write_obj,
    decode_ima_adpcm,
    parse_chunk,
    parse_pathfinder,
    parse_texture,
    parse_wave,
    reconstruct_pack,
    texture_dxt_rgba,
    texture_rgba,
)
from tools.c47conv import (
    MODEL_BATCH,
    MODEL_HEADER,
    MODEL_VERTEX,
    SCENE_CELL,
    SCENE_HEADER,
    SCENE_OBJECT,
    _mission_texture_sources,
    _mesh_binary,
    _write_scene,
    _texture_dimensions,
    _texture_encoding,
)


def pack_chunk(tag, data=b""):
    return tag.encode("ascii")[::-1] + struct.pack("<I", len(data) + 8) + data


def pack_repeat_single_data(tag, repeat_bytes, data_offset, payload_offset=0, patch=0):
    virtual_size = data_offset + len(repeat_bytes)
    virtual_chunk = (
        tag.encode("ascii")[::-1]
        + struct.pack("<II", virtual_size | 0x40000000, data_offset)
        + struct.pack("<II", 1, len(repeat_bytes))
    )
    tree = virtual_chunk
    record = struct.pack("<IIII", payload_offset, data_offset, len(repeat_bytes), patch)
    header = struct.pack("<II", 0, len(virtual_chunk))
    return header + tree + record


def make_texture(tag, payload, width=4, height=4):
    data = (
        struct.pack("<IHHIII", 7, height, width, 1, 0, 0)
        + b"test\0"
        + struct.pack("<I", len(payload))
        + payload
    )
    return parse_texture(Chunk(int.from_bytes(tag.encode("ascii"), "big"), len(data), data))


class ChunkReaderTests(unittest.TestCase):
    def test_reads_plain_chunk(self):
        chunk = parse_chunk(pack_chunk("DATA", b"hello"))
        self.assertEqual(chunk.name, "DATA")
        self.assertEqual(chunk.main_data, b"hello")

    def test_rejects_chunk_past_end_of_buffer(self):
        with self.assertRaisesRegex(FormatError, "exceeds available data"):
            parse_chunk(b"DATA" + struct.pack("<I", 100))

    def test_reconstructs_pack_repeat_payload_and_patch_word(self):
        repeat = b"\x00\x00\x00\x00payload"
        packed = pack_repeat_single_data("TEST", repeat, 20, patch=0x12345678)
        chunk = reconstruct_pack(packed, repeat)
        self.assertEqual(chunk.name, "TEST")
        self.assertEqual(chunk.multi_data, [struct.pack("<I", 0x12345678) + b"payload"])

    def test_decodes_indexed_texture_pixels_with_rgba_palette(self):
        texture = {
            "format": "PALN",
            "mips": [bytes((1, 0, 1))],
            "palette": bytes((255, 0, 0, 255, 0, 128, 255, 64)),
        }
        self.assertEqual(
            texture_rgba(texture),
            bytes((0, 128, 255, 64, 255, 0, 0, 255, 0, 128, 255, 64)),
        )

    def test_decodes_dxt1_opaque_color_block(self):
        block = struct.pack("<HHI", 0xF800, 0, 0)
        self.assertEqual(
            texture_dxt_rgba(make_texture("DXT1", block)),
            bytes((255, 0, 0, 255)) * 16,
        )

    def test_decodes_dxt1_transparent_index(self):
        block = struct.pack("<HHI", 0, 0xF800, 0xFFFFFFFF)
        self.assertEqual(
            texture_dxt_rgba(make_texture("DXT1", block, width=3, height=2)),
            bytes((0, 0, 0, 0)) * 6,
        )

    def test_decodes_dxt3_explicit_alpha(self):
        block = b"\xFF" * 8 + struct.pack("<HHI", 0xF800, 0, 0)
        self.assertEqual(
            texture_dxt_rgba(make_texture("DXT3", block)),
            bytes((255, 0, 0, 255)) * 16,
        )

    def test_parses_empty_pathfinder_structure(self):
        data = (
            struct.pack("<II", 0x12312312, 1)
            + b"\0"
            + struct.pack("<IIII", 0, 0, 0, 123)
        )
        result = parse_pathfinder(data)
        self.assertEqual(result["room_count"], 0)
        self.assertEqual(result["room_instances"], 0)
        self.assertEqual(result["door_instances"], 0)
        self.assertEqual(result["last_value"], 123)

    def test_parses_ima_adpcm_wave_header(self):
        fmt = struct.pack("<HHIIHHHH", 0x0011, 1, 22050, 11100, 512, 4, 2, 1017)
        chunks = (
            b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"fact" + struct.pack("<II", 4, 1017)
            + b"data" + struct.pack("<I", 512) + bytes(512)
        )
        wave = b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WAVE" + chunks
        result = parse_wave(wave)
        self.assertEqual(result["format_tag"], 0x0011)
        self.assertEqual(result["sample_rate"], 22050)
        self.assertEqual(result["samples_per_block"], 1017)
        self.assertEqual(result["sample_count"], 1017)
        self.assertEqual(result["data_bytes"], 512)

    def test_decodes_mono_ima_adpcm_block(self):
        fmt = struct.pack("<HHIIHHHH", 0x0011, 1, 8000, 7111, 8, 4, 2, 9)
        chunks = (
            b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"fact" + struct.pack("<II", 4, 9)
            + b"data" + struct.pack("<I", 8) + struct.pack("<hBB", 1000, 0, 0)
            + bytes((0x17, 0x26, 0x35, 0x44))
        )
        wave = b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WAVE" + chunks
        self.assertEqual(
            decode_ima_adpcm(wave),
            struct.pack("<9h", 1000, 1013, 1019, 1041, 1056, 1087, 1116, 1150, 1191),
        )

    def test_rejects_ima_adpcm_invalid_step_index(self):
        fmt = struct.pack("<HHIIHHHH", 0x0011, 1, 8000, 8000, 8, 4, 2, 9)
        chunks = (
            b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"fact" + struct.pack("<II", 4, 9)
            + b"data" + struct.pack("<I", 8) + struct.pack("<hBB", 1000, 89, 0) + b"\0" * 4
        )
        wave = b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WAVE" + chunks
        with self.assertRaisesRegex(FormatError, "invalid step index"):
            decode_ima_adpcm(wave)

    def test_obj_uses_correct_vertex_offsets_between_objects(self):
        identity = (
            (1.0, 0.0, 0.0, 0.0),
            (0.0, 1.0, 0.0, 0.0),
            (0.0, 0.0, 1.0, 0.0),
            (0.0, 0.0, 0.0, 1.0),
        )
        mesh = {
            "vertices": [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
            "triangles": [0, 2, 4],
            "quads": [],
            "ftx_faces": [],
            "uv_data": [],
            "invalid_uv_values": 0,
        }
        scene = {
            "objects": [
                {"name": "first", "path": ("first",), "matrix": identity, "mesh": mesh},
                {"name": "second", "path": ("second",), "matrix": identity, "mesh": mesh},
            ],
            "meshes": [mesh],
            "textures": {},
            "light_textures": {},
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            _write_obj(scene, output)
            obj_lines = (output / "C1_1.obj").read_text(encoding="utf-8").splitlines()
            faces = [line for line in obj_lines if line.startswith("f ")]

        self.assertEqual(faces, ["f 1 2 3", "f 4 5 6"])


class New3dsConverterTests(unittest.TestCase):
    def test_texture_limits_and_selects_compact_alpha_format(self):
        self.assertEqual(_texture_dimensions(2, 9952), (8, 1024))
        self.assertEqual(_texture_dimensions(100, 60), (128, 64))
        self.assertEqual(_texture_encoding(bytes((20, 30, 40, 255))), ("rgb565", 1))
        self.assertEqual(_texture_encoding(bytes((20, 30, 40, 0))), ("rgba5551", 2))
        self.assertEqual(_texture_encoding(bytes((20, 30, 40, 128))), ("rgba4", 3))

    def test_resolves_base_texture_fallbacks_from_light_pack(self):
        base_texture = {"name": "base"}
        light_texture = {"name": "light"}
        scene = {
            "textures": {1: base_texture},
            "light_textures": {2: light_texture, 3: light_texture},
            "meshes": [
                {
                    "ftx_faces": [
                        (0x0020, 0, 1, 0, 0, 0),
                        (0x0020, 0, 2, 0, 0, 0),
                        (0x0080, 0, 0, 3, 0, 0),
                        (0x0020, 0, 4, 0, 0, 0),
                        (0x0080, 0, 0, 5, 0, 0),
                    ]
                }
            ],
        }

        textures, unresolved = _mission_texture_sources(scene)

        self.assertIs(textures[(1, 1)], base_texture)
        self.assertIs(textures[(2, 2)], light_texture)
        self.assertIs(textures[(2, 3)], light_texture)
        self.assertEqual(
            unresolved,
            [
                {"kind": "base", "texture_id": 4},
                {"kind": "lightmap", "texture_id": 5},
            ],
        )

    def test_model_groups_faces_by_material_and_quantizes_attributes(self):
        mesh = {
            "vertices": [
                (-4.0, 1.0, 0.0),
                (4.0, 1.0, 0.0),
                (4.0, 9.0, 4.0),
                (-4.0, 9.0, 4.0),
            ],
            "triangles": [0, 2, 4],
            "quads": [0, 2, 4, 6],
            "ftx_faces": [
                (0x0030, 0, 42, 0, 0, 0),
                (0, 0, 0, 0, 0, 0),
            ],
            "uv_data": [0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 0.0],
            "light_uv_data": [],
        }
        data, info = _mesh_binary(mesh)
        (
            signature,
            version,
            header_size,
            vertex_count,
            index_count,
            batch_count,
            sanitized,
            *ranges,
        ) = MODEL_HEADER.unpack_from(data)
        self.assertEqual((signature, version, header_size), (b"C47M", 1, MODEL_HEADER.size))
        self.assertEqual((vertex_count, index_count, batch_count, sanitized), (7, 9, 2, 0))
        self.assertEqual(info["triangles"], 3)

        first_batch = MODEL_BATCH.unpack_from(data, MODEL_HEADER.size)
        second_batch = MODEL_BATCH.unpack_from(data, MODEL_HEADER.size + MODEL_BATCH.size)
        self.assertEqual(first_batch, (42, 0xFFFFFFFF, 0, 3, 0, 0x0030))
        self.assertEqual(
            second_batch,
            (0xFFFFFFFF, 0xFFFFFFFF, 3, 6, 3, 0),
        )

        vertices_offset = MODEL_HEADER.size + batch_count * MODEL_BATCH.size
        indices_offset = vertices_offset + vertex_count * MODEL_VERTEX.size
        indices = struct.unpack_from(f"<{index_count}H", data, indices_offset)
        self.assertTrue(all(0 <= index < vertex_count for index in indices))
        first_vertex = MODEL_VERTEX.unpack_from(data, vertices_offset)
        position_origin = ranges[:3]
        position_scale = ranges[3:6]
        reconstructed_x = position_origin[0] + first_vertex[0] * position_scale[0]
        self.assertAlmostEqual(reconstructed_x, -4.0, delta=position_scale[0])

    def test_model_preserves_lightmap_uvs_and_material_ids(self):
        mesh = {
            "vertices": [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
            "triangles": [0, 2, 4],
            "quads": [],
            "ftx_faces": [(0x00F0, 0, 42, 7, 0, 0)],
            "uv_data": [0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 0.0, 0.0],
            "light_uv_data": [
                0.25,
                0.75,
                0.5,
                0.5,
                0.75,
                0.25,
                0.0,
                0.0,
            ],
        }
        data, info = _mesh_binary(mesh)

        batch = MODEL_BATCH.unpack_from(data, MODEL_HEADER.size)
        vertices_offset = MODEL_HEADER.size + MODEL_BATCH.size
        vertex = MODEL_VERTEX.unpack_from(data, vertices_offset)

        self.assertEqual(batch, (42, 7, 0, 3, 0, 0x00F0))
        self.assertEqual(info["batches"], 1)
        self.assertNotEqual(vertex[5:7], (0, 0))
        self.assertEqual(vertex[7:], (255, 255, 255, 255))

    def test_scene_cells_reference_every_object(self):
        identity = (
            (1.0, 0.0, 0.0, 0.0),
            (0.0, 1.0, 0.0, 0.0),
            (0.0, 0.0, 1.0, 0.0),
            (0.0, 0.0, 0.0, 1.0),
        )
        translated = identity[:3] + ((5000.0, 0.0, 0.0, 1.0),)
        mesh = {"key": (7,)}
        scene = {
            "objects": [
                {
                    "type": 1,
                    "flags": 0,
                    "path": ("PROT", "first"),
                    "matrix": identity,
                    "mesh": mesh,
                },
                {
                    "type": 1,
                    "flags": 0,
                    "path": ("PROT", "second"),
                    "matrix": translated,
                    "mesh": mesh,
                },
            ]
        }
        model_info = [{"bounds": (-1, -1, -1, 1, 1, 1)}]
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "scene.c47s"
            _write_scene(path, scene, {(7,): 0}, model_info)
            data = path.read_bytes()

        header = SCENE_HEADER.unpack_from(data)
        object_count, cell_count = header[3:5]
        object_offset, cell_offset, refs_offset, strings_offset = header[9:13]
        self.assertEqual(object_count, 2)
        first_object = SCENE_OBJECT.unpack_from(data, object_offset)
        self.assertEqual(first_object[7], 0xFFFFFFFF)
        self.assertGreaterEqual(cell_count, 2)
        self.assertEqual(object_offset, SCENE_HEADER.size)
        self.assertEqual(cell_offset, object_offset + 2 * SCENE_OBJECT.size)
        cells = [
            SCENE_CELL.unpack_from(data, cell_offset + i * SCENE_CELL.size)
            for i in range(cell_count)
        ]
        self.assertEqual(sum(cell[-1] for cell in cells), 6)
        references = struct.unpack_from(
            f"<{(strings_offset - refs_offset) // 4}I",
            data,
            refs_offset,
        )
        self.assertEqual(sorted(set(references)), [0, 1])


if __name__ == "__main__":
    unittest.main()
