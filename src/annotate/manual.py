"""Manual annotation: hand-boundaries for parse-FAILED files.

A file that failed `ai_parse_document` never gets a `split_results` row, so it is
never sampled, never shown in the normal annotate worklist, and never delivered.
This module lets a human draw boundaries for such files (rendered from the raw PDF
still in `inbox/`, since PyMuPDF renders fine even when the LLM parse failed) and
makes them **deliverable** by writing:

  * a ground-truth JSON in ground_truth/{day}/  (GT overrides the model in
    nb_pdf_split, and un-blocks the needs_review gate), and
  * a `split_results` row (boundary_source='manual', needs_review=false) so
    nb_pdf_split treats the file as a split candidate.

It deliberately does NOT write an `evaluation_results` row — manual files are
never scored, so metrics stay untouched. The file universe here is the
`processing_log` rows with status='manual' (set from the Errors tab), not a
volume listing.
"""
from __future__ import annotations

from ..core.config import config
from ..core.db import get_sql
from ..core.volumes import get_volumes
from ..pipeline.actions import _log_event
from . import annotation

_LOG = config.fq(config.TABLE_PROCESSING_LOG)
_SPLIT = config.fq(config.TABLE_SPLIT_RESULTS)


# ─────────────────────────────────────────────────────────────────────────────
#  Worklist — files marked 'manual' for the day
# ─────────────────────────────────────────────────────────────────────────────
def build_worklist(day_id: str) -> dict:
    """Files with status='manual' for the batch, split into pending / done (GT
    JSON already written)."""
    sql = get_sql()
    rows = sql.execute(
        f"""SELECT filename FROM {_LOG}
            WHERE day_id = :day AND status = 'manual'""",
        parameters=[sql.str_param("day", day_id)],
    )
    files = sorted({r["filename"] for r in rows})
    done = get_volumes().list_json_stems(config.ground_truth_path(day_id))

    pending = [f for f in files if f not in done]
    completed = [f for f in files if f in done]
    return {
        "day_id": day_id,
        "pending": pending,
        "completed": completed,
        "n_total": len(files),
        "n_pending": len(pending),
        "n_completed": len(completed),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Rendering — from inbox/ (the raw PDF), not validation/
# ─────────────────────────────────────────────────────────────────────────────
def page_count(day_id: str, filename: str) -> int:
    return annotation.page_count(day_id, filename, config.inbox_path(day_id))


def render_page_jpeg(day_id: str, filename: str, n: int) -> bytes:
    return annotation.render_page_jpeg(day_id, filename, n, config.inbox_path(day_id))


# ─────────────────────────────────────────────────────────────────────────────
#  Save — GT JSON + split_results row, NO evaluation (metrics untouched)
# ─────────────────────────────────────────────────────────────────────────────
def save_manual(
    day_id: str,
    filename: str,
    starts: list[int],
    is_multidoc: bool,
    total_pages: int,
    folder_id: str | None,
    annotator: str,
) -> dict:
    payload = annotation.build_gt_payload(
        filename=filename,
        folder_id=folder_id,
        total_pages=total_pages,
        gt_starts=[int(x) for x in starts],
        is_multidoc=is_multidoc,
        annotator=annotator,
        day_id=day_id,
    )
    payload["source"] = "manual"          # marker: not a sampled/scored GT
    gt_path = annotation.save_ground_truth(payload)
    _upsert_manual_split(payload)

    sql = get_sql()
    _log_event(sql, day_id, "manual_annotated", filename,
               f"manual boundaries {payload['predicted_starts']} → deliverable")
    return {"saved": True, "ground_truth_path": gt_path, "ground_truth": payload}


def _upsert_manual_split(gt: dict) -> None:
    """Delete-before-append a single split_results row for a manual file, so a
    re-save stays idempotent (one candidate row, never two).

    Same bound-string / array-literal discipline as annotation._insert_evaluation_row:
    strings are bound; predicted_starts is an `array(...)` literal because
    StatementExecution has no ARRAY parameter type — safe because every element is
    int()-coerced in _int_array_literal. model_used/run_id/boundary_source are
    constant literals (no user data). A manual file has no legitimate non-manual
    split_results row (it failed parsing), so the delete is safe.
    """
    sql = get_sql()
    day = gt.get("day_id")
    fname = gt["filename"]

    sql.execute(
        f"""DELETE FROM {_SPLIT}
            WHERE day_id = :day AND filename = :f""",
        parameters=[sql.str_param("day", str(day)), sql.str_param("f", str(fname))],
    )

    params = []

    def p(name: str, value) -> str:
        if value is None:
            return "NULL"
        params.append(sql.str_param(name, str(value)))
        return f":{name}"

    fname_b = p("fname", fname)
    folder_b = p("folder", gt.get("folder_id"))
    day_b = p("day", day)
    starts_lit = annotation._int_array_literal(gt["predicted_starts"])
    n_docs = int(gt["n_documents"])
    total_pages = int(gt["total_pages"])

    stmt = f"""
        INSERT INTO {_SPLIT} (
            filename, folder_id, total_pages, predicted_starts, n_documents, model_used,
            fallback_used, verification_applied, processing_timestamp, run_id, day_id,
            needs_review, boundary_source
        ) VALUES (
            {fname_b}, {folder_b}, {total_pages}, {starts_lit}, {n_docs}, 'manual',
            FALSE, FALSE, current_timestamp(), 'manual', {day_b},
            FALSE, 'manual'
        )
    """
    sql.execute(stmt, parameters=params)
