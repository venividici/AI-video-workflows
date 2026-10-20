"""Version-bound review records and an offline shot review board."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
from urllib.parse import quote

from .common import WorkflowError, emit, load_project, load_shots, project_path, read_json, write_json


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _paths(project, root, shot):
    sid = shot.get("id")
    if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", sid):
        raise WorkflowError("Unsafe shot id; run validate first.")
    renders = project_path(project, root, "renders") / sid
    reviews = project_path(project, root, "outputs") / "reviews" / sid
    return renders / "preview.mp4", reviews


def shot_status(project, root, shots, scene, shot):
    from .blender import _current_build, _current_render
    video, reviews = _paths(project, root, shot)
    item = {"id": shot["id"], "duration_seconds": shot.get("duration_seconds"),
            "frames": shot.get("frames"), "build": "missing", "render": "missing", "review": "pending"}
    try:
        _, build = _current_build(project, root, shots, scene, shot)
        item["build"] = "current"
        item["origin"] = build.get("origin", "declarative")
    except WorkflowError as exc:
        item["build_error"] = str(exc)
    try:
        _, receipt = _current_render(project, root, shots, scene, shot)
        item["render"] = "current"
        item["video_sha256"] = receipt["video_sha256"]
    except WorkflowError as exc:
        item["render_error"] = str(exc)
        if video.exists():
            item["render"] = "stale_or_invalid"
    latest = reviews / "latest.json"
    if latest.exists():
        review = read_json(latest)
        if item["render"] == "current" and review.get("video_sha256") == item.get("video_sha256"):
            item["review"] = review.get("verdict", "pending")
        else:
            item["review"] = "stale"
        item["reviewer"] = review.get("reviewer")
        item["note"] = review.get("note", "")
    return item


def get_status(path):
    project, root = load_project(path)
    shots = load_shots(project, root)
    scene = read_json(project_path(project, root, "scene"))
    items = [shot_status(project, root, shots, scene, shot) for shot in shots]
    return {"project": project.get("id"), "shots": items, "counts": {
        "total": len(items), "built": sum(s["build"] == "current" for s in items),
        "rendered": sum(s["render"] == "current" for s in items),
        "review_pass": sum(s["review"] == "pass" for s in items)},
        "next": "Inspect rendered beginning/middle/end frames and the motion; technical completion does not assert artistic quality."}


def status_command(args):
    result = get_status(args.project)
    if args.report:
        write_json(args.report, result)
    emit(result)


def record_review(args):
    project, root = load_project(args.project)
    shots = load_shots(project, root)
    shot = next((s for s in shots if s.get("id") == args.shot), None)
    if shot is None:
        raise WorkflowError(f"Unknown shot: {args.shot}")
    scene = read_json(project_path(project, root, "scene"))
    status = shot_status(project, root, shots, scene, shot)
    if status["render"] != "current":
        raise WorkflowError("Only a current, verified render can receive a review. Rebuild/render first.")
    if not args.note.strip():
        raise WorkflowError("Record what was observed, including remaining issues; --note must not be empty.")
    video, reviews = _paths(project, root, shot)
    timestamp = datetime.now(timezone.utc)
    evidence = []
    for file in args.evidence or []:
        path = Path(file).expanduser().resolve()
        if not path.is_file():
            raise WorkflowError(f"Review evidence does not exist: {file}")
        evidence.append({"file": str(path), "sha256": file_hash(path)})
    record = {"shot_id": shot["id"], "verdict": args.verdict, "reviewer": args.reviewer,
              "note": args.note, "video_sha256": status["video_sha256"],
              "video": str(video.relative_to(root)), "evidence": evidence,
              "reviewed_at": timestamp.isoformat(timespec="microseconds")}
    history = reviews / (timestamp.strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    write_json(history, record)
    write_json(reviews / "latest.json", record)
    emit({"ok": True, "review": record, "history": str(history)})


def review_board(args):
    project, root = load_project(args.project)
    shots = load_shots(project, root)
    report = get_status(args.project)
    output = project_path(project, root, "outputs") / "review.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    cards = []
    escape = html.escape
    labels = {"current": "当前版本", "missing": "尚未渲染", "stale_or_invalid": "需要重新渲染",
              "pending": "待检查", "pass": "检查通过", "fix": "需要修改", "blocked": "待解决", "stale": "检查已过期"}
    for shot, status in zip(shots, report["shots"]):
        video, _ = _paths(project, root, shot)
        source = shot.get("source", {})
        source_label = source.get("shot_number") or shot["id"]
        if video.is_file():
            url = quote(os.path.relpath(video, output.parent).replace(os.sep, "/"), safe="/.")
            player = f'<video controls preload="metadata" src="{url}"></video>'
        else:
            player = '<div class="placeholder">镜头预演尚未生成</div>'
        cards.append(f'''<article class="shot" data-search="{escape((shot['id']+' '+str(source_label)+' '+shot.get('description','')).lower(), quote=True)}">
<div class="shot-title"><h2>镜头 {escape(str(source_label))}</h2><span>{escape(str(shot.get('duration_seconds')))} 秒 · {escape(str(shot.get('frames')))} 帧</span></div>
{player}<div class="badges"><span>{labels.get(status['render'],status['render'])}</span><span>{labels.get(status['review'],status['review'])}</span></div>
<p>{escape(shot.get('description',''))}</p><details><summary>检查记录与来源</summary><p>{escape(status.get('note','尚无检查记录。'))}</p><small>{escape(shot['id'])} · 原始行 {escape(str(source.get('row','—')))} · {escape(str(source.get('file','手工创建')))}</small></details></article>''')
    title = escape(project.get("title", project.get("id", "视频项目")))
    markup = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{title} · 镜头审阅</title>
<style>:root{{color-scheme:dark}}*{{box-sizing:border-box}}body{{margin:0;background:#111820;color:#edf1f5;font:16px/1.7 system-ui,"Microsoft YaHei",sans-serif}}header,main{{max-width:1280px;margin:auto;padding:32px}}header{{padding-bottom:12px}}.eyebrow{{color:#88d1c5;font-size:13px;letter-spacing:.15em}}h1{{font-size:32px;margin:8px 0}}header p,small{{color:#a9b7c5}}input{{width:100%;padding:14px;border:1px solid #3b4b59;border-radius:10px;background:#192430;color:inherit;font:inherit}}main{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:24px}}.shot{{background:#1b2530;border:1px solid #34414e;border-radius:14px;overflow:hidden}}.shot-title,.badges,.shot p,details{{margin:0;padding:16px 20px}}.shot-title{{display:flex;justify-content:space-between;align-items:center;gap:8px}}h2{{font-size:19px;margin:0}}.shot-title span{{font-size:13px;color:#b2c0ce}}video{{display:block;width:100%;aspect-ratio:16/9;background:#080b10}}.placeholder{{display:grid;place-items:center;aspect-ratio:16/9;background:#121a23;color:#8292a2}}.badges{{display:flex;gap:8px;padding-bottom:0}}.badges span{{background:#273a46;border-radius:6px;padding:2px 9px;font-size:12px;color:#b9e0d9}}details{{border-top:1px solid #34414e}}summary{{cursor:pointer;color:#b5d0de}}details p{{padding:12px 0!important}}[hidden]{{display:none!important}}</style>
<header><div class="eyebrow">PREVIS REVIEW</div><h1>{title}</h1><p>共 {len(shots)} 镜 · 已渲染 {report['counts']['rendered']} 镜 · 检查通过 {report['counts']['review_pass']} 镜。依次查看构图、运动、接触和剪辑衔接。</p><input id="search" placeholder="搜索镜头编号或画面内容" aria-label="搜索镜头"></header><main>{''.join(cards)}</main>
<script>document.getElementById('search').addEventListener('input',e=>{{const q=e.target.value.toLowerCase();document.querySelectorAll('.shot').forEach(s=>s.hidden=!s.dataset.search.includes(q));}});</script></html>'''
    output.write_text(markup, encoding="utf-8")
    write_json(output.with_suffix(".json"), report)
    emit({"ok": True, "review_board": str(output), "counts": report["counts"]})


def register(subparsers):
    parser = subparsers.add_parser("status", help="Report current builds, renders and version-bound reviews")
    parser.add_argument("--project", required=True)
    parser.add_argument("--report")
    parser.set_defaults(func=status_command)
    parser = subparsers.add_parser("review", help="Record an observed review against the current video hash")
    parser.add_argument("--project", required=True)
    parser.add_argument("--shot", required=True)
    parser.add_argument("--verdict", choices=["pass", "fix", "blocked"], required=True)
    parser.add_argument("--reviewer", choices=["codex", "human"], default="codex")
    parser.add_argument("--note", required=True)
    parser.add_argument("--evidence", action="append")
    parser.set_defaults(func=record_review)
    parser = subparsers.add_parser("review-board", help="Create a local HTML board with per-shot videos and review state")
    parser.add_argument("--project", required=True)
    parser.set_defaults(func=review_board)
