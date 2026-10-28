#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hitman: Codename 47 -> New 3DS : outil tout-en-un (interface graphique).
 
Remplace c47conv.py (lanceur), c47pack.py et c47diag.py :
    - Convertir    : jeu -> dossier de conversion (modeles, textures, scenes, audio)
    - Empaqueter   : dossier de conversion -> data.c47p (+ copie sur la carte SD)
    - Diagnostiquer: explique les textures blanches / modeles ecrases
 
Lancement :  python c47tool.py   (le dossier "tools" doit rester a cote)
"""
 
from __future__ import annotations
 
import json
import math
import os
import queue
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter, defaultdict
from pathlib import Path
 
if sys.stdout is None:  # lancement via pythonw : pas de console
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")
 
APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
 
if sys.platform.startswith("win"):
    try:  # texte net sur les ecrans haute densite
        import ctypes
 
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
 
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    print("tkinter est introuvable : reinstalle Python en cochant 'tcl/tk and IDLE'.")
    raise SystemExit(1)
 
try:
    from tools import c47_phase1 as p1
    from tools import c47conv as conv
except ImportError as error:  # pragma: no cover - message pour l'utilisateur
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(
        "Dossier 'tools' introuvable",
        "Le dossier 'tools' (avec c47conv.py et c47_phase1.py) doit se trouver "
        f"a cote de c47tool.py.\n\nDetail : {error}",
    )
    raise SystemExit(1)
 
SETTINGS_FILE = APP_DIR / "c47tool.json"
 
# ------------------------------------------------------------------ palette
BG = "#0e1014"
CARD = "#161920"
CARD2 = "#1e222b"
BORDER = "#2a2f3a"
FG = "#e8eaf0"
MUTED = "#8a90a2"
ACCENT = "#e5232e"
ACCENT_HOVER = "#ff3a46"
OK = "#3ddc84"
WARN = "#f5b942"
ERR = "#ff6b6b"
 
FONT = "Segoe UI"
MONO = "Consolas"
 
 
class Cancelled(Exception):
    """Operation annulee par l'utilisateur."""
 
 
class UserError(Exception):
    """Erreur de saisie : affichee telle quelle (sans trace technique)."""
 
 
# =========================================================================
# Empaquetage (ex c47pack.py)
# =========================================================================
PACK_VERSION = 1
PACK_HEADER = struct.Struct("<4sHHIIII")
PACK_ENTRY = struct.Struct("<III")
MAX_ARCHIVE_SIZE = 0x7FFFFFFF
 
 
def _pack_entries(source: Path) -> list[tuple[str, Path, int]]:
    if not source.is_dir():
        raise UserError(f"Le dossier de conversion n'existe pas :\n{source}")
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
            raise UserError(f"Chemin invalide dans le paquet : {relative_path!r}")
        files.append((relative_path, path, path.stat().st_size))
    files.sort(key=lambda item: item[0])
    names = {name for name, _, _ in files}
    for required in ("index.bin", "tex/index.bin"):
        if required not in names:
            raise UserError(
                f"Ce dossier n'est pas une conversion complète (il manque {required}).\n"
                "Lance d'abord l'onglet « Convertir »."
            )
    return files
 
 
def build_archive(source: Path, output: Path, ctx: "Ctx") -> tuple[int, int]:
    source = source.resolve()
    output = output.resolve()
    try:
        output.relative_to(source)
    except ValueError:
        pass
    else:
        raise UserError("Le fichier .c47p doit être en dehors du dossier de conversion.")
 
    ctx.progress(0.0, "Lecture de la liste des fichiers…")
    files = _pack_entries(source)
    total_bytes = max(1, sum(size for _, _, size in files))
 
    strings = bytearray()
    records: list[tuple[int, int, int]] = []
    for name, _, size in files:
        encoded_name = name.encode("utf-8") + b"\0"
        records.append((len(strings), 0, size))
        strings.extend(encoded_name)
    data_offset = PACK_HEADER.size + len(files) * PACK_ENTRY.size + len(strings)
    for index, (name, _, size) in enumerate(files):
        path_offset, _, _ = records[index]
        if data_offset > MAX_ARCHIVE_SIZE or size > MAX_ARCHIVE_SIZE - data_offset:
            raise UserError("Le paquet dépasse 2 Go : la 3DS ne peut pas le lire. "
                            "Convertis moins de missions.")
        records[index] = (path_offset, data_offset, size)
        data_offset += size
 
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{output.name}.", suffix=".tmp",
            dir=output.parent, delete=False,
        ) as archive:
            temporary_path = Path(archive.name)
            archive.write(
                PACK_HEADER.pack(
                    b"C47P", PACK_VERSION, 0, len(files), PACK_HEADER.size,
                    PACK_HEADER.size + len(files) * PACK_ENTRY.size, len(strings),
                )
            )
            for record in records:
                archive.write(PACK_ENTRY.pack(*record))
            archive.write(strings)
 
            written = 0
            for position, ((name, path, expected_size), record) in enumerate(zip(files, records)):
                if ctx.cancelled():
                    raise Cancelled()
                if archive.tell() != record[1]:
                    raise OSError(f"Décalage inattendu en écrivant {path}")
                if position % 20 == 0 or expected_size > 1_000_000:
                    ctx.progress(
                        0.96 * written / total_bytes,
                        f"Fichier {position + 1}/{len(files)} — {name}",
                    )
                copied = 0
                with path.open("rb") as source_file:
                    while True:
                        chunk = source_file.read(4 * 1024 * 1024)
                        if not chunk:
                            break
                        archive.write(chunk)
                        copied += len(chunk)
                if copied != expected_size:
                    raise OSError(f"Le fichier a changé pendant l'empaquetage : {path}")
                written += copied
 
        os.replace(temporary_path, output)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
 
    ctx.progress(0.97, "Vérification de l'archive…")
    count, size = validate_archive(output)
    if (count, size) != (len(files), output.stat().st_size):
        raise OSError("La vérification de l'archive a donné un résultat incohérent.")
    return count, size
 
 
