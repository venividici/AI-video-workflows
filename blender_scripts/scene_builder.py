"""JSON-driven Blender scene creation, callable through MCP or command line.

Inside a connected Blender session:
    from blender_scripts.scene_builder import build_shot
    build_shot(bundle, '/project/build/s001.blend')

This builds a new owned Scene; it does not delete the user's current scene.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

import bpy
from mathutils import Matrix, Vector


def _primitive(spec, prefix, materials):
    kind = spec["type"]
    if kind in {"empty", "actor"}:
        obj = bpy.data.objects.new(prefix + spec["id"], None)
        bpy.context.scene.collection.objects.link(obj)
        obj.empty_display_type = "PLAIN_AXES"
    elif kind == "text":
        data = bpy.data.curves.new(prefix + spec["id"], "FONT")
        data.body = spec.get("text", "")
        data.align_x = "CENTER"
        data.size = 0.3
        data.extrude = 0.002
        obj = bpy.data.objects.new(prefix + spec["id"], data)
        bpy.context.scene.collection.objects.link(obj)
    else:
        operators = {"box": (bpy.ops.mesh.primitive_cube_add, {"size": 1}),
                     "sphere": (bpy.ops.mesh.primitive_uv_sphere_add, {"segments": 16, "ring_count": 8, "radius": 0.5}),
                     "cylinder": (bpy.ops.mesh.primitive_cylinder_add, {"vertices": 24, "radius": 0.5, "depth": 1}),
                     "cone": (bpy.ops.mesh.primitive_cone_add, {"vertices": 24, "radius1": 0.5, "depth": 1}),
                     "plane": (bpy.ops.mesh.primitive_plane_add, {"size": 1})}
        operator, kwargs = operators[kind]
        operator(**kwargs)
        obj = bpy.context.object
        obj.name = prefix + spec["id"]
    material = materials.get(spec.get("material"))
    if material and hasattr(obj.data, "materials"):
        obj.data.materials.append(material)
        obj.color = material.diffuse_color
    if kind == "actor":
        # A recognisable, deliberately simple 1.75 m blocking mannequin.
        parts = [("torso", "box", [0, 0, 1.12], [0.42, 0.23, 0.55]),
                 ("head", "sphere", [0, 0, 1.62], [0.27, 0.27, 0.29]),
                 ("left_leg", "cylinder", [-0.12, 0, 0.43], [0.15, 0.15, 0.85]),
                 ("right_leg", "cylinder", [0.12, 0, 0.43], [0.15, 0.15, 0.85]),
                 ("left_arm", "cylinder", [-0.29, 0, 1.09], [0.12, 0.12, 0.60]),
                 ("right_arm", "cylinder", [0.29, 0, 1.09], [0.12, 0.12, 0.60]),
                 ("nose", "box", [0, -0.15, 1.61], [0.07, 0.08, 0.07])]
        for name, shape, location, dimensions in parts:
            child = _primitive({"id": spec["id"] + "." + name, "type": shape,
                                "location": location, "dimensions": dimensions,
                                "material": spec.get("material")}, prefix, materials)
            child.parent = obj
    obj.location = spec.get("location", [0, 0, 0])
    obj.rotation_euler = [math.radians(v) for v in spec.get("rotation_degrees", [0, 0, 0])]
    if "dimensions" in spec and kind not in {"empty", "actor"}:
        dimensions = spec["dimensions"]
        if kind == "plane":
            obj.dimensions = [dimensions[0], dimensions[1], 0]
        else:
            obj.dimensions = dimensions
        if obj.type == "MESH":
            obj.data.transform(Matrix.Diagonal(Vector((*obj.scale, 1.0))))
            obj.scale = (1, 1, 1)
    if "scale" in spec:
        obj.scale = [a*b for a,b in zip(obj.scale, spec["scale"])]
    return obj


def _fcurves(obj):
    if not obj.animation_data or not obj.animation_data.action:
        return []
    action = obj.animation_data.action
    try:
        return list(action.fcurves)
    except (AttributeError, RuntimeError):
        result = []
        for layer in action.layers:
            for strip in layer.strips:
                for channelbag in strip.channelbags:
                    result.extend(channelbag.fcurves)
        return result


def _interpolation(obj, value):
    for curve in _fcurves(obj):
        for point in curve.keyframe_points:
            point.interpolation = value
            if value == "BEZIER":
                point.handle_left_type = "AUTO_CLAMPED"
                point.handle_right_type = "AUTO_CLAMPED"


def _aim(camera, target, objects):
    if isinstance(target, str):
        bpy.context.view_layer.update()
        position = objects[target].matrix_world.translation
    else:
        position = Vector(target)
    direction = position - camera.location
    if direction.length < 1e-6:
        raise ValueError("Camera location and target coincide.")
    camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def build_shot(bundle, output_path, *, replace_scene=False):
    """Build one validated shot and save a .blend; suitable for an MCP Python call.

    A bundle is created by aivw.blender.make_bundle.  Existing unrelated scenes
    are preserved.  Explicit replace_scene only replaces a previously owned shot.
    """
    repo = Path(__file__).resolve().parents[1]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from aivw.blender import _archive_build, content_hash, sha256_file, validate_scene
    from aivw.common import WorkflowError, read_json, write_json
    project_root = Path(bundle["project_root"]).resolve()
    output_path = Path(output_path).resolve()
    if not output_path.is_relative_to(project_root):
        raise WorkflowError("Blender output must remain inside the project.")
    source = {key: value for key, value in bundle.items() if key not in {"schema_version", "project_root", "input_hash"}}
    if content_hash(source) != bundle["input_hash"]:
        raise WorkflowError("Build bundle fingerprint mismatch.")
    spec, shot, settings = bundle["scene"], bundle["shot"], bundle["settings"]
    old_receipt = output_path.with_suffix(".build.json")
    if old_receipt.is_file() and read_json(old_receipt).get("origin") == "mcp_snapshot":
        raise WorkflowError("This file is an adopted MCP snapshot. Use host build --force to explicitly return to a declarative layout.")
    if settings["engine"] not in {"WORKBENCH", "CYCLES"}:
        raise WorkflowError("Unsupported engine in build bundle.")
    # Validate the selected shot without pretending all other shots are rendered.
    local_spec = {**spec, "shots": {shot["id"]: spec["shots"][shot["id"]]}}
    validate_scene(local_spec, [shot], settings["fps"])
    layout = spec["shots"][shot["id"]]
    name = "aivw_" + shot["id"]
    old_scene = bpy.data.scenes.get(name)
    if old_scene:
        if not replace_scene or old_scene.get("aivw_shot_id") != shot["id"]:
            raise WorkflowError(f"Scene {name} exists; explicitly request replace_scene for an owned scene.")
        for obj in list(old_scene.objects):
            if obj.users_scene == (old_scene,):
                bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.scenes.remove(old_scene)
    scene = bpy.data.scenes.new(name)
    scene["aivw_shot_id"] = shot["id"]
    scene["aivw_input_hash"] = bundle["input_hash"]
    scene["aivw_settings"] = json.dumps(settings, sort_keys=True)
    scene["aivw_project_root"] = str(project_root)
    if bpy.context.window is None:
        raise WorkflowError("Blender has no window context for assigning its active scene.")
    bpy.context.window.scene = scene
    scene.frame_start, scene.frame_end = 1, shot["frames"]
    scene.render.fps, scene.render.fps_base = settings["fps"], 1.0
    scene.render.resolution_x, scene.render.resolution_y = settings["resolution"]
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.color_depth = "8"
    scene.render.film_transparent = False
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.exposure = 0
    scene.view_settings.gamma = 1
    scene.render.engine = "BLENDER_WORKBENCH" if settings["engine"] == "WORKBENCH" else "CYCLES"
    if settings["engine"] == "CYCLES":
        scene.cycles.device = "CPU"
        scene.cycles.samples = settings["samples"]
        scene.cycles.use_denoising = False
        scene.cycles.use_adaptive_sampling = False
        scene.cycles.seed = 0
    else:
        shading = scene.display.shading
        shading.light, shading.color_type = "STUDIO", "MATERIAL"
        shading.show_shadows, shading.show_cavity = True, True
        shading.cavity_type = "BOTH"
        shading.background_type = "WORLD"
        scene.display.render_aa = "8"
    group = spec["scenes"][layout["scene"]]
    background = group.get("background", [0.08, 0.09, 0.12])
    world = bpy.data.worlds.new(name + "_world")
    world.color = background
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (*background, 1)
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.7
    scene.world = world
    materials = {}
    for mid, material_spec in spec["materials"].items():
        material = bpy.data.materials.new(name + "_" + mid)
        material.diffuse_color = material_spec["base_color"]
        material.use_nodes = True
        bsdf = material.node_tree.nodes.get("Principled BSDF")
        bsdf.inputs["Base Color"].default_value = material_spec["base_color"]
        bsdf.inputs["Roughness"].default_value = material_spec.get("roughness", 0.8)
        materials[mid] = material
    wanted = set(group["objects"])
    objects = {o["id"]: _primitive(o, name + "_", materials) for o in spec["objects"] if o["id"] in wanted}
    for o in spec["objects"]:
        if o["id"] in objects and o.get("parent"):
            objects[o["id"]].parent = objects[o["parent"]]
    for animation in layout.get("animation", []):
        obj = objects[animation["object"]]
        prop = animation["property"]
        data_path = "rotation_euler" if prop == "rotation_degrees" else prop
        for key in animation["keyframes"]:
            value = [math.radians(v) for v in key["value"]] if prop == "rotation_degrees" else key["value"]
            setattr(obj, data_path, value)
            obj.keyframe_insert(data_path=data_path, frame=key["frame"])
        _interpolation(obj, animation.get("interpolation", "BEZIER"))
    camera_spec = spec["cameras"][layout["camera"]]
    camera_data = bpy.data.cameras.new(name + "_camera")
    camera = bpy.data.objects.new(name + "_camera", camera_data)
    scene.collection.objects.link(camera)
    camera.location = camera_spec["location"]
    camera_data.lens = camera_spec.get("lens_mm", 50)
    camera_data.clip_start, camera_data.clip_end = 0.01, 1000
    if camera_spec.get("projection") == "orthographic":
        camera_data.type, camera_data.ortho_scale = "ORTHO", camera_spec["ortho_scale"]
    scene.frame_set(1)
    _aim(camera, camera_spec["target"], objects)
    keys = layout.get("camera_keyframes", [])
    for key in keys:
        scene.frame_set(key["frame"])
        camera.location = key.get("location", camera_spec["location"])
        _aim(camera, key.get("target", camera_spec["target"]), objects)
        camera_data.lens = key.get("lens_mm", camera_spec.get("lens_mm", 50))
        camera.keyframe_insert(data_path="location", frame=key["frame"])
        camera.keyframe_insert(data_path="rotation_euler", frame=key["frame"])
        camera_data.keyframe_insert(data_path="lens", frame=key["frame"])
    _interpolation(camera, "BEZIER")
    _interpolation(camera_data, "BEZIER")
    scene.camera = camera
    for light_name, location, energy, size in [("key", (1, -3, 6), 900, 5), ("fill", (-3, 2, 4), 500, 4)]:
        data = bpy.data.lights.new(name + "_" + light_name, "AREA")
        data.energy, data.shape, data.size = energy, "DISK", size
        obj = bpy.data.objects.new(name + "_" + light_name, data)
        scene.collection.objects.link(obj)
        obj.location = location
        obj.rotation_euler = (Vector((0, 0, 0.5)) - obj.location).to_track_quat("-Z", "Y").to_euler()
    scene.frame_set(1)
    bpy.context.view_layer.update()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.name == shot["id"] + ".blend":
        _archive_build(output_path.parent, shot["id"], project_root)
    filepaths = bpy.context.preferences.filepaths
    previous_save_version = filepaths.save_version
    try:
        filepaths.save_version = 0
        bpy.ops.wm.save_as_mainfile(filepath=str(output_path))
    finally:
        filepaths.save_version = previous_save_version
    if output_path.name == shot["id"] + ".blend":
        # MCP callers receive the same receipt consumed by the host render CLI.
        write_json(output_path.with_suffix(".build.json"), {"schema_version": "1.0",
                   "shot_id": shot["id"], "status": "complete", "origin": "declarative",
                   "input_hash": bundle["input_hash"], "blend_sha256": sha256_file(output_path),
                   "blender_version": "Blender " + bpy.app.version_string, "settings": settings,
                   "frames": shot["frames"], "file": str(output_path.relative_to(project_root))})
    return {"shot_id": shot["id"], "scene": scene.name, "file": str(output_path),
            "frames": shot["frames"], "input_hash": bundle["input_hash"], "blender": bpy.app.version_string}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    bundle = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    print(json.dumps(build_shot(bundle, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
