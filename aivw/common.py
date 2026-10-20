"""Small, dependency-free helpers shared by workflow commands."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile


class WorkflowError(Exception):
    """An actionable workflow failure, suitable for CLI output."""


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise WorkflowError(f"Cannot read JSON {path}: {exc}") from exc


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            json.dump(data, out, ensure_ascii=False, indent=2, allow_nan=False)
            out.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_project(path):
    source = Path(path).expanduser().resolve()
    if source.is_dir():
        source = source / "project.json"
    project = read_json(source)
    if not isinstance(project, dict) or project.get("schema_version") != "1.0":
        raise WorkflowError("project.json must be an object with schema_version '1.0'.")
    return project, source.parent


def project_path(project, root, key):
    root = Path(root).resolve()
    path = (root / project.get("paths", {}).get(key, key)).resolve()
    if not path.is_relative_to(root):
        raise WorkflowError(f"Project path '{key}' escapes project directory: {path}")
    return path


def load_shots(project, root):
    data = read_json(project_path(project, root, "shots"))
    shots = data.get("shots") if isinstance(data, dict) else None
    if not isinstance(shots, list):
        raise WorkflowError("shots.json must contain a shots list.")
    return shots


def find_executable(name, explicit=None):
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        return shutil.which(str(explicit))
    return shutil.which(name)


def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False))