def validate_archive(archive_path: Path) -> tuple[int, int]:
    with archive_path.open("rb") as archive:
        archive.seek(0, os.SEEK_END)
        archive_size = archive.tell()
        archive.seek(0)
        header_data = archive.read(PACK_HEADER.size)
        if len(header_data) != PACK_HEADER.size:
            raise ValueError("En-tête d'archive tronqué")
        signature, version, reserved, count, records_offset, strings_offset, strings_size = (
            PACK_HEADER.unpack(header_data)
        )
        records_end = PACK_HEADER.size + count * PACK_ENTRY.size
        if (
            signature != b"C47P" or version != PACK_VERSION or reserved != 0
            or records_offset != PACK_HEADER.size or strings_offset != records_end
            or strings_offset + strings_size > archive_size
        ):
            raise ValueError("En-tête ou tables de l'archive invalides")
        records_data = archive.read(count * PACK_ENTRY.size)
        strings = archive.read(strings_size)
        if len(records_data) != count * PACK_ENTRY.size or len(strings) != strings_size:
            raise ValueError("Index de l'archive tronqué")
        previous_name: str | None = None
        data_start = strings_offset + strings_size
        for index in range(count):
            path_offset, data_offset, data_size = PACK_ENTRY.unpack_from(
                records_data, index * PACK_ENTRY.size
            )
            if path_offset >= strings_size or data_offset < data_start or (
                data_offset > archive_size or data_size > archive_size - data_offset
            ):
                raise ValueError(f"Entrée {index} hors limites")
            terminator = strings.find(b"\0", path_offset)
            if terminator < 0:
                raise ValueError(f"Chemin non terminé (entrée {index})")
            name = strings[path_offset:terminator].decode("utf-8")
            if not name or (previous_name is not None and name <= previous_name):
                raise ValueError(f"Chemins non triés ou invalides (entrée {index})")
            previous_name = name
    return count, archive_size
 
 
# =========================================================================
# Diagnostic (ex c47diag.py)
# =========================================================================
UV_LIMIT = 1024.0
 
 
def _extent(points) -> float:
    if not points:
        return 0.0
    return max(max(p[a] for p in points) - min(p[a] for p in points) for a in range(3))
 
 
def _mesh_faces(mesh):
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
    unresolved_base = sorted(t for t in base_refs if t not in textures and t not in light_textures)
    unresolved_light = sorted(t for t in light_refs if t not in light_textures)
    add(f"textures de base referencees: {len(base_refs)}  -> INTROUVABLES: {len(unresolved_base)}"
        + (f"  ids {unresolved_base[:15]}" if unresolved_base else ""))
    add(f"lightmaps referencees: {len(light_refs)}  -> INTROUVABLES: {len(unresolved_light)}"
        + (f"  ids {unresolved_light[:15]}" if unresolved_light else ""))
    base_only_in_light = sorted(t for t in base_refs if t not in textures and t in light_textures)
    if base_only_in_light:
        add(f"textures de base qui n'existent que dans les lightmaps: {len(base_only_in_light)}")
 
    transparent = []
    big = []
    for pool in (textures, light_textures):
        for tex in pool.values():
            try:
                alpha = set(p1.texture_rgba(tex)[3::4])
            except Exception:
                continue
            if alpha == {0}:
                transparent.append(tex["name"])
            if max(int(tex["width"]), int(tex["height"])) > 512:
                big.append((tex["name"], tex["width"], tex["height"]))
    add(f"textures ENTIEREMENT transparentes (alpha=0 partout): {len(transparent)}"
        + (f"  ex: {transparent[:5]}" if transparent else ""))
    add(f"textures > 512 px: {len(big)}" + (f"  ex: {big[:3]}" if big else ""))
 
    add("")
    add("--- FACES (instances dans la scene) ---")
    totals: Counter = Counter()
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
 
    add("")
    add("--- MAILLAGES SUSPECTS ---")
    inflated = []
    bad_uv = []
    for index, mesh in enumerate(meshes):
        vertices = mesh["vertices"]
        if not vertices:
            continue
        used = {i >> 1 for i in (*mesh["triangles"], *mesh["quads"]) if (i >> 1) < len(vertices)}
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
        groups[tuple(round(v, 1) for v in item["matrix"][3][:3])].append(item)
    stacked = [(pos, items) for pos, items in groups.items() if len(items) >= 5]
    stacked.sort(key=lambda kv: -len(kv[1]))
    add(f"points ou >= 5 objets a maillage sont superposes: {len(stacked)}")
    for pos, items in stacked[:10]:
        parent = " / ".join(items[0]["path"][:-1][-2:]) or "(racine)"
        types = sorted({int(i["type"]) for i in items})
        add(f"   {len(items):3d} objets en {pos}  types {types}  parent: {parent}")
        add(f"        ex: {', '.join(sorted({i['name'] for i in items})[:5])}")
    return out
 
 
