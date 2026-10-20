"""Read-only storyboard import with explicit mapping and auditable row identities."""

from __future__ import annotations

import argparse
import csv
from decimal import Decimal, DecimalException, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
import posixpath

from .common import WorkflowError, emit, load_project, project_path, read_json, write_json


NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
ALIASES = {
    "shot_number": ("shot", "shot number", "shot_number", "shot no", "shot id", "id", "镜号", "镜头号", "镜头编号", "编号"),
    "duration_seconds": ("duration", "duration_seconds", "duration (seconds)", "seconds", "时长", "时长（秒）", "时长(s)", "秒数"),
    "description": ("description", "action", "content", "visual", "画面内容", "内容", "画面", "动作描述"),
    "dialogue": ("dialogue", "sound", "audio", "声音 / 对白", "声音", "对白", "台词"),
    "scene_id": ("scene", "scene_id", "location", "场次", "场景", "地点"),
    "camera": ("camera", "camera movement", "camera_motion", "运镜方式", "运镜", "摄影机运动"),
    "framing": ("framing", "shot size", "shot_size", "景别"),
    "notes": ("notes", "remarks", "特效 / 备注", "备注", "特效"),
}


def issue(code, message, *, severity="error", row=None, field=None, **extra):
    result = {"severity": severity, "code": code, "message": message}
    if row is not None:
        result["row"] = row
    if field:
        result["field"] = field
    result.update(extra)
    return result


def normalized_name(value):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", str(value)).casefold())


def col_name(index):
    """Return the one-based spreadsheet column label."""
    if index < 1:
        raise ValueError("Column index is one-based")
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _cell_text(node):
    if node is None:
        return ""
    # Only displayed text: phonetic annotations are not additional cell content.
    return "".join((part.text or "") for part in node.findall("m:t", NS) + node.findall("m:r/m:t", NS))


def _xlsx_sheets(archive):
    try:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except (KeyError, ET.ParseError) as exc:
        raise WorkflowError(f"Invalid XLSX workbook: {exc}") from exc
    targets = {}
    for rel in relationships:
        if rel.attrib.get("TargetMode") == "External":
            continue
        target = rel.attrib.get("Target", "")
        resolved = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join("xl", target))
        if not resolved.startswith("xl/"):
            raise WorkflowError("XLSX worksheet relationship leaves the workbook.")
        targets[rel.attrib["Id"]] = resolved
    sheets = []
    for node in workbook.findall("m:sheets/m:sheet", NS):
        target = targets.get(node.attrib.get(f"{{{REL}}}id"))
        if target:
            sheets.append({"name": node.attrib["name"], "path": target})
    if not sheets:
        raise WorkflowError("The XLSX workbook contains no readable worksheets.")
    return sheets


def list_xlsx_sheets(path):
    try:
        with zipfile.ZipFile(path) as archive:
            return _xlsx_sheets(archive)
    except (OSError, zipfile.BadZipFile) as exc:
        raise WorkflowError(f"Cannot open XLSX {path}: {exc}") from exc


