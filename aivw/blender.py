"""Deterministic Blender previews, cache provenance and frame-accurate assembly.

The ordinary Python process never imports bpy.  The same scene builder can be
called inside Blender through MCP; the CLI runs it in a separate Blender process.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import zlib

from .common import (WorkflowError, emit, find_executable, load_project,
                     load_shots, project_path, read_json, write_json)

REPO = Path(__file__).resolve().parents[1]
BUILDER = REPO / "blender_scripts" / "scene_builder.py"
RENDERER = REPO / "blender_scripts" / "render_frames.py"
INSPECTOR = REPO / "blender_scripts" / "inspect_blend.py"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
KINDS = {"box", "sphere", "cylinder", "cone", "plane", "empty", "actor", "text"}
PROPERTIES = {"location", "rotation_degrees", "scale"}


def sha256_file(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def content_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _safe_id(value, label):
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value) or value in {".", ".."}:
        raise WorkflowError(f"{label} must be a safe, nonempty identifier: {value!r}")
    return value


def _positive_int(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise WorkflowError(f"{label} must be a positive integer.")
    return value


def _number(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise WorkflowError(f"{label} must be finite numeric data.")
    if positive and value <= 0:
        raise WorkflowError(f"{label} must be positive.")


def _vector(value, label, *, size=3, positive=False, color=False):
    if not isinstance(value, list) or len(value) != size:
        raise WorkflowError(f"{label} must have {size} numbers.")
    for item in value:
        _number(item, label, positive=positive)
        if color and not 0 <= item <= 1:
            raise WorkflowError(f"{label} color values must be between 0 and 1.")


def _inside(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    if not path.is_relative_to(root):
        raise WorkflowError(f"Output path escapes the project: {path}")
    if any(c in str(path) for c in "\n\r\0"):
        raise WorkflowError("Project paths must not contain control characters.")
    return path


def validate_scene(scene, shots, fps):
    """Validate the executable layout; never infer a missing layout from prose."""
    _positive_int(fps, "fps")
    if not isinstance(scene, dict) or scene.get("schema_version") != "1.0":
        raise WorkflowError("scene.json must have schema_version '1.0'.")
    for key, kind in (("objects", list), ("materials", dict), ("scenes", dict),
                      ("cameras", dict), ("shots", dict)):
        if not isinstance(scene.get(key), kind):
            raise WorkflowError(f"scene.json requires {key} ({kind.__name__}).")
    materials = scene["materials"]
    for mid, material in materials.items():
        _safe_id(mid, "material id")
        if not isinstance(material, dict):
            raise WorkflowError(f"Material {mid} must be an object.")
        _vector(material.get("base_color"), f"material {mid}.base_color", size=4, color=True)
        _number(material.get("roughness", 0.8), f"material {mid}.roughness")
        if not 0 <= material.get("roughness", 0.8) <= 1:
            raise WorkflowError(f"Material {mid}.roughness must be between 0 and 1.")
    objects = {}
    for obj in scene["objects"]:
        if not isinstance(obj, dict):
            raise WorkflowError("Every scene object must be an object.")
        oid = _safe_id(obj.get("id"), "object id")
        if oid in objects:
            raise WorkflowError(f"Duplicate object id: {oid}")
        objects[oid] = obj
        if obj.get("type") not in KINDS:
            raise WorkflowError(f"Unsupported primitive type for {oid}: {obj.get('type')!r}")
        for prop in PROPERTIES:
            if prop in obj:
                _vector(obj[prop], f"{oid}.{prop}", positive=(prop == "scale"))
        if "dimensions" in obj:
            _vector(obj["dimensions"], f"{oid}.dimensions", positive=True)
        if obj.get("material") is not None and obj["material"] not in materials:
            raise WorkflowError(f"Unknown material on {oid}: {obj['material']}")
        if "text" in obj and (not isinstance(obj["text"], str) or len(obj["text"]) > 2000):
            raise WorkflowError(f"{oid}.text must be a string of at most 2000 characters.")
    for oid, obj in objects.items():
        visited = {oid}
        parent = obj.get("parent")
        while parent is not None:
            if parent not in objects:
                raise WorkflowError(f"Unknown parent {parent!r} for {oid}.")
            if parent in visited:
                raise WorkflowError(f"Parent cycle involving {oid}.")
            visited.add(parent)
            parent = objects[parent].get("parent")
    for sid, group in scene["scenes"].items():
        if not isinstance(sid, str) or not sid or not isinstance(group, dict):
            raise WorkflowError("Scene group names and specifications must be nonempty.")
        ids = group.get("objects")
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
            raise WorkflowError(f"Scene {sid}.objects must be an id list.")
        if len(ids) != len(set(ids)):
            raise WorkflowError(f"Scene {sid} has repeated object ids.")
        for oid in ids:
            if oid not in objects:
                raise WorkflowError(f"Scene {sid} refers to missing object {oid}.")
            if objects[oid].get("parent") is not None and objects[oid]["parent"] not in ids:
                raise WorkflowError(f"Scene {sid} omits parent of {oid}.")
        _vector(group.get("background", [0.08, 0.09, 0.12]), f"{sid}.background", color=True)
    for cid, camera in scene["cameras"].items():
        _safe_id(cid, "camera id")
        if not isinstance(camera, dict):
            raise WorkflowError(f"Camera {cid} must be an object.")
        _vector(camera.get("location"), f"{cid}.location")
        _target(camera.get("target"), objects, f"{cid}.target")
        _number(camera.get("lens_mm", 50), f"{cid}.lens_mm", positive=True)
        if camera.get("projection", "perspective") not in {"perspective", "orthographic"}:
            raise WorkflowError(f"Unsupported projection on camera {cid}.")
        if camera.get("projection") == "orthographic":
            _number(camera.get("ortho_scale"), f"{cid}.ortho_scale", positive=True)
    seen = set()
    for shot in shots:
        if not isinstance(shot, dict):
            raise WorkflowError("Each shot must be an object.")
        sid = _safe_id(shot.get("id"), "shot id")
        if sid in seen:
            raise WorkflowError(f"Duplicate shot id: {sid}")
        seen.add(sid)
        frames = _positive_int(shot.get("frames"), f"{sid}.frames (resolve missing timing first)")
        if shot.get("duration_seconds") is None:
            raise WorkflowError(f"{sid} has unresolved duration_seconds.")
        try:
            seconds = Decimal(str(shot["duration_seconds"]))
            expected = int((seconds * fps).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        except (ValueError, InvalidOperation, OverflowError) as exc:
            raise WorkflowError(f"Invalid duration for {sid}.") from exc
        if not seconds.is_finite() or seconds <= 0 or expected != frames:
            raise WorkflowError(f"{sid}.frames does not match duration_seconds × fps (ROUND_HALF_UP).")
        layout = scene["shots"].get(sid)
        if not isinstance(layout, dict):
            raise WorkflowError(f"Missing executable layout for {sid}; author scene.json first.")
        group_id = layout.get("scene")
        if shot.get("scene_id") != group_id:
            raise WorkflowError(f"{sid}: shots.json scene_id and scene.json layout.scene differ.")
        if layout.get("mode", "declarative") not in {"declarative", "external_blend"}:
            raise WorkflowError(f"{sid}: unknown layout mode.")
        if layout.get("mode") == "external_blend":
            if not isinstance(group_id, str) or not group_id:
                raise WorkflowError(f"{sid}: external_blend requires the original scene_id.")
            continue
        if group_id not in scene["scenes"]:
            raise WorkflowError(f"Unknown scene for {sid}: {group_id!r}")
        cid = layout.get("camera")
        if cid not in scene["cameras"]:
            raise WorkflowError(f"Missing camera for {sid}: {cid!r}")
        allowed_objects = set(scene["scenes"][group_id]["objects"])
        camera = scene["cameras"][cid]
        if isinstance(camera["target"], str) and camera["target"] not in allowed_objects:
            raise WorkflowError(f"{sid}: camera target is outside this scene group.")
        animations = layout.get("animation", [])
        if not isinstance(animations, list):
            raise WorkflowError(f"{sid}.animation must be a list.")
        channels = set()
        for animation in animations:
            if not isinstance(animation, dict):
                raise WorkflowError(f"Invalid animation in {sid}.")
            oid, prop = animation.get("object"), animation.get("property")
            if oid not in allowed_objects or prop not in PROPERTIES:
                raise WorkflowError(f"{sid}: animation must target a present object and allowed transform.")
            if (oid, prop) in channels:
                raise WorkflowError(f"{sid}: repeated animation channel {oid}.{prop}.")
            channels.add((oid, prop))
            if animation.get("interpolation", "BEZIER") not in {"LINEAR", "BEZIER", "CONSTANT"}:
                raise WorkflowError(f"{sid}: unsupported interpolation.")
            keys = animation.get("keyframes")
            _keys(keys, frames, sid)
            for key in keys:
                _vector(key.get("value"), f"{sid} {oid}.{prop}", positive=(prop == "scale"))
        keys = layout.get("camera_keyframes", [])
        _keys(keys, frames, sid, allow_empty=True)
        for key in keys:
            if "location" in key:
                _vector(key["location"], f"{sid} camera.location")
            if "target" in key:
                _target(key["target"], objects, f"{sid} camera.target")
                if isinstance(key["target"], str) and key["target"] not in allowed_objects:
                    raise WorkflowError(f"{sid}: camera target is outside this scene group.")
            if "lens_mm" in key:
                _number(key["lens_mm"], f"{sid} camera.lens_mm", positive=True)
    extra = set(scene["shots"]) - seen
    if extra:
        raise WorkflowError(f"scene.json contains unknown shot ids: {', '.join(sorted(extra))}")


def _target(value, objects, label):
    if isinstance(value, str):
        if value not in objects:
            raise WorkflowError(f"{label} refers to missing object {value}.")
    else:
        _vector(value, label)


def _keys(keys, frames, label, allow_empty=False):
    if not isinstance(keys, list) or (not keys and not allow_empty):
        raise WorkflowError(f"{label}: keyframes must be a nonempty list.")
    previous = 0
    for key in keys:
        if not isinstance(key, dict):
            raise WorkflowError(f"{label}: keyframes must be objects.")
        frame = _positive_int(key.get("frame"), f"{label}.keyframe.frame")
        if not previous < frame <= frames:
            raise WorkflowError(f"{label}: keyframe frames must increase within 1..{frames}.")
        previous = frame


def _load(path, *, validate_layout=True):
    project, root = load_project(path)
    shots = load_shots(project, root)
    if not shots:
        raise WorkflowError("No shots; import and resolve the storyboard first.")
    scene_path = project_path(project, root, "scene")
    scene = read_json(scene_path) if scene_path.exists() else None
    if validate_layout:
        validate_scene(scene, shots, project.get("fps"))
    else:
        _positive_int(project.get("fps"), "fps")
        seen = set()
        for shot in shots:
            sid = _safe_id(shot.get("id"), "shot id")
            if sid in seen:
                raise WorkflowError(f"Duplicate shot id: {sid}")
            seen.add(sid)
            frames = _positive_int(shot.get("frames"), f"{sid}.frames")
            try:
                seconds = Decimal(str(shot.get("duration_seconds")))
                expected = int((seconds * project["fps"]).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            except (InvalidOperation, ValueError, OverflowError) as exc:
                raise WorkflowError(f"{sid}: unresolved or invalid duration.") from exc
            if not seconds.is_finite() or seconds <= 0 or expected != frames:
                raise WorkflowError(f"{sid}: frame count differs from resolved duration.")
    size = project.get("resolution")
    if not isinstance(size, list) or len(size) != 2:
        raise WorkflowError("Project resolution must be [width, height].")
    for value in size:
        _positive_int(value, "resolution")
        if value % 2:
            raise WorkflowError("Preview H.264 requires even width and height.")
    return project, root, shots, scene


def _selected(shots, requested):
    if not requested:
        return shots
    wanted = set(requested)
    unknown = wanted - {s["id"] for s in shots}
    if unknown:
        raise WorkflowError(f"Unknown shot ids: {', '.join(sorted(unknown))}")
    return [s for s in shots if s["id"] in wanted]


def _executable(name, explicit=None):
    result = find_executable(name, explicit or os.environ.get(name.upper() + "_BIN"))
    if not result:
        raise WorkflowError(f"{name} not found; use --{name} or {name.upper()}_BIN.")
    return result


def _run(command, log_path, *, timeout=None, env=None):
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("w", encoding="utf-8") as log:
            result = subprocess.run([str(v) for v in command], stdout=log, stderr=subprocess.STDOUT,
                                    timeout=timeout, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkflowError(f"Command could not finish ({exc}); log: {log_path}") from exc
    if result.returncode:
        tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-12:])
        raise WorkflowError(f"Command failed ({result.returncode}); log: {log_path}\n{tail}")


def _version(executable):
    try:
        result = subprocess.run([executable, "--version"], text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=30, check=True)
        return result.stdout.splitlines()[0]
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkflowError(f"Cannot query executable version: {exc}") from exc


def _settings(project, args):
    resolution = list(project["resolution"])
    override = getattr(args, "resolution", None)
    if override:
        if not re.fullmatch(r"[1-9][0-9]*x[1-9][0-9]*", override):
            raise WorkflowError("--resolution must use WIDTHxHEIGHT, for example 320x180.")
        resolution = [int(n) for n in override.split("x")]
        if any(n % 2 for n in resolution):
            raise WorkflowError("--resolution must use even dimensions for H.264.")
    samples = getattr(args, "samples", 8)
    _positive_int(samples, "samples")
    return {"fps": project["fps"], "resolution": resolution,
            "engine": getattr(args, "engine", "WORKBENCH"), "samples": samples,
            "draft_override": override is not None}


def make_bundle(project, root, shots, scene, shot, settings):
    """Return a validated, JSON-only input accepted by the Blender/MCP build API."""
    if not isinstance(scene, dict) or shot["id"] not in scene.get("shots", {}):
        raise WorkflowError(f"Missing executable layout for {shot['id']}.")
    layout = scene["shots"][shot["id"]]
    if layout.get("mode") == "external_blend":
        raise WorkflowError(f"{shot['id']} uses a real Blender snapshot; use adopt-blend.")
    try:
        group = scene["scenes"][layout["scene"]]
        wanted = set(group["objects"])
        selected_objects = [o for o in scene["objects"] if o["id"] in wanted]
        material_ids = {o["material"] for o in selected_objects if o.get("material")}
        local_scene = {"schema_version": "1.0", "objects": selected_objects,
                       "materials": {k:v for k,v in scene["materials"].items() if k in material_ids},
                       "scenes": {layout["scene"]: group},
                       "cameras": {layout["camera"]: scene["cameras"][layout["camera"]]},
                       "shots": {shot["id"]: layout}}
    except (KeyError, TypeError) as exc:
        raise WorkflowError(f"Incomplete declarative layout for {shot['id']}.") from exc
    validate_scene(local_scene, [shot], project["fps"])
    source = {"project_id": project.get("id"),
              "project_timing": {"fps": project["fps"], "resolution": project["resolution"]},
              "scene": local_scene, "shot": shot,
              "settings": settings, "builder_sha256": sha256_file(BUILDER),
              "validator_sha256": sha256_file(Path(__file__))}
    return {"schema_version": "1.0", **source, "project_root": str(root),
            "input_hash": content_hash(source)}


def _archive_build(build_dir, sid, root):
    """Keep a content-addressed copy before replacing an existing owned build."""
    output = _inside(build_dir / f"{sid}.blend", root)
    if not output.exists():
        return
    archive = _inside(build_dir / "history" / sid, root)
    archive.mkdir(parents=True, exist_ok=True)
    digest = sha256_file(output)
    target = archive / f"{digest}.blend"
    if not target.exists():
        shutil.copy2(output, target)
    receipt = build_dir / f"{sid}.build.json"
    if receipt.exists():
        shutil.copy2(receipt, archive / f"{digest}.build.json")


def _snapshot_source(project, shot):
    return content_hash({"project_id": project.get("id"), "fps": project["fps"],
                         "resolution": project["resolution"], "shot": shot})


def dependency_record(path):
    """Hash an external file or a reported cache/sequence directory."""
    # Preserve the reference path: resolving a symlink here would pin its old
    # target while Blender follows a newly retargeted link on the next render.
    path = Path(path).expanduser().absolute()
    if path.is_file():
        return {"path": str(path), "kind": "file", "sha256": sha256_file(path)}
    if path.is_dir():
        members = sorted(path.rglob("*"))
        if any(item.is_symlink() and item.is_dir() for item in members):
            raise WorkflowError(f"External cache directories must not contain directory symlinks: {path}")
        files = [{"path": str(item.relative_to(path)), "sha256": sha256_file(item)}
                 for item in members if item.is_file()]
        return {"path": str(path), "kind": "directory", "sha256": content_hash(files),
                "files": len(files)}
    raise WorkflowError(f"External Blender dependency is missing: {path}")


def _verify_dependencies(receipt, shot_id):
    dependencies = receipt.get("inspection", {}).get("dependencies")
    if not isinstance(dependencies, list):
        raise WorkflowError(f"{shot_id}: snapshot has no dependency record; adopt-blend again.")
    for recorded in dependencies:
        if not isinstance(recorded, dict) or not isinstance(recorded.get("path"), str):
            raise WorkflowError(f"{shot_id}: invalid external dependency record; adopt-blend again.")
        actual = dependency_record(recorded["path"])
        if actual != recorded:
            raise WorkflowError(f"{shot_id}: external dependency changed: {recorded['path']}; reconcile and adopt-blend again.")


def adopt_blend(args):
    """Freeze a real MCP/authored .blend without reducing it to primitive JSON."""
    project, root, shots, scene = _load(args.project, validate_layout=False)
    selected = _selected(shots, [args.shot])
    shot = selected[0]
    if scene is None:
        scene = {"schema_version": "1.0", "materials": {}, "objects": [], "scenes": {}, "cameras": {}, "shots": {}}
    if not isinstance(scene, dict) or not isinstance(scene.get("shots"), dict):
        raise WorkflowError("Repair scene.json structure before adopting a Blender snapshot.")
    if not isinstance(shot.get("scene_id"), str) or not shot["scene_id"].strip():
        raise WorkflowError(f"{shot['id']}: resolve the original scene_id before adoption.")
    source_file = Path(args.input).expanduser().resolve()
    if not source_file.is_file() or source_file.suffix.lower() != ".blend":
        raise WorkflowError("--input must be an existing .blend file.")
    if not args.note.strip():
        raise WorkflowError("--note must describe the adopted MCP/authored revision.")
    executable = _executable("blender", getattr(args, "blender", None))
    version = _version(executable)
    source_hash = _snapshot_source(project, shot)
    provenance = {"origin": "mcp_snapshot", "source_hash": source_hash,
                  "input_sha256": sha256_file(source_file), "note": args.note.strip(),
                  "inspector_sha256": sha256_file(INSPECTOR), "scene": getattr(args, "scene", None)}
    input_hash = content_hash(provenance)
    build_dir = project_path(project, root, "build")
    build_dir.mkdir(parents=True, exist_ok=True)
    sid = shot["id"]
    temporary = _inside(build_dir / f"{sid}.adopting.blend", root)
    inspection = _inside(build_dir / f"{sid}.inspection.json", root)
    specification = _inside(build_dir / f"{sid}.adopt-input.json", root)
    write_json(specification, {"project_root": str(root), "shot_id": sid,
                "fps": project["fps"], "frames": shot["frames"], "resolution": project["resolution"],
                "allow_draft_resolution": getattr(args, "allow_draft_resolution", False),
                "scene": getattr(args, "scene", None), "input_hash": input_hash,
                "output": str(temporary), "inspection": str(inspection)})
    temporary.unlink(missing_ok=True)
    inspection.unlink(missing_ok=True)
    _run([executable, "--background", "--disable-autoexec", source_file,
          "--python-exit-code", "1", "--python", INSPECTOR, "--", "--spec", specification],
         build_dir / f"{sid}.adopt.log")
    if not temporary.is_file() or not inspection.is_file():
        raise WorkflowError("Blender did not finish inspecting and saving the adopted snapshot.")
    inspected = read_json(inspection)
    _archive_build(build_dir, sid, root)
    output = _inside(build_dir / f"{sid}.blend", root)
    os.replace(temporary, output)
    receipt = {"schema_version": "1.0", "shot_id": sid, "status": "complete",
               **provenance, "input_hash": input_hash, "blend_sha256": sha256_file(output),
               "blender_version": version, "settings": inspected["settings"], "frames": shot["frames"],
               "inspection": inspected, "file": str(output.relative_to(root))}
    write_json(build_dir / f"{sid}.build.json", receipt)
    old_layout = scene["shots"].get(sid)
    if old_layout is not None:
        archive = _inside(build_dir / "history" / sid, root)
        write_json(archive / f"{content_hash(old_layout)}.layout.json", old_layout)
    scene["shots"][sid] = {"mode": "external_blend", "scene": shot["scene_id"]}
    write_json(project_path(project, root, "scene"), scene)
    emit({"operation": "adopt-blend", "shot": sid, "file": str(output), "inspection": inspected})
    return 0


def build_project(args):
    project, root, shots, scene = _load(args.project)
    selected = _selected(shots, getattr(args, "shot", None))
    executable = _executable("blender", getattr(args, "blender", None))
    settings = _settings(project, args)
    version = _version(executable)
    build_dir = project_path(project, root, "build")
    build_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for shot in selected:
        sid = shot["id"]
        if scene["shots"][sid].get("mode") == "external_blend":
            output, _ = _current_build(project, root, shots, scene, shot)
            results.append({"shot": sid, "status": "preserved_snapshot", "file": str(output)})
            continue
        bundle = make_bundle(project, root, shots, scene, shot, settings)
        output = _inside(build_dir / f"{sid}.blend", root)
        receipt_path = _inside(build_dir / f"{sid}.build.json", root)
        if receipt_path.exists() and not getattr(args, "force", False):
            if read_json(receipt_path).get("origin") == "mcp_snapshot":
                raise WorkflowError(f"{sid} is an adopted MCP snapshot. Re-adopt after edits; build --force explicitly replaces it with scene.json geometry.")
        if output.exists() and receipt_path.exists() and not getattr(args, "force", False):
            receipt = read_json(receipt_path)
            if (receipt.get("input_hash") == bundle["input_hash"] and
                    receipt.get("blend_sha256") == sha256_file(output) and
                    receipt.get("blender_version") == version):
                results.append({"shot": sid, "status": "reused", "file": str(output)})
                continue
        spec_path = _inside(build_dir / f"{sid}.spec.json", root)
        temporary = _inside(build_dir / f"{sid}.building.blend", root)
        write_json(spec_path, bundle)
        temporary.unlink(missing_ok=True)
        _run([executable, "--background", "--factory-startup", "--python-exit-code", "1",
              "--python", BUILDER, "--", "--spec", spec_path, "--output", temporary],
             build_dir / f"{sid}.build.log")
        if not temporary.is_file():
            raise WorkflowError(f"Blender exited without creating {temporary}.")
        _archive_build(build_dir, sid, root)
        os.replace(temporary, output)
        write_json(receipt_path, {"schema_version": "1.0", "shot_id": sid, "status": "complete",
                                 "origin": "declarative",
                                 "input_hash": bundle["input_hash"], "blend_sha256": sha256_file(output),
                                 "blender_version": version, "settings": settings,
                                 "frames": shot["frames"], "file": str(output.relative_to(root))})
        results.append({"shot": sid, "status": "built", "file": str(output)})
    emit({"operation": "build", "blender": version, "settings": settings, "shots": results})
    return 0


def _current_build(project, root, shots, scene, shot):
    build_dir = project_path(project, root, "build")
    receipt_path = _inside(build_dir / f"{shot['id']}.build.json", root)
    output = _inside(build_dir / f"{shot['id']}.blend", root)
    if not receipt_path.is_file() or not output.is_file():
        raise WorkflowError(f"{shot['id']}: build the shot first.")
    receipt = read_json(receipt_path)
    settings = receipt.get("settings")
    if not isinstance(settings, dict):
        raise WorkflowError(f"{shot['id']}: invalid build receipt; rebuild.")
    if receipt.get("origin") == "mcp_snapshot":
        if (receipt.get("status") != "complete" or receipt.get("source_hash") != _snapshot_source(project, shot)
                or receipt.get("inspector_sha256") != sha256_file(INSPECTOR)):
            raise WorkflowError(f"{shot['id']}: storyboard/settings changed after adoption; reconcile the MCP scene and adopt-blend again.")
        layout = scene.get("shots", {}).get(shot["id"], {}) if isinstance(scene, dict) else {}
        if not isinstance(layout, dict) or layout.get("mode") != "external_blend" or layout.get("scene") != shot.get("scene_id"):
            raise WorkflowError(f"{shot['id']}: snapshot needs an external_blend layout marker; re-adopt or explicitly rebuild its declarative layout.")
        _verify_dependencies(receipt, shot["id"])
    else:
        bundle = make_bundle(project, root, shots, scene, shot, settings)
        if receipt.get("status") != "complete" or receipt.get("input_hash") != bundle["input_hash"]:
            raise WorkflowError(f"{shot['id']}: source or builder changed; run build before rendering.")
    if receipt.get("blend_sha256") != sha256_file(output):
        raise WorkflowError(f"{shot['id']}: .blend differs from its receipt; adopt-blend this edited revision or rebuild declarative input.")
    return output, receipt


def valid_png(path, dimensions=None):
    """Check complete PNG chunks and CRCs, including truncated last frames."""
    try:
        data = Path(path).read_bytes()
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            return False
        offset, saw_header, saw_data = 8, False, False
        while offset + 12 <= len(data):
            length = struct.unpack_from(">I", data, offset)[0]
            end = offset + 12 + length
            if end > len(data):
                return False
            kind = data[offset + 4:offset + 8]
            payload = data[offset + 8:offset + 8 + length]
            crc = struct.unpack_from(">I", data, offset + 8 + length)[0]
            if zlib.crc32(kind + payload) & 0xffffffff != crc:
                return False
            if kind == b"IHDR":
                if saw_header or length != 13:
                    return False
                saw_header = True
                width, height = struct.unpack_from(">II", payload)
                if dimensions is not None and [width, height] != list(dimensions):
                    return False
            elif kind == b"IDAT":
                saw_data = True
            elif kind == b"IEND":
                return saw_header and saw_data and length == 0 and end == len(data)
            offset = end
    except (OSError, struct.error):
        return False
    return False


def probe_video(path, ffprobe=None):
    executable = _executable("ffprobe", ffprobe)
    command = [executable, "-v", "error", "-select_streams", "v:0", "-count_frames",
               "-show_entries", "stream=nb_read_frames,avg_frame_rate,width,height,duration",
               "-of", "json", str(path)]
    try:
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=120, check=True)
        streams = json.loads(result.stdout).get("streams", [])
        if len(streams) != 1:
            raise ValueError("video stream missing")
        stream = streams[0]
        return {"frames": int(stream["nb_read_frames"]), "fps": str(Fraction(stream["avg_frame_rate"])),
                "resolution": [stream["width"], stream["height"]], "duration": stream.get("duration")}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        raise WorkflowError(f"Cannot verify video {path}: {exc}") from exc


def _verify_video(path, frames, fps, size, ffprobe=None):
    actual = probe_video(path, ffprobe)
    if (actual["frames"] != frames or Fraction(actual["fps"]) != fps or actual["resolution"] != size):
        raise WorkflowError(f"Video timing/size mismatch: expected {frames} frames at {fps} fps, {size}; got {actual}")
    return actual


def render_fingerprint(build_receipt, blender_version):
    return content_hash({"build": build_receipt["input_hash"], "blend": build_receipt["blend_sha256"],
                         "settings": build_receipt["settings"], "frames": build_receipt["frames"],
                         "dependencies": build_receipt.get("inspection", {}).get("dependencies", []),
                         "renderer": sha256_file(RENDERER), "blender_version": blender_version})


def _clear_managed(render_dir):
    frames = render_dir / "frames"
    if frames.exists():
        for path in frames.glob("frame_*.png"):
            if re.fullmatch(r"frame_[0-9]{6}\.png", path.name):
                path.unlink()
    for name in ("preview.mp4", "preview.encoding.mp4", "render.json"):
        (render_dir / name).unlink(missing_ok=True)


def render_project(args):
    project, root, shots, scene = _load(args.project, validate_layout=False)
    selected = _selected(shots, getattr(args, "shot", None))
    executable = _executable("blender", getattr(args, "blender", None))
    ffmpeg = _executable("ffmpeg", getattr(args, "ffmpeg", None))
    ffprobe = _executable("ffprobe", getattr(args, "ffprobe", None))
    version = _version(executable)
    chunk_size = _positive_int(getattr(args, "chunk_size", 24), "chunk-size")
    # Validate every requested source before starting a potentially expensive batch.
    builds = {s["id"]: _current_build(project, root, shots, scene, s) for s in selected}
    results = []
    for shot in selected:
        sid, frames = shot["id"], shot["frames"]
        blend, build = builds[sid]
        settings = build["settings"]
        render_dir = _inside(project_path(project, root, "renders") / sid, root)
        frames_dir = _inside(render_dir / "frames", root)
        frames_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = render_dir / "render.json"
        fingerprint = render_fingerprint(build, version)
        previous = read_json(receipt_path) if receipt_path.exists() else {}
        invalidated = previous.get("fingerprint") != fingerprint or getattr(args, "force", False)
        if invalidated:
            _clear_managed(render_dir)
            previous = {}
        receipt = {"schema_version": "1.0", "shot_id": sid, "status": "rendering",
                   "fingerprint": fingerprint, "input_hash": build["input_hash"],
                   "blend_sha256": build["blend_sha256"], "blender_version": version,
                   "fps": settings["fps"], "frames": frames, "resolution": settings["resolution"],
                   "settings": settings, "frame_sha256": previous.get("frame_sha256", {})}
        missing = []
        for number in range(1, frames + 1):
            path = frames_dir / f"frame_{number:06d}.png"
            recorded = receipt["frame_sha256"].get(str(number))
            if (valid_png(path, settings["resolution"]) and
                    (recorded is None or recorded == sha256_file(path))):
                receipt["frame_sha256"][str(number)] = sha256_file(path)
            else:
                path.unlink(missing_ok=True)
                receipt["frame_sha256"].pop(str(number), None)
                missing.append(number)
        # Remove leftover managed frames outside the current, exact shot interval.
        for path in frames_dir.glob("frame_*.png"):
            match = re.fullmatch(r"frame_([0-9]{6})\.png", path.name)
            if match and not 1 <= int(match.group(1)) <= frames:
                path.unlink()
        write_json(receipt_path, receipt)
        env = os.environ.copy()
        if getattr(args, "software_gl", False):
            env["LIBGL_ALWAYS_SOFTWARE"] = "1"
        for start in range(0, len(missing), chunk_size):
            numbers = missing[start:start + chunk_size]
            _run([executable, "--background", "--disable-autoexec", blend, "--threads", "2", "--python-exit-code", "1",
                  "--python", RENDERER, "--", "--frames", ",".join(map(str, numbers)),
                  "--output", frames_dir, "--input-hash", build["input_hash"]],
                 render_dir / f"render_{numbers[0]:06d}.log", env=env)
            for number in numbers:
                path = frames_dir / f"frame_{number:06d}.png"
                if not valid_png(path, settings["resolution"]):
                    raise WorkflowError(f"Blender did not produce a complete frame: {path}")
                receipt["frame_sha256"][str(number)] = sha256_file(path)
            write_json(receipt_path, receipt)
        video = render_dir / "preview.mp4"
        reusable = (not missing and not invalidated and previous.get("status") == "complete" and
                    video.is_file() and previous.get("video_sha256") == sha256_file(video))
        if not reusable:
            temporary = render_dir / "preview.encoding.mp4"
            _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-framerate", settings["fps"],
                  "-start_number", "1", "-i", frames_dir / "frame_%06d.png", "-frames:v", frames,
                  "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-threads", "2",
                  "-pix_fmt", "yuv420p", "-r", settings["fps"], "-movflags", "+faststart", temporary],
                 render_dir / "encode.log")
            _verify_video(temporary, frames, settings["fps"], settings["resolution"], ffprobe)
            os.replace(temporary, video)
        actual = _verify_video(video, frames, settings["fps"], settings["resolution"], ffprobe)
        receipt.update({"status": "complete", "video_sha256": sha256_file(video), "verified": actual})
        write_json(receipt_path, receipt)
        results.append({"shot": sid, "rendered_frames": len(missing), "reused_frames": frames-len(missing),
                        "invalidated_cache": invalidated, "file": str(video), "verified": actual})
    emit({"operation": "render", "shots": results})
    return 0


def _current_render(project, root, shots, scene, shot, ffprobe=None):
    _, build = _current_build(project, root, shots, scene, shot)
    directory = _inside(project_path(project, root, "renders") / shot["id"], root)
    receipt_path, video = directory / "render.json", directory / "preview.mp4"
    if not receipt_path.is_file() or not video.is_file():
        raise WorkflowError(f"{shot['id']}: missing rendered preview; render before assembly.")
    receipt = read_json(receipt_path)
    fingerprint = render_fingerprint(build, receipt.get("blender_version", ""))
    if receipt.get("status") != "complete" or receipt.get("fingerprint") != fingerprint:
        raise WorkflowError(f"{shot['id']}: render is incomplete or stale; rerender.")
    if receipt.get("video_sha256") != sha256_file(video):
        raise WorkflowError(f"{shot['id']}: preview changed since verification; rerender.")
    _verify_video(video, shot["frames"], project["fps"], build["settings"]["resolution"], ffprobe)
    return directory, receipt


def _concat_line(path):
    value = str(Path(path).resolve())
    if any(c in value for c in "\n\r\0"):
        raise WorkflowError("Video paths cannot contain control characters.")
    return "file '" + value.replace("'", "'\\''") + "'\n"


def assemble_project(args):
    project, root, shots, scene = _load(args.project, validate_layout=False)
    ffmpeg = _executable("ffmpeg", getattr(args, "ffmpeg", None))
    ffprobe = _executable("ffprobe", getattr(args, "ffprobe", None))
    checked = [_current_render(project, root, shots, scene, shot, ffprobe) for shot in shots]
    resolutions = {tuple(receipt["resolution"]) for _, receipt in checked}
    if len(resolutions) != 1:
        raise WorkflowError("All clips must use the same resolution; rebuild/render inconsistent draft clips.")
    size = list(next(iter(resolutions)))
    output_dir = project_path(project, root, "outputs")
    output_dir.mkdir(parents=True, exist_ok=True)
    total = sum(s["frames"] for s in shots)
    list_path = _inside(output_dir / "previs.concat.txt", root)
    list_path.write_text("ffconcat version 1.0\n" + "".join(_concat_line(d / "preview.mp4") for d, _ in checked), encoding="utf-8")
    temporary = _inside(output_dir / "previs.assembling.mp4", root)
    _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
          "-map", "0:v:0", "-an", "-c", "copy", "-movflags", "+faststart", temporary], output_dir / "assemble.log")
    actual = _verify_video(temporary, total, project["fps"], size, ffprobe)
    output = _inside(output_dir / "previs.mp4", root)
    os.replace(temporary, output)
    manifest = {"schema_version": "1.0", "status": "complete", "frames": total, "fps": project["fps"],
                "resolution": size, "video_sha256": sha256_file(output), "verified": actual,
                "shots": [{"id": shot["id"], "frames": shot["frames"], "fingerprint": receipt["fingerprint"],
                           "video_sha256": receipt["video_sha256"]} for shot, (_, receipt) in zip(shots, checked)]}
    write_json(output_dir / "previs.json", manifest)
    emit({"operation": "assemble", "file": str(output), "verified": actual})
    return 0


def contact_sheet(args):
    project, root, shots, scene = _load(args.project, validate_layout=False)
    selected = _selected(shots, getattr(args, "shot", None))
    ffmpeg = _executable("ffmpeg", getattr(args, "ffmpeg", None))
    ffprobe = _executable("ffprobe", getattr(args, "ffprobe", None))
    columns = _positive_int(getattr(args, "columns", 3), "columns")
    checked = [_current_render(project, root, shots, scene, shot, ffprobe) for shot in selected]
    output_dir = project_path(project, root, "outputs")
    output_dir.mkdir(parents=True, exist_ok=True)
    samples = []
    for shot, (directory, receipt) in zip(selected, checked):
        for frame in (1, (shot["frames"] + 1)//2, shot["frames"]):
            image = _inside(directory / "frames" / f"frame_{frame:06d}.png", root)
            if (not valid_png(image, receipt["resolution"]) or
                    receipt["frame_sha256"].get(str(frame)) != sha256_file(image)):
                raise WorkflowError(f"Contact-sheet source frame changed or is missing: {image}")
            samples.append({"shot": shot["id"], "frame": frame, "image": image,
                            "fingerprint": receipt["fingerprint"]})
    rows = math.ceil(len(samples) / columns)
    if rows * 176 + (rows + 1)*8 > 65000 or columns * 320 + (columns + 1)*8 > 65000:
        raise WorkflowError("Contact sheet exceeds JPEG dimensions; select fewer shots or adjust --columns.")
    with tempfile.TemporaryDirectory(prefix=".contact-", dir=output_dir) as temporary_dir:
        temporary_dir = Path(temporary_dir)
        for index, sample in enumerate(samples):
            # Shot ids are validated ASCII, so the filter text contains no filter metacharacters.
            label = f"{sample['shot']}  frame {sample['frame']}"
            filter_chain = ("scale=320:144:force_original_aspect_ratio=decrease,"
                            "pad=320:176:(ow-iw)/2:0:color=0x111923,"
                            f"drawtext=text='{label}':fontcolor=white:fontsize=14:x=8:y=153")
            _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", sample["image"],
                  "-vf", filter_chain, "-frames:v", "1", "-threads", "1",
                  temporary_dir / f"tile_{index:06d}.png"], temporary_dir / "tile.log")
        destination = _inside(output_dir / "contact-sheet.jpg", root)
        _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-framerate", "1", "-start_number", "0",
              "-i", temporary_dir / "tile_%06d.png", "-vf",
              f"tile={columns}x{rows}:nb_frames={len(samples)}:padding=8:margin=8:color=0x111923",
              "-frames:v", "1", "-q:v", "2", "-threads", "1", destination], output_dir / "contact-sheet.log")
    write_json(output_dir / "contact-sheet.json", {"schema_version": "1.0", "sha256": sha256_file(destination),
               "columns": columns, "samples": [{k:v for k,v in sample.items() if k != "image"} for sample in samples]})
    emit({"operation": "contact-sheet", "file": str(destination), "tiles": len(samples)})
    return 0


def register(subparsers):
    def command(name, handler, help_text, *, select=False, render_tools=False):
        parser = subparsers.add_parser(name, help=help_text)
        parser.add_argument("--project", required=True, help="Project directory or project.json")
        if select:
            parser.add_argument("--shot", action="append", help="Select a shot id; repeat to select several")
        if render_tools:
            parser.add_argument("--ffmpeg", help="FFmpeg executable")
            parser.add_argument("--ffprobe", help="FFprobe executable")
        parser.set_defaults(func=handler)
        return parser
    parser = command("build", build_project, "Build validated per-shot Blender scenes", select=True)
    parser.add_argument("--blender", help="Blender executable (or BLENDER_BIN)")
    parser.add_argument("--engine", choices=("WORKBENCH", "CYCLES"), default="WORKBENCH")
    parser.add_argument("--samples", type=int, default=8, help="Cycles CPU sample count")
    parser.add_argument("--resolution", help="Explicit draft WIDTHxHEIGHT override; recorded in receipts")
    parser.add_argument("--force", action="store_true", help="Rebuild even if the current input is unchanged")
    parser = command("adopt-blend", adopt_blend, "Inspect and freeze a real MCP/authored .blend without rebuilding its geometry")
    parser.add_argument("--shot", required=True, help="Shot id from shots.json")
    parser.add_argument("--input", required=True, help="Existing .blend; external input paths are allowed")
    parser.add_argument("--note", required=True, help="What changed and why this snapshot is accepted")
    parser.add_argument("--scene", help="Choose a named scene; default is the saved active scene")
    parser.add_argument("--blender", help="Blender executable (or BLENDER_BIN)")
    parser.add_argument("--allow-draft-resolution", action="store_true", help="Explicitly accept a resolution differing from project.json")
    parser = command("render", render_project, "Resume verified PNG frames and encode exact-length clips", select=True, render_tools=True)
    parser.add_argument("--blender", help="Blender executable (or BLENDER_BIN)")
    parser.add_argument("--chunk-size", type=int, default=24, help="Frames per restartable Blender invocation")
    parser.add_argument("--software-gl", action="store_true", help="Request Mesa software OpenGL for Workbench")
    parser.add_argument("--force", action="store_true", help="Invalidate and rerender managed frame/video caches")
    command("assemble", assemble_project, "Assemble every verified shot in storyboard order", render_tools=True)
    parser = command("contact-sheet", contact_sheet, "Create labeled first/middle/last-frame contact sheet", select=True, render_tools=True)
    parser.add_argument("--columns", type=int, default=3)