# =========================================================================
# Taches (executees dans un thread)
# =========================================================================
class Ctx:
    """Passerelle thread -> interface."""
 
    def __init__(self, panel: "TaskPanel"):
        self._panel = panel
        self._cancel = panel.cancel_event
 
    def progress(self, fraction: float, text: str) -> None:
        with self._panel.lock:
            self._panel.latest = (max(0.0, min(1.0, fraction)), text)
 
    def log(self, text: str, tag: str | None = None) -> None:
        self._panel.queue.put(("log", text, tag))
 
    def cancelled(self) -> bool:
        return self._cancel.is_set()
 
 
def _fmt_size(size: float) -> str:
    for unit in ("o", "Ko", "Mo", "Go"):
        if size < 1024 or unit == "Go":
            return f"{size:.0f} {unit}" if unit == "o" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} Go"
 
 
def _need_game(settings: dict) -> Path:
    game = settings["game"]
    if not game or not game.is_dir():
        raise UserError("Choisis d'abord le dossier du jeu (Hitman Codename 47).")
    return game
 
 
def run_convert(ctx: Ctx, s: dict) -> dict:
    game = _need_game(s)
    out: Path = s["out"]
    if not str(out).strip() or str(out) == ".":
        raise UserError("Choisis un dossier de sortie pour la conversion.")
 
    missions = s["missions"]
    if missions is None:
        missions = [path.stem for path in p1._mission_archives(game)]
    if not missions:
        raise UserError("Aucune mission trouvée dans le dossier du jeu.")
 
    if s["clean"] and out.exists():
        looks_like_conversion = (out / "index.bin").is_file()
        if looks_like_conversion or not any(out.iterdir()):
            ctx.progress(0.0, "Suppression de l'ancienne conversion…")
            shutil.rmtree(out, ignore_errors=True)
        else:
            raise UserError(
                "Le dossier de sortie n'est pas vide et ne ressemble pas à une conversion "
                "précédente.\nChoisis un dossier vide, ou décoche « Supprimer l'ancienne "
                "conversion »."
            )
 
    ctx.log(f"Conversion de {len(missions)} mission(s) : {', '.join(missions)}", "head")
    try:
        report = conv.convert_package(
            game, out, missions, s["tex3ds"],
            progress=ctx.progress, cancelled=ctx.cancelled,
        )
    except conv.ConversionCancelled:
        raise Cancelled()
 
    for mission in report["missions"]:
        ctx.log(
            f"✔ {mission['name']:6s} {mission['object_count']:6d} objets · "
            f"{mission['model_count']:5d} modèles · {mission['texture_count']:5d} textures",
            "ok",
        )
        unresolved = len(mission["unresolved_texture_references"])
        if unresolved:
            ctx.log(f"    ⚠ {unresolved} texture(s) introuvable(s) dans le jeu (resteront blanches)", "warn")
    totals = report["totals"]
    ctx.log("")
    ctx.log(
        f"Total : {totals['mission_count']} missions · {totals['model_count']} modèles · "
        f"{totals['texture_count']} textures",
        "head",
    )
    ctx.log("Étape suivante : onglet « Empaqueter ».", "muted")
    return {"summary": f"{len(missions)} mission(s) converties", "open": out}
 
 
def run_pack(ctx: Ctx, s: dict) -> dict:
    source: Path = s["out"]
    output: Path = s["pack"]
    if not str(output).strip() or output.suffix.lower() != ".c47p":
        raise UserError("Le fichier du paquet doit se terminer par .c47p (ex : data.c47p).")
    ctx.log(f"Empaquetage de : {source}", "head")
    count, size = build_archive(source, output, ctx)
    ctx.progress(1.0, "Terminé")
    ctx.log(f"✔ {count} fichiers · {_fmt_size(size)}", "ok")
    ctx.log(f"Fichier : {output}", "muted")
    ctx.log("Copie ce fichier dans  carte SD:/3ds/hitman47/  (bouton ci-dessus).", "muted")
    return {"summary": f"{count} fichiers · {_fmt_size(size)}", "open": output.parent}
 
 
