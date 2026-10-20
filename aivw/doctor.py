"""Portable environment discovery without modifying user configuration."""

from __future__ import annotations

import glob
import os
from pathlib import Path
import platform
import subprocess
import sys
import tomllib

from .common import WorkflowError, emit, find_executable, write_json
from .mcp import REPO_ROOT, socket_request


def find_blender(explicit=None):
    requested = explicit or os.environ.get("BLENDER_BIN")
    if requested:
        return find_executable("blender", requested)
    direct = find_executable("blender")
    if direct:
        return direct
    candidates = []
    if sys.platform == "win32":
        for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            parent = os.environ.get(key)
            if parent:
                candidates += glob.glob(str(Path(parent) / "Blender Foundation" / "Blender*" / "blender.exe"))
    elif sys.platform == "darwin":
        candidates += glob.glob("/Applications/Blender*.app/Contents/MacOS/Blender")
    else:
        candidates += ["/snap/bin/blender", "/usr/bin/blender", "/usr/local/bin/blender"]
    return next((str(Path(p).resolve()) for p in sorted(candidates, reverse=True) if Path(p).is_file()), None)


def version_of(binary, arguments=("--version",)):
    if not binary:
        return None
    try:
        process = subprocess.run([binary, *arguments], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=12)
        lines = (process.stdout or process.stderr).strip().splitlines()
        return {"line": lines[0][:240] if lines else "", "returncode": process.returncode}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": str(exc)}


def inspect_config(path):
    path = Path(path)
    if not path.exists():
        return {"path": str(path), "exists": False, "servers": []}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return {"path": str(path), "exists": True, "error": str(exc)}
    servers = []
    for name, values in data.get("mcp_servers", {}).items():
        # Never dump env values, HTTP headers, credentials, or whole user config.
        servers.append({"name": name, "enabled": values.get("enabled", True),
                        "transport": "stdio" if "command" in values else "http",
                        "command": values.get("command"),
                        "is_blender": "blender" in name.lower() or any("blender" in str(x).lower() for x in values.get("args", []))})
    return {"path": str(path), "exists": True, "servers": servers}


def doctor(args):
    binaries = {"blender": find_blender(args.blender)}
    for name in ("ffmpeg", "ffprobe", "uvx", "git", "codex"):
        explicit = args.uvx if name == "uvx" else None
        if name in ("ffmpeg", "ffprobe"):
            explicit = os.environ.get(name.upper() + "_BIN")
        binaries[name] = find_executable(name, explicit)
    versions = {name: version_of(path, ("-version",) if name in ("ffmpeg", "ffprobe") else ("--version",))
                for name, path in binaries.items() if path}
    user_root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    repo = Path(args.repo).expanduser().resolve()
    config = [inspect_config(repo / ".codex" / "config.toml"), inspect_config(user_root / "config.toml")]
    missing = [name for name in ("blender", "ffmpeg", "ffprobe", "uvx") if not binaries[name]]
    result = {"ok": not missing, "python": sys.version.split()[0], "platform": platform.platform(),
              "executables": binaries, "versions": versions, "missing": missing,
              "codex_config": config, "ark_api_key_present": bool(os.environ.get("ARK_API_KEY")),
              "codex_session_tools_verified": False,
              "notes": ["A missing Codex CLI is compatible with a desktop/IDE Codex session.",
                        "API credentials are optional for offline previs and manual video handoff.",
                        "A cloud workspace cannot reach your PC's localhost. Run this repository in local Codex for local Blender."]}
    if args.probe:
        try:
            scene = socket_request("get_scene_info", host=args.host, port=args.port, timeout=3)
            if not isinstance(scene, dict) or "object_count" not in scene:
                raise WorkflowError("Unexpected service on Blender port.")
            result["addon"] = {"ok": True, "scene": scene}
        except WorkflowError as exc:
            result["addon"] = {"ok": False, "error": str(exc)}
            result["ok"] = False
    if args.report:
        write_json(args.report, result)
    emit(result)
    return 0 if result["ok"] else 2


def register(subparsers):
    parser = subparsers.add_parser("doctor", help="Find Blender, FFmpeg, uvx and Codex config without changing anything")
    parser.add_argument("--repo", default=str(REPO_ROOT))
    parser.add_argument("--blender")
    parser.add_argument("--uvx")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9876)
    parser.add_argument("--report")
    parser.set_defaults(func=doctor)
