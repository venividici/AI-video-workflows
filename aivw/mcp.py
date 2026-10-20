"""Read-only connection checks and an isolated Blender smoke test.

The addon's loopback socket is NOT MCP. --stdio also verifies a real MCP
initialize/tools-list/tools-call exchange through the upstream server.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import threading
import time
import uuid

from .common import WorkflowError, emit, find_executable, write_json

MCP_VERSION = "2.2.1"
REPO_ROOT = Path(__file__).resolve().parents[1]


def check_connection_target(host, port):
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise WorkflowError("Use a loopback address. For another machine, forward the Blender port through SSH first.")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise WorkflowError("Port must be between 1 and 65535.")


def socket_request(command, params=None, *, host="127.0.0.1", port=9876, timeout=10):
    check_connection_target(host, port)
    if timeout <= 0:
        raise WorkflowError("Timeout must be positive.")
    deadline = time.monotonic() + timeout
    data = bytearray()
    try:
        with socket.create_connection((host, port), timeout=timeout) as connection:
            connection.sendall(json.dumps({"type": command, "params": params or {}}, ensure_ascii=False).encode("utf-8"))
            while time.monotonic() < deadline:
                connection.settimeout(max(0.05, deadline - time.monotonic()))
                part = connection.recv(65536)
                if not part:
                    break
                data.extend(part)
                if len(data) > 16 * 1024 * 1024:
                    raise WorkflowError("Blender response exceeded the diagnostic size limit.")
                try:
                    result = json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(result, dict) or result.get("status") != "success":
                    raise WorkflowError(f"Blender command failed: {str(result)[:1000]}")
                value = result.get("result")
                if isinstance(value, dict) and value.get("error"):
                    raise WorkflowError(f"Blender returned an error: {value['error']}")
                return value
    except (OSError, TimeoutError) as exc:
        raise WorkflowError(f"Blender addon at {host}:{port} is not responding: {exc}") from exc
    raise WorkflowError("Blender closed the connection or timed out before a complete JSON response.")


def server_command(uvx=None, version=MCP_VERSION, host="127.0.0.1", port=9876):
    check_connection_target(host, port)
    binary = find_executable("uvx", uvx)
    if not binary:
        raise WorkflowError("uvx was not found. See docs/SETUP.md; set --uvx to its absolute path.")
    if not version or any(c not in "0123456789.abrc+-" for c in version):
        raise WorkflowError("Use an explicit numeric MCP package version.")
    return [binary, "--from", f"mcp-for-blender=={version}", "mcp-for-blender", "--host", host, "--port", str(port)]


class StdioClient:
    """Minimal synchronous JSON-RPC client for diagnostics, with bounded waits."""

    def __init__(self, command, timeout=45, env=None):
        if timeout <= 0:
            raise WorkflowError("Timeout must be positive.")
        self.timeout = timeout
        child_env = os.environ.copy()
        child_env.update({"BLENDER_MCP_DISABLE_TELEMETRY": "1", "PYTHONUTF8": "1"})
        child_env.update(env or {})
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                            errors="replace", env=child_env, bufsize=1)
        except OSError as exc:
            raise WorkflowError(f"Could not start MCP server: {exc}") from exc
        self.messages = queue.Queue()
        self.last_id = 0
        self.stderr_tail = []
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                self.messages.put({"_protocol_error": "MCP server emitted non-JSON stdout."})
        self.messages.put({"_eof": True})

    def _read_stderr(self):
        for line in self.process.stderr:
            self.stderr_tail.append(line.rstrip()[:300])
            self.stderr_tail = self.stderr_tail[-8:]

    def send(self, message):
        try:
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except (OSError, BrokenPipeError) as exc:
            raise WorkflowError("MCP server stopped before accepting a request.") from exc

    def request(self, method, params=None):
        self.last_id += 1
        request_id = self.last_id
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                message = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if message.get("_protocol_error"):
                raise WorkflowError(message["_protocol_error"])
            if message.get("_eof"):
                raise WorkflowError("MCP server exited. Check uvx/package availability and Blender addon status.")
            if "method" in message and "id" in message:
                self.send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "Diagnostic client does not implement this capability"}})
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise WorkflowError(f"MCP {method} failed: {str(message['error'])[:1000]}")
            return message.get("result", {})
        raise WorkflowError(f"MCP {method} exceeded {self.timeout}s. Do not resend a modifying call without checking Blender.")

    def initialize(self):
        result = self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                              "clientInfo": {"name": "aivw-diagnostic", "version": "0.1.0"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def call(self, name, arguments=None):
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        content = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if result.get("isError") or content.lstrip().lower().startswith("error"):
            raise WorkflowError(f"MCP tool {name} failed: {content[:1500]}")
        return content

    def close(self):
        try:
            if self.process.stdin:
                self.process.stdin.close()
            self.process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        finally:
            for stream in (self.process.stdout, self.process.stderr):
                if stream:
                    stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def probe(args):
    if args.stdio:
        command = server_command(args.uvx, args.mcp_version, args.host, args.port)
        with StdioClient(command, timeout=args.timeout) as client:
            handshake = client.initialize()
            tools = client.request("tools/list").get("tools", [])
            names = [tool["name"] for tool in tools]
            if "get_scene_info" not in names or "execute_blender_code" not in names:
                raise WorkflowError("The server lacks the required Blender observation/execution tools.")
            scene = client.call("get_scene_info", {})
        result = {"ok": True, "transport": "mcp-stdio", "server": handshake.get("serverInfo"),
                  "protocol": handshake.get("protocolVersion"), "tools": names, "scene": scene,
                  "codex_session_tools_verified": False,
                  "next": "In the actual Codex session, call get_scene_info once; a separate CLI probe cannot prove the session loaded its tools."}
    else:
        scene = socket_request("get_scene_info", host=args.host, port=args.port, timeout=args.timeout)
        if not isinstance(scene, dict) or "object_count" not in scene:
            raise WorkflowError("Port answered, but the payload is not the expected Blender scene information.")
        result = {"ok": True, "transport": "addon-socket", "scene": scene,
                  "mcp_stdio_verified": False, "next": "Run mcp-probe --stdio to verify the MCP server as well."}
    if args.report:
        write_json(args.report, result)
    emit(result)


def smoke_code(output, token):
    """Render a small temporary scene without editing or saving the user's scene."""
    return f'''import bpy, json, math
from mathutils import Vector
token = {token!r}
output = {str(output)!r}
original_window = bpy.context.window
original_scene = original_window.scene if original_window else bpy.context.scene
objects_before = set(bpy.data.objects)
meshes_before = set(bpy.data.meshes)
cameras_before = set(bpy.data.cameras)
worlds_before = set(bpy.data.worlds)
actions_before = set(bpy.data.actions)
scene = bpy.data.scenes.new(token)
try:
    if original_window:
        original_window.scene = scene
    scene.render.engine = 'CYCLES'
    scene.cycles.device = 'CPU'
    scene.cycles.samples = 4
    scene.render.resolution_x = 160
    scene.render.resolution_y = 120
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.render.filepath = output
    scene.frame_start, scene.frame_end = 1, 8
    world = bpy.data.worlds.new(token + '_world')
    world.use_nodes = True
    world.node_tree.nodes['Background'].inputs['Color'].default_value = (0.6, 0.6, 0.6, 1)
    world.node_tree.nodes['Background'].inputs['Strength'].default_value = 1.0
    scene.world = world
    mesh = bpy.data.meshes.new(token + '_mesh')
    mesh.from_pydata([(-1,-1,-1),(1,-1,-1),(1,1,-1),(-1,1,-1),(-1,-1,1),(1,-1,1),(1,1,1),(-1,1,1)], [], [(0,3,2,1),(4,5,6,7),(0,1,5,4),(1,2,6,5),(2,3,7,6),(3,0,4,7)])
    obj = bpy.data.objects.new(token + '_cube', mesh)
    scene.collection.objects.link(obj)
    obj.location.x = -0.5
    obj.keyframe_insert(data_path='location', frame=1)
    obj.location.x = 0.5
    obj.keyframe_insert(data_path='location', frame=8)
    camera = bpy.data.objects.new(token + '_camera', bpy.data.cameras.new(token + '_camera_data'))
    scene.collection.objects.link(camera)
    camera.location = (5,-7,4)
    camera.rotation_euler = (Vector((0,0,0)) - camera.location).to_track_quat('-Z','Y').to_euler()
    scene.camera = camera
    scene.frame_set(1)
    x1 = float(obj.location.x)
    scene.frame_set(8)
    x8 = float(obj.location.x)
    if abs(x1 + 0.5) > 0.001 or abs(x8 - 0.5) > 0.001:
        raise RuntimeError(f'Animation evaluation failed: frame 1={{x1}}, frame 8={{x8}}')
    bpy.ops.render.render(write_still=True, scene=scene.name)
    print('AIVW_SMOKE=' + json.dumps({{'token': token, 'blender_version': bpy.app.version_string, 'keyframes': [x1,x8], 'output': output, 'rendered': True}}))
finally:
    if original_window:
        original_window.scene = original_scene
    bpy.data.scenes.remove(scene)
    for datablocks, previous in [(bpy.data.objects,objects_before),(bpy.data.meshes,meshes_before),(bpy.data.cameras,cameras_before),(bpy.data.worlds,worlds_before),(bpy.data.actions,actions_before)]:
        for item in list(datablocks):
            if item not in previous and item.name.startswith(token):
                datablocks.remove(item)
    print('AIVW_CLEANUP=' + json.dumps({{'restored_scene': original_scene.name, 'temporary_scene_removed': token not in bpy.data.scenes}}))
'''


