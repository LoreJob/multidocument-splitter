"""Ground-truth annotation API.

Batches: PDFs live in dated volumes (validation/{day_id}/, ground_truth/{day_id}/).
Every route takes a ?day_id= selecting the batch — there is no day list here:
the shell's topbar selector owns batch choice and feeds it in (see /api/days).

Routes (mounted at /api/annotate)
  GET  /worklist?day_id=       pending / completed PDFs from validation/{day_id}/
  GET  /file/<name>?day_id=    page count + existing GT (operator-blind: NO model)
  GET  /page/<name>/<n>?day_id= server-rendered JPEG of one PDF page
  GET  /stats                  aggregate metrics for the dashboard
  POST /save                   persist GT JSON + run eval -> evaluation_results

  Manual (parse-failed files, rendered from inbox/, NEVER scored):
  GET  /manual/worklist?day_id=   files with status='manual'
  GET  /manual/file/<name>?day_id= page count (inbox PDF) + existing GT
  GET  /manual/page/<name>/<n>?day_id= server-rendered JPEG from the inbox PDF
  POST /manual/save               GT JSON + split_results row (deliverable, no eval)
"""
from __future__ import annotations

from flask import Blueprint, Response, jsonify, request

from ..core.auth import actor
from ..core.http import day_id_arg, internal_error, valid_name
from . import annotation, manual

bp = Blueprint("annotate", __name__, url_prefix="/api/annotate")


@bp.get("/worklist")
def worklist():
    day = day_id_arg()
    if not day:
        return jsonify({"error": "day_id query parameter required"}), 400
    try:
        return jsonify(annotation.build_worklist(day))
    except Exception:  # noqa: BLE001
        return internal_error("worklist")


@bp.get("/file/<name>")
def file_detail(name: str):
    day = day_id_arg()
    if not valid_name(name):
        return jsonify({"error": "invalid filename"}), 400
    if not day:
        return jsonify({"error": "day_id query parameter required"}), 400
    try:
        # Operator-blind: never return the model prediction. total_pages comes from
        # the PDF itself; existing GT (if any) is returned for re-annotation.
        gt = annotation.load_ground_truth(day, name)
        total_pages = annotation.page_count(day, name)
        annotation.prewarm(day, name, total_pages)  # background: pages ready before scroll
        return jsonify({
            "filename": name,
            "day_id": day,
            "total_pages": total_pages,
            "folder_id": name.split("_")[0],
            "ground_truth": gt,
        })
    except Exception:  # noqa: BLE001
        return internal_error("file_detail")