def run_copy_sd(ctx: Ctx, s: dict) -> dict:
    source: Path = s["pack"]
    destination: Path = s["sd_destination"]
    if not source.is_file():
        raise UserError("Le fichier data.c47p n'existe pas encore : lance « Empaqueter » d'abord.")
    total = source.stat().st_size
    destination.parent.mkdir(parents=True, exist_ok=True)
    ctx.log(f"Copie vers {destination}", "head")
    started = time.time()
    copied = 0
    with source.open("rb") as src, destination.open("wb") as dst:
        while True:
            if ctx.cancelled():
                dst.close()
                destination.unlink(missing_ok=True)
                raise Cancelled()
            chunk = src.read(4 * 1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
            copied += len(chunk)
            speed = copied / max(0.1, time.time() - started)
            ctx.progress(copied / max(1, total),
                         f"Copie sur la carte SD — {_fmt_size(copied)} / {_fmt_size(total)} "
                         f"({_fmt_size(speed)}/s)")
        dst.flush()
        os.fsync(dst.fileno())
    if destination.stat().st_size != total:
        raise OSError("La copie est incomplète (taille différente). Vérifie la carte SD.")
    ctx.progress(1.0, "Terminé")
    ctx.log(f"✔ Copié : {_fmt_size(total)}", "ok")
    ctx.log("Éjecte la carte SD proprement avant de la retirer de l'ordinateur.", "muted")
    return {"summary": "copié sur la carte SD", "open": destination.parent}
 
 
def run_diag(ctx: Ctx, s: dict) -> dict:
    game = _need_game(s)
    archives = p1._mission_archives(game)
    if s["missions"] is not None:
        wanted = {name.casefold() for name in s["missions"]}
        archives = [a for a in archives if a.stem.casefold() in wanted]
    if not archives:
        raise UserError("Aucune mission à analyser.")
 
    lines: list[str] = []
    failed = 0
    for index, archive in enumerate(archives):
        if ctx.cancelled():
            raise Cancelled()
        ctx.progress(index / len(archives),
                     f"Analyse de {archive.stem} ({index + 1}/{len(archives)}) — lecture de l'archive…")
        try:
            scene = p1.read_scene(archive, game)
            ctx.progress((index + 0.7) / len(archives),
                         f"Analyse de {archive.stem} ({index + 1}/{len(archives)}) — calcul du rapport…")
            report = build_report(scene, archive.stem)
            ctx.log(f"✔ {archive.stem} analysée", "ok")
        except Exception as error:  # une mission illisible ne bloque pas les autres
            failed += 1
            report = [f"=== DIAGNOSTIC {archive.stem} ===", f"ERREUR: {error}"]
            ctx.log(f"✖ {archive.stem} : {error}", "err")
        lines.extend(report)
        lines.extend(["", "=" * 60, ""])
 
    target = APP_DIR / "diag.txt"
    target.write_text("\n".join(lines), encoding="utf-8")
    ctx.progress(1.0, "Terminé")
    ctx.log("")
    ctx.log(f"Rapport complet enregistré : {target}", "head")
    if failed:
        ctx.log(f"{failed} mission(s) illisible(s).", "warn")
    ctx.log("Envoie le contenu de diag.txt pour qu'on cherche la cause.", "muted")
    return {"summary": f"{len(archives)} mission(s) analysée(s)", "open": target}
 
 
# =========================================================================
# Interface
# =========================================================================
class TaskPanel(ttk.Frame):
    def __init__(self, app: "App", parent, title: str, blurb: str,
                 start_text: str, runner, done_text: str):
        super().__init__(parent, style="Card.TFrame", padding=(22, 18, 22, 16))
        self.app = app
        self.runner = runner
        self.done_text = done_text
        self.queue: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.latest: tuple[float, str] | None = None
        self.cancel_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.started_at = 0.0
        self.result: dict | None = None
 
        ttk.Label(self, text=title, style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(self, text=blurb, style="CardMuted.TLabel", wraplength=640,
                  justify="left").pack(anchor="w", pady=(2, 14))
 
        buttons = ttk.Frame(self, style="Card.TFrame")
        buttons.pack(fill="x")
        self.start_button = ttk.Button(buttons, text=start_text, style="Accent.TButton",
                                       command=self.start)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(buttons, text="Annuler", command=self.cancel,
                                        state="disabled")
        self.cancel_button.pack(side="left", padx=(10, 0))
        self.extra_frame = ttk.Frame(buttons, style="Card.TFrame")
        self.extra_frame.pack(side="right")
        self.open_button = ttk.Button(self.extra_frame, text="Ouvrir le résultat",
                                      command=self.open_result, state="disabled")
        self.open_button.pack(side="right")
 
        top = ttk.Frame(self, style="Card.TFrame")
        top.pack(fill="x", pady=(18, 4))
        self.percent_var = tk.StringVar(value="0 %")
        ttk.Label(top, textvariable=self.percent_var, style="Percent.TLabel").pack(side="left")
        self.time_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.time_var, style="CardMuted.TLabel").pack(side="right", anchor="s")
 
        self.bar = ttk.Progressbar(self, style="Hitman.Horizontal.TProgressbar",
                                   maximum=1000, mode="determinate")
        self.bar.pack(fill="x", pady=(0, 8))
        self.status_var = tk.StringVar(value="Prêt.")
        self.status_label = ttk.Label(self, textvariable=self.status_var, style="Status.TLabel",
                                      wraplength=700, justify="left")
        self.status_label.pack(anchor="w")
 
        log_frame = tk.Frame(self, bg=BORDER, padx=1, pady=1)
        log_frame.pack(fill="both", expand=True, pady=(14, 0))
        inner = tk.Frame(log_frame, bg="#0b0d11")
        inner.pack(fill="both", expand=True)
        self.log_text = tk.Text(inner, height=8, bg="#0b0d11", fg="#c8ccd8", relief="flat",
                                borderwidth=0, highlightthickness=0, wrap="word",
                                font=(MONO, 9), padx=12, pady=10, state="disabled",
                                insertbackground=FG, selectbackground=ACCENT)
        scrollbar = ttk.Scrollbar(inner, orient="vertical", command=self.log_text.yview,
                                  style="Dark.Vertical.TScrollbar")
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        self.log_text.tag_configure("ok", foreground=OK)
        self.log_text.tag_configure("warn", foreground=WARN)
        self.log_text.tag_configure("err", foreground=ERR)
        self.log_text.tag_configure("muted", foreground=MUTED)
        self.log_text.tag_configure("head", foreground=FG, font=(MONO, 9, "bold"))
 
    # ---- journal ---------------------------------------------------------
    def write_log(self, text: str, tag: str | None = None) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n", tag or ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
 
    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
 
    # ---- cycle de vie ----------------------------------------------------
    def start(self, runner=None, extra_settings: dict | None = None, label: str | None = None) -> None:
        if self.app.busy:
            return
        try:
            settings = self.app.collect()
        except UserError as error:
            messagebox.showwarning("Il manque quelque chose", str(error), parent=self.app)
            return
        if extra_settings:
            settings.update(extra_settings)
        self.app.save_settings()
        self.clear_log()
        self.cancel_event.clear()
        self.result = None
        self.latest = None
        self.queue = queue.Queue()
        self.started_at = time.time()
        self.percent_var.set("0 %")
        self.bar["value"] = 0
        self.time_var.set("")
        self.status_var.set(label or "Démarrage…")
        self.status_label.configure(style="Status.TLabel")
        self.open_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.app.set_busy(True)
        task = runner or self.runner
        ctx = Ctx(self)
 
        def work() -> None:
            try:
                result = task(ctx, settings)
                self.queue.put(("done", result))
            except Cancelled:
                self.queue.put(("cancelled",))
            except UserError as error:
                self.queue.put(("usererror", str(error)))
            except Exception as error:
                self.queue.put(("error", f"{type(error).__name__}: {error}", traceback.format_exc()))
 
        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()
        self.after(80, self._poll)
 
    def cancel(self) -> None:
        if self.thread and self.thread.is_alive():
            self.cancel_event.set()
            self.cancel_button.configure(state="disabled")
            self.status_var.set("Annulation en cours… (l'étape actuelle doit se terminer)")
 
    def open_result(self) -> None:
        if self.result and self.result.get("open"):
            open_path(self.result["open"])
 
    def _show_progress(self) -> None:
        with self.lock:
            latest = self.latest
        if latest is None:
            return
        fraction, text = latest
        self.bar["value"] = fraction * 1000
        self.percent_var.set(f"{fraction * 100:.0f} %")
        if not self.cancel_event.is_set():
            self.status_var.set(text)
        elapsed = time.time() - self.started_at
        if fraction >= 0.03 and fraction < 1.0:
            remaining = elapsed * (1 - fraction) / fraction
            self.time_var.set(f"Écoulé {fmt_time(elapsed)}  ·  Reste environ {fmt_time(remaining)}")
        else:
            self.time_var.set(f"Écoulé {fmt_time(elapsed)}")
 
    def _poll(self) -> None:
        finished = False
        try:
            while True:
                message = self.queue.get_nowait()
                kind = message[0]
                if kind == "log":
                    self.write_log(message[1], message[2])
                elif kind == "done":
                    finished = True
                    self._finish_ok(message[1])
                elif kind == "cancelled":
                    finished = True
                    self._finish_text("Annulé.", WARN)
                    self.write_log("Opération annulée.", "warn")
                elif kind == "usererror":
                    finished = True
                    self._finish_text("Impossible de continuer.", ERR)
                    self.write_log(message[1], "err")
                    messagebox.showwarning("Impossible de continuer", message[1], parent=self.app)
                elif kind == "error":
                    finished = True
                    self._finish_text("Erreur.", ERR)
                    self.write_log(message[1], "err")
                    self.write_log(message[2], "muted")
                    self.app.log_error(message[2])
                    messagebox.showerror("Erreur", f"{message[1]}\n\nLe détail est dans le journal.",
                                         parent=self.app)
        except queue.Empty:
            pass
        if not finished:
            self._show_progress()
            if self.thread and self.thread.is_alive():
                self.after(80, self._poll)
            else:  # le thread s'est arrete sans message : on libere l'interface
                self.after(200, self._poll_last)
 
    def _poll_last(self) -> None:
        if not self.queue.empty():
            self._poll()
        else:
            self._finish_text("Terminé.", FG)
 
    def _finish_text(self, text: str, colour: str) -> None:
        self.status_var.set(text)
        self.status_label.configure(foreground=colour)
        self.cancel_button.configure(state="disabled")
        self.time_var.set(f"Durée {fmt_time(time.time() - self.started_at)}")
        self.app.set_busy(False)
 
    def _finish_ok(self, result: dict) -> None:
        self.result = result
        self.bar["value"] = 1000
        self.percent_var.set("100 %")
        self._finish_text(f"✔ {self.done_text} — {result['summary']}", OK)
        if result.get("open"):
            self.open_button.configure(state="normal")
        self.bell()
 
 
def fmt_time(seconds: float) -> str:
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"
 
 
def open_path(path: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            if path.is_file():
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path if path.is_dir() else path.parent)])
    except Exception:
        pass
 
 
