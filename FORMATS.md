# Hitman: Codename 47 - format research notes

Status: independent, executable reader validated against all 13 primary
`C<number>_<number>.zip` missions in the GOG installation in `Hitman Codename
47/`. It reads SPK hierarchy, mesh/UV tables, PAL/LGT and DXT textures,
PackRepeat data, Pathfinder geometry, and WAV/IMA-ADPCM samples. Other DBL
property types and runtime rendering/gameplay behavior are outside this phase.
No game assets are included in source files; locally exported OBJ/PNG files
stay in `build/phase1/`.

## Evidence and reproducibility

- `node tools/inspect-archives.mjs "Hitman Codename 47" "mission-archive-inventory.txt"`
  inventories ZIP contents without extracting or changing them.
- `python tools/c47_phase1.py export "Hitman Codename 47/C1_HongKong/C1_1.zip" --game-root "Hitman Codename 47" --output "build/phase1/C1_1"`
  writes a Wavefront OBJ, material file, and PNGs for referenced textures.
- `python tools/c47_phase1.py budget --game-root "Hitman Codename 47" --output "build/phase1/mission-budget.csv"`
  validates and counts geometry, textures, audio, pathfinder, and SDL data in
  all primary missions.
- `python tools/c47_phase1.py texture-audit "Hitman Codename 47/C1_HongKong/C1_1.zip" --game-root "Hitman Codename 47"`
  decodes DXT1/DXT3 and compares the result with same-resolution PAL images.
- `python -m unittest discover -s tests -v`
  checks chunk/pack-repeat parsing, palette/DXT decoding, Pathfinder/WAVE
  parsing, and IMA ADPCM decoding.
- The sample archive inspected was
  `Hitman Codename 47/C1_HongKong/C1_1.zip`.