@bp.get("/page/<name>/<int:n>")
def page_image(name: str, n: int):
    day = day_id_arg()
    if not valid_name(name):
        return jsonify({"error": "invalid filename"}), 400
    if not day:
        return jsonify({"error": "day_id query parameter required"}), 400
    try:
        if n < 1 or n > annotation.page_count(day, name):
            return jsonify({"error": "page out of range"}), 404
        jpeg = annotation.render_page_jpeg(day, name, n)
        resp = Response(jpeg, mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp
    except Exception:  # noqa: BLE001
        return internal_error("page_image")


@bp.get("/stats")
def stats():
    try:
        return jsonify(annotation.get_eval_stats())
    except Exception:  # noqa: BLE001
        return internal_error("stats")


@bp.post("/save")
def save():
    body = request.get_json(silent=True) or {}
    name = body.get("filename", "")
    day = (body.get("day_id") or "").strip()
    if not valid_name(name):
        return jsonify({"error": "invalid filename"}), 400
    if not valid_name(day):
        return jsonify({"error": "day_id required in body"}), 400

    starts = body.get("predicted_starts")
    is_multidoc = bool(body.get("is_multidoc", False))
    if not isinstance(starts, list) or not starts:
        return jsonify({"error": "predicted_starts must be a non-empty list"}), 400

    try:
        model = annotation.get_model_prediction(name, day)

        # total_pages / folder_id: trust the request (from the loaded PDF), fall
        # back to the model row when available.
        total_pages = int(body.get("total_pages") or (model or {}).get("total_pages") or 0)
        if total_pages <= 0:
            return jsonify({"error": "total_pages missing or invalid"}), 400
        folder_id = body.get("folder_id") or (model or {}).get("folder_id")

        payload = annotation.build_gt_payload(
            filename=name,
            folder_id=folder_id,
            total_pages=total_pages,
            gt_starts=[int(x) for x in starts],
            is_multidoc=is_multidoc,
            annotator=actor(),
            day_id=day,
        )
        gt_path = annotation.save_ground_truth(payload)
        metrics = annotation.run_and_store_evaluation(payload, model)

        return jsonify({
            "saved": True,
            "ground_truth_path": gt_path,
            "ground_truth": payload,
            "evaluation": metrics,
        })
    except Exception:  # noqa: BLE001
        return internal_error("save")


# ─────────────────────────────────────────────────────────────────────────────
#  Manual annotation — parse-failed files (status='manual'), rendered from inbox/.
#  Makes them deliverable via a split_results row, but NEVER scored.
# ─────────────────────────────────────────────────────────────────────────────
@bp.get("/manual/worklist")
def manual_worklist():
    day = day_id_arg()
    if not day:
        return jsonify({"error": "day_id query parameter required"}), 400
    try:
        return jsonify(manual.build_worklist(day))
    except Exception:  # noqa: BLE001
        return internal_error("manual worklist")


@bp.get("/manual/file/<name>")
def manual_file_detail(name: str):
    day = day_id_arg()
    if not valid_name(name):
        return jsonify({"error": "invalid filename"}), 400
    if not day:
        return jsonify({"error": "day_id query parameter required"}), 400
    try:
        gt = annotation.load_ground_truth(day, name)
        total_pages = manual.page_count(day, name)
        manual.prewarm(day, name, total_pages)  # background: render inbox pages ahead
        return jsonify({
            "filename": name,
            "day_id": day,
            "total_pages": total_pages,
            "folder_id": name.split("_")[0],
            "ground_truth": gt,
        })
    except Exception:  # noqa: BLE001
        return internal_error("manual file_detail")


@bp.get("/manual/page/<name>/<int:n>")
def manual_page_image(name: str, n: int):
    day = day_id_arg()
    if not valid_name(name):
        return jsonify({"error": "invalid filename"}), 400
    if not day:
        return jsonify({"error": "day_id query parameter required"}), 400
    try:
        if n < 1 or n > manual.page_count(day, name):
            return jsonify({"error": "page out of range"}), 404
        jpeg = manual.render_page_jpeg(day, name, n)
        resp = Response(jpeg, mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp
    except Exception:  # noqa: BLE001
        return internal_error("manual page_image")


@bp.post("/manual/save")
def manual_save():
    body = request.get_json(silent=True) or {}
    name = body.get("filename", "")
    day = (body.get("day_id") or "").strip()
    if not valid_name(name):
        return jsonify({"error": "invalid filename"}), 400
    if not valid_name(day):
        return jsonify({"error": "day_id required in body"}), 400

    starts = body.get("predicted_starts")
    is_multidoc = bool(body.get("is_multidoc", False))
    if not isinstance(starts, list) or not starts:
        return jsonify({"error": "predicted_starts must be a non-empty list"}), 400

    try:
        total_pages = int(body.get("total_pages") or 0)
        if total_pages <= 0:
            return jsonify({"error": "total_pages missing or invalid"}), 400
        folder_id = body.get("folder_id") or name.split("_")[0]

        res = manual.save_manual(
            day_id=day,
            filename=name,
            starts=[int(x) for x in starts],
            is_multidoc=is_multidoc,
            total_pages=total_pages,
            folder_id=folder_id,
            annotator=actor(),
        )
        return jsonify(res)
    except Exception:  # noqa: BLE001
        return internal_error("manual save")
