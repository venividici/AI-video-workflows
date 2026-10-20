"""Offline Seedance handoff and explicit, resumable paid API operations.

Only the documented Seedance 2.5 protocol is implemented. A model identifier is
always supplied by the operator; local hashes do not attest remote URL content.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
from http.client import HTTPException
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .common import (WorkflowError, emit, find_executable, load_project,
                     load_shots, project_path, read_json, write_json)


API_BASE = "https://ark.cn-beijing.volces.com/api/v3"
API_HOSTS = {"ark.cn-beijing.volces.com", "ark.ap-southeast.bytepluses.com"}
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
SAFE_TAKE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")
TERMINAL = {"succeeded", "failed", "expired", "cancelled", "downloaded"}
UNRESOLVED = {"submitting", "unknown"}
MAX_JSON_BYTES = 2 * 1024 * 1024


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _safe_id(value, label="id"):
    pattern = SAFE_TAKE if label == "take" else SAFE_ID
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise WorkflowError(f"Invalid filesystem/task {label}: {value!r}; use a leading alphanumeric and at most 96 safe characters.")
    return value


def _inside(root, path):
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(Path(root).resolve()):
        raise WorkflowError(f"Path escapes the project/package: {path}")
    return resolved


def _selected(project, root, selected, require=False):
    shots = load_shots(project, root)
    by_id = {}
    for shot in shots:
        if not isinstance(shot, dict):
            raise WorkflowError("Each shot must be an object.")
        shot_id = _safe_id(shot.get("id"), "shot id")
        if shot_id in by_id:
            raise WorkflowError(f"Duplicate shot id: {shot_id}")
        by_id[shot_id] = shot
    if require and not selected:
        raise WorkflowError("Specify at least one --shot; network operations never select all shots implicitly.")
    if selected:
        if len(selected) != len(set(selected)):
            raise WorkflowError("Duplicate --shot values are not allowed.")
        missing = set(selected) - set(by_id)
        if missing:
            raise WorkflowError(f"Unknown shot ids: {', '.join(sorted(missing))}")
        return [by_id[item] for item in selected]
    return shots


def _bundle(project, root, shot_id, take):
    base = project_path(project, root, "outputs") / "seedance"
    return _inside(root, base / _safe_id(shot_id) / _safe_id(take, "take"))


@contextmanager
def _lock(bundle):
    bundle.parent.mkdir(parents=True, exist_ok=True)
    path = bundle.parent / ("." + bundle.name + ".lock")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise WorkflowError(f"Operation already locked: {path}. Check the running process before removing a stale lock.") from exc
    try:
        with os.fdopen(fd, "w") as out:
            out.write(f"pid={os.getpid()} time={_now()}\n")
        yield
    finally:
        path.unlink(missing_ok=True)


def _probe(path, explicit=None, required=False):
    executable = find_executable("ffprobe", explicit)
    if not executable:
        if required:
            raise WorkflowError("ffprobe is required for this operation. Install FFmpeg or specify --ffprobe.")
        return None
    command = [executable, "-v", "error", "-count_frames", "-show_entries",
               "stream=codec_type,codec_name,width,height,avg_frame_rate,nb_read_frames,nb_frames,duration:format=duration,format_name",
               "-of", "json", str(path)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
        info = json.loads(result.stdout) if result.returncode == 0 else {}
        streams = [item for item in info.get("streams", []) if item.get("codec_type") == "video"]
        if len(streams) != 1:
            raise ValueError("expected one video stream")
        video = streams[0]
        numerator, denominator = video["avg_frame_rate"].split("/")
        fps = float(numerator) / float(denominator)
        frame_value = video.get("nb_read_frames", video.get("nb_frames"))
        frames = int(frame_value) if frame_value not in {None, "N/A"} else None
        duration = float(video.get("duration", info["format"]["duration"]))
        if fps <= 0 or duration <= 0 or not math.isfinite(duration):
            raise ValueError("invalid video timing")
        return {"fps": fps, "frames": frames, "duration_seconds": duration,
                "width": video.get("width"), "height": video.get("height"),
                "codec": video.get("codec_name"), "container": info["format"].get("format_name")}
    except (OSError, ValueError, KeyError, ZeroDivisionError, subprocess.TimeoutExpired) as exc:
        raise WorkflowError(f"Cannot verify video with ffprobe: {path}") from exc


def _video_container(path):
    with Path(path).open("rb") as source:
        header = source.read(64)
    if len(header) < 16:
        raise WorkflowError("Video file is too small to contain a valid MP4/MOV header.")
    if header[4:8] == b"ftyp":
        return "mov" if header[8:12] == b"qt  " else "mp4"
    if header[4:8] in {b"wide", b"moov", b"mdat", b"free"}:
        return "mov"
    raise WorkflowError("Downloaded/input file is not an MP4/MOV container; HTML/JSON error pages are rejected.")


def _duration(shot, fps):
    frames = shot.get("frames")
    if isinstance(frames, bool) or not isinstance(frames, int) or frames <= 0:
        raise WorkflowError(f"Shot {shot['id']} has unresolved/invalid frames; fix timing before preparing video.")
    if shot.get("duration_seconds") is None:
        raise WorkflowError(f"Shot {shot['id']} has no source duration; do not invent one.")
    try:
        requested = Decimal(str(shot["duration_seconds"]))
    except Exception as exc:
        raise WorkflowError(f"Invalid duration in shot {shot['id']}.") from exc
    exact = Decimal(frames) / Decimal(fps)
    if not requested.is_finite() or requested <= 0 or abs(requested - exact) > Decimal("0.501") / fps:
        raise WorkflowError(f"Shot {shot['id']} duration and frames disagree.")
    return frames, float(exact)


def _reference_url(value):
    if not isinstance(value, str):
        return False
    if value.startswith("asset://"):
        return bool(re.fullmatch(r"asset://[A-Za-z0-9_-]+", value))
    parsed = urlparse(value)
    return (parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username
            and not parsed.password and not parsed.fragment)


def _input_mapping(args):
    if not args.reference_manifest:
        return {}, None
    path = Path(args.reference_manifest).expanduser().resolve()
    data = read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("shots", {}), dict):
        raise WorkflowError("Reference manifest must be an object with a 'shots' mapping.")
    return data["shots"], path.parent


def _render_evidence(project, root, shot, ffprobe=None):
    """Use the same provenance checks as assembly, including current layout."""
    from .blender import _current_render
    scene_path = project_path(project, root, "scene")
    scene = read_json(scene_path) if scene_path.is_file() else None
    _, receipt = _current_render(project, root, load_shots(project, root), scene, shot, ffprobe)
    return {"mode": "managed", "fingerprint": receipt["fingerprint"],
            "input_hash": receipt["input_hash"], "video_sha256": receipt["video_sha256"]}


def _pad(source, target, fps, frames, seconds, executable=None):
    ffmpeg = find_executable("ffmpeg", executable)
    if not ffmpeg:
        raise WorkflowError("Explicit padding requires FFmpeg; install it or specify --ffmpeg.")
    wanted = math.ceil(seconds * fps - 1e-9)
    extra = (wanted - frames) / fps
    if extra <= 0:
        shutil.copyfile(source, target)
        return
    command = [ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(source), "-an", "-vf",
               f"tpad=stop_mode=clone:stop_duration={extra:.9f},fps={fps}",
               "-frames:v", str(wanted), "-c:v", "libx264", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", str(target)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        raise WorkflowError("FFmpeg padding failed; original preview has not been modified.")


def _manual_text(manifest):
    timing = manifest["timing"]
    unresolved = "\n".join("- " + value for value in manifest["api_unresolved"]) or "- 无。仍需操作者核对平台版本、模型开通状态和素材权限。"
    return f"""# Seedance 手动交接包：{manifest['shot_id']} / {manifest['take']}