def _xlsx_rows(path, sheet):
    try:
        with zipfile.ZipFile(path) as archive:
            sheets = _xlsx_sheets(archive)
            if sheet is None:
                if len(sheets) != 1:
                    raise WorkflowError("Select --sheet explicitly. Available sheets: " + ", ".join(s["name"] for s in sheets))
                selected = sheets[0]
            else:
                selected = next((s for s in sheets if s["name"] == sheet), None)
                if selected is None:
                    raise WorkflowError(f"Worksheet {sheet!r} does not exist; choose from {[s['name'] for s in sheets]}.")
            shared = []
            if "xl/sharedStrings.xml" in archive.namelist():
                shared = [_cell_text(n) for n in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall("m:si", NS)]
            worksheet = ET.fromstring(archive.read(selected["path"]))
            rows = []
            for fallback, row in enumerate(worksheet.findall("m:sheetData/m:row", NS), 1):
                row_number = int(row.attrib.get("r", fallback))
                values, formulas, cell_errors = {}, {}, []
                for index, cell in enumerate(row.findall("m:c", NS), 1):
                    address = cell.attrib.get("r", f"{col_name(index)}{row_number}")
                    match = re.fullmatch(r"([A-Z]+)[0-9]+", address)
                    if match is None:
                        raise WorkflowError(f"Invalid XLSX cell reference {address!r}.")
                    column = match.group(1)
                    kind = cell.attrib.get("t")
                    value = cell.find("m:v", NS)
                    cached = value.text if value is not None else None
                    formula = cell.find("m:f", NS)
                    if formula is not None:
                        formulas[column] = {"formula": formula.text or "", "has_cached_value": cached not in (None, "")}
                    if kind == "inlineStr":
                        text = _cell_text(cell.find("m:is", NS))
                    elif kind == "s" and cached is not None:
                        try:
                            text = shared[int(cached)]
                        except (ValueError, IndexError) as exc:
                            raise WorkflowError(f"Invalid shared string at {address}.") from exc
                    else:
                        text = cached if cached is not None else ""
                    if kind == "e":
                        cell_errors.append({"column": column, "error": text})
                    values[column] = text
                rows.append({"row": row_number, "values": values, "formulas": formulas, "cell_errors": cell_errors})
            return selected["name"], rows
    except (OSError, zipfile.BadZipFile, KeyError, ET.ParseError, ValueError) as exc:
        if isinstance(exc, WorkflowError):
            raise
        raise WorkflowError(f"Cannot parse XLSX {path}: {exc}") from exc


def read_input(path, *, sheet=None, header_row=None, encoding="utf-8-sig"):
    """Return headers and original row data; do not modify the source file."""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise WorkflowError(f"Input file does not exist: {path}")
    suffix = path.suffix.lower()
    if suffix not in (".csv", ".tsv", ".json", ".xlsx"):
        raise WorkflowError("Supported input types: .csv, .tsv, .json, .xlsx")
    if suffix != ".xlsx" and sheet is not None:
        raise WorkflowError("--sheet applies only to XLSX inputs.")
    if suffix == ".json":
        if header_row is not None:
            raise WorkflowError("JSON object records have named fields; omit --header-row.")
        data = read_json(path)
        records = data.get("rows", data.get("shots")) if isinstance(data, dict) else data
        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
            raise WorkflowError("JSON input must be an array of objects, or an object with a rows/shots array.")
        keys = list(dict.fromkeys(key for item in records for key in item))
        headers = {key: key for key in keys}
        rows = [{"row": index, "values": item, "formulas": {}, "cell_errors": []} for index, item in enumerate(records, 1)]
        selected = None
    else:
        if not isinstance(header_row, int) or isinstance(header_row, bool) or header_row < 1:
            raise WorkflowError("Provide --header-row with the one-based header row number; use inspect-input first.")
        if suffix == ".xlsx":
            selected, all_rows = _xlsx_rows(path, sheet)
        else:
            selected = None
            try:
                with path.open(encoding=encoding, newline="") as stream:
                    reader = csv.reader(stream, delimiter="\t" if suffix == ".tsv" else ",", strict=True)
                    all_rows = [{"row": index, "values": {col_name(i): value for i, value in enumerate(row, 1)}, "formulas": {}, "cell_errors": []} for index, row in enumerate(reader, 1)]
            except (OSError, UnicodeError, csv.Error) as exc:
                raise WorkflowError(f"Cannot read delimited input {path}: {exc}") from exc
        header = next((item for item in all_rows if item["row"] == header_row), None)
        if header is None:
            raise WorkflowError(f"Header row {header_row} does not exist.")
        headers = {key: str(value).strip() for key, value in header["values"].items()}
        rows = [item for item in all_rows if item["row"] > header_row]
    return {
        "file": path.name, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "format": suffix[1:], "sheet": selected, "header_row": header_row,
        "headers": headers, "rows": rows,
    }