- The format descriptions below were cross-checked against the public parser
  sources in [c47edit](https://github.com/AdrienTD/c47edit) and
  [47tools](https://github.com/AdrienTD/47tools). They are independent
  observations, not copied parser code.

## ZIP scene archives

The scene files are standard ZIP archives. `C1_1.zip` contains 30 files:
19 `.SDL` scripts, 5 `.MOR` files, and one each of `.ANM`, `.DXT`, `.LGT`,
`.PAL`, `.SPK`, and `.WAV`.

| Entry | Observed representation | Notes |
|---|---|---|
| `.SDL` | Plain text | Script source, commonly CRLF. Includes refer to other scripts. |
| `.MOR` | Plain-text animation/morph keyframes in the inspected sample | Begins with blank CRLF lines and sections such as `ANIMATION`, `OBJECTS`, `KEYS`, `TARGETS`, and `FRAME`. |
| `Pack.SPK` | Chunk container | Scene hierarchy, objects, meshes, and other records. |
| `PackRepeat.PAL`, `.DXT`, `.LGT`, `.WAV`, `.ANM` | Pack-repeat container | Must be reconstructed with the matching `Repeat.*` file when present. |
| `Pack.PAL`, etc. | Chunk container | Used where a direct pack is present instead of `PackRepeat.*`. |

The installation root has the shared `Repeat.ANM`, `Repeat.DXT`,
`Repeat.PAL`, and `Repeat.WAV` files. These are inputs to reading the
corresponding `PackRepeat.*` entries; they are not themselves mission archives.

## Chunk container

The community parser reads a chunk header as little-endian 32-bit values.
The tag is a four-byte code; source code uses multi-character constants, so
the on-disk bytes appear reversed relative to the logical code (for example,
the `Pack.LGT` sample starts with bytes `54 47 4C 00`, while the parser calls
the root tag `LGT`).

| Offset | Type | Meaning |
|---:|---|---|
| `0x00` | `uint32` | Four-byte tag |
| `0x04` | `uint32` | Total chunk size in low 30 bits; bit 31 means subchunks follow; bit 30 means multidata follows |
| `0x08` | `uint32`, conditional | Offset from this chunk's start to its data section, present when either flag is set |
| next | `uint32`, conditional | Number of subchunks, present when bit 31 is set |
| next | `uint32[]`, conditional | Multidata count followed by one `uint32` byte length per item, present when bit 30 is set |
| data offset | bytes | Main data, or concatenated multidata items; child chunks are stored before this section |

For `Pack.LGT` (1,482,782 bytes), the first 16 bytes are:

```text
54 47 4C 00  1E A0 16 80  1E A0 16 00  12 03 00 00
```

Interpreted little-endian:

- tag bytes: `54 47 4C 00` (logical `LGT` in the parser);
- size/flags: `0x8016A01E`, total size `0x0016A01E` (1,482,782 bytes), with subchunks enabled;
- data offset: `0x0016A01E`, at the end of this root chunk;
- subchunk count: `0x00000312` (786).

The parser's chunk reader assumes structurally valid input. A production reader
must add bounds checks and reject impossible offsets/counts.

## Pack-repeat container

`c47edit` first prefers `PackRepeat.<ext>` in the archive. It reconstructs a
normal chunk tree from that entry and the matching installation-level
`Repeat.<ext>` file. Its reconstruction metadata contains a chunk layout and
records that copy byte ranges from the shared repeat file into data buffers,
then restore a 32-bit word at each destination. Independently implemented
reconstruction successfully reads the repeated PAL/DXT/WAV/ANM packs of
`C0_1`, `C1_1`, and the other primary missions; unit tests also check bounds,
payload copying, and the replacement word. Reconstruction is recursive and
bounds-checked. It is not a complete specification of every possible
corrupt or non-GOG variant of the format.

## Texture packs

`Pack.PAL` (or reconstructed `PackRepeat.PAL`) stores one texture chunk per
subchunk. The parser recognizes `PALN` (indexed pixels) and `RGBA` (direct
RGBA pixels). The paired DXT pack stores `DXT1`/`DXT3` blocks and a small number
of `PALN` exceptions.

| Offset in texture data | Type | Meaning |
|---:|---|---|
| `0x00` | `uint32` | Texture ID |
| `0x04` | `uint16` | Height |
| `0x06` | `uint16` | Width |
| `0x08` | `uint32` | Mipmap count in the declared structure; one rendering path reads its low 16 bits |
| `0x0C` | `uint32` | Flags |
| `0x10` | `uint32` | Unknown/random field |
| `0x14` | NUL-terminated bytes | Texture name |
| after name | per mip | `uint32` byte count, then indexed pixels (`PALN`), RGBA bytes (`RGBA`), or compressed blocks (`DXT1`/`DXT3`) |
| after mip data | conditional | `PALN` has a `uint32` palette entry count and 4-byte RGBA palette entries |

For a `PALN` mip, each pixel byte indexes the palette. The first mip follows
the name and begins with its byte count. Further mip counts and payloads precede
the palette. Texture ID ordering is cross-checked against the corresponding `Pack.DXT`
chunks; IDs and subchunk order agree in all 13 missions. Compressed mip payload
sizes match 4x4 block storage: `ceil(width/4) * ceil(height/4) * 8` bytes for
DXT1 and `ceil(width/4) * ceil(height/4) * 16` bytes for DXT3. Mips are stored
after the name as repeated length/payload pairs. The reader decodes the first
mip on CPU: DXT1 uses the RGB565 color endpoints and optional transparent
index; DXT3 adds explicit 4-bit alpha values. DXT1/DXT3 dimensions differ from
their PAL counterpart for 13 texture pairs across the mission set; these are
reported rather than treated as malformed.

The `texture-audit` command compares decoded DXT pixels with their PAL image
counterparts. Across all 13 missions, 7,106 DXT1/DXT3 textures with matching
dimensions were decoded; the remaining 13 pairs have differing PAL/DXT
dimensions and are reported separately. Per-mission mean absolute error
ranged from 3.11 to 3.95 per visible RGB channel and 0.29 to 0.66 per alpha
channel. In C1_1, the corresponding errors were 3.23 and 0.41. The OBJ/PNG
export still uses the uncompressed PAL/RGBA representation.

In the C1_1 export, 266 texture IDs are referenced by mesh faces (of 375 PAL
and 786 LGT texture chunks). Only referenced textures are exported. Texture
images are user-installation-derived and must not be packaged with the port.

## Scene objects and meshes in `Pack.SPK`

The sample `Pack.SPK` starts with on-disk bytes `4B 50 53 00` (logical root
tag `SPK`). The following table maps the on-disk byte-reversed tags to the
corresponding logical tags used by the editor:

| On-disk tag bytes | Logical tag | Observed contents |
|---|---|---|
| `54 4F 52 50` | `PROT` | Main (non-clip) object hierarchy |
| `50 4C 43 50` | `PCLP` | Clip-object hierarchy |
| `41 45 48 50` | `PHEA` | 15-word object property records |
| `4D 41 4E 50` | `PNAM` | NUL-terminated object names |
| `53 4F 50 50` | `PPOS` | Three-float translations |
| `58 54 4D 50` | `PMTX` | Four signed 16.30 transform coefficients plus packed sign bits |
| `52 45 56 50` | `PVER` | Three-component float vertex positions |
| `43 41 46 50` | `PFAC` | 16-bit quad and triangle index arrays |
| `58 54 46 50` | `PFTX` | Face texture records |
| `43 56 55 50` | `PUVC` | Float texture and lightmap coordinates |
| `54 41 44 50` | `PDAT` | Extra object data and animation references |
| `4C 42 44 50` | `PDBL` | Packed typed object-property records |
| `43 58 45 50` | `PEXC` | Optional per-object extension chunks |

The object hierarchy chunk's low 24 tag bits point to a record in `PHEA`.
Records are little-endian `uint32` words; when a flag does not require a
subrecord, trailing words may be omitted. The mesh flag is `0x0020`, and the
line flag is `0x0400`.

| Word | Meaning |
|---:|---|
| 0 | Offset into `PDBL` for the object's typed property list |
| 1 | One-based offset into `PEXC` for an optional extension (zero means absent) |
| 2 | Offset into `PNAM` for the NUL-terminated name |
| 3 | Index into `PMTX`; each transform uses four 32-bit values |
| 4 | Byte offset into `PPOS`; position is three little-endian `float32`s |
| 5 low/high 16 bits | Object class ID / flags |
| 6 | Float index into `PVER` for vertex positions |
| 7 | 16-bit index-array index in `PFAC` for quads |
| 8 | 16-bit index-array index in `PFAC` for triangles |
| 9 | One-based `PFTX` offset; high bit instead points into an animation record in `PDAT` |
| 10 | Number of vertices |
| 11 | Number of quads |
| 12 | Number of triangles; for line objects this refers to the line-term buffer |
| 13 | Per-object packed color |
| 14 | Additional mesh key / unknown value |

`PVER` positions are three `float32`s. The quad index slice starts at word 7
and contains `4 * word[11]` `uint16`s; the triangle slice starts at word 8
and contains `3 * word[12]` `uint16`s. For exported OBJ, each encoded vertex
index selects `index >> 1` in the vertex array. Quads are triangulated as
`(0,1,2)` and `(0,2,3)`.

At `PFTX` offset `word[9] - 1`, the record begins with three `uint32`s:
texture-UV float index, lightmap-UV float index, and face count, followed by
six `uint16`s per face. Face flag bit mask `0x0030` indicates texture UVs;
the texture ID is face field 2. Each textured face has four `(u,v)` float
pairs (eight floats) in the order consumed by the mesh face array. Bit mask
`0x00C0` indicates lightmap UVs; the second UV-float offset in the PFTX header
indexes those arrays in `PUVC`. Child transform composition follows the
editor's row-vector convention: `local * parent`.

The C1_1 reader exported 3,506 renderable object instances, 1,711 unique mesh
buffers, 57,594 triangles across unique meshes, and 158,911 triangles after
applying all instances. Its OBJ plus material file parsed successfully by the
independent reader. UVs are a known exception: four missions contain 12–72
non-finite sentinel float values in base or lightmap UV coordinates. The exporter
replaces those only in the OBJ with zero and records a warning/count; source
game data is not modified. Visual approval in Blender is not yet recorded.

## Pathfinder and audio

Object type 111 (`ZPathFinder2`) stores its Pathfinder structure in PDBL entry
14, encoded as a length-prefixed byte vector. The vector begins with marker
`0x12312312`, a name-buffer length and its NUL-terminated string data, then
count-prefixed rooms and room instances, followed by door instances and a final
`uint32`. Room data contains leaf nodes (24 bytes each), tree nodes (11 bytes
each), layers (4 bytes each), leaf-edge indexes followed by their float costs,
three 3D float vectors (minimum, maximum, resolution), doors (16 bytes each),
pairwise door edges (12 bytes each), per-door 12-byte records, kongs (3 floats
each), and a string-buffer offset. A room instance stores a name offset, room
index, and one index per door in that room. The reader bounds-checks every
array and string, checks finite bounds and room indexes, and requires the
structure to be consumed exactly. All 13 primary missions parse; C1_1 contains
10 rooms, 10 room instances, 34 door instances, 4,542 leaf nodes, and 8,607
tree nodes.

Every `WPCM` child in the mission WAV packs contains a complete RIFF/WAVE file.
The checked RIFF sizes and subchunk boundaries are exact. All 3,478 entries
across the 13 missions use WAVE format tag `0x0011` (IMA ADPCM), mono, 4-bit
samples, and include `fmt`, `fact`, and `data` chunks. C1_1 has 214 entries;
sample rates across the missions are 11,025 and 22,050 Hz, with matching
256/512-byte blocks and 505/1,017 samples per block. The reader validates headers, `fact` counts, and block
boundaries, decodes mono IMA ADPCM to signed 16-bit PCM, trims padded blocks
to the declared `fact` sample count, and records total audio duration in the
budget CSV. PCM output matched FFmpeg exactly through the declared sample
count on representative 11,025 Hz and 22,050 Hz tracks; FFmpeg's raw output
also includes padded samples in the last block. All 3,478 mission tracks
decode and produce exactly the declared sample count. Other DBL entry types
and non-IMA WAV encodings have not been exhaustively specified.

## Phase 2: New 3DS data package

Run the converter from the project root:

```text
python c47conv.py "Hitman Codename 47" "build/phase2" --mission C1_1
```

With no selector it converts `C1_1`; repeat `--mission` for several missions,
or use `--all` to select every primary mission. `--tex3ds` can specify the
devkitPro texture compiler explicitly. The source installation is read-only;
the output directory must be outside it. Pillow and `tex3ds` are required.
The converter validates its generated index, scenes, models, collisions,
navigation, scripts, texture references and audio checksums before returning.

The output contains `index.bin` (`C47I`), `tex/index.bin` (`C47T`) and
deduplicated `tex/*.t3x` files, mission-scoped `mesh/<mission>/*.c47m`, scene
files `scene/<mission>.c47s`, `.c47c`, `.c47n`, and `.c47sc`, and
`audio/<mission>.pcm` with an accompanying `.c47a` index. `report.json`
records counts, sizes, quantization details and unresolved lightmap IDs. The
report includes local input/output paths; do not distribute it with the
game-derived package.

To combine the package into one file for the SD card, run:

```text
python tools/c47pack.py build/phase2
```

This creates `build/phase2.c47p`, excludes the local-only `report.json`, and
checks the archive index after writing. The `.c47p` file contains the original
files without compression, so the 3DS can seek to a model or texture directly
without extracting thousands of files. The current phase-2 package becomes
one approximately 282 MB file instead of 54,162 separate files.
Copy `phase2.c47p` to `sdmc:/3ds/hitman47/data.c47p`. Its format uses a
`<4sHHIIII>` `C47P` version-1 header, sorted `<III>` path-offset/data-offset/
size records, a UTF-8 NUL-terminated path table, then the uncompressed file
contents. The archive is limited to 2 GiB for the 3DS runtime's file offsets.

All integers and floats are little-endian. Binary files start with a four-byte
ASCII signature, a `uint16` version (currently 1), and the file-specific
header size or flags:

| File | Header and following records |
|---|---|
| `index.bin` (`C47I`) | `<4sHHIII>`: signature, version, mission count, texture-record count, string-table byte count, string-table offset. Each `<9I>` mission record is name offset, scene/collision/navigation/script/PCM byte sizes, object count, cell count, and model count. |
| `tex/index.bin` (`C47T`) | `<4sHHIII>`: signature, version, reserved, texture count, string-table offset and size. Each `<9I>` record is mission index, source texture ID, width, height, GPU encoding (1 RGB565, 2 RGBA5551, 3 RGBA4), source kind (1 base texture, 2 lightmap), asset-path offset, source-name offset, and source encoding (1 PALN, 2 RGBA). |
| `*.c47m` (`C47M`) | `<4sHHIIII14f>`: signature, version, header size, vertex/index/batch counts, sanitized-UV count, then position origin/scale, base-UV origin/scale, and lightmap-UV origin/scale (three, three, two, two, two, two floats). Each `<6I>` batch stores base texture ID, lightmap ID, first index, index count, first vertex and original face flags; `0xFFFFFFFF` means no texture. Each `<3h4H4B>` vertex stores quantized XYZ, base UV, lightmap UV and RGBA color; 16-bit indices follow. |
| `scene/*.c47s` (`C47S`) | `<4sHHIIIIIfIIIIII>` header followed by `<8I16f6f>` objects, `<ii6fII>` cells, `uint32` cell object references, then UTF-8 strings. Objects store ID/type/source flags/model/cell/name offset/scene flags/raw source color, row-major transform, and bounds. Each cell stores X/Z, bounds, and a reference range. |
| `scene/*.c47c` (`C47C`) | `<4sHHII>` header with record size/count/CRC32, then `<II16f6f>` candidate collision instances (object/model IDs, transform and bounds). |
| `scene/*.c47n` (`C47N`) and `*.c47sc` (`C47B`) | `<4sHHIIIII>` header, `<4I>` named payload records (string offset, data offset, size, CRC32), string table and raw payloads. Navigation payloads are Pathfinder data; script payloads are SDL source, not compiled bytecode. |
| `audio/*.c47a` (`C47A`) | `<4sHHII>` header followed by `<5I>` mono PCM track records (sample rate, sample count, byte offset, byte size, CRC32). The matching `.pcm` file contains signed 16-bit little-endian samples; track payloads are aligned to four bytes. |

Models retain the source XYZ coordinate system and store per-model quantization
ranges; they do not yet convert axes or fuse object instances. Base and
lightmap UVs are separate 16-bit attributes. Vertex colors are white in shared
mesh buffers; the source's per-object packed color is retained in each scene
object. Textures are resized to powers of two from 8 to 1024 pixels and
compiled by `tex3ds`. Texture lookup follows the source editor: base references
are resolved from PAL first and fall back to LGT, while lightmap references use
LGT. References absent from both applicable packs are listed in
`unresolved_texture_references` in the report and have no emitted texture; for
example C1_1 references lightmap ID 24, which is absent from its LGT pack. The
renderer must tolerate unresolved texture references.

Collision records currently reuse `PROT` render meshes as approximate
candidates, not verified collision surfaces. Pathfinder and SDL data are
preserved as indexed raw blocks; the SDL virtual machine is not implemented.
PCLP clipping objects are retained in the scene but excluded from collision
candidates. Runtime rendering and loading are not part of the phase-2
converter; the phase-3 scaffold is described below.

## Phase 3: New 3DS preview application

The first native application scaffold is in `source/`, with its PICA200
vertex shader in `shaders/vshader.v.pica`; `romfs/` is reserved for
application-owned resources. Build it from the project root in
the devkitPro MSYS2 shell with `make`; the Makefile keeps generated object
files in `build-phase3/` so it does not overwrite the phase-1/phase-2 output
directory. It produces `hitman47.3dsx` at the project root.

Copy `build/phase2.c47p` to `sdmc:/3ds/hitman47/data.c47p` (or, alternatively,
extract the package to `sdmc:/3ds/hitman47/data/`), then launch the 3DSX. The
current preview reads `mesh/C1_1/00000.c47m`, resolves its first material
through the C47I/C47T indexes, imports its T3X file, and draws the rotating
mesh on the top screen. Arrow keys rotate the model, START exits, and the
bottom-screen console reports the mission and FPS. New 3DS CPU acceleration
is enabled when the APT service reports a New 3DS; startup errors are written
to `sdmc:/3ds/hitman47/log.txt`.

This is a buildable preview scaffold, not the phase-3 completion criterion:
it has not yet been run on a New 3DS or emulator, and therefore the rendered
model, texture orientation, GPU memory use, controls, FPS display and log-file
behavior still require runtime verification. The app uses the console for the
bottom display; full scene loading, camera controls and the 30 FPS mission
target are phase 4.

## Verified phase-1 results and remaining gaps

- All 13 primary mission archives passed independent chunk, pack-repeat,
  palette/DXT mip, audio, Pathfinder, scene-object, mesh-index, and UV-buffer
  bounds checks.
- A budget report records objects, renderable instances, unique meshes,
  unique and instanced triangles, texture counts and RGBA memory estimate,
  DXT formats and mismatched resolutions, audio bytes/duration, Pathfinder
  counts, invalid UV counts, script counts, and pack byte sizes for all
  13 missions.
- `C1_1` has an independent OBJ/MTL/PNG export. Parse/export and data counts
  are checked, and the community editor source independently corroborates
  the chunk tags, mesh fields, UV flags, and triangulation.
- The C1_1 export was visually sanity-checked using a generated top-down
  geometry preview at `build/phase1/C1_1/preview-top.png`. This is a
  texture-average-color projection, not a full 3D render or a c47edit
  comparison; loading the OBJ in Blender/c47edit is still unverified.
- Audio sample decoding is implemented and checked against FFmpeg as
  described above. All remaining DBL types and non-GOG/corrupt-file variants
  are outside this phase; these notes do not claim a complete specification
  of every game format.
