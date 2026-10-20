"""Protocol tests use a loopback HTTP server; no paid provider calls are made."""

import argparse
from contextlib import contextmanager, redirect_stdout
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from aivw import seedance
from aivw.common import WorkflowError, read_json, write_json


@contextmanager
def mock_provider(video, post_status=200, post_body=None, poll_status="succeeded",
                  post_delay=0, content_type="video/mp4", extra_length=0,
                  post_entered=None, post_release=None):
    state = {"posts": [], "gets": [], "media_auth": [], "port": None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def respond(self, status, content, content_type="application/json", extra=0):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content) + extra))
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))
            state["posts"].append({"path": self.path, "body": data,
                                   "authorization": self.headers.get("Authorization")})
            if post_entered:
                post_entered.set()
            if post_release:
                post_release.wait(2)
            if post_delay:
                time.sleep(post_delay)
            body = {"id": "cgt_mock_001"} if post_body is None else post_body
            self.respond(post_status, json.dumps(body).encode())

        def do_GET(self):
            state["gets"].append(self.path)
            if self.path == "/media.mp4":
                state["media_auth"].append(self.headers.get("Authorization"))
                self.respond(200, video, content_type, extra_length)
            else:
                body = {"id": "cgt_mock_001", "status": poll_status}
                if poll_status == "succeeded":
                    body["content"] = {"video_url": f"http://127.0.0.1:{state['port']}/media.mp4"}
                if poll_status == "failed":
                    body["error"] = {"code": "MockFailure", "message": "Intentional local test failure"}
                self.respond(200, json.dumps(body).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    state["port"] = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{state['port']}/api/v3", state
    finally:
        if post_release:
            post_release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class SeedanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise unittest.SkipTest("Media protocol tests require the documented FFmpeg/ffprobe tools.")
        cls.media_tmp = tempfile.TemporaryDirectory(prefix="aivw-seedance-media-")
        cls.source = Path(cls.media_tmp.name) / "generic.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                        "color=c=navy:s=1280x720:r=24", "-frames:v", "96", "-an",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                        str(cls.source)], check=True, capture_output=True)
        cls.video = cls.source.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.media_tmp.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aivw-seedance-test-")
        self.root = Path(self.tmp.name)
        self.project = {"schema_version": "1.0", "id": "generic", "title": "Generic protocol test",
                        "fps": 24, "resolution": [1280, 720],
                        "paths": {"shots": "shots.json", "renders": "renders", "outputs": "outputs"},
                        "provider": {"name": "seedance", "mode": "manual", "model": None}}
        self.shot = {"id": "s001", "frames": 96, "duration_seconds": 4,
                     "description": "A generic product remains on a studio table.", "dialogue": "",
                     "source": {"file": "self-authored.csv", "row": 2, "shot_number": "1"}}
        self.shots = [self.shot]
        write_json(self.root / "project.json", self.project)
        write_json(self.root / "shots.json", {"schema_version": "1.0", "shots": self.shots})
        self._copy_preview("s001")
        self.env = patch.dict(os.environ, {"ARK_API_KEY": "unit-test-only-secret"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _copy_preview(self, shot_id):
        path = self.root / "renders" / shot_id / "preview.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.video)
        return path

    def _args(self, command, *extra):
        parser = argparse.ArgumentParser()
        seedance.register(parser.add_subparsers(dest="command", required=True))
        # This test fixture is an explicitly supplied generic external video;
        # Blender's managed-render provenance is exercised by its own tests.
        external = ["--allow-unmanaged-preview"] if command == "prepare" else []
        return parser.parse_args(["seedance", command, "--project", str(self.root), *external, *extra])

    def _run(self, command, *extra):
        args = self._args(command, *extra)
        output = io.StringIO()
        with redirect_stdout(output):
            status = args.func(args)
        return status, json.loads(output.getvalue())

    def bundle(self, shot_id="s001", take="take001"):
        return self.root / "outputs" / "seedance" / shot_id / take

    def _ready(self, shot_id="s001", *extra):
        self._run("prepare", "--shot", shot_id, "--model", "operator-selected-test-model", *extra)
        manifest = read_json(self.bundle(shot_id) / "manifest.json")
        mapping = {"shots": {shot_id: {"preview_url": "https://operator-media.example/preview.mp4",
                                       "preview_sha256": manifest["preview_reference"]["sha256"]}}}
        filename = self.root / (shot_id + "-references.json")
        write_json(filename, mapping)
        self._run("prepare", "--shot", shot_id, "--model", "operator-selected-test-model",
                  "--reference-manifest", str(filename), *extra)
        self.assertTrue(read_json(self.bundle(shot_id) / "manifest.json")["api_ready"])

    def _submit(self, base, *extra):
        return self._run("submit", "--shot", "s001", "--confirm-paid", "--max-tasks", "1",
                         "--api-base", base, "--allow-local-http", *extra)

    def _poll(self, base, *extra):
        return self._run("poll", "--shot", "s001", "--api-base", base, "--allow-local-http", *extra)

    def test_manual_prepare_is_offline_and_retains_exact_original(self):
        with patch.object(seedance, "_json_http", side_effect=AssertionError("network called")):
            _, result = self._run("prepare", "--shot", "s001")
        bundle = self.bundle()
        for name in ["preview.mp4", "prompt.txt", "references.json", "timing.json", "request.json", "manifest.json", "receipt.json", "HANDOFF.md"]:
            self.assertTrue((bundle / name).is_file(), name)
        self.assertEqual((bundle / "preview.mp4").read_bytes(), self.video)
        self.assertFalse(result["network_used"])
        self.assertEqual(result["paid_tasks"], 0)
        manifest = read_json(bundle / "manifest.json")
        self.assertFalse(manifest["api_ready"])
        self.assertFalse(manifest["preview_reference"]["remote_bytes_verified"])
        self.assertEqual(manifest["timing"]["source_frames"], 96)
        self.assertEqual(manifest["timing"]["trim"]["project_end_frame_exclusive"], 96)
        self.assertEqual(manifest["render_provenance"]["mode"], "unmanaged-explicit")

    def test_default_prepare_rejects_unmanaged_preview(self):
        args = self._args("prepare", "--shot", "s001")
        args.allow_unmanaged_preview = False
        with self.assertRaisesRegex(WorkflowError, "build the shot first"):
            seedance.prepare(args)
        self.assertFalse(self.bundle().exists())

    def test_managed_provenance_is_checked_at_prepare_and_submit(self):
        write_json(self.root / "renders/s001/render.json", {"test_marker": True})
        evidence = {"mode": "managed", "fingerprint": "test-current-render", "input_hash": "test-current-layout", "video_sha256": hashlib.sha256(self.video).hexdigest()}
        with patch.object(seedance, "_render_evidence", return_value=evidence) as verifier:
            self._ready()
            self.assertEqual(verifier.call_count, 2)
        self.assertEqual(read_json(self.bundle() / "manifest.json")["render_provenance"], evidence)
        with patch.object(seedance, "_render_evidence", side_effect=WorkflowError("layout source changed; rebuild")):
            with self.assertRaisesRegex(WorkflowError, "layout source changed"):
                self._submit("https://ark.cn-beijing.volces.com/api/v3")
        self.assertEqual(read_json(self.bundle() / "receipt.json")["state"], "prepared")

    def test_full_success_and_idempotence_without_cdn_authorization(self):
        self._ready()
        with mock_provider(self.video) as (base, state):
            self._submit(base)
            self._submit(base)
            self._poll(base)
            self._run("download", "--shot", "s001", "--allow-local-http")
            self._poll(base, "--adopt-task-id", "cgt_mock_001", "--confirm-task-match")
            _, second = self._run("download", "--shot", "s001", "--allow-local-http")
        self.assertEqual(len(state["posts"]), 1)
        request = state["posts"][0]["body"]
        self.assertEqual(request["model"], "operator-selected-test-model")
        self.assertEqual(request["omni_reference_task_type"], "reference")
        self.assertEqual(request["duration"], 4)
        self.assertNotIn("seed", request)
        self.assertNotIn("camera_fixed", request)
        self.assertEqual(state["media_auth"], [None])
        receipt = read_json(self.bundle() / "receipt.json")
        self.assertEqual(receipt["state"], "downloaded")
        self.assertEqual(receipt["result"]["probe"]["frames"], 96)
        self.assertEqual(receipt["result"]["sha256"], hashlib.sha256(self.video).hexdigest())
        self.assertTrue(receipt["result"]["untouched_original"])
        self.assertTrue(second["results"][0]["already_present"])
        self.assertNotIn("unit-test-only-secret", (self.bundle() / "receipt.json").read_text())

    def test_timeout_is_unknown_and_never_automatically_reposted(self):
        self._ready()
        with mock_provider(self.video, post_delay=0.15) as (base, state):
            with self.assertRaisesRegex(WorkflowError, "unknown"):
                self._submit(base, "--timeout", "0.03")
            self.assertEqual(read_json(self.bundle() / "receipt.json")["state"], "unknown")
            with self.assertRaisesRegex(WorkflowError, "No retry"):
                self._submit(base)
            self.assertEqual(len(state["posts"]), 1)
            self._poll(base, "--adopt-task-id", "cgt_mock_001", "--confirm-task-match")
            self.assertEqual(read_json(self.bundle() / "receipt.json")["state"], "succeeded")

    def test_concurrent_submission_cannot_duplicate_post(self):
        self._ready()
        entered, release = threading.Event(), threading.Event()
        failures = []
        with mock_provider(self.video, post_entered=entered, post_release=release) as (base, state):
            args = self._args("submit", "--shot", "s001", "--confirm-paid", "--max-tasks", "1",
                              "--api-base", base, "--allow-local-http")
            def first():
                try:
                    seedance.submit(args)
                except Exception as exc:
                    failures.append(exc)
            with patch.object(seedance, "emit"):
                worker = threading.Thread(target=first)
                worker.start()
                self.assertTrue(entered.wait(2))
                with self.assertRaises(WorkflowError):
                    seedance.submit(args)
                release.set()
                worker.join(timeout=3)
        self.assertFalse(failures)
        self.assertEqual(len(state["posts"]), 1)

    def test_rejected_submission_is_saved_and_not_retried(self):
        self._ready()
        with mock_provider(self.video, post_status=400, post_body={"error": "bad request"}) as (base, state):
            with self.assertRaisesRegex(WorkflowError, "HTTP 400"):
                self._submit(base)
            self._submit(base)
        self.assertEqual(len(state["posts"]), 1)
        self.assertEqual(read_json(self.bundle() / "receipt.json")["state"], "submission_rejected")

    def test_missing_task_id_after_post_is_unknown(self):
        self._ready()
        with mock_provider(self.video, post_body={"accepted": True}) as (base, state):
            with self.assertRaisesRegex(WorkflowError, "unknown"):
                self._submit(base)
            with self.assertRaises(WorkflowError):
                self._submit(base)
        self.assertEqual(len(state["posts"]), 1)

    def test_explicit_paid_and_duration_limits_are_enforced_before_post(self):
        self._ready()
        with mock_provider(self.video) as (base, state):
            with self.assertRaisesRegex(WorkflowError, "confirm-paid"):
                self._run("submit", "--shot", "s001", "--max-tasks", "1", "--api-base", base, "--allow-local-http")
            with self.assertRaisesRegex(WorkflowError, "max-generated-seconds"):
                self._submit(base, "--max-generated-seconds", "3")
            with self.assertRaisesRegex(WorkflowError, "max-generated-seconds"):
                self._submit(base, "--max-generated-seconds", "nan")
        self.assertEqual(state["posts"], [])
        self.assertEqual(read_json(self.bundle() / "receipt.json")["state"], "prepared")

    def test_batch_limit_is_preflighted_before_any_post(self):
        second = {**self.shot, "id": "s002"}
        self.shots.append(second)
        write_json(self.root / "shots.json", {"schema_version": "1.0", "shots": self.shots})
        self._copy_preview("s002")
        self._ready("s001")
        self._ready("s002")
        with mock_provider(self.video) as (base, state):
            with self.assertRaisesRegex(WorkflowError, "2 new tasks"):
                self._submit(base, "--shot", "s002")
        self.assertEqual(state["posts"], [])

    def test_changed_payload_or_preview_blocks_submission(self):
        self._ready()
        bundle = self.bundle()
        request = read_json(bundle / "request.json")
        request["duration"] = 8
        write_json(bundle / "request.json", request)
        with self.assertRaisesRegex(WorkflowError, "changed"):
            self._submit("https://ark.cn-beijing.volces.com/api/v3")
        self._ready()
        (self.root / "renders/s001/preview.mp4").write_bytes(self.video + b"changed")
        with self.assertRaisesRegex(WorkflowError, "preview changed"):
            self._submit("https://ark.cn-beijing.volces.com/api/v3")

    def test_existing_task_can_be_recovered_after_project_preview_updates(self):
        self._ready()
        with mock_provider(self.video) as (base, state):
            self._submit(base)
            (self.root / "renders/s001/preview.mp4").write_bytes(self.video + b"new version")
            self._poll(base)
            self._run("download", "--shot", "s001", "--allow-local-http")
        self.assertEqual(read_json(self.bundle() / "receipt.json")["state"], "downloaded")

    def test_failed_task_preserves_error_and_is_not_resubmitted(self):
        self._ready()
        with mock_provider(self.video, poll_status="failed") as (base, state):
            self._submit(base)
            self._poll(base)
            self._submit(base)
            with self.assertRaisesRegex(WorkflowError, "not succeeded"):
                self._run("download", "--shot", "s001", "--allow-local-http")
        receipt = read_json(self.bundle() / "receipt.json")
        self.assertEqual(receipt["provider_response"]["error"]["code"], "MockFailure")
        self.assertEqual(len(state["posts"]), 1)
        with self.assertRaisesRegex(WorkflowError, "history"):
            self._run("prepare", "--shot", "s001")

    def test_html_and_truncated_downloads_do_not_become_originals(self):
        for content, mime, extra in [(b"<html>error</html>", "text/html", 0), (self.video, "video/mp4", 20)]:
            with self.subTest(mime=mime, extra=extra):
                self._ready()
                with mock_provider(content, content_type=mime, extra_length=extra) as (base, state):
                    self._submit(base)
                    self._poll(base)
                    with self.assertRaises(WorkflowError):
                        self._run("download", "--shot", "s001", "--allow-local-http")
                self.assertEqual(read_json(self.bundle() / "receipt.json")["state"], "succeeded")
                self.assertFalse(list((self.bundle() / "result").glob("original.*")))
                # This subtest's simulated receipt is terminal; next case uses a fresh project.
                if extra == 0:
                    shutil.rmtree(self.root / "outputs")

    def test_manual_result_import_needs_no_api_key(self):
        self._run("prepare", "--shot", "s001")
        with patch.dict(os.environ, {}, clear=True):
            self._run("download", "--shot", "s001", "--file", str(self.source))
        receipt = read_json(self.bundle() / "receipt.json")
        self.assertEqual(receipt["result"]["origin"], "manual-file")
        self.assertEqual(receipt["result"]["sha256"], hashlib.sha256(self.video).hexdigest())

    def test_short_edit_requires_explicit_padding_and_records_trim(self):
        preview = self.root / "renders/s001/preview.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(self.source),
                        "-frames:v", "24", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(preview)],
                       check=True, capture_output=True)
        original = preview.read_bytes()
        self.shot.update({"frames": 24, "duration_seconds": 1})
        write_json(self.root / "shots.json", {"schema_version": "1.0", "shots": self.shots})
        with self.assertRaisesRegex(WorkflowError, "allow-padding"):
            self._run("prepare", "--shot", "s001", "--task-type", "edit")
        self._run("prepare", "--shot", "s001", "--task-type", "edit", "--allow-padding")
        timing = read_json(self.bundle() / "timing.json")
        request = read_json(self.bundle() / "request.json")
        self.assertEqual(timing["source_frames"], 24)
        self.assertEqual(timing["padding_frames"], 72)
        self.assertEqual(timing["trim"]["project_end_frame_exclusive"], 24)
        self.assertEqual(seedance._probe(self.bundle() / "reference_padded.mp4")["frames"], 96)
        self.assertEqual(request["duration"], -1)
        self.assertEqual(request["ratio"], "adaptive")
        self.assertEqual(request["omni_reference_task_type"], "edit")
        self.assertEqual(preview.read_bytes(), original)

    def test_first_last_reference_mode_mixing_is_rejected(self):
        mapping = self.root / "refs.json"
        write_json(mapping, {"shots": {"s001": {"references": [{"type": "image", "role": "first_frame", "usage": "start"}]}}})
        with self.assertRaisesRegex(WorkflowError, "cannot be mixed"):
            self._run("prepare", "--shot", "s001", "--reference-manifest", str(mapping))

    def test_untrusted_api_host_cannot_receive_environment_secret(self):
        self._ready()
        with patch.object(seedance, "_json_http", side_effect=AssertionError("must not call")):
            with self.assertRaisesRegex(WorkflowError, "official Ark hosts"):
                self._submit("https://untrusted.example/api/v3")
        self.assertEqual(read_json(self.bundle() / "receipt.json")["state"], "prepared")


if __name__ == "__main__":
    unittest.main()