def smoke(args):
    output = Path(args.output).expanduser().resolve()
    if output.exists():
        raise WorkflowError(f"Smoke output already exists; choose a new path: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    token = "AIVW_SMOKE_" + uuid.uuid4().hex[:12]
    code = smoke_code(output, token)
    if args.stdio:
        with StdioClient(server_command(args.uvx, args.mcp_version, args.host, args.port), timeout=args.timeout) as client:
            client.initialize()
            content = client.call("execute_blender_code", {"code": code})
    else:
        value = socket_request("execute_code", {"code": code}, host=args.host, port=args.port, timeout=args.timeout)
        content = value.get("result", "") if isinstance(value, dict) else str(value)
    # Some server versions wrap stdout in a JSON result string.
    if content.startswith("{"):
        try:
            value = json.loads(content)
            if isinstance(value, dict):
                content = value.get("result", content)
        except ValueError:
            pass
    if token not in content or "AIVW_CLEANUP=" not in content:
        raise WorkflowError("Smoke execution did not confirm completion and cleanup. Inspect Blender before repeating.")
    if not output.is_file() or output.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
        raise WorkflowError("Blender reported rendering, but the local PNG was not found. Check shared paths/host selection.")
    result = {"ok": True, "transport": "mcp-stdio" if args.stdio else "addon-socket", "output": str(output),
              "token": token, "result": content, "codex_session_tools_verified": False}
    if args.report:
        write_json(args.report, result)
    emit(result)


def register(subparsers):
    for name, function, description in (("mcp-probe", probe, "Verify addon socket or real MCP stdio connection"),
                                         ("mcp-smoke", smoke, "Create, animate, render and clean up an isolated temporary scene")):
        parser = subparsers.add_parser(name, help=description)
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=9876)
        parser.add_argument("--stdio", action="store_true", help="Verify the full MCP server path, not just the addon socket")
        parser.add_argument("--uvx")
        parser.add_argument("--mcp-version", default=MCP_VERSION)
        parser.add_argument("--timeout", type=float, default=45 if name == "mcp-probe" else 90)
        parser.add_argument("--report")
        if name == "mcp-smoke":
            parser.add_argument("--output", required=True)
        parser.set_defaults(func=function)