def resolve_mapping(headers, explicit=None):
    """Resolve column names without choosing between ambiguous alternatives."""
    explicit = explicit or {}
    if not isinstance(explicit, dict):
        raise WorkflowError("Column mapping must be a JSON object.")
    if "columns" in explicit:
        explicit = explicit["columns"]
    if not isinstance(explicit, dict):
        raise WorkflowError("mapping.columns must be a JSON object.")
    unknown = set(explicit) - set(ALIASES)
    if unknown:
        raise WorkflowError("Unknown mapping fields: " + ", ".join(sorted(unknown)))
    mapping, issues = {}, []
    for field, aliases in ALIASES.items():
        if field in explicit:
            target = explicit[field]
            if target is None:
                continue
            if not isinstance(target, str):
                raise WorkflowError(f"Mapping for {field} must be a column letter/key or exact header string.")
            matches = [key for key, name in headers.items() if name == target]
            # Explicit @A forces a column key in the unusual case of an A header.
            if target.startswith("@") and target[1:] in headers:
                matches = [target[1:]]
            elif not matches and target in headers:
                matches = [target]
        else:
            names = {normalized_name(name) for name in aliases}
            matches = [key for key, name in headers.items() if normalized_name(name) in names]
        if len(matches) > 1:
            issues.append(issue("ambiguous_column", f"Multiple columns match {field}; provide an explicit mapping.", field=field, columns=matches))
        elif matches:
            mapping[field] = matches[0]
        elif field in ("duration_seconds", "description"):
            issues.append(issue("missing_column", f"No column mapped to required field {field}.", field=field))
        elif field in explicit and explicit[field] is not None:
            issues.append(issue("unknown_column", f"Configured column {explicit[field]!r} does not exist.", field=field))
    reversed_mapping = {}
    for field, column in mapping.items():
        reversed_mapping.setdefault(column, []).append(field)
    for column, fields in reversed_mapping.items():
        if len(fields) > 1:
            issues.append(issue("column_reused", "One source column is mapped to multiple canonical fields.", column=column, fields=fields))
    return mapping, issues


def duration_to_frames(value, fps):
    """Parse explicit seconds, preserving short durations, and round HALF_UP."""
    if not isinstance(fps, int) or isinstance(fps, bool) or fps <= 0:
        raise WorkflowError("fps must be a positive integer.")
    if value is None or isinstance(value, bool):
        raise ValueError("Duration must be a positive number of seconds.")
    if not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("Duration must be a positive number of seconds.")
    text = str(value).strip()
    if not text:
        raise ValueError("Duration is missing.")
    text = re.sub(r"\s*(?:seconds?|secs?|s|秒)\s*$", "", text, flags=re.IGNORECASE)
    try:
        duration = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid duration: {value!r}; use seconds, e.g. 0.5 or 2s.") from exc
    if not duration.is_finite() or duration <= 0:
        raise ValueError("Duration must be finite and greater than zero.")
    try:
        exact = duration * Decimal(fps)
        frames = int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except DecimalException as exc:
        raise ValueError("Duration exceeds the supported numeric precision.") from exc
    if frames < 1:
        raise ValueError("Duration rounds to zero frames at the project fps; specify a supported duration explicitly.")
    seconds_float = float(duration)
    if seconds_float == float("inf"):
        raise ValueError("Duration exceeds the supported numeric range.")
    quantization = None
    if exact != Decimal(frames):
        quantization = {
            "input_seconds": str(duration), "exact_frames": str(exact), "frames": frames,
            "rendered_seconds": str(Decimal(frames) / Decimal(fps)),
            "delta_seconds": str(Decimal(frames) / Decimal(fps) - duration),
            "rounding": "ROUND_HALF_UP",
        }
    return seconds_float, frames, quantization


