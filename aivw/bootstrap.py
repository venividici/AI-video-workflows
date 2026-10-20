"""Project-scoped, idempotent MCP setup and Blender GUI launcher."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import tomllib

from .common import WorkflowError, emit, write_json
from .doctor import find_blender
from .mcp import MCP_VERSION, REPO_ROOT, check_connection_target, server_command, socket_request


def configuration(command, server_name):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", server_name):
        raise WorkflowError("Server name may contain only letters, digits, '-' and '_'.")
    values = {"command": command[0], "args": command[1:], "startup_timeout_sec": 90,
              "tool_timeout_sec": 120, "env": {"BLENDER_MCP_DISABLE_TELEMETRY": "1"}}
    quote = lambda item: json.dumps(item, ensure_ascii=False)
    block = (f"\n# AI-video-workflows: project-local Blender connection\n[mcp_servers.{server_name}]\n"
             f"command = {quote(values['command'])}\nargs = {quote(values['args'])}\n"
             "startup_timeout_sec = 90\ntool_timeout_sec = 120\n"
             f"\n[mcp_servers.{server_name}.env]\nBLENDER_MCP_DISABLE_TELEMETRY = \"1\"\n")
    return values, block


def merge_configuration(path, server_name, values, block, apply=False):
    path = Path(path)
    old = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    try:
        parsed = tomllib.loads(old)
    except ValueError as exc:
        raise WorkflowError(f"Existing config has invalid TOML; it was not changed: {exc}") from exc
    existing = parsed.get("mcp_servers", {}).get(server_name)
    if existing is not None:
        if existing == values:
            return {"action": "already_configured", "path": str(path)}
        return {"action": "existing_preserved", "path": str(path),
                "next": "Probe the existing server first. If it is unrelated, choose --server-name blender_video. Existing settings were not overwritten."}
    new = old.rstrip() + "\n" + block
    tomllib.loads(new)
    result = {"action": "write" if apply else "would_write", "path": str(path), "block": block}
    if apply:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = path.with_name(path.name + ".backup-" + str(time.time_ns()))
            shutil.copy2(path, backup)
            result["backup"] = str(backup)
        temporary = path.with_name(path.name + ".aivw-tmp")
        temporary.write_text(new, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    return result


def setup(args):
    repo = Path(args.repo).expanduser().resolve()
    command = server_command(args.uvx, args.mcp_version, args.host, args.port)
    values, block = configuration(command, args.server_name)
    result = {"ok": True, "config": merge_configuration(repo / ".codex/config.toml", args.server_name, values, block, args.apply),
              "restart_required": args.apply, "model_unchanged": True}
    addons = repo / ".runtime/blender-addons"
    installer = command[:4] + ["install-addon", "--addons-dir", str(addons)]
    result["addon_install_command"] = installer
    if args.install_addon:
        if not args.apply:
            result["addon"] = "planned; add --apply to install into the repository's .runtime directory"
        else:
            env = os.environ.copy()
            env["BLENDER_MCP_DISABLE_TELEMETRY"] = "1"
            try:
                process = subprocess.run(installer, capture_output=True, text=True, encoding="utf-8",
                                         errors="replace", env=env, timeout=240)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise WorkflowError(f"Addon installer could not complete. Check uvx/network access and rerun setup-mcp: {exc}") from exc
            if process.returncode:
                raise WorkflowError(f"Addon installer failed ({process.returncode}): {(process.stderr or process.stdout)[-1500:]}")
            addon = addons / "blender_mcp.py"
            if not addon.is_file():
                raise WorkflowError("Installer exited successfully but did not create blender_mcp.py.")
            receipt = {"package": "mcp-for-blender", "version": args.mcp_version,
                       "addon_sha256": hashlib.sha256(addon.read_bytes()).hexdigest(), "addon": str(addon),
                       "installed_at": time.time()}
            write_json(repo / ".runtime/addon-install.json", receipt)
            result["addon"] = receipt
    result["next"] = ["Launch Blender with python -m aivw launch-blender.",
                      "Restart/reopen the local Codex session after config changes; repository trust may be required by Codex.",
                      "Run mcp-probe --stdio, then call get_scene_info from the actual Codex session."]
    emit(result)


def launch(args):
    check_connection_target(args.host, args.port)
    repo = Path(args.repo).expanduser().resolve()
    try:
        existing = socket_request("get_scene_info", host=args.host, port=args.port, timeout=2)
        if isinstance(existing, dict) and "object_count" in existing:
            emit({"ok": True, "action": "reused_existing", "scene": existing, "port": args.port,
                  "next": "Read scene filepath/settings through MCP before modifying it. No new Blender was launched."})
            return
    except WorkflowError:
        pass
    blender = find_blender(args.blender)
    if not blender:
        raise WorkflowError("Blender was not found. Set BLENDER_BIN or --blender to its executable; see docs/SETUP.md.")
    addon = repo / ".runtime/blender-addons/blender_mcp.py"
    if not addon.is_file():
        raise WorkflowError("Repository-local addon is missing. Run setup-mcp --apply --install-addon first.")
    if os.name != "nt" and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY") and os.sys.platform != "darwin":
        raise WorkflowError("Blender MCP requires a GUI event loop. Start a desktop/Xvfb display, or use build/render for headless batch work.")
    startup = REPO_ROOT / "scripts/start_blender_mcp.py"
    # A new workflow instance must not auto-load another copy of the addon from
    # the user's startup preferences. Existing responding instances are reused above.
    command = [blender, "--factory-startup"]
    if args.blend:
        blend = Path(args.blend).expanduser().resolve()
        if not blend.is_file() or blend.suffix != ".blend":
            raise WorkflowError("--blend must point to an existing .blend file.")
        command.append(str(blend))
    command += ["--python", str(startup), "--", "--addon", str(addon), "--host", args.host, "--port", str(args.port)]
    logs = repo / ".runtime/logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"blender-mcp-{args.port}-{time.time_ns()}.log"
    child_env = os.environ.copy()
    child_env["BLENDER_MCP_DISABLE_TELEMETRY"] = "1"
    with log_path.open("w", encoding="utf-8") as log:
        options = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   env=child_env, **options)
    record = {"pid": process.pid, "command": command, "log": str(log_path), "port": args.port}
    write_json(repo / f".runtime/blender-{args.port}.json", record)
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise WorkflowError(f"Blender exited with {process.returncode}. Inspect {log_path}")
        try:
            scene = socket_request("get_scene_info", host=args.host, port=args.port, timeout=1)
            if isinstance(scene, dict) and "object_count" in scene:
                emit({"ok": True, "action": "launched", **record, "scene": scene})
                return
        except WorkflowError:
            time.sleep(0.25)
    emit({"ok": False, "action": "launched_but_not_yet_ready", **record,
          "next": "Read the log and run mcp-probe; do not launch duplicate instances."})
    return 2


def register(subparsers):
    parser = subparsers.add_parser("setup-mcp", help="Plan/apply project-local Codex MCP configuration and pinned addon")
    parser.add_argument("--repo", default=str(REPO_ROOT))
    parser.add_argument("--uvx")
    parser.add_argument("--mcp-version", default=MCP_VERSION)
    parser.add_argument("--server-name", default="blender")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9876)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--install-addon", action="store_true")
    parser.set_defaults(func=setup)
    parser = subparsers.add_parser("launch-blender", help="Start a Blender GUI with the repository-local addon, or reuse an existing connection")
    parser.add_argument("--repo", default=str(REPO_ROOT))
    parser.add_argument("--blender")
    parser.add_argument("--blend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9876)
    parser.add_argument("--wait", type=float, default=15)
    parser.set_defaults(func=launch)
