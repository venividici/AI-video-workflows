"""Explicit frame-accurate conforming and verified straight-cut final assembly."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .common import (WorkflowError, emit, find_executable, load_project, load_shots,
                     project_path, read_json, write_json)


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _inside(root, path):
    result = Path(path).resolve()
    if not result.is_relative_to(Path(root).resolve()):
        raise WorkflowError(f"Output/package path escapes its directory: {path}")
    return result


def _id(value):
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise WorkflowError(f"Unsafe shot/take identifier: {value!r}")
    return value


def _load(path):
    project, root = load_project(path)
    fps, size = project.get("fps"), project.get("resolution")
    if isinstance(fps, bool) or not isinstance(fps, int) or fps <= 0:
        raise WorkflowError("Project fps must be a positive integer.")
    if not isinstance(size, list) or len(size) != 2 or any(isinstance(x, bool) or not isinstance(x, int) or x <= 0 or x % 2 for x in size):
        raise WorkflowError("Final video requires positive even project resolution [width, height].")
    shots = load_shots(project, root)
    if not shots:
        raise WorkflowError("No shots to finish.")
    seen = set()
    for shot in shots:
        sid = _id(shot.get("id"))
        if sid in seen:
            raise WorkflowError(f"Duplicate shot id: {sid}")
        seen.add(sid)
        frames = shot.get("frames")
        if isinstance(frames, bool) or not isinstance(frames, int) or frames <= 0:
            raise WorkflowError(f"Unresolved/invalid frame count: {sid}")
        try:
            duration = Decimal(str(shot.get("duration_seconds")))
            if not duration.is_finite() or duration <= 0 or abs(duration - Decimal(frames) / fps) > Decimal("0.501") / fps:
                raise ValueError("duration/frames mismatch")
        except (InvalidOperation, ValueError) as exc:
            raise WorkflowError(f"Unresolved/inconsistent source duration: {sid}") from exc
    return project, root, shots


def _tool(name, explicit=None):
    path = find_executable(name, explicit)
    if not path:
        raise WorkflowError(f"{name} is required for finishing; install FFmpeg or pass --{name}.")
    return path


def _run(command, timeout):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkflowError("Finishing process failed or timed out; original media is unchanged.") from exc
    if result.returncode:
        raise WorkflowError("FFmpeg/ffprobe failed: " + result.stderr[-1800:].strip())
    return result.stdout


def _probe(path, ffprobe):
    raw = _run([ffprobe, "-v", "error", "-count_frames", "-show_entries",
                "stream=codec_type,codec_name,width,height,avg_frame_rate,nb_read_frames,nb_frames,duration,start_time,sample_rate,channels:format=duration,start_time",
                "-of", "json", str(path)], 60)
    try:
        info = json.loads(raw)
        videos = [s for s in info["streams"] if s.get("codec_type") == "video"]
        audios = [s for s in info["streams"] if s.get("codec_type") == "audio"]
        if len(videos) != 1:
            raise ValueError("expected one video stream")
        video = videos[0]
        num, den = video["avg_frame_rate"].split("/")
        fps = float(num) / float(den)
        frames = int(video.get("nb_read_frames", video.get("nb_frames")))
        duration = float(video.get("duration", info.get("format", {}).get("duration", 0)))
        start = float(video.get("start_time", info.get("format", {}).get("start_time", 0)))
        if frames <= 0 or fps <= 0 or duration <= 0 or not all(math.isfinite(x) for x in (fps, duration, start)):
            raise ValueError("invalid timing")
        audio = None
        if audios:
            item = audios[0]
            audio = {"codec": item.get("codec_name"), "sample_rate": int(item.get("sample_rate", 0)),
                     "channels": item.get("channels"), "start_seconds": float(item.get("start_time", 0)),
                     "duration_seconds": float(item["duration"]) if item.get("duration") not in {None, "N/A"} else None}
        return {"frames": frames, "fps": fps, "duration_seconds": duration, "start_seconds": start,
                "width": video["width"], "height": video["height"], "video_codec": video.get("codec_name"),
                "audio_stream_count": len(audios), "audio": audio}
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise WorkflowError(f"Cannot verify video timing/streams: {path}") from exc


def _verify_media(path, project, frames, ffprobe):
    probe = _probe(path, ffprobe)
    if probe["frames"] != frames or abs(probe["fps"] - project["fps"]) > 0.0001:
        raise WorkflowError("Output does not match the exact required frame count/fps; no final is committed.")
    if [probe["width"], probe["height"]] != project["resolution"]:
        raise WorkflowError("Output resolution differs from project settings.")
    if probe["audio_stream_count"] != 1 or probe["audio"]["sample_rate"] != 48000 or probe["audio"]["channels"] != 2:
        raise WorkflowError("Finished clips must have one normalized 48 kHz stereo audio track.")
    return probe


def _source_record(path, root):
    path = Path(path).resolve()
    inside = path.is_relative_to(root)
    return {"path": str(path.relative_to(root)) if inside else str(path), "project_relative": inside,
            "sha256": _hash(path), "size_bytes": path.stat().st_size}


def _resolve_source(record, root):
    path = (_inside(root, root / record["path"]) if record.get("project_relative")
            else Path(record["path"]).expanduser().resolve())
    if not path.is_file() or _hash(path) != record["sha256"]:
        raise WorkflowError("Raw source changed or is missing; the conformed clip is stale.")
    if record.get("origin") == "seedance":
        receipt = _inside(root, root / record["seedance_receipt_path"])
        if not receipt.is_file() or _hash(receipt) != record["seedance_receipt_sha256"]:
            raise WorkflowError("Seedance source receipt changed or is missing; re-conform after review.")
    return path


def _source(args, project, root, shot):
    if args.input:
        path = Path(args.input).expanduser().resolve()
        if not path.is_file():
            raise WorkflowError(f"Explicit input does not exist: {path}")
        return path, {**_source_record(path, root), "origin": "explicit-file"}
    take = _id(args.take)
    bundle = _inside(root, project_path(project, root, "outputs") / "seedance" / shot["id"] / take)
    receipt_path = bundle / "receipt.json"
    receipt = read_json(receipt_path)
    manifest = read_json(bundle / "manifest.json")
    if receipt.get("state") != "downloaded":
        raise WorkflowError(f"{shot['id']}/{take}: download/import the original generated video first.")
    if receipt.get("manifest_sha256") != _json_hash(manifest):
        raise WorkflowError("Seedance manifest differs from the downloaded take receipt.")
    if manifest.get("shot_id") != shot["id"] or manifest.get("source", {}).get("shot_sha256") != _json_hash(shot):
        raise WorkflowError("Seedance take belongs to an older/different source shot; regenerate or explicitly select a reviewed --input.")
    result = receipt.get("result", {})
    path = _inside(bundle, bundle / result.get("path", ""))
    if not path.is_file() or _hash(path) != result.get("sha256"):
        raise WorkflowError("Downloaded raw video differs from its receipt.")
    record = {**_source_record(path, root), "origin": "seedance", "take": take,
              "task_id": receipt.get("task_id"), "seedance_receipt_path": str(receipt_path.relative_to(root)),
              "seedance_receipt_sha256": _hash(receipt_path)}
    return path, record


def _seconds(value):
    return f"{value:.12f}".rstrip("0").rstrip(".") or "0"


@contextmanager
def _lock(output):
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = output.with_suffix(output.suffix + ".lock")
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise WorkflowError(f"Finishing output is locked: {lock}. Check running processes before removing a stale lock.") from exc
    os.close(fd)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def _archive(output, receipt_path, outputs, label):
    if not output.exists() and not receipt_path.exists():
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    history = _inside(outputs, outputs / "finishing-history" / label / stamp)
    history.mkdir(parents=True)
    for path in (output, receipt_path):
        if path.exists():
            os.replace(path, history / path.name)


def _timeout(args):
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 1800:
        raise WorkflowError("--timeout must be greater than zero and at most 1800 seconds.")
    return args.timeout


def conform(args):
    project, root, shots = _load(args.project)
    selected = set(args.shot)
    if len(selected) != len(args.shot) or selected - {s["id"] for s in shots}:
        raise WorkflowError("Select existing shot IDs exactly once.")
    shots = [shot for shot in shots if shot["id"] in selected]
    if args.input and len(shots) != 1:
        raise WorkflowError("--input is an explicit single-shot source; choose one --shot.")
    try:
        start = Decimal(args.start_seconds)
        if not start.is_finite() or start < 0:
            raise ValueError("negative/nonfinite")
    except (InvalidOperation, ValueError) as exc:
        raise WorkflowError("--start-seconds must be a finite nonnegative number.") from exc
    ffmpeg, ffprobe = _tool("ffmpeg", args.ffmpeg), _tool("ffprobe", args.ffprobe)
    timeout = _timeout(args)
    outputs = project_path(project, root, "outputs")
    results = []
    for shot in shots:
        source, source_record = _source(args, project, root, shot)
        actual = _probe(source, ffprobe)
        target_seconds = Decimal(shot["frames"]) / project["fps"]
        if start + target_seconds > Decimal(str(actual["duration_seconds"])) + Decimal("0.000001"):
            raise WorkflowError(f"{shot['id']}: raw video is too short for start={start}s + {target_seconds}s. No speed change or video padding is performed.")
        if actual["audio_stream_count"] > 1 and args.audio == "preserve":
            raise WorkflowError("Multiple source audio tracks need an explicit external mix or --audio mute; no track is silently discarded.")
        cut = float(start) + actual["start_seconds"]
        duration_text = _seconds(target_seconds)
        cut_text = _seconds(cut)
        width, height = project["resolution"]
        video_filter = (f"[0:v:0]trim=start={cut_text}:duration={duration_text},setpts=PTS-({cut_text})/TB,"
                        f"fps={project['fps']}:start_time=0:round=near,"
                        f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p[v]")
        has_audio = actual["audio"] is not None
        preserve = has_audio and args.audio == "preserve"
        if preserve:
            audio_filter = (f"[0:a:0]atrim=start={cut_text}:duration={duration_text},asetpts=PTS-({cut_text})/TB,"
                            "aresample=48000:async=1:first_pts=0,aformat=channel_layouts=stereo,"
                            f"apad,atrim=duration={duration_text}[a]")
            audio_action = "preserved-and-normalized"
        else:
            audio_filter = f"[1:a:0]atrim=duration={duration_text},asetpts=PTS-STARTPTS[a]"
            audio_action = "explicit-muted" if args.audio == "mute" else "silent-track-for-silent-source"
        audio = {"requested_mode": args.audio, "source_had_audio": has_audio, "action": audio_action,
                 "sample_rate": 48000, "channels": 2,
                 "note": "Audio gaps are silence; missing/short audio never extends the video. This is not dialogue/music mixing."}
        recipe = {"version": "1.0", "shot_id": shot["id"], "source_shot_sha256": _json_hash(shot),
                  "source": source_record, "start_seconds": float(start), "frames": shot["frames"],
                  "fps": project["fps"], "resolution": project["resolution"], "target_seconds": float(target_seconds),
                  "audio": audio, "resize": "fit-with-black-letterbox", "speed": 1.0, "video_padding": False}
        output = _inside(root, outputs / "final-clips" / (shot["id"] + ".mp4"))
        if source in {output, _inside(root, outputs / "final.mp4")}:
            raise WorkflowError("Use a separate raw source file; conform/final assembly must not overwrite its own input.")
        receipt_path = output.with_suffix(".json")
        with _lock(output):
            stage = Path(tempfile.mkdtemp(prefix=".conform-", dir=output.parent))
            temporary = stage / "clip.mp4"
            try:
                command = [ffmpeg, "-v", "error", "-nostdin", "-y", "-copyts", "-i", str(source)]
                if not preserve:
                    command += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
                command += ["-filter_complex", video_filter + ";" + audio_filter,
                            "-map", "[v]", "-map", "[a]", "-frames:v", str(shot["frames"]),
                            "-t", duration_text, "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                            "-movflags", "+faststart", str(temporary)]
                _run(command, timeout)
                checked = _verify_media(temporary, project, shot["frames"], ffprobe)
                _resolve_source(source_record, root)
                receipt = {"schema_version": "1.0", "kind": "conformed-final-clip", "status": "complete",
                           "recipe": recipe, "recipe_sha256": _json_hash(recipe), "source_probe": actual,
                           "output": {"path": str(output.relative_to(root)), "sha256": _hash(temporary), "probe": checked},
                           "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                           "needs_editorial_review": True}
                _archive(output, receipt_path, outputs, "clip-" + shot["id"])
                os.replace(temporary, output)
                write_json(receipt_path, receipt)
                results.append({"shot_id": shot["id"], "path": str(output), "frames": shot["frames"],
                                "fps": project["fps"], "start_seconds": float(start), "audio": audio,
                                "receipt": str(receipt_path), "raw_preserved": True})
            finally:
                shutil.rmtree(stage)
    emit({"operation": "conform", "clips": results, "automatic_speed_change": False, "automatic_video_padding": False})
    return 0


def _current_clip(project, root, shot, ffprobe):
    output = _inside(root, project_path(project, root, "outputs") / "final-clips" / (shot["id"] + ".mp4"))
    receipt_path = output.with_suffix(".json")
    if not output.is_file() or not receipt_path.is_file():
        raise WorkflowError(f"Missing conformed final clip or receipt: {shot['id']}; run conform first.")
    receipt = read_json(receipt_path)
    recipe = receipt.get("recipe", {})
    if receipt.get("kind") != "conformed-final-clip" or receipt.get("status") != "complete" or receipt.get("recipe_sha256") != _json_hash(recipe):
        raise WorkflowError(f"Invalid/stale conform receipt: {shot['id']}")
    expected = {"shot_id": shot["id"], "source_shot_sha256": _json_hash(shot), "frames": shot["frames"],
                "fps": project["fps"], "resolution": project["resolution"]}
    if any(recipe.get(key) != value for key, value in expected.items()):
        raise WorkflowError(f"Source shot/project settings changed after conform: {shot['id']}")
    if receipt.get("output", {}).get("path") != str(output.relative_to(root)) or receipt["output"].get("sha256") != _hash(output):
        raise WorkflowError(f"Conformed clip changed after verification: {shot['id']}")
    _resolve_source(recipe["source"], root)
    _verify_media(output, project, shot["frames"], ffprobe)
    return output, receipt


def assemble_final(args):
    project, root, shots = _load(args.project)
    ffmpeg, ffprobe = _tool("ffmpeg", args.ffmpeg), _tool("ffprobe", args.ffprobe)
    timeout = _timeout(args)
    checked = [_current_clip(project, root, shot, ffprobe) for shot in shots]
    outputs = project_path(project, root, "outputs")
    output = _inside(root, outputs / "final.mp4")
    receipt_path = _inside(root, outputs / "final.json")
    total_frames = sum(shot["frames"] for shot in shots)
    duration = Decimal(total_frames) / project["fps"]
    with _lock(output):
        stage = Path(tempfile.mkdtemp(prefix=".assemble-final-", dir=outputs))
        temporary = stage / "final.mp4"
        try:
            lines = ["ffconcat version 1.0"]
            for shot, (clip, _) in zip(shots, checked):
                value = clip.as_posix()
                if any(char in value for char in "\r\n\0"):
                    raise WorkflowError("Video paths cannot contain newline/control characters.")
                escaped = value.replace("'", "'\\''")
                lines += [f"file '{escaped}'", "duration " + _seconds(Decimal(shot["frames"]) / project["fps"])]
            concat = stage / "clips.ffconcat"
            concat.write_text("\n".join(lines) + "\n", encoding="utf-8")
            command = [ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
                       "-map", "0:v:0", "-map", "0:a:0", "-vf", f"fps={project['fps']}",
                       "-af", f"aresample=48000,apad,atrim=duration={_seconds(duration)}",
                       "-frames:v", str(total_frames), "-t", _seconds(duration),
                       "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                       "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(temporary)]
            _run(command, timeout)
            probe = _verify_media(temporary, project, total_frames, ffprobe)
            for shot, (_, prior) in zip(shots, checked):
                _, current = _current_clip(project, root, shot, ffprobe)
                if current != prior:
                    raise WorkflowError("A source clip changed while assembling; final was not committed.")
            frame_cursor = 0
            timeline = []
            for shot, (clip, receipt) in zip(shots, checked):
                timeline.append({"shot_id": shot["id"], "start_frame_inclusive": frame_cursor,
                                 "end_frame_exclusive": frame_cursor + shot["frames"], "frames": shot["frames"],
                                 "clip": str(clip.relative_to(root)), "clip_sha256": receipt["output"]["sha256"],
                                 "conform_recipe_sha256": receipt["recipe_sha256"]})
                frame_cursor += shot["frames"]
            receipt = {"schema_version": "1.0", "kind": "final-straight-cut", "status": "complete",
                       "frames": total_frames, "fps": project["fps"], "resolution": project["resolution"],
                       "duration_seconds": float(duration), "timeline": timeline,
                       "output": {"path": str(output.relative_to(root)), "sha256": _hash(temporary), "probe": probe},
                       "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "note": "Verified straight-cut rough edit; dialogue/music mixing, color grading and creative approval remain separate."}
            _archive(output, receipt_path, outputs, "final")
            os.replace(temporary, output)
            write_json(receipt_path, receipt)
        finally:
            shutil.rmtree(stage)
    emit({"operation": "assemble-final", "path": str(output), "receipt": str(receipt_path),
          "frames": total_frames, "fps": project["fps"], "duration_seconds": float(duration),
          "shots": [shot["id"] for shot in shots], "type": "straight-cut-rough-edit"})
    return 0


def register(subparsers):
    parser = subparsers.add_parser("conform", help="显式裁切原片到原分镜帧数，保留原片与来源回执")
    parser.add_argument("--project", required=True)
    parser.add_argument("--shot", action="append", required=True)
    parser.add_argument("--take", default="take001", help="已下载的 Seedance take；--input 可显式替代")
    parser.add_argument("--input", help="显式使用本地 CG/实拍文件，只能对应一个 --shot")
    parser.add_argument("--start-seconds", required=True, help="从原片第一帧起算的裁切开始时间；必须明确给出")
    parser.add_argument("--audio", choices=("preserve", "mute"), default="preserve")
    parser.add_argument("--ffmpeg")
    parser.add_argument("--ffprobe")
    parser.add_argument("--timeout", type=float, default=120)
    parser.set_defaults(func=conform)
    assembly = subparsers.add_parser("assemble-final", help="按全部分镜顺序拼接已验证成片片段，输出 final.mp4")
    assembly.add_argument("--project", required=True)
    assembly.add_argument("--ffmpeg")
    assembly.add_argument("--ffprobe")
    assembly.add_argument("--timeout", type=float, default=120)
    assembly.set_defaults(func=assemble_final)