本目录可直接交给操作员，不需要 API。`preview.mp4` 是原始预演的完整副本；`{manifest['preview_reference']['path']}` 是本次参考视频。`prompt.txt` 是本次提示词；`references.json` 说明其他素材及用途；`timing.json` 保留原镜头时长和裁切计划；`request.json` 是对应 API 请求草稿；`receipt.json` 保存状态。

1. 在可用的 Seedance 2.5 界面选择与本包一致的 `{manifest['task_type']}`（参考生成或视频编辑），逐项导入参考视频及参考图。
2. 按 references.json 的顺序核对素材编号与用途，再粘贴 prompt.txt。原稿文字不等于已验证的画面控制。
3. 原镜头为 {timing['source_frames']} 帧 / {timing['source_fps']} fps，即 {timing['target_seconds']:.6f} 秒；本次请求的生成/编辑参考时长为 {timing['generation_seconds']:.6f} 秒。补帧是否发生、补多少帧及保留区间见 timing.json。
4. 生成后先审动作、相机、身份、道具数量和接触关系，再保留原始视频并按裁切计划剪辑。此工作流不会自动把未审片的生成结果当成最终成片。
5. 手动下载的原视频可用 `python -m aivw seedance download --project <项目目录> --shot {manifest['shot_id']} --take {manifest['take']} --file <原始视频路径>` 导入本包，并记录文件哈希。

## API 路线的待办

{unresolved}

如果需要 API：自行把参考素材上传到你选择的存储或平台可信素材库，在 reference-manifest 中填写 HTTPS URL / asset URI。`preview_sha256` 是操作者确认“该 URL 指向本包参考视频”的声明；本工具只验证本地文件，不声称已经核验远端字节。不要把 API Key 写入素材映射或提示词。

