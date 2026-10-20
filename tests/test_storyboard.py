"""Data-integrity tests for user-authored spreadsheets and reusable projects."""

import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from aivw.common import WorkflowError
from aivw.project import init_command, validate_project
from aivw.storyboard import duration_to_frames, import_command, normalize_rows, read_input, resolve_mapping


class StoryboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def csv(self, text, name="story.csv"):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def basic_project(self, data, *, fps=24):
        project = {
            "schema_version": "1.0", "id": "test", "title": "Generic test", "fps": fps,
            "resolution": [1280, 720],
            "paths": {"shots": "shots.json", "scene": "scene.json", "build": "build", "renders": "renders", "outputs": "outputs", "references": "references"},
            "provider": {"name": "seedance", "mode": "manual", "model": None},
        }
        (self.root / "project.json").write_text(json.dumps(project), encoding="utf-8")
        (self.root / "shots.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        scene = {
            "schema_version": "1.0", "materials": {},
            "objects": [{"id": "cart", "type": "box", "location": [0, 0, 0]}],
            "scenes": {"stage": {"objects": ["cart"]}},
            "cameras": {"wide": {"location": [3, -4, 2], "target": "cart", "lens_mm": 40}},
            "shots": {s["id"]: {"scene": s["scene_id"], "camera": "wide"} for s in data["shots"]},
        }
        (self.root / "scene.json").write_text(json.dumps(scene), encoding="utf-8")
        return project, scene

    def make_xlsx(self):
        path = self.root / "story.xlsx"
        parts = {
            "xl/workbook.xml": '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="镜头" sheetId="1" r:id="rId1"/><sheet name="Other" sheetId="2" r:id="rId2"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Target="/xl/worksheets/sheet2.xml"/></Relationships>',
            "xl/sharedStrings.xml": '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>镜号</t></si><si><t>时长（秒）</t></si><si><t>画面内容</t></si><si><r><t>红色</t></r><r><t>小车移动。</t></r></si></sst>',
            "xl/worksheets/sheet1.xml": '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Generic storyboard</t></is></c></row><row r="2"><c r="A2" t="s"><v>0</v></c><c r="B2" t="s"><v>1</v></c><c r="C2" t="s"><v>2</v></c></row><row r="3"><c r="A3"><v>7</v></c><c r="B3"><v>0.5</v></c><c r="C3" t="s"><v>3</v></c></row><row r="4"><c r="A4"><v>7</v></c><c r="B4"><f>1+1</f><v>2</v></c><c r="C4" t="inlineStr"><is><r><t>Indicator </t></r><r><t>changes.</t></r></is></c></row><row r="5"><c r="A5"><v>8</v></c><c r="B5"><f>2+1</f><v/></c><c r="C5" t="inlineStr"><is><t>Cart stops.</t></is></c></row></sheetData></worksheet>',
            "xl/worksheets/sheet2.xml": '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Not the target sheet</t></is></c></row></sheetData></worksheet>',
        }
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in parts.items():
                archive.writestr(name, content)
        return path

    def test_duplicate_numbers_preserved_with_stable_ids_and_source_hash(self):
        path = self.csv('镜号,时长（秒）,画面内容,场次\n46,0.5,红色小车启动。,stage\n46,1.5,指示灯闪烁。,stage\n')
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        table = read_input(path, header_row=1)
        result = normalize_rows(table, 24)
        self.assertEqual(len(result["shots"]), 2)
        self.assertNotEqual(result["shots"][0]["id"], result["shots"][1]["id"])
        self.assertEqual([s["source"]["shot_number"] for s in result["shots"]], ["46", "46"])
        self.assertEqual([s["frames"] for s in result["shots"]], [12, 36])
        self.assertEqual(result["shots"][1]["source"]["raw"]["C"], "指示灯闪烁。")
        self.assertIn("duplicate_shot_number", [x["code"] for x in result["issues"]])
        self.assertEqual(result, normalize_rows(read_input(path, header_row=1), 24))
        self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_english_columns_and_exact_half_up_quantization(self):
        path = self.csv("Shot,Seconds,Action,Scene,Dialogue\nA,0.0625,Indicator changes.,stage,\n")
        result = normalize_rows(read_input(path, header_row=1), 24)
        self.assertEqual(result["shots"][0]["duration_seconds"], 0.0625)
        self.assertEqual(result["shots"][0]["frames"], 2)
        report = result["import"]["quantization"][0]
        self.assertEqual(report["exact_frames"], "1.5000")
        self.assertEqual(report["rounding"], "ROUND_HALF_UP")
        self.assertEqual(duration_to_frames("1.5秒", 24)[:2], (1.5, 36))

    def test_missing_and_invalid_duration_rows_are_retained(self):
        path = self.csv("Shot,Seconds,Action\n1,,First action\n2,banana,Second action\n3,-1,Third action\n4,NaN,Fourth action\n5,0.001,Fifth action\n")
        result = normalize_rows(read_input(path, header_row=1), 24)
        self.assertEqual(len(result["shots"]), 5)
        self.assertTrue(all(s["frames"] is None for s in result["shots"]))
        self.assertEqual(len([x for x in result["issues"] if x["code"] == "invalid_duration"]), 5)

    def test_xlsx_inline_shared_strings_duplicate_and_uncached_formula(self):
        path = self.make_xlsx()
        before = path.read_bytes()
        with self.assertRaisesRegex(WorkflowError, "Select --sheet"):
            read_input(path, header_row=2)
        table = read_input(path, sheet="镜头", header_row=2)
        result = normalize_rows(table, 24)
        self.assertEqual([s["source"]["row"] for s in result["shots"]], [3, 4, 5])
        self.assertEqual(result["shots"][0]["description"], "红色小车移动。")
        self.assertEqual(result["shots"][1]["description"], "Indicator changes.")
        self.assertEqual(result["shots"][1]["frames"], 48)
        self.assertIsNone(result["shots"][2]["frames"])
        codes = {i["code"] for i in result["issues"]}
        self.assertTrue({"uncached_formula", "cached_formula", "duplicate_shot_number"}.issubset(codes))
        self.assertEqual(result["shots"][2]["source"]["formulas"]["B"]["formula"], "2+1")
        self.assertEqual(path.read_bytes(), before)

    def test_explicit_column_map_resolves_ambiguity(self):
        path = self.csv("Shot,Duration,Seconds,Action\n1,1,2,Cart moves\n")
        table = read_input(path, header_row=1)
        mapping, problems = resolve_mapping(table["headers"])
        self.assertNotIn("duration_seconds", mapping)
        self.assertIn("ambiguous_column", [i["code"] for i in problems])
        result = normalize_rows(table, 24, mapping={"duration_seconds": "@C"})
        self.assertEqual(result["shots"][0]["frames"], 48)
        self.assertFalse(any(i["severity"] == "error" for i in result["issues"]))

    def test_json_named_fields_preserved_and_no_header_required(self):
        path = self.root / "source.json"
        rows = [{"id": "A", "duration_seconds": 0.5, "description": "Move cart", "scene_id": "stage", "extra": {"author": "generic"}}]
        path.write_text(json.dumps(rows), encoding="utf-8")
        result = normalize_rows(read_input(path), 24)
        self.assertEqual(result["shots"][0]["source"]["raw"], rows[0])
        self.assertEqual(result["shots"][0]["source"]["shot_number"], "A")
        self.assertEqual(result["shots"][0]["frames"], 12)

    def test_blank_rows_and_explicit_footer_exclusion_reconcile(self):
        path = self.csv("Shot,Seconds,Action\n1,2,Move cart\n,,\nTotal,2,\n")
        table = read_input(path, header_row=1)
        without_exclusion = normalize_rows(table, 24)
        self.assertEqual(len(without_exclusion["shots"]), 2)
        self.assertIn("missing_description", [i["code"] for i in without_exclusion["issues"]])
        result = normalize_rows(table, 24, exclude_rows=[4])
        self.assertEqual(len(result["shots"]), 1)
        self.assertEqual(result["import"]["blank_rows_skipped"], [3])
        self.assertEqual(result["import"]["excluded_rows"][0]["raw"]["A"], "Total")
        self.assertEqual(result["import"]["source_data_rows"], 3)

    def test_header_row_required_for_importable_csv_table(self):
        path = self.csv("Shot,Seconds,Action\n1,2,Move cart\n")
        with self.assertRaisesRegex(WorkflowError, "header-row"):
            read_input(path)

    def test_validation_detects_timing_layout_path_and_changed_source(self):
        path = self.csv("Shot,Seconds,Action,Scene\n1,2,Move cart,stage\n")
        data = normalize_rows(read_input(path, header_row=1), 24)
        project, scene = self.basic_project(data)
        self.assertTrue(validate_project(project, self.root, source_audit=True)["valid"])
        path.write_text("changed source", encoding="utf-8")
        report = validate_project(project, self.root, source_audit=True)
        self.assertIn("source_changed", [x["code"] for x in report["issues"]])
        data["shots"][0]["frames"] = 47
        (self.root / "shots.json").write_text(json.dumps(data), encoding="utf-8")
        scene["shots"] = {}
        (self.root / "scene.json").write_text(json.dumps(scene), encoding="utf-8")
        project["paths"]["renders"] = "../outside"
        report = validate_project(project, self.root)
        self.assertTrue({"frame_mismatch", "missing_layout", "unsafe_path"}.issubset({i["code"] for i in report["issues"]}))

    def test_new_topic_import_command_does_not_reuse_example_layout(self):
        target = self.root / "different-topic"
        args = argparse.Namespace(directory=str(target), example=None, id=None, title="机器人演示", fps=30, width=960, height=540)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(init_command(args), 0)
        source = self.csv("Shot,Seconds,Action,Scene\n1,0.5,A delivery cart waits.,courtyard\n2,2,The cart moves away.,courtyard\n")
        import_args = argparse.Namespace(project=str(target), input=str(source), sheet=None, header_row=1, mapping=None, encoding="utf-8-sig", exclude_row=[], id_prefix="robot", allow_unresolved=False, force=False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(import_command(import_args), 0)
        imported = json.loads((target / "shots.json").read_text(encoding="utf-8"))
        self.assertEqual([s["frames"] for s in imported["shots"]], [15, 60])
        project = json.loads((target / "project.json").read_text(encoding="utf-8"))
        report = validate_project(project, target)
        self.assertEqual(len([i for i in report["issues"] if i["code"] == "missing_layout"]), 2)
        self.assertIn("/renders/", (target / ".gitignore").read_text(encoding="utf-8"))
        with self.assertRaisesRegex(WorkflowError, "already contains"):
            import_command(import_args)
        with self.assertRaisesRegex(WorkflowError, "never overwritten"):
            init_command(args)

    def test_invalid_import_never_silently_overwrites_empty_template(self):
        target = self.root / "draft"
        with contextlib.redirect_stdout(io.StringIO()):
            init_command(argparse.Namespace(directory=str(target), example=None, id=None, title=None, fps=None, width=None, height=None))
        source = self.csv("Shot,Seconds,Action\n1,,Unresolved action\n")
        args = argparse.Namespace(project=str(target), input=str(source), sheet=None, header_row=1, mapping=None, encoding="utf-8-sig", exclude_row=[], id_prefix="shot", allow_unresolved=False, force=False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(import_command(args), 2)
        self.assertEqual(json.loads((target / "shots.json").read_text())["shots"], [])
        args.allow_unresolved = True
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(import_command(args), 2)
        saved = json.loads((target / "shots.json").read_text())
        self.assertEqual(len(saved["shots"]), 1)
        self.assertIsNone(saved["shots"][0]["frames"])

    def test_even_resolution_is_checked_before_expensive_render(self):
        path = self.csv("Shot,Seconds,Action,Scene\n1,2,Move cart,stage\n")
        data = normalize_rows(read_input(path, header_row=1), 24)
        project, _ = self.basic_project(data)
        project["resolution"] = [1279, 720]
        result = validate_project(project, self.root)
        self.assertFalse(result["valid"])
        self.assertIn("invalid_resolution", [i["code"] for i in result["issues"]])

    def test_external_snapshot_layout_does_not_require_primitive_camera_or_group(self):
        path = self.csv("Shot,Seconds,Action,Scene\n1,2,Move cart,Complex stage\n")
        data = normalize_rows(read_input(path, header_row=1), 24)
        project, _ = self.basic_project(data)
        shot_id = data["shots"][0]["id"]
        external = {"schema_version": "1.0", "materials": {}, "objects": [], "scenes": {}, "cameras": {},
                    "shots": {shot_id: {"mode": "external_blend", "scene": "Complex stage"}}}
        (self.root / "scene.json").write_text(json.dumps(external), encoding="utf-8")
        result = validate_project(project, self.root)
        self.assertTrue(result["valid"])
        self.assertIn("external_snapshot_unverified", [i["code"] for i in result["issues"]])
        external["shots"][shot_id]["scene"] = "Wrong scene"
        (self.root / "scene.json").write_text(json.dumps(external), encoding="utf-8")
        self.assertIn("layout_scene_mismatch", [i["code"] for i in validate_project(project, self.root)["issues"]])


if __name__ == "__main__":
    unittest.main()
