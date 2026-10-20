"""Render only requested missing frames from a fingerprinted Blender scene."""

import argparse
import json
from pathlib import Path
import sys

import bpy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--input-hash", required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    scene = bpy.context.scene
    if scene.get("aivw_input_hash") != args.input_hash:
        raise ValueError("Active Blender scene does not match the requested build fingerprint.")
    settings = json.loads(scene["aivw_settings"])
    if (scene.render.fps != settings["fps"] or scene.render.fps_base != 1 or
            [scene.render.resolution_x, scene.render.resolution_y] != settings["resolution"] or
            scene.render.resolution_percentage != 100):
        raise ValueError("Scene timing/resolution changed after build.")
    output = Path(args.output).resolve()
    if not output.is_relative_to(Path(scene["aivw_project_root"]).resolve()):
        raise ValueError("Render output escapes project directory.")
    numbers = [int(n) for n in args.frames.split(",")]
    if not numbers or len(numbers) != len(set(numbers)) or any(not 1 <= n <= scene.frame_end for n in numbers):
        raise ValueError("Requested frames fall outside this shot or contain duplicates.")
    output.mkdir(parents=True, exist_ok=True)
    for number in numbers:
        scene.frame_set(number)
        scene.render.filepath = str(output / f"frame_{number:06d}.png")
        bpy.ops.render.render(write_still=True, scene=scene.name)
        print(f"AIVW_FRAME_COMPLETE {number}", flush=True)


if __name__ == "__main__":
    main()
