"""Actual FFmpeg tests for generated-source conform and final straight cuts."""

import argparse
from array import array
from contextlib import redirect_stdout
import hashlib
import io
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from aivw import finishing, seedance
from aivw.common import WorkflowError, read_json, write_json


class FinishingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise unittest.SkipTest("Actual finishing tests require FFmpeg/ffprobe.")
        cls.media = tempfile.TemporaryDirectory(prefix="aivw-finish-media-")
        cls.red = Path(cls.media.name) / "red-with-tone.mp4"
        cls.blue = Path(cls.media.name) / "blue-no-audio.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                        "color=c=red:s=192x108:r=24:d=4", "-f", "lavfi", "-i",
                        "sine=frequency=440:sample_rate=48000:duration=4", "-map", "0:v:0", "-map", "1:a:0",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "4", str(cls.red)],
                       check=True, capture_output=True)
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                        "color=c=blue:s=192x108:r=24:d=4", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(cls.blue)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.media.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aivw-finish-test-")
        self.root = Path(self.tmp.name)
        self.project = {"schema_version": "1.0", "id": "generic-finish", "title": "Generic finishing test",
                        "fps": 24, "resolution": [320, 180], "provider": {"name": "seedance", "mode": "manual", "model": None},
                        "paths": {"shots": "shots.json", "renders": "renders", "outputs": "outputs", "build": "build", "scene": "scene.json"}}
        self.shots = [{"id": "s001", "frames": 36, "duration_seconds": 1.5, "description": "Red shot.", "dialogue": ""},
                      {"id": "s002", "frames": 48, "duration_seconds": 2.0, "description": "Blue shot.", "dialogue": ""}]
        self.raw1 = self.root / "raw-red.mp4"
        self.raw2 = self.root / "raw-blue.mp4"
        shutil.copyfile(self.red, self.raw1)
        shutil.copyfile(self.blue, self.raw2)
        self._save()

    def tearDown(self):
        self.tmp.cleanup()

    def _save(self):
        write_json(self.root / "project.json", self.project)
        write_json(self.root / "shots.json", {"schema_version": "1.0", "shots": self.shots, "issues": []})

    def _run(self, *args):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command", required=True)
        finishing.register(sub)
        seedance.register(sub)
        if args[0] == "seedance":
            parameters = [args[0], args[1], "--project", str(self.root), *args[2:]]
        else:
            parameters = [args[0], "--project", str(self.root), *args[1:]]
        parsed = parser.parse_args(parameters)
        output = io.StringIO()
        with redirect_stdout(output):
            status = parsed.func(parsed)
        return status, json.loads(output.getvalue())

    def _both(self):
        self._run("conform", "--shot", "s001", "--input", str(self.raw1), "--start-seconds", "0.5")
        self._run("conform", "--shot", "s002", "--input", str(self.raw2), "--start-seconds", "1")

    def _audio_rms(self, path, start, duration):
        result = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-ss", str(start),
                                 "-t", str(duration), "-vn", "-ac", "1", "-ar", "48000", "-f", "s16le", "pipe:1"],
                                check=True, capture_output=True)
        samples = array("h")
        samples.frombytes(result.stdout)
        self.assertTrue(samples)
        return math.sqrt(sum(float(x) * x for x in samples) / len(samples))

    def _color(self, path, second):
        result = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", str(second), "-i", str(path),
                                 "-frames:v", "1", "-vf", "scale=1:1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"],
                                check=True, capture_output=True)
        self.assertEqual(len(result.stdout), 3)
        return tuple(result.stdout)

    def test_two_shot_final_exact_frames_order_and_mixed_audio(self):
        original1 = hashlib.sha256(self.raw1.read_bytes()).hexdigest()
        original2 = hashlib.sha256(self.raw2.read_bytes()).hexdigest()
        self._both()
        _, output = self._run("assemble-final")
        final = self.root / "outputs/final.mp4"
        receipt = read_json(self.root / "outputs/final.json")
        self.assertEqual(output["frames"], 84)
        self.assertEqual(receipt["output"]["probe"]["frames"], 84)
        self.assertEqual(receipt["output"]["probe"]["fps"], 24)
        self.assertEqual(receipt["resolution"], [320, 180])
        self.assertEqual(receipt["timeline"][0]["end_frame_exclusive"], 36)
        self.assertEqual(receipt["timeline"][1]["start_frame_inclusive"], 36)
        self.assertGreater(self._audio_rms(final, 0.2, 0.5), 100)
        self.assertLess(self._audio_rms(final, 1.8, 0.5), 5)
        red = self._color(final, 0.5)
        blue = self._color(final, 2.5)
        self.assertGreater(red[0], red[2] + 100)
        self.assertGreater(blue[2], blue[0] + 100)
        self.assertEqual(hashlib.sha256(self.raw1.read_bytes()).hexdigest(), original1)
        self.assertEqual(hashlib.sha256(self.raw2.read_bytes()).hexdigest(), original2)
        second = read_json(self.root / "outputs/final-clips/s002.json")
        self.assertEqual(second["recipe"]["audio"]["action"], "silent-track-for-silent-source")
        self.assertFalse((self.root / "outputs/previs.mp4").exists())

    def test_fps_conversion_preserves_duration_and_exact_target_frames(self):
        self.project["fps"] = 30
        self.shots[0]["frames"] = 45
        self.shots[1]["frames"] = 60
        self._save()
        self._run("conform", "--shot", "s001", "--input", str(self.raw1), "--start-seconds", "0.5")
        receipt = read_json(self.root / "outputs/final-clips/s001.json")
        self.assertEqual(receipt["output"]["probe"]["frames"], 45)
        self.assertEqual(receipt["output"]["probe"]["fps"], 30)
        self.assertAlmostEqual(receipt["output"]["probe"]["duration_seconds"], 1.5, places=5)
        self.assertEqual(receipt["recipe"]["speed"], 1.0)
        self.assertFalse(receipt["recipe"]["video_padding"])

    def test_insufficient_raw_refuses_speed_change_or_video_padding(self):
        with self.assertRaisesRegex(WorkflowError, "too short"):
            self._run("conform", "--shot", "s001", "--input", str(self.raw1), "--start-seconds", "3")
        self.assertFalse((self.root / "outputs/final-clips/s001.mp4").exists())

    def test_invalid_start_is_rejected(self):
        for value in ("-1", "nan", "inf"):
            with self.subTest(value=value), self.assertRaisesRegex(WorkflowError, "finite nonnegative"):
                self._run("conform", "--shot", "s001", "--input", str(self.raw1), "--start-seconds", value)

    def test_explicit_mute_has_silent_audio_and_records_choice(self):
        self._run("conform", "--shot", "s001", "--input", str(self.raw1), "--start-seconds", "0", "--audio", "mute")
        receipt = read_json(self.root / "outputs/final-clips/s001.json")
        self.assertEqual(receipt["recipe"]["audio"]["action"], "explicit-muted")
        self.assertLess(self._audio_rms(self.root / "outputs/final-clips/s001.mp4", 0.2, 0.5), 5)

    def test_missing_shot_refuses_assembly(self):
        self._run("conform", "--shot", "s001", "--input", str(self.raw1), "--start-seconds", "0")
        with self.assertRaisesRegex(WorkflowError, "Missing conformed final clip"):
            self._run("assemble-final")
        self.assertFalse((self.root / "outputs/final.mp4").exists())

    def test_changed_clip_hash_is_rejected_and_existing_final_survives(self):
        self._both()
        self._run("assemble-final")
        final = self.root / "outputs/final.mp4"
        before = final.read_bytes()
        clip = self.root / "outputs/final-clips/s001.mp4"
        clip.write_bytes(clip.read_bytes() + b"changed")
        with self.assertRaisesRegex(WorkflowError, "clip changed"):
            self._run("assemble-final")
        self.assertEqual(final.read_bytes(), before)

    def test_changed_shot_or_raw_source_invalidates_final_clip(self):
        self._both()
        self.shots[0]["description"] = "An intentionally revised shot."
        self._save()
        with self.assertRaisesRegex(WorkflowError, "settings changed"):
            self._run("assemble-final")
        self.shots[0]["description"] = "Red shot."
        self._save()
        self.raw1.write_bytes(self.raw1.read_bytes() + b"changed raw")
        with self.assertRaisesRegex(WorkflowError, "Raw source changed"):
            self._run("assemble-final")

    def test_seedance_manual_download_to_conform_integration(self):
        self.shots[0].update({"frames": 96, "duration_seconds": 4.0})
        self._save()
        preview = self.root / "renders/s001/preview.mp4"
        preview.parent.mkdir(parents=True)
        shutil.copyfile(self.raw1, preview)
        self._run("seedance", "prepare", "--shot", "s001", "--allow-unmanaged-preview")
        self._run("seedance", "download", "--shot", "s001", "--file", str(self.raw1))
        self._run("conform", "--shot", "s001", "--take", "take001", "--start-seconds", "0")
        receipt = read_json(self.root / "outputs/final-clips/s001.json")
        self.assertEqual(receipt["recipe"]["source"]["origin"], "seedance")
        self.assertEqual(receipt["recipe"]["source"]["take"], "take001")
        self.assertEqual(receipt["output"]["probe"]["frames"], 96)
        raw = self.root / receipt["recipe"]["source"]["path"]
        self.assertEqual(raw.read_bytes(), self.raw1.read_bytes())

    def test_reconform_keeps_previous_derivative_and_never_overwrites_raw(self):
        self._run("conform", "--shot", "s001", "--input", str(self.raw1), "--start-seconds", "0")
        self._run("conform", "--shot", "s001", "--input", str(self.raw1), "--start-seconds", "1")
        history = list((self.root / "outputs/finishing-history/clip-s001").glob("*/s001.mp4"))
        self.assertEqual(len(history), 1)
        output = self.root / "outputs/final-clips/s001.mp4"
        with self.assertRaisesRegex(WorkflowError, "separate raw source"):
            self._run("conform", "--shot", "s001", "--input", str(output), "--start-seconds", "0")


if __name__ == "__main__":
    unittest.main()
