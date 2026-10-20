"""Machine-readable CLI; production work is directed by Codex."""

import argparse
import importlib
import sys

from . import __version__
from .common import WorkflowError, emit, write_json


def main(argv=None):
    # Codex consumes JSON and help through pipes. Windows may otherwise use a
    # legacy code page that cannot encode Chinese project names or help text.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(prog="python -m aivw", description="Codex + Blender MCP + AI video workflow")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "bootstrap", "mcp", "project", "storyboard", "blender", "seedance", "finishing", "review"):
        importlib.import_module(f"aivw.{name}").register(commands)
    args = parser.parse_args(argv)
    try:
        return args.func(args) or 0
    except WorkflowError as exc:
        result = {"ok": False, "error": str(exc)}
        if getattr(args, "report", None):
            write_json(args.report, {"command": args.command, **result})
        emit(result)
        return 2
    except KeyboardInterrupt:
        print("Interrupted. Inspect task receipts before resuming external submissions.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
