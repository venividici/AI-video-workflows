"""Load a repo-local upstream addon in this GUI process only; preserve user preferences."""

import argparse
import importlib.util
from pathlib import Path
import sys

import bpy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--addon", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9876)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    if bpy.app.background:
        raise RuntimeError("Blender MCP needs the GUI event loop; use a desktop or Xvfb. Use background Blender only for batch rendering.")
    if args.host not in ("127.0.0.1", "localhost"):
        raise RuntimeError("The upstream addon uses IPv4 sockets; use 127.0.0.1 or localhost.")
    if not 1 <= args.port <= 65535:
        raise RuntimeError("Invalid Blender MCP port.")
    if getattr(bpy.types, "blendermcp_server", None):
        raise RuntimeError("An existing Blender MCP server is active in this process; reuse it instead of replacing it.")
    spec = importlib.util.spec_from_file_location("aivw_runtime_blender_mcp", Path(args.addon).resolve())
    addon = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = addon
    spec.loader.exec_module(addon)
    addon.register()
    bpy.context.scene.blendermcp_port = args.port
    bpy.context.scene.blendermcp_auto_start_server = True
    server = addon.BlenderMCPServer(host=args.host, port=args.port)
    bpy.types.blendermcp_server = server
    server.start()
    bpy.context.scene.blendermcp_server_running = server.running
    if not server.running:
        raise RuntimeError("Addon did not start. Check port conflicts in the Blender log.")
    print(f"AIVW: Blender MCP ready on {args.host}:{args.port}; user preferences unchanged", flush=True)


if __name__ == "__main__":
    main()
