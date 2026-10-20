"""Inspect an artist/MCP Blender file and save an immutable rendering snapshot."""

import argparse
import json
from pathlib import Path
import sys

import bpy


def preserve_dependencies():
    """Pack supported assets; fingerprint files that Blender still reads externally."""
    repo = Path(__file__).resolve().parents[1]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from aivw.blender import dependency_record

    # The snapshot is saved in a different folder.  Keep unresolved resources
    # anchored to their original location instead of rebasing // paths.
    bpy.ops.file.make_paths_absolute()
    bpy.ops.file.pack_all()
    # Blender 4.5.3 runtime: packed=False excludes embedded assets.  Its older
    # docstring says the reverse; the integration test checks the real behavior.
    paths = set(bpy.utils.blend_paths(absolute=True, packed=False, local=False))
    # blend_paths reports the first file for image/volume sequences, not every
    # member.  Track their directory to detect changed, added or removed frames.
    for collection in (bpy.data.images, bpy.data.volumes):
        for data in collection:
            if (getattr(data, "source", None) == "SEQUENCE" or getattr(data, "is_sequence", False)):
                raw = bpy.path.abspath(data.filepath, library=data.library)
                paths.add(str(Path(raw).absolute().parent))
    records = [dependency_record(path) for path in sorted(paths) if path]
    packed_images = sum(bool(image.packed_file or image.packed_files) for image in bpy.data.images)
    return records, packed_images


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    request = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    scene = bpy.data.scenes.get(request["scene"]) if request.get("scene") else bpy.context.scene
    if scene is None:
        raise ValueError("Requested Blender scene does not exist.")
    bpy.context.window.scene = scene
    if scene.camera is None or scene.camera.type != "CAMERA" or scene.camera.name not in scene.objects:
        raise ValueError("Snapshot needs a valid active camera present in the selected scene.")
    if scene.frame_start != 1 or scene.frame_end != request["frames"]:
        raise ValueError(f"Expected frame range 1..{request['frames']}, got {scene.frame_start}..{scene.frame_end}.")
    if scene.render.fps != request["fps"] or scene.render.fps_base != 1:
        raise ValueError(f"Expected exactly {request['fps']} fps with fps_base=1.")
    size = [scene.render.resolution_x, scene.render.resolution_y]
    if scene.render.resolution_percentage != 100 or any(n <= 0 or n % 2 for n in size):
        raise ValueError("Snapshot must use positive even dimensions at 100% resolution.")
    draft = size != request["resolution"]
    if draft and not request["allow_draft_resolution"]:
        raise ValueError("Snapshot resolution differs from project; use --allow-draft-resolution explicitly.")
    if scene.render.engine not in {"BLENDER_WORKBENCH", "CYCLES", "BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"}:
        raise ValueError(f"Unsupported render engine: {scene.render.engine}")
    engine = {"BLENDER_WORKBENCH": "WORKBENCH", "CYCLES": "CYCLES",
              "BLENDER_EEVEE_NEXT": "EEVEE", "BLENDER_EEVEE": "EEVEE"}[scene.render.engine]
    settings = {"fps": request["fps"], "resolution": size, "engine": engine,
                "samples": int(scene.cycles.samples) if engine == "CYCLES" else None,
                "draft_override": draft}
    root = Path(request["project_root"]).resolve()
    for key in ("output", "inspection"):
        if not Path(request[key]).resolve().is_relative_to(root):
            raise ValueError("Snapshot output escapes the project.")
    # Normalize delivery format only.  Geometry, rigging, modifiers and shaders stay intact.
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.color_depth = "8"
    scene["aivw_shot_id"] = request["shot_id"]
    scene["aivw_input_hash"] = request["input_hash"]
    scene["aivw_settings"] = json.dumps(settings, sort_keys=True)
    scene["aivw_project_root"] = str(root)
    scene.frame_set(1)
    dependencies, packed_images = preserve_dependencies()
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=request["output"], relative_remap=False)
    report = {"scene": scene.name, "camera": scene.camera.name, "objects": len(scene.objects),
              "frame_start": scene.frame_start, "frame_end": scene.frame_end,
              "settings": settings, "blender": bpy.app.version_string,
              "dependencies": dependencies, "packed_images": packed_images}
    Path(request["inspection"]).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
