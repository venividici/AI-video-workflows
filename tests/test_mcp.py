"""Protocol/config integrity tests. These do not pretend to be live Blender tests."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from aivw.bootstrap import configuration, merge_configuration
from aivw.common import WorkflowError, project_path, read_json, write_json
from aivw.doctor import inspect_config
from aivw.mcp import StdioClient, check_connection_target, socket_request, smoke_code


class FakeSocket:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = b""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def settimeout(self, value):
        pass

    def sendall(self, value):
        self.sent += value

    def recv(self, _):
        return self.chunks.pop(0) if self.chunks else b""


class SocketTests(unittest.TestCase):
    def test_fragmented_utf8_response_and_request_shape(self):
        response = json.dumps({"status": "success", "result": {"name": "场景", "object_count": 2}}, ensure_ascii=False).encode()
        fake = FakeSocket([response[i:i+1] for i in range(len(response))])
        with patch("aivw.mcp.socket.create_connection", return_value=fake):
            result = socket_request("get_scene_info")
        self.assertEqual(result["name"], "场景")
        self.assertEqual(json.loads(fake.sent), {"type": "get_scene_info", "params": {}})

    def test_addon_error_is_not_success(self):
        fake = FakeSocket([b'{"status":"error","message":"wrong command"}'])
        with patch("aivw.mcp.socket.create_connection", return_value=fake), self.assertRaises(WorkflowError):
            socket_request("get_scene_info")

    def test_truncated_response_is_not_success(self):
        with patch("aivw.mcp.socket.create_connection", return_value=FakeSocket([b'{"status":'])), self.assertRaises(WorkflowError):
            socket_request("get_scene_info")

    def test_remote_and_invalid_port_rejected(self):
        for host, port in (("0.0.0.0", 9876), ("example.com", 9876), ("127.0.0.1", -1)):
            with self.subTest(host=host, port=port), self.assertRaises(WorkflowError):
                check_connection_target(host, port)


FAKE_SERVER = '''import json,sys
for line in sys.stdin:
    m=json.loads(line)
    if "id" not in m: continue
    method=m.get("method")
    if method=="initialize": result={"protocolVersion":"2024-11-05","serverInfo":{"name":"fixture","version":"1"},"capabilities":{"tools":{}}}
    elif method=="tools/list": result={"tools":[{"name":"get_scene_info"},{"name":"execute_blender_code"}]}
    elif method=="tools/call": result={"content":[{"type":"text","text":"scene fixture"}],"isError":False}
    else:
        print(json.dumps({"jsonrpc":"2.0","id":m["id"],"error":{"code":-32601,"message":"unknown"}}),flush=True)
        continue
    print(json.dumps({"jsonrpc":"2.0","method":"notifications/message","params":{}}),flush=True)
    print(json.dumps({"jsonrpc":"2.0","id":m["id"],"result":result}),flush=True)
'''


class StdioTests(unittest.TestCase):
    def test_initialize_list_call_notifications_and_close(self):
        with StdioClient([sys.executable, "-u", "-c", FAKE_SERVER], timeout=3) as client:
            self.assertEqual(client.initialize()["serverInfo"]["name"], "fixture")
            self.assertEqual(len(client.request("tools/list")["tools"]), 2)
            self.assertEqual(client.call("get_scene_info"), "scene fixture")
            process = client.process
        self.assertIsNotNone(process.poll())

    def test_json_rpc_error(self):
        with StdioClient([sys.executable, "-u", "-c", FAKE_SERVER], timeout=3) as client:
            with self.assertRaises(WorkflowError):
                client.request("missing")

    def test_non_protocol_stdout_is_not_a_connection(self):
        with StdioClient([sys.executable, "-u", "-c", "print('not MCP',flush=True);input()"], timeout=3) as client:
            with self.assertRaises(WorkflowError):
                client.initialize()


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / ".codex/config.toml"
        self.values, self.block = configuration([r"C:\Tools\uvx.exe", "--from", "mcp-for-blender==2.2.1", "mcp-for-blender"], "blender")

    def tearDown(self):
        self.tmp.cleanup()

    def test_plan_has_no_side_effect_and_toml_escapes_windows_path(self):
        merge_configuration(self.path, "blender", self.values, self.block)
        self.assertFalse(self.path.exists())
        self.assertEqual(tomllib.loads(self.block)["mcp_servers"]["blender"]["command"], r"C:\Tools\uvx.exe")

    def test_preserves_other_settings_backs_up_and_is_idempotent(self):
        self.path.parent.mkdir()
        original = 'model = "my-chosen-model"\n[mcp_servers.other]\ncommand="other"\n'
        self.path.write_text(original)
        result = merge_configuration(self.path, "blender", self.values, self.block, apply=True)
        self.assertEqual(Path(result["backup"]).read_text(), original)
        parsed = tomllib.loads(self.path.read_text())
        self.assertEqual(parsed["model"], "my-chosen-model")
        self.assertEqual(parsed["mcp_servers"]["other"]["command"], "other")
        before = self.path.read_bytes()
        self.assertEqual(merge_configuration(self.path, "blender", self.values, self.block, True)["action"], "already_configured")
        self.assertEqual(before, self.path.read_bytes())

    def test_conflicting_entry_is_preserved(self):
        self.path.parent.mkdir()
        self.path.write_text('[mcp_servers.blender]\ncommand="existing"\n')
        before = self.path.read_bytes()
        result = merge_configuration(self.path, "blender", self.values, self.block, True)
        self.assertEqual(result["action"], "existing_preserved")
        self.assertEqual(before, self.path.read_bytes())

    def test_diagnostics_do_not_dump_env_credentials(self):
        self.path.parent.mkdir()
        self.path.write_text('[mcp_servers.blender]\ncommand="uvx"\n[mcp_servers.blender.env]\nPRIVATE_KEY="do-not-display"\n')
        self.assertNotIn("do-not-display", json.dumps(inspect_config(self.path)))

    def test_project_path_escape_and_atomic_json_unicode(self):
        with self.assertRaises(WorkflowError):
            project_path({"paths": {"outputs": "../outside"}}, self.root, "outputs")
        path = self.root / "unicode.json"
        write_json(path, {"标题": "新项目"})
        self.assertEqual(read_json(path), {"标题": "新项目"})

    def test_smoke_code_has_valid_syntax_with_quoted_path(self):
        code = smoke_code(self.root / "quote'folder" / "检查.png", "AIVW_SMOKE_fixture")
        compile(code, "<blender-smoke>", "exec")


if __name__ == "__main__":
    unittest.main()