def _as_text(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def normalize_rows(table, fps, *, mapping=None, exclude_rows=(), id_prefix="shot"):
    """Produce every nonempty, non-excluded source row, including unresolved rows."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", id_prefix):
        raise WorkflowError("--id-prefix must contain 1–40 ASCII letters, digits, underscores or hyphens.")
    resolved, issues = resolve_mapping(table["headers"], mapping)
    if not isinstance(fps, int) or isinstance(fps, bool) or fps <= 0:
        raise WorkflowError("fps must be a positive integer.")
    shots, skipped, excluded, quantized = [], [], [], []
    excluded_numbers = set(exclude_rows)
    seen_numbers = {}
    sheet_key = "_" + hashlib.sha256(table["sheet"].encode()).hexdigest()[:8] if table["sheet"] else ""
    for row in table["rows"]:
        number, raw = row["row"], row["values"]
        if number in excluded_numbers:
            excluded.append({"row": number, "reason": "explicit --exclude-row", "raw": raw})
            continue
        if not any(_as_text(value) for value in raw.values()) and not row.get("formulas"):
            skipped.append(number)
            continue
        shot_id = f"{id_prefix}{sheet_key}_r{number:06d}"
        values = {field: raw.get(column) for field, column in resolved.items()}
        source = {"file": table["file"], "sheet": table["sheet"], "row": number, "shot_number": _as_text(values.get("shot_number")), "raw": raw}
        if row.get("formulas"):
            source["formulas"] = row["formulas"]
        if row.get("cell_errors"):
            source["cell_errors"] = row["cell_errors"]
        shot = {"id": shot_id, "duration_seconds": None, "frames": None,
                "scene_id": _as_text(values.get("scene_id")) or "unassigned",
                "description": _as_text(values.get("description")), "dialogue": _as_text(values.get("dialogue")), "source": source}
        for field in ("camera", "framing", "notes"):
            if field in values:
                shot[field] = _as_text(values[field])
        if not shot["description"]:
            issues.append(issue("missing_description", "Nonempty source row has no description; it remains in the import.", row=number, field="description", shot_id=shot_id))
        for field, column in resolved.items():
            formula = row.get("formulas", {}).get(column)
            if formula and not formula["has_cached_value"]:
                issues.append(issue("uncached_formula", "Formula has no cached result; formulas are never evaluated by the importer.", row=number, field=field, column=column, shot_id=shot_id))
            elif formula:
                issues.append(issue("cached_formula", "Imported cached formula result; freshness has not been verified.", severity="warning", row=number, field=field, column=column, shot_id=shot_id))
        for cell_error in row.get("cell_errors", []):
            issues.append(issue("spreadsheet_cell_error", "Source cell contains a spreadsheet error.", row=number, shot_id=shot_id, **cell_error))
        try:
            seconds, frames, report = duration_to_frames(values.get("duration_seconds"), fps)
            shot["duration_seconds"], shot["frames"] = seconds, frames
            if report:
                quantized.append({"shot_id": shot_id, "row": number, **report})
                issues.append(issue("frame_quantization", "Duration was rounded to a whole frame using ROUND_HALF_UP.", severity="warning", row=number, field="duration_seconds", shot_id=shot_id, **report))
        except (ValueError, InvalidOperation) as exc:
            issues.append(issue("invalid_duration", str(exc), row=number, field="duration_seconds", shot_id=shot_id))
        original_number = source["shot_number"]
        if original_number:
            if original_number in seen_numbers:
                issues.append(issue("duplicate_shot_number", "Original shot number is duplicated; both source rows are retained with distinct IDs.", severity="warning", row=number, shot_id=shot_id, shot_number=original_number, first_row=seen_numbers[original_number]))
            else:
                seen_numbers[original_number] = number
        else:
            issues.append(issue("missing_shot_number", "Original shot number is empty; the stable source-row ID is used.", severity="warning", row=number, shot_id=shot_id))
        shots.append(shot)
    for number in sorted(excluded_numbers - {r["row"] for r in table["rows"]}):
        issues.append(issue("unknown_excluded_row", f"Excluded row {number} is not a data row in this input.", row=number))
    if not shots:
        issues.append(issue("empty_storyboard", "No nonempty storyboard rows remain."))
    return {
        "schema_version": "1.0", "shots": shots, "issues": issues,
        "import": {"source": {key: table[key] for key in ("file", "path", "sha256", "format", "sheet", "header_row")},
                   "headers": table["headers"], "mapping": resolved, "fps": fps, "id_prefix": id_prefix,
                   "source_data_rows": len(table["rows"]), "imported_rows": len(shots),
                   "blank_rows_skipped": skipped, "excluded_rows": excluded, "quantization": quantized},
    }


def inspect_command(args):
    path = Path(args.input).expanduser().resolve()
    if path.suffix.lower() == ".xlsx" and args.sheet is None:
        sheets = list_xlsx_sheets(path)
        if len(sheets) > 1:
            emit({"input": str(path), "sheets": sheets, "next": "Select --sheet NAME and --header-row N to inspect that sheet."})
            return 0
    header_row = args.header_row
    if header_row is None and path.suffix.lower() != ".json":
        # Inspection may show row 1 as a candidate, but import always requires explicit selection.
        header_row = 1
    table = read_input(path, sheet=args.sheet, header_row=header_row, encoding=args.encoding)
    mapping, issues = resolve_mapping(table["headers"])
    emit({"input": str(path), "sheet": table["sheet"], "header_row": header_row,
          "header_row_is_inspection_default": args.header_row is None and header_row is not None,
          "headers": table["headers"], "suggested_mapping": mapping, "mapping_issues": issues,
          "data_row_count": len(table["rows"]), "sample_rows": table["rows"][:args.limit],
          "note": "Inspection only. Select the header row and mapping explicitly before importing."})
    return 0


def import_command(args):
    project, root = load_project(args.project)
    destination = project_path(project, root, "shots")
    if destination.exists() and not args.force:
        existing = read_json(destination)
        if not isinstance(existing, dict) or existing.get("shots"):
            raise WorkflowError("shots.json already contains data. Review the import first; pass --force to replace it explicitly.")
    table = read_input(args.input, sheet=args.sheet, header_row=args.header_row, encoding=args.encoding)
    explicit = read_json(args.mapping) if args.mapping else None
    data = normalize_rows(table, project.get("fps"), mapping=explicit, exclude_rows=args.exclude_row, id_prefix=args.id_prefix)
    errors = [item for item in data["issues"] if item["severity"] == "error"]
    saved = not errors or args.allow_unresolved
    if saved:
        write_json(destination, data)
    emit({"saved": saved, "path": str(destination) if saved else None, "shots": len(data["shots"]),
          "unresolved_shots": sum(s["frames"] is None or not s["description"] for s in data["shots"]),
          "errors": len(errors), "issues": data["issues"], "audit": data["import"],
          "next": "Create per-shot layout in scene.json, then run validate." if not errors else "Correct the mapping/source values, or explicitly save an unresolved draft with --allow-unresolved. Rendering remains blocked until validation passes."})
    return 2 if errors else 0


def register(subparsers):
    inspect_parser = subparsers.add_parser("inspect-input", help="Inspect CSV/JSON/XLSX without changing source data")
    inspect_parser.add_argument("input")
    inspect_parser.add_argument("--sheet")
    inspect_parser.add_argument("--header-row", type=int)
    inspect_parser.add_argument("--encoding", default="utf-8-sig")
    inspect_parser.add_argument("--limit", type=int, default=6)
    inspect_parser.set_defaults(func=inspect_command)
    parser = subparsers.add_parser("import-shots", help="Import source rows with stable IDs and audit records")
    parser.add_argument("--project", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--sheet")
    parser.add_argument("--header-row", type=int)
    parser.add_argument("--mapping")
    parser.add_argument("--encoding", default="utf-8-sig")
    parser.add_argument("--exclude-row", type=int, action="append", default=[])
    parser.add_argument("--id-prefix", default="shot")
    parser.add_argument("--allow-unresolved", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.set_defaults(func=import_command)
