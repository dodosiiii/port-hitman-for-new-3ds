#!/usr/bin/env python3
"""Diagnostic : explique pourquoi des murs sont blancs, des textures absentes
ou des personnages ecrases. Lit le jeu (comme c47conv.py), ne modifie rien.

Usage (depuis la racine du projet) :
    py c47diag.py "C:\\...\\Hitman Codename 47" --mission C1_1 > diag.txt
    py c47diag.py "C:\\...\\Hitman Codename 47" --all > diag.txt
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.c47_phase1 import FormatError, _mission_archives, read_scene, texture_rgba

UV_LIMIT = 1024.0


def _extent(points) -> float:
    if not points:
        return 0.0
    return max(
        max(p[a] for p in points) - min(p[a] for p in points) for a in range(3)
    )


def _mesh_faces(mesh):
    """(textured, lightmap_only, untextured, both) pour un maillage."""
    textured = lightmap_only = untextured = both = 0
    for face in mesh["ftx_faces"] or []:
        has_tex = bool(face[0] & 0x0030)
        has_light = bool(face[0] & 0x00C0) and face[3] != 0xFFFF
        if has_tex and has_light:
            both += 1
        elif has_tex:
            textured += 1
        elif has_light:
            lightmap_only += 1
        else:
            untextured += 1
    if not mesh["ftx_faces"]:
        untextured += len(mesh["triangles"]) // 3 + len(mesh["quads"]) // 4
    return textured, lightmap_only, untextured, both


def build_report(scene: dict, mission: str) -> list[str]:
    out: list[str] = []
    add = out.append
    objects = scene["objects"]
    meshes = scene["meshes"]
    textures = scene["textures"]
    light_textures = scene["light_textures"]

    add(f"=== DIAGNOSTIC {mission} ===")
    add(f"objets: {len(objects)}  (avec maillage: {sum(1 for o in objects if o['mesh'])})")
    add(f"maillages uniques: {len(meshes)}")

    # ---- 1) textures ------------------------------------------------------
    add("")
    add("--- TEXTURES ---")
    base_refs: set[int] = set()
    light_refs: set[int] = set()
    for mesh in meshes:
        for face in mesh["ftx_faces"] or []:
            if face[0] & 0x0030:
                base_refs.add(face[2])
            if face[0] & 0x00C0 and face[3] != 0xFFFF:
                light_refs.add(face[3])
    unresolved_base = sorted(
        t for t in base_refs if t not in textures and t not in light_textures
    )
    unresolved_light = sorted(t for t in light_refs if t not in light_textures)
    add(f"textures de base referencees: {len(base_refs)}  -> INTROUVABLES: {len(unresolved_base)}"
        + (f"  ids {unresolved_base[:15]}" if unresolved_base else ""))
    add(f"lightmaps referencees: {len(light_refs)}  -> INTROUVABLES: {len(unresolved_light)}"
        + (f"  ids {unresolved_light[:15]}" if unresolved_light else ""))
    base_only_in_light = sorted(
        t for t in base_refs if t not in textures and t in light_textures
    )
    if base_only_in_light:
        add(f"textures de base qui n'existent que dans les lightmaps: {len(base_only_in_light)}")

    transparent = []
    big = []
    for pool in (textures, light_textures):
        for tex in pool.values():
            try:
                alpha = set(texture_rgba(tex)[3::4])
            except Exception:
                continue
            if alpha == {0}:
                transparent.append(tex["name"])
            if max(int(tex["width"]), int(tex["height"])) > 512:
                big.append((tex["name"], tex["width"], tex["height"]))
    add(f"textures ENTIEREMENT transparentes (alpha=0 partout): {len(transparent)}"
        + (f"  ex: {transparent[:5]}" if transparent else ""))
    add(f"textures > 512 px: {len(big)}" + (f"  ex: {big[:3]}" if big else ""))

    # ---- 2) faces ---------------------------------------------------------
    add("")
    add("--- FACES (instances dans la scene) ---")
    totals = Counter()
    untextured_by_name: Counter = Counter()
    untextured_colors: Counter = Counter()
    for item in objects:
        mesh = item["mesh"]
        if mesh is None:
            continue
        tex, lonly, none, both = _mesh_faces(mesh)
        totals["texturees"] += tex
        totals["texture+lightmap"] += both
        totals["lightmap seule"] += lonly
        totals["SANS texture ni lightmap (blanches)"] += none
        if none:
            key = f"type {item['type']}  {item['path'][-1] if item['path'] else '?'}"
            untextured_by_name[key] += none
            untextured_colors[f"0x{int(item.get('color', 0xFFFFFFFF)) & 0xFFFFFFFF:08X}"] += none
    for label, value in totals.items():
        add(f"{label:40s} {value}")
    if untextured_by_name:
        add("objets avec le plus de faces blanches:")
        for name, count in untextured_by_name.most_common(12):
            add(f"   {count:7d}  {name}")
        add("valeurs du champ 'couleur' de ces objets (si variees = vraie couleur):")
        for value, count in untextured_colors.most_common(6):
            add(f"   {count:7d}  {value}")

    # ---- 3) maillages suspects -------------------------------------------
    add("")
    add("--- MAILLAGES SUSPECTS ---")
    inflated = []
    bad_uv = []
    for index, mesh in enumerate(meshes):
        vertices = mesh["vertices"]
        if not vertices:
            continue
        used = {
            i >> 1
            for i in (*mesh["triangles"], *mesh["quads"])
            if (i >> 1) < len(vertices)
        }
        if used:
            all_ext = _extent(vertices)
            used_ext = _extent([vertices[i] for i in used])
            if used_ext > 0 and all_ext / used_ext >= 4.0:
                inflated.append((all_ext / used_ext, index, len(vertices), len(used)))
        uvs = [*mesh["uv_data"], *mesh.get("light_uv_data", [])]
        worst = max((abs(v) for v in uvs if math.isfinite(v)), default=0.0)
        nonfinite = sum(1 for v in uvs if not math.isfinite(v))
        if worst > UV_LIMIT or nonfinite:
            bad_uv.append((worst, nonfinite, index))
    inflated.sort(reverse=True)
    bad_uv.sort(reverse=True)
    add(f"maillages dont la boite est gonflee par des sommets inutilises (x4 ou plus): {len(inflated)}")
    for ratio, index, total, used in inflated[:8]:
        add(f"   maillage {index}: boite x{ratio:.0f} trop grande ({used}/{total} sommets utilises)")
    add(f"maillages avec UV absurdes ou non finies: {len(bad_uv)}")
    for worst, nonfinite, index in bad_uv[:8]:
        add(f"   maillage {index}: UV max {worst:.3g}, non finies: {nonfinite}")

    # ---- 4) types d'objets + personnages ---------------------------------
    add("")
    add("--- TYPES D'OBJETS ---")
    by_type: dict[int, list] = defaultdict(list)
    for item in objects:
        by_type[int(item["type"])].append(item)
    for object_type, items in sorted(by_type.items(), key=lambda kv: -len(kv[1]))[:20]:
        with_mesh = sum(1 for i in items if i["mesh"])
        depth = max(len(i["path"]) for i in items)
        names = ", ".join(sorted({i["name"] for i in items})[:3])
        add(f"type {object_type:4d}: {len(items):5d} objets ({with_mesh:5d} avec maillage) "
            f"profondeur max {depth}  ex: {names}")

    add("")
    add("--- PIECES EMPILEES AU MEME POINT (personnages 'en boule' ?) ---")
    groups: dict[tuple, list] = defaultdict(list)
    for item in objects:
        if item["mesh"] is None:
            continue
        matrix = item["matrix"]
        groups[tuple(round(v, 1) for v in matrix[3][:3])].append(item)
    stacked = [(pos, items) for pos, items in groups.items() if len(items) >= 5]
    stacked.sort(key=lambda kv: -len(kv[1]))
    add(f"points ou >= 5 objets a maillage sont superposes: {len(stacked)}")
    for pos, items in stacked[:10]:
        parent = " / ".join(items[0]["path"][:-1][-2:]) or "(racine)"
        types = sorted({int(i["type"]) for i in items})
        add(f"   {len(items):3d} objets en {pos}  types {types}  parent: {parent}")
        add(f"        ex: {', '.join(sorted({i['name'] for i in items})[:5])}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("game_root", type=Path, help="dossier d'installation du jeu")
    parser.add_argument("--mission", default="C1_1", help="mission a analyser (defaut C1_1)")
    parser.add_argument("--all", action="store_true", help="analyser toutes les missions")
    arguments = parser.parse_args()
    try:
        root = arguments.game_root.resolve()
        archives = _mission_archives(root)
        if arguments.all:
            matches = archives
        else:
            matches = [
                p for p in archives
                if p.stem.casefold() == arguments.mission.casefold()
            ]
        if not matches:
            raise FormatError(f"mission {arguments.mission!r} introuvable sous {root}")
        failed = 0
        for archive in matches:
            try:
                scene = read_scene(archive, root)
                print("\n".join(build_report(scene, archive.stem)))
            except (FormatError, OSError, ValueError, KeyError, IndexError) as error:
                # Une mission illisible ne doit pas arreter les autres
                failed += 1
                print(f"=== DIAGNOSTIC {archive.stem} ===\nERREUR: {error}")
            print("\n" + "=" * 60 + "\n")
            print(f"[{archive.stem} termine]", file=sys.stderr)
        if failed:
            print(f"{failed} mission(s) illisible(s)", file=sys.stderr)
    except (FormatError, OSError) as error:
        print(f"erreur: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