首尾帧模式与全模态参考模式互斥。本包使用全模态参考模式，不混入 `first_frame` / `last_frame`。真人参考素材应走所用平台支持的授权素材流程。官方网页界面与 API 的入口、权限可能不同。
"""


def _prepare_one(args, project, root, shot, mapping, mapping_root):
    shot_id = shot["id"]
    provider = project.get("provider", {})
    task_type = args.task_type or provider.get("task_type", "reference")
    if task_type not in {"reference", "edit"}:
        raise WorkflowError("Seedance task_type must be reference or edit.")
    profile = provider.get("profile", "seedance-2.5")
    if profile != "seedance-2.5":
        raise WorkflowError("This adapter implements the verified seedance-2.5 profile only; do not mix version parameters.")
    fps = project.get("fps")
    if isinstance(fps, bool) or not isinstance(fps, int) or fps <= 0:
        raise WorkflowError("Project fps must be a positive integer.")
    frames, source_seconds = _duration(shot, fps)
    source = _inside(root, project_path(project, root, "renders") / shot_id / "preview.mp4")
    if not source.is_file():
        raise WorkflowError(f"Missing actual rendered preview: {source}; run render/assemble first.")
    has_managed_evidence = ((source.parent / "render.json").exists()
                            or (project_path(project, root, "build") / f"{shot_id}.build.json").exists())
    if args.allow_unmanaged_preview and not has_managed_evidence:
        render_evidence = {"mode": "unmanaged-explicit",
                           "note": "Operator supplied this preview explicitly; layout/build provenance has not been verified."}
    else:
        render_evidence = _render_evidence(project, root, shot, args.ffprobe)
    _video_container(source)
    source_probe = _probe(source, args.ffprobe)
    if source_probe and (source_probe["frames"] != frames or abs(source_probe["fps"] - fps) > 0.001):
        raise WorkflowError(f"Preview timing differs from the shot: {shot_id}. Re-render at original project fps/frames.")
    if source_seconds > 30:
        raise WorkflowError(f"Shot {shot_id} exceeds 30 seconds. Split it explicitly before Seedance preparation.")
    if args.duration is not None and task_type == "edit":
        raise WorkflowError("edit uses duration=-1; --duration is available only for reference generation.")
    generation_seconds = source_seconds if task_type == "edit" else float(args.duration or math.ceil(max(4, source_seconds)))
    if task_type == "reference" and (generation_seconds < max(4, source_seconds) or generation_seconds > 30):
        raise WorkflowError("Reference --duration must be 4–30 integer seconds and at least the original shot duration.")
    if task_type == "edit" and source_seconds < 4 and not args.allow_padding:
        raise WorkflowError(f"edit requires at least 4 seconds; {shot_id} is {source_seconds:g}s. Use --allow-padding for an explicit final-frame hold, or choose reference.")
    if task_type == "edit" and source_seconds < 4:
        generation_seconds = 4.0
    padding_seconds = max(0.0, generation_seconds - source_seconds) if args.allow_padding else 0.0
    prepared_seconds = source_seconds + padding_seconds
    model = args.model or provider.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise WorkflowError("provider.model / --model must be an actual nonempty model or endpoint ID.")
    resolution = args.resolution or provider.get("resolution", "720p")
    if resolution not in {"480p", "720p", "1080p"}:
        raise WorkflowError("The seedance-2.5 profile supports output 480p, 720p or 1080p.")
    shot_mapping = mapping.get(shot_id, {})
    if not isinstance(shot_mapping, dict):
        raise WorkflowError(f"Reference mapping for {shot_id} must be an object.")
    bundle = _bundle(project, root, shot_id, args.take)
    with _lock(bundle):
        if bundle.exists():
            old_receipt = read_json(bundle / "receipt.json")
            if old_receipt.get("state") != "prepared":
                raise WorkflowError(f"Take already has remote/manual history ({old_receipt.get('state')}): {bundle}. Preserve it; use a new --take for a deliberate new attempt.")
        stage = Path(tempfile.mkdtemp(prefix=".prepare-", dir=bundle.parent))
        try:
            shutil.copyfile(source, stage / "preview.mp4")
            input_files = [{"path": "preview.mp4", "sha256": _hash(stage / "preview.mp4"), "purpose": "original_preview"}]
            reference_name = "preview.mp4"
            if padding_seconds > 0:
                reference_name = "reference_padded.mp4"
                _pad(source, stage / reference_name, fps, frames, generation_seconds, args.ffmpeg)
                padded = _probe(stage / reference_name, args.ffprobe, required=True)
                if padded["frames"] != math.ceil(generation_seconds * fps - 1e-9) or abs(padded["fps"] - fps) > 0.001:
                    raise WorkflowError("Padded reference timing did not match the explicit plan.")
                input_files.append({"path": reference_name, "sha256": _hash(stage / reference_name), "purpose": "padded_reference"})
            reference_hash = _hash(stage / reference_name)
            preview_url = shot_mapping.get("preview_url")
            missing = []
            if not model:
                missing.append("未配置实际模型 ID：填写 project.provider.model 或 --model。")
            if not source_probe:
                missing.append("缺少 ffprobe，尚未验证参考视频的帧数、帧率和容器。")
            if not 24 <= fps <= 60:
                missing.append("参考视频帧率须在 24–60 fps；请显式转换预演后重新准备。")
            if prepared_seconds < 2:
                missing.append("参考视频不足 2 秒；可显式使用 --allow-padding 补尾帧。")
            if not _reference_url(preview_url):
                missing.append("未提供合法的 preview_url（HTTPS URL 或 asset:// URI）。")
            if preview_url and shot_mapping.get("preview_sha256") != reference_hash:
                missing.append("preview_sha256 未与本包参考视频匹配；请确认上传的是同一文件并填写其哈希。")
            references = []
            api_refs = []
            counts = {"image": 0, "video": 1, "audio": 0}
            listed = shot_mapping.get("references", [])
            if not isinstance(listed, list):
                raise WorkflowError("references must be a list.")
            for index, entry in enumerate(listed, 1):
                if not isinstance(entry, dict) or entry.get("type") not in {"image", "video", "audio"}:
                    raise WorkflowError("Each reference must have type image, video or audio.")
                kind = entry["type"]
                if entry.get("role") not in {None, "reference_" + kind}:
                    raise WorkflowError("First/last frame roles cannot be mixed with this omni-reference package.")
                counts[kind] += 1
                item = {"index": index, "type": kind, "provider_number": counts[kind],
                        "name": entry.get("name", f"reference_{index}"), "usage": entry.get("usage", ""),
                        "url": entry.get("url"), "duration_seconds": entry.get("duration_seconds")}
                if not isinstance(item["usage"], str) or not item["usage"].strip():
                    raise WorkflowError(f"Reference {index} needs an explicit usage mapping.")
                local = entry.get("local_path")
                if local:
                    if not isinstance(local, str):
                        raise WorkflowError("local_path must be a string.")
                    original = (mapping_root / local).expanduser().resolve()
                    if not original.is_file():
                        raise WorkflowError(f"Reference file does not exist: {original}")
                    suffix = original.suffix.lower()
                    allowed = {"image": {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif", ".gif", ".heic", ".heif"},
                               "video": {".mp4", ".mov"}, "audio": {".wav", ".mp3"}}
                    if suffix not in allowed[kind]:
                        raise WorkflowError(f"Unsupported {kind} reference extension: {suffix}")
                    destination = f"references/ref_{index:02d}{suffix}"
                    (stage / "references").mkdir(exist_ok=True)
                    shutil.copyfile(original, stage / destination)
                    item.update({"local_path": destination, "sha256": _hash(stage / destination)})
                    input_files.append({"path": destination, "sha256": item["sha256"], "purpose": "reference_" + kind})
                    if kind == "video":
                        _video_container(stage / destination)
                        info = _probe(stage / destination, args.ffprobe)
                        if info:
                            item["duration_seconds"] = info["duration_seconds"]
                if not _reference_url(item["url"]):
                    missing.append(f"参考素材 {index} 缺少合法 HTTPS / asset URI，手动界面可使用本地副本。")
                references.append(item)
                api_refs.append({"type": kind + "_url", kind + "_url": {"url": item["url"] or f"__REFERENCE_{index}_URL__"},
                                 "role": "reference_" + kind})
            if counts["image"] > 30 or counts["video"] > 10 or counts["audio"] > 10:
                raise WorkflowError("Seedance 2.5 accepts at most 30 images, 10 videos and 10 audio clips (including the preview).")
            for kind in ("video", "audio"):
                durations = [item.get("duration_seconds") for item in references if item["type"] == kind]
                if any(value is None for value in durations):
                    missing.append(f"额外 {kind} 参考须填写 duration_seconds，以核对总时长上限。")
                    continue
                try:
                    numeric = [float(value) for value in durations]
                except (TypeError, ValueError) as exc:
                    raise WorkflowError("Reference duration_seconds must be numeric.") from exc
                if any(not math.isfinite(value) or value < 2 or value > 30 for value in numeric):
                    raise WorkflowError("Each extra video/audio reference must be 2–30 seconds long.")
                if sum(numeric) + (prepared_seconds if kind == "video" else 0) > 30.001:
                    raise WorkflowError(f"Combined {kind} references exceed 30 seconds.")
            prompt = shot_mapping.get("prompt")
            if prompt is None:
                action = "编辑视频1：依据白模结构完成写实重渲染，保留原运镜、站位、动作顺序。" if task_type == "edit" else "参考视频1的白模运镜、站位和动作，生成同一镜头。"
                prompt = action + "\n镜头内容：" + str(shot.get("description", "")) + "\n仅实现该镜头，不额外增加情节。"
                if padding_seconds > 0:
                    prompt += f"\n参考视频末尾的 {padding_seconds:g} 秒为保持最后画面的剪辑余量。"
                for item in references:
                    prompt += f"\n{item['type']} {item['provider_number']}：{item['usage']}"
            if not isinstance(prompt, str) or not prompt.strip():
                raise WorkflowError("Prompt must be nonempty text.")
            audio = args.generate_audio if args.generate_audio is not None else provider.get("generate_audio", False)
            ratio = "adaptive" if task_type == "edit" else (args.ratio or provider.get("ratio", "16:9"))
            if ratio not in {"16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"}:
                raise WorkflowError("Unsupported output ratio.")
            request = {"model": model or "__MODEL_ID__", "content": [
                {"type": "text", "text": prompt},
                {"type": "video_url", "video_url": {"url": preview_url or "__PREVIEW_URL__"}, "role": "reference_video"},
                *api_refs], "omni_reference_task_type": task_type,
                "resolution": resolution, "ratio": ratio,
                "duration": -1 if task_type == "edit" else int(generation_seconds),
                "generate_audio": bool(audio), "output_format": "mov" if task_type == "edit" else "mp4",
                "watermark": bool(provider.get("watermark", True))}
            timing = {"source_frames": frames, "source_fps": fps,
                      "source_requested_seconds": shot["duration_seconds"], "target_seconds": source_seconds,
                      "reference_seconds": prepared_seconds, "generation_seconds": generation_seconds,
                      "padding_explicitly_authorized": bool(args.allow_padding),
                      "padding_frames": math.ceil(prepared_seconds * fps - 1e-9) - frames,
                      "padding_method": "hold-final-frame" if padding_seconds > 0 else "none",
                      "trim": {"start_seconds": 0, "end_seconds": source_seconds,
                               "project_start_frame_inclusive": 0, "project_end_frame_exclusive": frames,
                               "target_fps": fps, "target_frames": frames,
                               "instruction": "审片后截取上述时段并在剪辑器中对齐项目帧率与帧数；不自动变速。生成时序不受逐帧保证，必要时重做。"},
                      "expected_provider_fps": 24,
                      "note": "edit 输出时长可能略短于输入；reference 只参考运动。原镜头时长与生成时长分别记录。"}
            (stage / "prompt.txt").write_text(prompt + "\n", encoding="utf-8")
            input_files.append({"path": "prompt.txt", "sha256": _hash(stage / "prompt.txt"), "purpose": "prompt"})
            write_json(stage / "references.json", {"preview": {"provider_number": 1, "path": reference_name, "usage": "运镜、站位、动作及空间参考"}, "references": references})
            write_json(stage / "timing.json", timing)
            for metadata_name in ("references.json", "timing.json"):
                input_files.append({"path": metadata_name, "sha256": _hash(stage / metadata_name), "purpose": "review_metadata"})
            write_json(stage / "request.json", request)
            manifest = {"schema_version": "1.0", "adapter": "aivw-seedance", "profile": profile,
                        "project_id": project.get("id"), "shot_id": shot_id, "take": args.take,
                        "created_at": _now(), "task_type": task_type, "model": model,
                        "source": {"preview_path": str(source.relative_to(root)), "preview_sha256": _hash(source),
                                   "shot_sha256": _json_hash(shot)},
                        "render_provenance": render_evidence,
                        "preview_reference": {"path": reference_name, "sha256": reference_hash,
                                              "url": preview_url, "operator_claimed_remote_sha256": shot_mapping.get("preview_sha256"),
                                              "remote_bytes_verified": False},
                        "source_probe": source_probe, "timing": timing, "references": references,
                        "input_files": input_files, "request_sha256": _json_hash(request),
                        "api_ready": not missing, "api_unresolved": missing}
            write_json(stage / "manifest.json", manifest)
            write_json(stage / "receipt.json", {"schema_version": "1.0", "state": "prepared", "task_id": None,
                       "shot_id": shot_id, "take": args.take, "manifest_sha256": _json_hash(manifest),
                       "request_sha256": _json_hash(request), "prepared_at": _now()})
            (stage / "HANDOFF.md").write_text(_manual_text(manifest), encoding="utf-8")
            if bundle.exists():
                archive = bundle.parent / "_prepared_archive"
                archive.mkdir(exist_ok=True)
                previous = archive / (bundle.name + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
                os.replace(bundle, previous)
            os.replace(stage, bundle)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    return {"shot_id": shot_id, "take": args.take, "bundle": str(bundle), "api_ready": not missing,
            "api_unresolved": missing, "reference_sha256": reference_hash, "timing": timing}


def prepare(args):
    project, root = load_project(args.project)
    shots = _selected(project, root, args.shot)
    if args.max_shots < 1 or len(shots) > args.max_shots:
        raise WorkflowError("Selected shot count exceeds --max-shots; select shots or raise the explicit local preparation limit.")
    mapping, mapping_root = _input_mapping(args)
    results = [_prepare_one(args, project, root, shot, mapping, mapping_root) for shot in shots]
    emit({"operation": "seedance.prepare", "network_used": False, "paid_tasks": 0, "packages": results})
    return 0


def _verify(project, root, shot, bundle, api=False, ffprobe=None):
    manifest, request, receipt = (read_json(bundle / name) for name in ("manifest.json", "request.json", "receipt.json"))
    if manifest.get("adapter") != "aivw-seedance" or manifest.get("profile") != "seedance-2.5":
        raise WorkflowError("Not a supported Seedance package.")
    if manifest.get("shot_id") != shot["id"] or manifest.get("take") != bundle.name:
        raise WorkflowError("Package identity does not match the selected shot/take.")
    if receipt.get("manifest_sha256") != _json_hash(manifest) or receipt.get("request_sha256") != _json_hash(request):
        raise WorkflowError("Manifest/request changed after prepare. Re-prepare an unsubmitted take; never edit a paid receipt.")
    if manifest.get("request_sha256") != _json_hash(request):
        raise WorkflowError("Request hash mismatch.")
    if api and manifest["source"]["shot_sha256"] != _json_hash(shot):
        raise WorkflowError("Source shot changed after prepare; rebuild and prepare a new take.")
    source = _inside(root, root / manifest["source"]["preview_path"])
    if api and (not source.is_file() or _hash(source) != manifest["source"]["preview_sha256"]):
        raise WorkflowError("Actual rendered preview changed or disappeared after prepare.")
    if api and manifest.get("render_provenance", {}).get("mode") == "managed":
        if _render_evidence(project, root, shot, ffprobe) != manifest["render_provenance"]:
            raise WorkflowError("Managed build/render provenance changed after prepare; rebuild and prepare a new take.")
    for item in manifest["input_files"]:
        path = _inside(bundle, bundle / item["path"])
        if not path.is_file() or _hash(path) != item["sha256"]:
            raise WorkflowError(f"Packaged input changed after prepare: {item['path']}")
    if api and not manifest.get("api_ready"):
        raise WorkflowError("Package is for manual handoff and not API-ready: " + "; ".join(manifest.get("api_unresolved", [])))
    return manifest, request, receipt


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _api_settings(args, project):
    base = (args.api_base or project.get("provider", {}).get("api_base") or API_BASE).rstrip("/")
    parsed = urlparse(base)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise WorkflowError("API base URL must not contain credentials, query or fragment.")
    if not (parsed.scheme == "https" and parsed.hostname in API_HOSTS):
        if not (args.allow_local_http and parsed.scheme == "http" and local):
            raise WorkflowError("API Key is sent only to official Ark hosts, or an explicitly enabled loopback HTTP mock.")
    key = os.environ.get("ARK_API_KEY")
    if not key or not key.strip():
        raise WorkflowError("Set ARK_API_KEY in the environment; do not write it into project files or commands.")
    if not 0 < args.timeout <= 60:
        raise WorkflowError("--timeout must be greater than zero and at most 60 seconds.")
    return base, key.strip()


def _json_http(method, url, key, timeout, data=None):
    body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode() if data is not None else None
    request = Request(url, data=body, method=method, headers={"Authorization": "Bearer " + key,
                      "Accept": "application/json", "Content-Type": "application/json"})
    with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
        raw = response.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("API response exceeded the JSON limit")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("API response is not an object")
    return parsed


def _redact(value, key):
    if isinstance(value, str):
        return value.replace(key, "[REDACTED]")
    if isinstance(value, dict):
        return {name: _redact(item, key) for name, item in value.items() if name.lower() not in {"authorization", "api_key", "apikey"}}
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    return value


def submit(args):
    project, root = load_project(args.project)
    shots = _selected(project, root, args.shot, require=True)
    if not args.confirm_paid:
        raise WorkflowError("submit can charge your provider account. Use --confirm-paid after reviewing the prepared package.")
    if args.max_tasks is None or args.max_tasks < 1:
        raise WorkflowError("Provide --max-tasks N as an explicit limit on new paid tasks for this invocation.")
    pending = []
    existing = []
    for shot in shots:
        bundle = _bundle(project, root, shot["id"], args.take)
        manifest, request, receipt = _verify(project, root, shot, bundle, api=True, ffprobe=args.ffprobe)
        if receipt["state"] in UNRESOLVED:
            raise WorkflowError(f"Submission outcome is unresolved for {shot['id']}/{args.take}. No retry is sent. Find the task in the provider console and adopt its task ID with poll.")
        if receipt["state"] != "prepared":
            existing.append({"shot_id": shot["id"], "state": receipt["state"], "task_id": receipt.get("task_id"), "posted": False})
        else:
            pending.append((shot, bundle, manifest, request))
    seconds = sum(item[2]["timing"]["generation_seconds"] for item in pending)
    if len(pending) > args.max_tasks:
        raise WorkflowError(f"{len(pending)} new tasks exceed --max-tasks {args.max_tasks}; no requests were sent.")
    if args.max_generated_seconds is not None and (not math.isfinite(args.max_generated_seconds) or args.max_generated_seconds <= 0 or seconds > args.max_generated_seconds + 1e-9):
        raise WorkflowError("Planned generation duration exceeds --max-generated-seconds; no requests were sent.")
    if not pending:
        emit({"operation": "seedance.submit", "new_paid_tasks": 0, "existing": existing})
        return 0
    base, key = _api_settings(args, project)
    results = []
    for shot, bundle, manifest, request in pending:
        with _lock(bundle):
            manifest, request, receipt = _verify(project, root, shot, bundle, api=True, ffprobe=args.ffprobe)
            if receipt["state"] != "prepared":
                raise WorkflowError("Take state changed during submission. No duplicate request was sent.")
            receipt.update({"state": "submitting", "submission_started_at": _now(), "api_base": base,
                            "budget": {"max_tasks_this_invocation": args.max_tasks,
                                       "max_generated_seconds_this_invocation": args.max_generated_seconds,
                                       "planned_generation_seconds": manifest["timing"]["generation_seconds"],
                                       "currency_cap": None, "note": "Count/duration limits are not a monetary budget guarantee."}})
            write_json(bundle / "receipt.json", receipt)
            try:
                response = _json_http("POST", base + "/contents/generations/tasks", key, args.timeout, request)
                task_id = response.get("id")
                _safe_id(task_id, "provider task id")
            except HTTPError as exc:
                receipt.update({"state": "submission_rejected" if 400 <= exc.code < 500 and exc.code not in {408, 409, 429} else "unknown",
                                "last_error": {"http_status": exc.code, "message": "POST failed; no automatic retry is permitted."}, "updated_at": _now()})
                write_json(bundle / "receipt.json", receipt)
                raise WorkflowError(f"Provider returned HTTP {exc.code}; receipt saved and POST will not be retried automatically.") from exc
            except (URLError, TimeoutError, OSError, ValueError, HTTPException, WorkflowError) as exc:
                receipt.update({"state": "unknown", "last_error": {"type": type(exc).__name__, "message": "No usable submission acknowledgement; the provider may have accepted and charged the task."}, "updated_at": _now()})
                write_json(bundle / "receipt.json", receipt)
                raise WorkflowError("Submission acknowledgement is unknown. No retry is sent; reconcile this take with the provider console.") from exc
            receipt.update({"state": "submitted", "task_id": task_id, "submitted_at": _now(),
                            "submission_response": _redact(response, key)})
            write_json(bundle / "receipt.json", receipt)
            results.append({"shot_id": shot["id"], "task_id": task_id, "state": "submitted", "posted": True})
    emit({"operation": "seedance.submit", "new_paid_tasks": len(results), "results": results, "existing": existing})
    return 0


def poll(args):
    project, root = load_project(args.project)
    shots = _selected(project, root, args.shot, require=True)
    if args.adopt_task_id and (len(shots) != 1 or not args.confirm_task_match):
        raise WorkflowError("Adopting a task requires one --shot and --confirm-task-match after checking the provider console.")
    base, key = _api_settings(args, project)
    results = []
    for shot in shots:
        bundle = _bundle(project, root, shot["id"], args.take)
        with _lock(bundle):
            manifest, request, receipt = _verify(project, root, shot, bundle)
            if args.adopt_task_id:
                task_id = _safe_id(args.adopt_task_id, "task id")
                if receipt.get("task_id") not in {None, task_id}:
                    raise WorkflowError("This take already points to a different task; it cannot be overwritten.")
                if receipt.get("task_id") is None:
                    receipt.update({"task_id": task_id, "state": "submitted", "task_adopted_at": _now(),
                                    "task_adoption": "operator confirmed the task matches this exact prepared package", "api_base": base})
                    write_json(bundle / "receipt.json", receipt)
            task_id = receipt.get("task_id")
            if not task_id:
                raise WorkflowError("No task ID is known. Poll cannot discover or resubmit an unknown POST.")
            _safe_id(task_id, "task id")
            if receipt.get("api_base", base) != base:
                raise WorkflowError("API base differs from the original task receipt.")
            if receipt["state"] not in TERMINAL:
                try:
                    response = _json_http("GET", base + "/contents/generations/tasks/" + task_id, key, args.timeout)
                    if response.get("id", task_id) != task_id:
                        raise ValueError("Provider returned a different task ID")
                    status = response.get("status")
                    if status not in {"queued", "running", "succeeded", "failed", "expired", "cancelled"}:
                        raise ValueError("Unknown provider task status")
                except (HTTPError, URLError, TimeoutError, OSError, ValueError, HTTPException) as exc:
                    receipt["last_poll_error"] = {"at": _now(), "type": type(exc).__name__, "message": "Poll failed; task and submission state are preserved."}
                    write_json(bundle / "receipt.json", receipt)
                    raise WorkflowError("Task poll failed; re-run poll later. No POST was made.") from exc
                receipt.update({"state": status, "last_polled_at": _now(), "provider_response": _redact(response, key)})
                write_json(bundle / "receipt.json", receipt)
            results.append({"shot_id": shot["id"], "task_id": task_id, "state": receipt["state"], "new_paid_tasks": 0})
    emit({"operation": "seedance.poll", "results": results})
    return 0


def _download_url(value, allow_local):
    if not isinstance(value, str):
        raise WorkflowError("Provider response contains no video URL.")
    parsed = urlparse(value)
    if parsed.username or parsed.password or not parsed.hostname:
        raise WorkflowError("Invalid video download URL.")
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    try:
        address = ipaddress.ip_address(parsed.hostname)
        if not address.is_global and not (allow_local and address.is_loopback):
            raise WorkflowError("Private/link-local video download addresses are rejected.")
    except ValueError:
        pass
    if not (parsed.scheme == "https" and not local) and not (allow_local and local and parsed.scheme == "http"):
        raise WorkflowError("Video download requires HTTPS; loopback HTTP is for explicitly enabled local tests only.")
    return value


def download(args):
    project, root = load_project(args.project)
    shots = _selected(project, root, args.shot, require=True)
    if args.file and len(shots) != 1:
        raise WorkflowError("Manual --file import requires exactly one --shot.")
    if args.max_mib <= 0 or not 0 < args.timeout <= 60:
        raise WorkflowError("--max-mib must be positive; --timeout must be in (0, 60].")
    maximum = args.max_mib * 1024 * 1024
    results = []
    for shot in shots:
        bundle = _bundle(project, root, shot["id"], args.take)
        with _lock(bundle):
            manifest, request, receipt = _verify(project, root, shot, bundle)
            if receipt["state"] == "downloaded":
                stored = _inside(bundle, bundle / receipt["result"]["path"])
                if not stored.is_file() or _hash(stored) != receipt["result"]["sha256"]:
                    raise WorkflowError("Previously downloaded original is missing or changed; it will not be overwritten.")
                results.append({"shot_id": shot["id"], "path": str(stored), "downloaded": False, "already_present": True})
                continue
            if args.file:
                if receipt["state"] != "prepared":
                    raise WorkflowError("Manual file import is only allowed for an unsubmitted take; preserve existing API history.")
                local = Path(args.file).expanduser().resolve()
                if not local.is_file() or local.stat().st_size > maximum:
                    raise WorkflowError("Manual result does not exist or exceeds --max-mib.")
                origin = "manual-file"
            else:
                if receipt["state"] != "succeeded":
                    raise WorkflowError("Task has not succeeded; poll it first.")
                url = _download_url(receipt.get("provider_response", {}).get("content", {}).get("video_url"), args.allow_local_http)
                origin = "provider-url"
            destination_dir = bundle / "result"
            destination_dir.mkdir(exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(prefix=".original-", suffix=".part", dir=destination_dir)
            os.close(fd)
            temporary = Path(temporary_name)
            try:
                if args.file:
                    shutil.copyfile(local, temporary)
                else:
                    # Deliberately omit ARK_API_KEY: generated media may be on a separate CDN.
                    try:
                        with build_opener(_NoRedirect()).open(Request(url, headers={"Accept": "video/*,application/octet-stream"}), timeout=args.timeout) as response:
                            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                            if content_type and not (content_type.startswith("video/") or content_type == "application/octet-stream"):
                                raise WorkflowError("Video URL returned a non-video content type.")
                            declared = response.headers.get("Content-Length")
                            declared_size = int(declared) if declared is not None else None
                            if declared_size is not None and (declared_size > maximum or declared_size < 16):
                                raise WorkflowError("Video response size is outside the allowed range.")
                            total = 0
                            with temporary.open("wb") as out:
                                while True:
                                    block = response.read(1024 * 1024)
                                    if not block:
                                        break
                                    total += len(block)
                                    if total > maximum:
                                        raise WorkflowError("Video exceeded --max-mib while downloading.")
                                    out.write(block)
                            if declared_size is not None and total != declared_size:
                                raise WorkflowError("Video download is truncated (Content-Length mismatch).")
                    except (HTTPError, URLError, TimeoutError, OSError, ValueError, HTTPException) as exc:
                        raise WorkflowError("Video download failed; existing task history and originals are preserved. Re-run download later.") from exc
                container = _video_container(temporary)
                probe = _probe(temporary, args.ffprobe)
                target = destination_dir / ("original." + container)
                if target.exists():
                    raise WorkflowError("An unrecorded original already exists; it will not be overwritten. Inspect it before retrying.")
                digest = _hash(temporary)
                size = temporary.stat().st_size
                os.replace(temporary, target)
                warnings = []
                if not probe:
                    warnings.append("ffprobe unavailable: container signature/size checked; codec and duration remain unverified.")
                elif probe["duration_seconds"] + 0.001 < manifest["timing"]["target_seconds"]:
                    warnings.append("Generated result is shorter than the original shot; do not silently stretch it to fit.")
                result = {"path": str(target.relative_to(bundle)), "sha256": digest, "size_bytes": size,
                          "origin": origin, "container": container, "probe": probe, "warnings": warnings,
                          "untouched_original": True, "needs_human_review": True}
                receipt.update({"state": "downloaded", "downloaded_at": _now(), "result": result})
                write_json(bundle / "receipt.json", receipt)
                results.append({**result, "shot_id": shot["id"], "path": str(target), "downloaded": True})
            finally:
                temporary.unlink(missing_ok=True)
    emit({"operation": "seedance.download", "new_paid_tasks": 0, "results": results})
    return 0


def register(subparsers):
    parser = subparsers.add_parser("seedance", help="离线手动包、显式提交、可恢复查询与原片下载")
    operations = parser.add_subparsers(dest="seedance_command", required=True)
    for name, function in (("prepare", prepare), ("submit", submit), ("poll", poll), ("download", download)):
        item = operations.add_parser(name)
        item.add_argument("--project", required=True, help="项目目录或 project.json")
        item.add_argument("--shot", action="append", help="可重复；网络操作必须显式指定")
        item.add_argument("--take", default="take001", help="独立尝试编号，保存历史并防重复提交")
        item.set_defaults(func=function)
        if name == "prepare":
            item.add_argument("--model", help="从账号模型列表复制的实际 ID；不猜测模型 ID")
            item.add_argument("--task-type", choices=("reference", "edit"))
            item.add_argument("--reference-manifest", help="逐镜素材、提示词、远端引用映射 JSON")
            item.add_argument("--duration", type=int, help="reference 生成整数秒数；原镜头时长另存")
            item.add_argument("--resolution", choices=("480p", "720p", "1080p"))
            item.add_argument("--ratio", choices=("16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"))
            item.add_argument("--generate-audio", action=argparse.BooleanOptionalAction, default=None)
            item.add_argument("--allow-padding", action="store_true", help="显式授权保持尾帧补足参考时长，原文件不变")
            item.add_argument("--allow-unmanaged-preview", action="store_true", help="显式接受没有构建/渲染凭据的外来预演；已有受管理凭据时仍校验其当前性")
            item.add_argument("--max-shots", type=int, default=20)
            item.add_argument("--ffmpeg")
            item.add_argument("--ffprobe")
        else:
            item.add_argument("--timeout", type=float, default=30)
            item.add_argument("--allow-local-http", action="store_true", help="仅本机 HTTP mock 测试")
            if name in {"submit", "poll"}:
                item.add_argument("--api-base", help="官方 Ark API base；默认来自项目配置")
            if name == "submit":
                item.add_argument("--confirm-paid", action="store_true", help="确认本次提交会产生供应商费用")
                item.add_argument("--max-tasks", type=int, help="本次最多新建的收费任务数，必须明确提供")
                item.add_argument("--max-generated-seconds", type=float)
                item.add_argument("--ffprobe", help="验证受管理渲染来源时使用的 ffprobe")
            elif name == "poll":
                item.add_argument("--adopt-task-id", help="从控制台找到的任务 ID，用于恢复未知提交结果")
                item.add_argument("--confirm-task-match", action="store_true")
            else:
                item.add_argument("--file", help="离线导入手动界面下载的原始视频")
                item.add_argument("--max-mib", type=int, default=512)
                item.add_argument("--ffprobe")
