"""CLI subprocess behavior under legacy Windows-style pipe encoding."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class CliEncodingTests(unittest.TestCase):
    def run_cli(self, *arguments):
        env = os.environ.copy()
        env.update({"PYTHONIOENCODING": "cp1252:strict", "PYTHONUTF8": "0"})
        return subprocess.run([sys.executable, "-m", "aivw", *map(str, arguments)],
                              cwd=Path(__file__).resolve().parents[1], env=env,
                              capture_output=True, timeout=20)

    def test_chinese_help_is_utf8_when_pipe_defaults_to_cp1252(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        self.assertIn("离线", result.stdout.decode("utf-8"))

    def test_unicode_project_name_survives_cli_and_json_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            result = self.run_cli("init", root, "--title", "新片场")
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
            self.assertIn("created", json.loads(result.stdout.decode("utf-8")))
            project = json.loads((root / "project.json").read_text(encoding="utf-8"))
            self.assertEqual(project["title"], "新片场")

    def test_failed_probe_also_writes_requested_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "failed-probe.json"
            result = self.run_cli("mcp-probe", "--port", "0", "--report", path)
            self.assertEqual(result.returncode, 2)
            output = json.loads(result.stdout.decode("utf-8"))
            report = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(report["ok"])
            self.assertEqual(report["command"], "mcp-probe")
            self.assertEqual(report["error"], output["error"])


if __name__ == "__main__":
    unittest.main()
