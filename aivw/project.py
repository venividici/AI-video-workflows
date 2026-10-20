"""Project creation and integrity validation for reusable video workflows."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import shutil

from .common import WorkflowError, emit, load_project, project_path, read_json, write_json
from .storyboard import duration_to_frames, issue


REPOSITORY = Path(__file__).resolve().parents[1]
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z")
PATH_KEYS = ("shots", "scene", "build", "renders", "outputs", "references")
RUNTIME_KEYS = ("build", "renders", "outputs")


def _positive_integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def init_command(args):
    target = Path(args.directory).expanduser().resolve()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise WorkflowError("Project destination must be absent or an empty directory; existing files are never overwritten by init.")
    if args.example:
        named = REPOSITORY / "examples" / args.example
        source = named if named.is_dir() else Path(args.example).expanduser().resolve()
    else:
        source = REPOSITORY / "templates" / "empty-project"
    source = source.resolve()
    if not (source / "project.json").is_file():
        raise WorkflowError(f"Example/template has no project.json: {source}")
    if target == source or target.is_relative_to(source) or source.is_relative_to(target):
        raise WorkflowError("Project destination and template must not contain each other.")
    project = read_json(source / "project.json")
    if not isinstance(project, dict):
        raise WorkflowError("Template project.json must be an object.")
    project_id = args.id or re.sub(r"[^A-Za-z0-9._-]+", "-", target.name).strip("-._") or "my-project"
    if not SAFE_ID.fullmatch(project_id):
        raise WorkflowError("Project id must be 1–96 filesystem-safe ASCII letters/digits, dots, underscores or hyphens.")
    project["id"] = project_id
    if args.title:
        project["title"] = args.title
    elif not args.example:
        project["title"] = target.name
    if args.fps is not None:
        if not _positive_integer(args.fps):
            raise WorkflowError("--fps must be a positive integer.")
        if args.example and args.fps != project.get("fps"):
            raise WorkflowError("Changing an example's fps requires re-timing its shots and layout. Copy it first, then edit explicitly.")
        project["fps"] = args.fps
    resolution = project.get("resolution", [1280, 720])
    for index, value in enumerate((args.width, args.height)):
        if value is not None:
            if not _positive_integer(value) or value % 2:
                raise WorkflowError("--width and --height must be positive even integers for H.264 previews.")
            resolution[index] = value
    project["resolution"] = resolution
    # Validate path boundaries before copying or creating runtime directories.
    for key in PATH_KEYS:
        project_path(project, target, key)
    ignore = shutil.ignore_patterns(".git", "__pycache__", ".DS_Store", ".env", "*.pyc", "build", "renders", "outputs")
    shutil.copytree(source, target, dirs_exist_ok=target.exists(), ignore=ignore)
    write_json(target / "project.json", project)
    for key in (*RUNTIME_KEYS, "references"):
        project_path(project, target, key).mkdir(parents=True, exist_ok=True)
    ignore_file = target / ".gitignore"
    existing = ignore_file.read_text(encoding="utf-8") if ignore_file.exists() else ""
    additions = []
    for key in RUNTIME_KEYS:
        relative = project_path(project, target, key).relative_to(target).as_posix()
        pattern = f"/{relative}/"
        if pattern not in existing.splitlines():
            additions.append(pattern)
    for pattern in (".env", ".env.*", "__pycache__/", "*.pyc"):
        if pattern not in existing.splitlines():
            additions.append(pattern)
    if additions:
        ignore_file.write_text(existing.rstrip("\n") + ("\n" if existing else "") + "\n".join(additions) + "\n", encoding="utf-8")
    emit({"created": str(target), "project": str(target / "project.json"), "example": args.example,
          "next": "Run validate, then build and render." if args.example else "Inspect a source storyboard, import its rows, then author scene.json layouts for those IDs."})
    return 0


def _read_for_validation(path, issues, label):
    try:
        return read_json(path)
    except WorkflowError as exc:
        issues.append(issue(f"invalid_{label}", str(exc)))
        return None


def _scene_shot_ids(scene):
    layouts = scene.get("shots") if isinstance(scene, dict) else None
    if isinstance(layouts, dict):
        return set(layouts)
    if isinstance(layouts, list):
        return {item.get("id", item.get("shot_id")) for item in layouts if isinstance(item, dict)}
    return set()


def validate_project(project, root, *, source_audit=False):
    """Return a structured report; validation never rewrites project or source data."""
    root = Path(root).resolve()
    issues = []
    if not isinstance(project, dict):
        return {"valid": False, "issues": [issue("invalid_project", "project.json must be an object.")]}
    if project.get("schema_version") != "1.0":
        issues.append(issue("schema_version", "project.json schema_version must be '1.0'."))
    if not isinstance(project.get("id"), str) or not SAFE_ID.fullmatch(project["id"]):
        issues.append(issue("project_id", "Project id must be a filesystem-safe ASCII string."))
    if not isinstance(project.get("title"), str) or not project["title"].strip():
        issues.append(issue("project_title", "Project title must be a nonempty string."))
    fps = project.get("fps")
    fps_valid = _positive_integer(fps)
    if not fps_valid:
        issues.append(issue("invalid_fps", "fps must be a positive integer."))
    resolution = project.get("resolution")
    if not isinstance(resolution, list) or len(resolution) != 2 or not all(_positive_integer(x) for x in resolution):
        issues.append(issue("invalid_resolution", "resolution must contain two positive integers: [width, height]."))
    elif any(value % 2 for value in resolution):
        issues.append(issue("invalid_resolution", "H.264 previews require even width and height; choose explicit even dimensions."))
    paths = project.get("paths")
    resolved = {}
    if not isinstance(paths, dict):
        issues.append(issue("invalid_paths", "paths must be an object containing shots, scene, build, renders, outputs and references."))
    else:
        for key in PATH_KEYS:
            value = paths.get(key)
            if not isinstance(value, str) or not value.strip() or Path(value).is_absolute():
                issues.append(issue("invalid_path", f"paths.{key} must be a nonempty project-relative path.", field=key))
                continue
            try:
                resolved[key] = project_path(project, root, key)
                if resolved[key] == root:
                    issues.append(issue("invalid_path", f"paths.{key} must identify a file/subdirectory, not the project root.", field=key))
            except WorkflowError as exc:
                issues.append(issue("unsafe_path", str(exc), field=key))
        for i, key in enumerate(resolved):
            for other in list(resolved)[:i]:
                if resolved[key] == resolved[other]:
                    issues.append(issue("path_collision", f"paths.{key} and paths.{other} resolve to the same location."))
        for key in RUNTIME_KEYS:
            folder = resolved.get(key)
            if folder and folder.exists() and not folder.is_dir():
                issues.append(issue("invalid_runtime_directory", f"paths.{key} points to a file."))
    provider = project.get("provider")
    if not isinstance(provider, dict) or provider.get("mode") not in ("manual", "api"):
        issues.append(issue("invalid_provider", "provider.mode must be 'manual' or 'api'."))
    data = _read_for_validation(resolved["shots"], issues, "shots") if "shots" in resolved else None
    shots = data.get("shots") if isinstance(data, dict) else None
    if data is not None and (not isinstance(data, dict) or data.get("schema_version") != "1.0" or not isinstance(shots, list)):
        issues.append(issue("shots_structure", "shots.json must contain schema_version '1.0' and a shots array."))
        shots = None
    if isinstance(shots, list) and not shots:
        issues.append(issue("empty_storyboard", "No shots are defined yet; import or author shots before rendering."))
    seen_ids, source_rows = set(), set()
    total_frames = 0
    for index, shot in enumerate(shots or []):
        if not isinstance(shot, dict):
            issues.append(issue("invalid_shot", f"Shot at index {index} must be an object."))
            continue
        shot_id = shot.get("id")
        if not isinstance(shot_id, str) or not SAFE_ID.fullmatch(shot_id):
            issues.append(issue("invalid_shot_id", f"Shot at index {index} needs a filesystem-safe ID."))
        elif shot_id in seen_ids:
            issues.append(issue("duplicate_shot_id", f"Duplicate shot ID: {shot_id}", shot_id=shot_id))
        else:
            seen_ids.add(shot_id)
        for field in ("description", "scene_id"):
            if not isinstance(shot.get(field), str) or not shot[field].strip():
                issues.append(issue("missing_shot_field", f"Shot requires a nonempty {field}.", shot_id=shot_id, field=field))
        if not isinstance(shot.get("dialogue", ""), str):
            issues.append(issue("invalid_dialogue", "dialogue must be a string.", shot_id=shot_id))
        frames = shot.get("frames")
        if not _positive_integer(frames):
            issues.append(issue("invalid_frames", "Shot frames must be a positive integer.", shot_id=shot_id))
        else:
            total_frames += frames
        if fps_valid:
            try:
                _, expected, report = duration_to_frames(shot.get("duration_seconds"), fps)
                if frames != expected or isinstance(frames, bool):
                    issues.append(issue("frame_mismatch", "Stored frames do not match duration_seconds × fps with ROUND_HALF_UP.", shot_id=shot_id, stored=frames, expected=expected))
                if report:
                    issues.append(issue("frame_quantization", "Original duration is preserved; render duration is quantized to whole frames.", severity="warning", shot_id=shot_id, **report))
            except (ValueError, WorkflowError) as exc:
                issues.append(issue("invalid_duration", str(exc), shot_id=shot_id))
        source = shot.get("source")
        if "source" in shot:
            if not isinstance(source, dict) or not _positive_integer(source.get("row")) or not isinstance(source.get("raw"), dict):
                issues.append(issue("invalid_source_record", "source must preserve a positive row number and a raw object.", shot_id=shot_id))
            else:
                identity = (str(source.get("file")), str(source.get("sheet")), source["row"])
                if identity in source_rows:
                    issues.append(issue("duplicate_source_row", "The same source row occurs more than once in shots.json.", shot_id=shot_id, source_row=source["row"]))
                source_rows.add(identity)
    if isinstance(data, dict):
        imported_issues = data.get("issues", [])
        if not isinstance(imported_issues, list):
            issues.append(issue("invalid_import_issues", "shots.json issues must be an array."))
        else:
            for entry in imported_issues:
                if isinstance(entry, dict) and entry.get("severity") == "error":
                    issues.append(issue("unresolved_import_issue", "Import contains an unresolved error; fix/re-import source data or mapping.", original_issue=entry))
        audit = data.get("import")
        if isinstance(audit, dict):
            if audit.get("fps") != fps:
                issues.append(issue("import_fps_changed", "Project fps differs from the import fps. Re-import to re-quantize frames and audit timing."))
            if audit.get("imported_rows") != len(shots or []):
                issues.append(issue("import_row_count_changed", "Shot count differs from the import audit; confirm that no source rows were lost."))
            expected_count = len(shots or []) + len(audit.get("blank_rows_skipped", [])) + len(audit.get("excluded_rows", []))
            if audit.get("source_data_rows") != expected_count:
                issues.append(issue("source_row_reconciliation", "Imported, blank and explicitly excluded rows do not reconcile to source_data_rows."))
            if source_audit:
                source = audit.get("source", {})
                source_path = Path(source.get("path", "")).expanduser()
                if not source_path.is_file():
                    issues.append(issue("source_unavailable", "Original import source is unavailable; audit cannot verify its hash.", path=str(source_path)))
                else:
                    actual_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
                    if actual_hash != source.get("sha256"):
                        issues.append(issue("source_changed", "Original source bytes differ from the recorded import hash; re-import or restore the intended source."))
        elif source_audit:
            issues.append(issue("no_import_audit", "This authored project has no import audit to verify.", severity="warning"))
    scene = _read_for_validation(resolved["scene"], issues, "scene") if "scene" in resolved else None
    if scene is not None:
        if not isinstance(scene, dict) or scene.get("schema_version") != "1.0":
            issues.append(issue("invalid_scene_structure", "scene.json must be an object with schema_version '1.0'."))
        else:
            layout_ids = _scene_shot_ids(scene)
            for shot_id in sorted(seen_ids - layout_ids):
                issues.append(issue("missing_layout", "No scene.json layout exists for this shot; no generic fallback is rendered.", shot_id=shot_id))
            for shot_id in sorted((layout_ids - seen_ids) - {None}):
                issues.append(issue("unused_layout", "scene.json contains a layout without a matching storyboard shot.", severity="warning", shot_id=shot_id))
            layouts = scene.get("shots")
            groups = scene.get("scenes")
            if not isinstance(layouts, dict) or not isinstance(groups, dict):
                issues.append(issue("scene_layout_structure", "scene.json shots and scenes must be objects keyed by their IDs."))
            else:
                for shot in shots or []:
                    if not isinstance(shot, dict):
                        continue
                    layout = layouts.get(shot.get("id"))
                    if layout is None:
                        continue
                    if not isinstance(layout, dict):
                        issues.append(issue("invalid_layout", "Per-shot layout must be an object.", shot_id=shot.get("id")))
                    elif layout.get("scene") != shot.get("scene_id"):
                        issues.append(issue("layout_scene_mismatch", "Layout scene must match storyboard scene_id exactly.", shot_id=shot.get("id")))
                    elif layout.get("mode") != "external_blend" and layout.get("scene") not in groups:
                        issues.append(issue("missing_scene_group", "Layout refers to an undefined named scene.", shot_id=shot.get("id")))
    if not any(item["severity"] == "error" for item in issues) and isinstance(scene, dict) and isinstance(shots, list):
        # Import lazily to keep CLI registration independent of Blender setup.
        from .blender import validate_scene
        try:
            validate_scene(scene, shots, fps)
        except WorkflowError as exc:
            issues.append(issue("invalid_scene_layout", str(exc)))
        else:
            from .blender import _current_build
            for shot in shots:
                if scene["shots"][shot["id"]].get("mode") == "external_blend":
                    try:
                        _current_build(project, root, shots, scene, shot)
                    except WorkflowError as exc:
                        issues.append(issue("external_snapshot_unverified", "External layout is declared, but its adopted snapshot is not current/verified. Run adopt-blend before rendering: " + str(exc), severity="warning", shot_id=shot["id"]))
    errors = [item for item in issues if item["severity"] == "error"]
    return {"valid": not errors, "project": str(root / "project.json"), "shot_count": len(shots or []),
            "fps": fps, "resolution": resolution, "total_frames": total_frames,
            "rendered_seconds": total_frames / fps if fps_valid else None,
            "errors": len(errors), "warnings": len(issues) - len(errors), "issues": issues}


def validate_command(args):
    project, root = load_project(args.project)
    report = validate_project(project, root, source_audit=args.source_audit)
    emit(report)
    return 0 if report["valid"] else 2


def register(subparsers):
    parser = subparsers.add_parser("init", help="Create an empty project or copy a generic example")
    parser.add_argument("directory")
    parser.add_argument("--example", help="Example name under examples/, or a template directory")
    parser.add_argument("--id")
    parser.add_argument("--title")
    parser.add_argument("--fps", type=int)
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.set_defaults(func=init_command)
    parser = subparsers.add_parser("validate", help="Validate paths, source rows, timing and per-shot layouts")
    parser.add_argument("--project", required=True)
    parser.add_argument("--source-audit", action="store_true", help="Also verify the recorded original input SHA-256")
    parser.set_defaults(func=validate_command)