class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Hitman 47 → 3DS · Outil de conversion")
        self.configure(bg=BG)
        self.minsize(980, 700)
        width, height = 1100, 780
        self.geometry(f"{width}x{height}+{max(0, (self.winfo_screenwidth() - width) // 2)}"
                      f"+{max(0, (self.winfo_screenheight() - height) // 3)}")
        self.busy = False
        self.mission_names: list[str] = []
        self.report_callback_exception = self._tk_error
        self._style()
        saved = self._load_settings()
 
        # ---- variables ----------------------------------------------------
        default_game = APP_DIR / "Hitman Codename 47"
        self.game_var = tk.StringVar(value=saved.get("game") or (str(default_game) if default_game.is_dir() else ""))
        self.out_var = tk.StringVar(value=saved.get("out") or str(APP_DIR / "convertion"))
        self.pack_var = tk.StringVar(value=saved.get("pack") or str(APP_DIR / "data.c47p"))
        self.tex3ds_var = tk.StringVar(value=saved.get("tex3ds") or "")
        self.all_var = tk.BooleanVar(value=saved.get("all", True))
        self.clean_var = tk.BooleanVar(value=saved.get("clean", True))
        self.saved_missions: list[str] = saved.get("missions", [])
 
        self._build_header()
        body = ttk.Frame(self, style="Root.TFrame")
        body.pack(fill="both", expand=True, padx=24, pady=(0, 20))
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        self._build_sidebar(body)
        self._build_main(body)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.refresh_missions()
 
    # ---- style -------------------------------------------------------------
    def _style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=FG, font=(FONT, 10), borderwidth=0,
                        focuscolor=BG)
        style.configure("Root.TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Brand.TLabel", background=BG, foreground=ACCENT, font=(FONT, 9, "bold"))
        style.configure("Title.TLabel", background=BG, foreground=FG, font=(FONT, 22, "bold"))
        style.configure("Sub.TLabel", background=BG, foreground=MUTED, font=(FONT, 10))
        style.configure("Card.TLabel", background=CARD, foreground=FG)
        style.configure("CardTitle.TLabel", background=CARD, foreground=FG, font=(FONT, 15, "bold"))
        style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED, font=(FONT, 9))
        style.configure("Field.TLabel", background=CARD, foreground=MUTED, font=(FONT, 9))
        style.configure("Section.TLabel", background=CARD, foreground=FG, font=(FONT, 11, "bold"))
        style.configure("Percent.TLabel", background=CARD, foreground=ACCENT, font=(FONT, 30, "bold"))
        style.configure("Status.TLabel", background=CARD, foreground=FG, font=(FONT, 10))
        style.configure("TEntry", fieldbackground=CARD2, foreground=FG, insertcolor=FG,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER, padding=7)
        style.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)],
                  darkcolor=[("focus", ACCENT)])
        style.configure("TButton", background=CARD2, foreground=FG, padding=(14, 8),
                        bordercolor=BORDER, lightcolor=CARD2, darkcolor=CARD2, focuscolor=CARD2)
        style.map("TButton", background=[("active", "#2c3240"), ("disabled", "#13161c")],
                  foreground=[("disabled", "#555b6b")])
        style.configure("Accent.TButton", background=ACCENT, foreground="white",
                        font=(FONT, 10, "bold"), padding=(20, 9), bordercolor=ACCENT,
                        lightcolor=ACCENT, darkcolor=ACCENT, focuscolor=ACCENT)
        style.map("Accent.TButton", background=[("active", ACCENT_HOVER), ("disabled", "#40222a")],
                  foreground=[("disabled", "#8d6a70")], bordercolor=[("disabled", "#40222a")],
                  lightcolor=[("disabled", "#40222a")], darkcolor=[("disabled", "#40222a")])
        style.configure("Small.TButton", padding=(10, 6))
        style.configure("TCheckbutton", background=CARD, foreground=FG, focuscolor=CARD,
                        indicatorbackground=CARD2, indicatorforeground=ACCENT)
        style.map("TCheckbutton", background=[("active", CARD)],
                  indicatorbackground=[("selected", ACCENT), ("active", CARD2)])
        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", background=BG, foreground=MUTED, padding=(22, 11),
                        font=(FONT, 10, "bold"), borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", CARD), ("active", CARD2)],
                  foreground=[("selected", FG), ("active", FG)])
        style.layout("Hitman.Horizontal.TProgressbar", [
            ("Horizontal.Progressbar.trough",
             {"children": [("Horizontal.Progressbar.pbar", {"side": "left", "sticky": "ns"})],
              "sticky": "nswe"})])
        style.configure("Hitman.Horizontal.TProgressbar", troughcolor=CARD2, background=ACCENT,
                        bordercolor=CARD2, lightcolor=ACCENT, darkcolor=ACCENT, thickness=20)
        style.configure("Dark.Vertical.TScrollbar", background=CARD2, troughcolor="#0b0d11",
                        bordercolor="#0b0d11", arrowcolor=MUTED, lightcolor=CARD2, darkcolor=CARD2)
        style.map("Dark.Vertical.TScrollbar", background=[("active", "#2c3240")])
 
    # ---- reglages ------------------------------------------------------------
    def _load_settings(self) -> dict:
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
 
    def save_settings(self) -> None:
        data = {
            "game": self.game_var.get(), "out": self.out_var.get(), "pack": self.pack_var.get(),
            "tex3ds": self.tex3ds_var.get(), "all": self.all_var.get(),
            "clean": self.clean_var.get(),
            "missions": [self.mission_names[i] for i in self.listbox.curselection()],
        }
        try:
            SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass
 
    def collect(self) -> dict:
        game_text = self.game_var.get().strip()
        tex3ds_text = self.tex3ds_var.get().strip()
        missions = None
        if not self.all_var.get():
            missions = [self.mission_names[i] for i in self.listbox.curselection()]
            if not missions:
                raise UserError("Sélectionne au moins une mission dans la liste de gauche "
                                "(ou coche « Toutes les missions »).")
        return {
            "game": Path(game_text) if game_text else None,
            "out": Path(self.out_var.get().strip() or "."),
            "pack": Path(self.pack_var.get().strip() or "data.c47p"),
            "tex3ds": Path(tex3ds_text) if tex3ds_text else None,
            "missions": missions,
            "clean": self.clean_var.get(),
        }
 
    def log_error(self, text: str) -> None:
        try:
            with (APP_DIR / "c47tool_error.log").open("a", encoding="utf-8") as handle:
                handle.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n{text}\n")
        except OSError:
            pass
 
    def _tk_error(self, exc, value, tb) -> None:
        text = "".join(traceback.format_exception(exc, value, tb))
        self.log_error(text)
        messagebox.showerror("Erreur inattendue", f"{exc.__name__}: {value}", parent=self)
 
    def _on_close(self) -> None:
        if self.busy and not messagebox.askyesno(
                "Quitter ?", "Une opération est en cours. Quitter l'interrompt.", parent=self):
            return
        self.save_settings()
        self.destroy()
 
    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for panel in self.panels:
            panel.start_button.configure(state=state)
        self.copy_button.configure(state=state)
        for widget in self.lockable:
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass
        if not busy:
            self._sync_list_state()
 
    # ---- en-tete -------------------------------------------------------------
    def _build_header(self) -> None:
        header = ttk.Frame(self, style="Root.TFrame")
        header.pack(fill="x", padx=24, pady=(20, 16))
        ttk.Label(header, text="HITMAN: CODENAME 47  ·  NEW 3DS", style="Brand.TLabel").pack(anchor="w")
        ttk.Label(header, text="Outil de conversion", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="Convertir le jeu, créer le paquet pour la carte SD, "
                               "diagnostiquer les problèmes de textures.",
                  style="Sub.TLabel").pack(anchor="w")
 
    # ---- colonne missions ------------------------------------------------------
    def _build_sidebar(self, body) -> None:
        side = ttk.Frame(body, style="Card.TFrame", padding=16)
        side.grid(row=0, column=0, sticky="ns", padx=(0, 16))
        ttk.Label(side, text="Missions", style="Section.TLabel").pack(anchor="w")
        self.count_var = tk.StringVar(value="")
        ttk.Label(side, textvariable=self.count_var, style="CardMuted.TLabel").pack(anchor="w", pady=(0, 8))
        self.all_check = ttk.Checkbutton(side, text="Toutes les missions", variable=self.all_var,
                                         command=self._sync_list_state)
        self.all_check.pack(anchor="w", pady=(0, 8))
        holder = tk.Frame(side, bg=BORDER, padx=1, pady=1)
        holder.pack(fill="y", expand=True)
        inner = tk.Frame(holder, bg=CARD2)
        inner.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(inner, selectmode="extended", exportselection=False, width=17,
                                  bg=CARD2, fg=FG, selectbackground=ACCENT, selectforeground="white",
                                  highlightthickness=0, borderwidth=0, activestyle="none",
                                  font=(FONT, 11), disabledforeground="#c9ced9")
        scroll = ttk.Scrollbar(inner, orient="vertical", command=self.listbox.yview,
                               style="Dark.Vertical.TScrollbar")
        self.listbox.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.listbox.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        ttk.Label(side, text="Utilisé par « Convertir »\net « Diagnostiquer ».",
                  style="CardMuted.TLabel", justify="left").pack(anchor="w", pady=(10, 0))
 
    def _sync_list_state(self) -> None:
        if self.all_var.get():
            self.listbox.selection_set(0, "end")
            self.listbox.configure(state="disabled")
        else:
            self.listbox.configure(state="normal")
 
    def refresh_missions(self) -> None:
        game = Path(self.game_var.get().strip()) if self.game_var.get().strip() else None
        names: list[str] = []
        if game and game.is_dir():
            try:
                names = [path.stem for path in p1._mission_archives(game)]
            except OSError:
                names = []
        self.mission_names = names
        self.listbox.configure(state="normal")
        self.listbox.delete(0, "end")
        for name in names:
            self.listbox.insert("end", f"  {name}")
        for index, name in enumerate(names):
            if name in self.saved_missions:
                self.listbox.selection_set(index)
        if names:
            self.count_var.set(f"{len(names)} missions trouvées")
        elif game and game.is_dir():
            self.count_var.set("Aucune mission trouvée")
        else:
            self.count_var.set("Choisis le dossier du jeu")
        self._sync_list_state()
 
    # ---- zone principale ---------------------------------------------------------
    def _build_main(self, body) -> None:
        main = ttk.Frame(body, style="Root.TFrame")
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)
 
        folders = ttk.Frame(main, style="Card.TFrame", padding=(18, 14, 18, 14))
        folders.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        folders.columnconfigure(1, weight=1)
        self.lockable: list = []
 
        def row(index: int, label: str, variable: tk.StringVar, browse, on_change=None,
                button_text: str = "Parcourir…") -> None:
            ttk.Label(folders, text=label, style="Field.TLabel", width=22).grid(
                row=index, column=0, sticky="w", pady=4)
            entry = ttk.Entry(folders, textvariable=variable)
            entry.grid(row=index, column=1, sticky="ew", padx=(0, 10), pady=4)
            button = ttk.Button(folders, text=button_text, style="Small.TButton", command=browse)
            button.grid(row=index, column=2, pady=4)
            self.lockable.extend([entry, button])
            if on_change:
                entry.bind("<FocusOut>", lambda _e: on_change())
                entry.bind("<Return>", lambda _e: on_change())
 
        row(0, "Dossier du jeu", self.game_var, self._browse_game, self.refresh_missions)
        row(1, "Dossier de conversion", self.out_var, lambda: self._browse_dir(self.out_var))
        row(2, "Fichier paquet (.c47p)", self.pack_var, self._browse_pack, button_text="Choisir…")
        row(3, "tex3ds.exe (auto si vide)", self.tex3ds_var, self._browse_tex3ds)
 
        notebook = ttk.Notebook(main)
        notebook.grid(row=1, column=0, sticky="nsew")
        self.panels: list[TaskPanel] = []
 
        convert = TaskPanel(
            self, notebook, "Convertir le jeu",
            "Lit les archives du jeu et crée les modèles, textures, scènes et sons pour la 3DS. "
            "C'est la partie la plus longue (surtout les textures).",
            "▶  Lancer la conversion", run_convert, "Conversion terminée")
        clean = ttk.Checkbutton(convert.extra_frame, text="Supprimer l'ancienne conversion avant",
                                variable=self.clean_var)
        clean.pack(side="right", padx=(0, 14))
        self.lockable.append(clean)
 
        pack = TaskPanel(
            self, notebook, "Créer le paquet pour la carte SD",
            "Regroupe tout le dossier de conversion dans un seul fichier data.c47p, à copier dans "
            "« 3ds/hitman47/ » sur la carte SD de la console.",
            "▶  Créer data.c47p", run_pack, "Paquet créé")
        self.copy_button = ttk.Button(pack.extra_frame, text="Copier sur la carte SD…",
                                      command=self._copy_to_sd)
        self.copy_button.pack(side="right", padx=(0, 10))
 
        diag = TaskPanel(
            self, notebook, "Diagnostiquer",
            "Analyse les missions sélectionnées pour trouver pourquoi des murs sont blancs, des "
            "textures absentes ou des personnages écrasés. Le rapport est écrit dans diag.txt.",
            "▶  Lancer le diagnostic", run_diag, "Diagnostic terminé")
        diag.open_button.configure(text="Ouvrir diag.txt")
 
        self.panels = [convert, pack, diag]
        notebook.add(convert, text="  1 · Convertir  ")
        notebook.add(pack, text="  2 · Empaqueter  ")
        notebook.add(diag, text="  3 · Diagnostiquer  ")
 
    # ---- boutons Parcourir ---------------------------------------------------------
    def _browse_game(self) -> None:
        path = filedialog.askdirectory(parent=self, title="Dossier d'installation de Hitman Codename 47",
                                       initialdir=self.game_var.get() or str(APP_DIR))
        if path:
            self.game_var.set(str(Path(path)))
            self.saved_missions = []
            self.refresh_missions()
 
    def _browse_dir(self, variable: tk.StringVar) -> None:
        path = filedialog.askdirectory(parent=self, initialdir=variable.get() or str(APP_DIR))
        if path:
            variable.set(str(Path(path)))
 
    def _browse_pack(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self, title="Où enregistrer le paquet ?", defaultextension=".c47p",
            filetypes=[("Paquet Hitman 3DS", "*.c47p")],
            initialfile="data.c47p", initialdir=str(Path(self.pack_var.get() or APP_DIR).parent))
        if path:
            self.pack_var.set(str(Path(path)))
 
    def _browse_tex3ds(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="tex3ds.exe",
                                          filetypes=[("tex3ds", "tex3ds*"), ("Tous", "*.*")])
        if path:
            self.tex3ds_var.set(str(Path(path)))
 
    def _copy_to_sd(self) -> None:
        pack_path = Path(self.pack_var.get().strip() or "data.c47p")
        if not pack_path.is_file():
            messagebox.showwarning("Paquet introuvable",
                                   "Le fichier data.c47p n'existe pas encore.\n"
                                   "Clique d'abord sur « Créer data.c47p ».", parent=self)
            return
        root = filedialog.askdirectory(parent=self, title="Choisis la RACINE de la carte SD (ex : E:\\)")
        if not root:
            return
        destination = Path(root) / "3ds" / "hitman47" / "data.c47p"
        if destination.exists() and not messagebox.askyesno(
                "Remplacer ?", f"{destination}\nexiste déjà. Le remplacer ?", parent=self):
            return
        free = shutil.disk_usage(root).free
        needed = pack_path.stat().st_size - (destination.stat().st_size if destination.exists() else 0)
        if needed > free:
            messagebox.showerror("Pas assez de place",
                                 f"Il faut {_fmt_size(needed)} mais il reste {_fmt_size(free)} "
                                 "sur ce disque.", parent=self)
            return
        self.panels[1].start(runner=run_copy_sd, extra_settings={"sd_destination": destination},
                             label="Préparation de la copie…")
 
 
def main() -> int:
    try:
        app = App()
        app.mainloop()
    except Exception:
        text = traceback.format_exc()
        try:
            (APP_DIR / "c47tool_error.log").write_text(text, encoding="utf-8")
        except OSError:
            pass
        try:
            messagebox.showerror("Erreur au démarrage", text)
        except Exception:
            print(text)
        return 1
    return 0
 
 
if __name__ == "__main__":
    raise SystemExit(main())