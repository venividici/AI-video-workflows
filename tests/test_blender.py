"""Integrity tests plus opt-in, real Blender/FFmpeg end-to-end verification."""

from contextlib import redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
import zlib

from aivw import blender as workflow
from aivw.common import WorkflowError, read_json, write_json

ROOT = Path(__file__).resolve().parents[1]
BLENDER = os.environ.get("BLENDER_BIN") or shutil.which("blender")
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def args(project, **changes):
    defaults = dict(project=str(project), shot=None, blender=BLENDER, ffmpeg=FFMPEG,
                    ffprobe=FFPROBE, engine="CYCLES", samples=2, resolution="160x90",
                    force=False, chunk_size=48, software_gl=False, columns=3)
    defaults.update(changes)
    return SimpleNamespace(**defaults)


def png_bytes(width=2, height=2):
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
    raw = b"".join(b"\x00" + bytes([20, 80, 140])*width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class SceneIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.project, self.root, self.shots, self.scene = workflow._load(ROOT / "examples/product-orbit")

    def test_two_different_scene_types_validate(self):
        self.assertEqual(sum(s["frames"] for s in self.shots), 84)
        _, _, shots, _ = workflow._load(ROOT / "examples/dialogue-blocking")
        self.assertEqual(sum(s["frames"] for s in shots), 120)

    def test_missing_layout_never_falls_back_to_generic_geometry(self):
        del self.scene["shots"]["p001"]
        with self.assertRaisesRegex(WorkflowError, "Missing executable layout"):
            workflow.validate_scene(self.scene, self.shots, 24)

    def test_timing_is_not_silently_changed(self):
        self.shots[0]["frames"] = 47
        with self.assertRaisesRegex(WorkflowError, "frames does not match"):
            workflow.validate_scene(self.scene, self.shots, 24)
        self.shots[0]["frames"] = 48
        self.shots[0]["duration_seconds"] = None
        with self.assertRaisesRegex(WorkflowError, "unresolved"):
            workflow.validate_scene(self.scene, self.shots, 24)

    def test_animation_cannot_escape_shot_range(self):
        self.scene["shots"]["p001"]["camera_keyframes"][-1]["frame"] = 49
        with self.assertRaisesRegex(WorkflowError, "within 1..48"):
            workflow.validate_scene(self.scene, self.shots, 24)

    def test_missing_camera_parent_cycles_and_nonfinite_values(self):
        original = copy.deepcopy(self.scene)
        self.scene["shots"]["p001"]["camera"] = "absent"
        with self.assertRaisesRegex(WorkflowError, "Missing camera"):
            workflow.validate_scene(self.scene, self.shots, 24)
        self.scene = copy.deepcopy(original)
        self.scene["objects"][0]["parent"] = "stage"
        with self.assertRaisesRegex(WorkflowError, "Parent cycle"):
            workflow.validate_scene(self.scene, self.shots, 24)
        self.scene = original
        self.scene["objects"][0]["location"][0] = float("nan")
        with self.assertRaisesRegex(WorkflowError, "finite"):
            workflow.validate_scene(self.scene, self.shots, 24)

    def test_external_blend_marker_does_not_require_primitive_camera(self):
        scene = {"schema_version": "1.0", "materials": {}, "objects": [], "scenes": {}, "cameras": {},
                 "shots": {s["id"]: {"mode": "external_blend", "scene": s["scene_id"]} for s in self.shots}}
        workflow.validate_scene(scene, self.shots, 24)
        scene["shots"]["p001"]["scene"] = "wrong-scene"
        with self.assertRaisesRegex(WorkflowError, "differ"):
            workflow.validate_scene(scene, self.shots, 24)

    def test_fingerprint_changes_for_geometry_but_not_unrelated_camera(self):
        settings = workflow._settings(self.project, args(self.root))
        before = workflow.make_bundle(self.project, self.root, self.shots, self.scene, self.shots[0], settings)
        self.scene["cameras"]["detail"]["lens_mm"] = 80
        unrelated = workflow.make_bundle(self.project, self.root, self.shots, self.scene, self.shots[0], settings)
        self.assertEqual(before["input_hash"], unrelated["input_hash"])
        self.scene["objects"][1]["dimensions"][0] = 2.3
        changed = workflow.make_bundle(self.project, self.root, self.shots, self.scene, self.shots[0], settings)
        self.assertNotEqual(before["input_hash"], changed["input_hash"])

    def test_path_traversal_and_symlink_outputs_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            outside = Path(temp) / "outside"
            outside.mkdir()
            with self.assertRaises(WorkflowError):
                workflow._inside(root / ".." / "outside" / "frame.png", root)
            try:
                (root / "renders").symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation unavailable")
            with self.assertRaises(WorkflowError):
                workflow._inside(root / "renders" / "frame.png", root)

    def test_png_partial_crc_and_resolution_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "frame.png"
            file.write_bytes(png_bytes())
            self.assertTrue(workflow.valid_png(file, [2, 2]))
            self.assertFalse(workflow.valid_png(file, [4, 2]))
            file.write_bytes(png_bytes()[:-1])
            self.assertFalse(workflow.valid_png(file))
            corrupted = bytearray(png_bytes())
            corrupted[45] ^= 1
            file.write_bytes(corrupted)
            self.assertFalse(workflow.valid_png(file))

    def test_cached_build_rejects_mutated_geometry_and_binary(self):
        with tempfile.TemporaryDirectory() as temp:
            project_dir = Path(temp) / "project"
            shutil.copytree(self.root, project_dir, ignore=shutil.ignore_patterns("build", "renders", "outputs"))
            p, r, shots, scene = workflow._load(project_dir)
            settings = workflow._settings(p, args(r))
            bundle = workflow.make_bundle(p, r, shots, scene, shots[0], settings)
            build_dir = r / "build"
            build_dir.mkdir()
            blend = build_dir / "p001.blend"
            blend.write_bytes(b"unit-test receipt fixture; not a real Blender scene")
            write_json(build_dir / "p001.build.json", {"status": "complete", "settings": settings,
                       "input_hash": bundle["input_hash"], "blend_sha256": workflow.sha256_file(blend)})
            workflow._current_build(p, r, shots, scene, shots[0])
            scene["objects"][0]["dimensions"][0] = 7
            with self.assertRaisesRegex(WorkflowError, "source or builder changed"):
                workflow._current_build(p, r, shots, scene, shots[0])
            scene["objects"][0]["dimensions"][0] = 6
            blend.write_bytes(b"modified binary")
            with self.assertRaisesRegex(WorkflowError, "differs from its receipt"):
                workflow._current_build(p, r, shots, scene, shots[0])

    def test_snapshot_external_dependency_content_and_directory_membership_are_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            asset = folder / "asset.blend"
            asset.write_bytes(b"first artist revision")
            file_record = workflow.dependency_record(asset)
            receipt = {"inspection": {"dependencies": [file_record]}}
            workflow._verify_dependencies(receipt, "p001")
            asset.write_bytes(b"second artist revision")
            with self.assertRaisesRegex(WorkflowError, "external dependency changed"):
                workflow._verify_dependencies(receipt, "p001")
            receipt["inspection"]["dependencies"] = [workflow.dependency_record(folder)]
            (folder / "new_frame.exr").write_bytes(b"new cache member")
            with self.assertRaisesRegex(WorkflowError, "external dependency changed"):
                workflow._verify_dependencies(receipt, "p001")
            asset.unlink()
            with self.assertRaisesRegex(WorkflowError, "dependency is missing"):
                workflow.dependency_record(asset)

    def test_external_symlink_retarget_does_not_pin_the_old_dependency(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            first, second, link = folder / "first.blend", folder / "second.blend", folder / "active.blend"
            first.write_bytes(b"old asset")
            second.write_bytes(b"new asset")
            try:
                link.symlink_to(first)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation unavailable")
            receipt = {"inspection": {"dependencies": [workflow.dependency_record(link)]}}
            workflow._verify_dependencies(receipt, "p001")
            link.unlink()
            link.symlink_to(second)
            with self.assertRaisesRegex(WorkflowError, "external dependency changed"):
                workflow._verify_dependencies(receipt, "p001")


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg/FFprobe are required for real frame-count verification")
class VideoIntegrityTests(unittest.TestCase):
    def test_actual_encoded_frame_count_is_verified(self):
        with tempfile.TemporaryDirectory() as temp:
            video = Path(temp) / "six.mp4"
            subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=64x48:r=24",
                            "-frames:v", "6", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-threads", "1", str(video)], check=True)
            verified = workflow._verify_video(video, 6, 24, [64, 48], FFPROBE)
            self.assertEqual(verified["frames"], 6)
            with self.assertRaisesRegex(WorkflowError, "timing/size mismatch"):
                workflow._verify_video(video, 7, 24, [64, 48], FFPROBE)


@unittest.skipUnless(os.environ.get("AIVW_RUN_BLENDER_TESTS") == "1" and BLENDER and FFMPEG and FFPROBE,
                     "Set AIVW_RUN_BLENDER_TESTS=1 and BLENDER_BIN for real Blender tests")
class BlenderEndToEndTests(unittest.TestCase):
    def test_two_examples_and_mcp_snapshot_adoption(self):
        with tempfile.TemporaryDirectory(prefix="aivw-e2e-") as temp, redirect_stdout(io.StringIO()):
            for name, expected in (("product-orbit", 84), ("dialogue-blocking", 120)):
                project = Path(temp) / name
                shutil.copytree(ROOT / "examples" / name, project,
                                ignore=shutil.ignore_patterns("build", "renders", "outputs"))
                command = args(project)
                workflow.build_project(command)
                workflow.render_project(command)
                workflow.assemble_project(command)
                workflow.contact_sheet(command)
                self.assertEqual(workflow.probe_video(project / "outputs/previs.mp4", FFPROBE)["frames"], expected)
                self.assertGreater((project / "outputs/contact-sheet.jpg").stat().st_size, 1000)
                first = "p001" if name == "product-orbit" else "d001"
                frame = project / "renders" / first / "frames/frame_000024.png"
                original_time = frame.stat().st_mtime_ns
                workflow.render_project(args(project, shot=[first]))
                self.assertEqual(frame.stat().st_mtime_ns, original_time, "unchanged verified frames should be reused")
                # A partial/truncated output must be rendered again, never encoded as if complete.
                frame.write_bytes(frame.read_bytes()[:35])
                workflow.render_project(args(project, shot=[first]))
                self.assertTrue(workflow.valid_png(frame, [160, 90]))
                if name == "product-orbit":
                    # The callable API shares the user's Blender process through MCP.
                    # Saving must restore their backup preference even when saving fails.
                    api_check = f"""
import sys
from pathlib import Path
import bpy
sys.path.insert(0, {str(ROOT)!r})
from aivw.blender import _load, make_bundle
from blender_scripts.scene_builder import build_shot
p, root, shots, scene = _load({str(project)!r})
settings = {{'fps': p['fps'], 'resolution': [160, 90], 'engine': 'CYCLES', 'samples': 2, 'draft_override': True}}
bundle = make_bundle(p, root, shots, scene, shots[0], settings)
bpy.context.preferences.filepaths.save_version = 3
output = root / 'api_success.blend'
build_shot(bundle, output)
assert output.is_file()
assert bpy.context.preferences.filepaths.save_version == 3, 'save success changed user backup preference'
bad_output = root / 'cannot-save.blend'
bad_output.mkdir()
try:
    build_shot(bundle, bad_output, replace_scene=True)
except RuntimeError:
    pass
else:
    raise AssertionError('expected actual Blender save failure for an existing directory')
assert bpy.context.preferences.filepaths.save_version == 3, 'save failure changed user backup preference'
"""
                    subprocess.run([BLENDER, "--background", "--factory-startup", "--python-exit-code", "1",
                                    "--python-expr", api_check], check=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
                    # Preserve a custom modifier/mesh that the primitive schema does not express.
                    snapshot = project / "authored.blend"
                    texture = project / "external_texture.png"
                    texture.write_bytes(png_bytes())
                    library = project / "linked_asset.blend"
                    code = ("import bpy; bpy.ops.mesh.primitive_torus_add(major_radius=0.25,minor_radius=0.08,location=(1,0,0.5)); "
                            "o=bpy.context.object; o.name='custom_mcp_torus'; o.modifiers.new('Artist bevel','BEVEL'); "
                            f"image=bpy.data.images.load({str(texture)!r}); image.use_fake_user=True; "
                            f"bpy.data.libraries.write({str(library)!r},{{o}}); "
                            "bpy.data.objects.remove(o,do_unlink=True); "
                            f"manager=bpy.data.libraries.load({str(library)!r},link=True); "
                            "src,dst=manager.__enter__(); dst.objects=['custom_mcp_torus']; manager.__exit__(None,None,None); "
                            "bpy.context.scene.collection.objects.link(dst.objects[0]); "
                            f"bpy.ops.wm.save_as_mainfile(filepath={str(snapshot)!r})")
                    subprocess.run([BLENDER, "--background", str(project / "build/p001.blend"),
                                    "--python-exit-code", "1", "--python-expr", code],
                                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
                    workflow.adopt_blend(args(project, shot="p001", input=str(snapshot), note="Preserve custom torus and modifier",
                                              scene=None, allow_draft_resolution=True))
                    receipt = read_json(project / "build/p001.build.json")
                    self.assertEqual(receipt["origin"], "mcp_snapshot")
                    self.assertGreaterEqual(receipt["inspection"]["packed_images"], 1)
                    dependencies = receipt["inspection"]["dependencies"]
                    self.assertIn(str(library), [d["path"] for d in dependencies])
                    self.assertNotIn(str(texture), [d["path"] for d in dependencies])
                    texture.unlink()  # The packed resource survives removal of its old external source.
                    self.assertEqual(read_json(project / "scene.json")["shots"]["p001"]["mode"], "external_blend")
                    self.assertTrue(list((project / "build/history/p001").glob("*.blend")))
                    workflow.render_project(args(project, shot=["p001"]))
                    # Linked libraries remain external and cannot change behind the render cache.
                    original_library = library.read_bytes()
                    library.write_bytes(original_library + b"changed dependency")
                    with self.assertRaisesRegex(WorkflowError, "external dependency changed"):
                        workflow.render_project(args(project, shot=["p001"]))
                    library.write_bytes(original_library)
                    # A source/timing revision cannot silently use the old accepted snapshot.
                    data = read_json(project / "shots.json")
                    data["shots"][0]["description"] += " changed source"
                    write_json(project / "shots.json", data)
                    with self.assertRaisesRegex(WorkflowError, "reconcile"):
                        workflow.render_project(args(project, shot=["p001"]))


if __name__ == "__main__":
    unittest.main()
