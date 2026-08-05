"""Operational actions on (day_id, filename). Every action:
  1. is fully parameterized (no user data in SQL text),
  2. writes a pipeline_events row with actor = the dashboard user,
so nothing the control tower does is ever silent or unattributed.
"""
from __future__ import annotations

import uuid

from ..core.auth import actor
from ..core.config import config
from ..core.db import get_sql

_LOG = config.fq("processing_log")
_EVENTS = config.fq("pipeline_events")
_SPLIT = config.fq("split_results")
_SIGNALS = config.fq("page_signals")
_SUMMARIES = config.fq("package_summaries")

# A delivered file must never be hijacked into the manual flow: it would land in
# the manual worklist and a manual save would DELETE its real split_results row.
# Defined once — the single-file and bulk paths must not drift apart.
_NOT_DELIVERED = ("(sftp_delivery_status IS NULL "
                  "OR sftp_delivery_status <> 'delivered')")

# Bound parameters per statement stay well inside the StatementExecution limit;
# a bigger selection is split into chunks, each still costing 3 round trips.
_BULK_CHUNK = 200


def _log_event(sql, day_id: str, event_type: str, filename: str | None,
               detail: str | None = None):
    params = [
        sql.str_param("eid", str(uuid.uuid4())),
        sql.str_param("day", day_id),
        sql.str_param("etype", event_type),
        sql.str_param("actor", actor()),
        sql.str_param("detail", detail or ""),
    ]
    fname_sql = ":f"
    if filename is None:
        fname_sql = "NULL"
    else:
        params.append(sql.str_param("f", filename))
    sql.execute(
        f"""INSERT INTO {_EVENTS}
            (event_id, run_id, day_id, stage, event_type, filename, folder_id,
             old_status, new_status, detail, error_message, actor, event_ts)
            VALUES (:eid, NULL, :day, 'dashboard', :etype, {fname_sql}, NULL,
                    NULL, NULL, :detail, NULL, :actor, current_timestamp())""",
        parameters=params,
    )


def retry_parse(day_id: str, filename: str) -> str:
    """error → pending (picked up by the next job_ingest run)."""
    sql = get_sql()
    sql.execute(
        f"""UPDATE {_LOG}
            SET status = 'pending', error_message = NULL, error_stage = NULL,
                retry_count = retry_count + 1
            WHERE day_id = :day AND filename = :f AND status = 'error'""",
        parameters=[sql.str_param("day", day_id), sql.str_param("f", filename)],
    )
    _log_event(sql, day_id, "requeued", filename, "retry parse: error → pending")
    return "requeued for parse"


def retry_split(day_id: str, filename: str) -> str:
    """Hard re-split: drop the file's split artifacts and reset to 'parsed'."""
    sql = get_sql()
    p = [sql.str_param("day", day_id), sql.str_param("f", filename)]
    for table in (_SPLIT, _SIGNALS, _SUMMARIES):
        sql.execute(
            f"DELETE FROM {table} WHERE day_id = :day AND filename = :f",
            parameters=p,
        )
    sql.execute(
        f"""UPDATE {_LOG}
            SET status = 'parsed', error_message = NULL, error_stage = NULL,
                n_documents_found = NULL
            WHERE day_id = :day AND filename = :f AND status IN ('done', 'error')""",
        parameters=p,
    )
    _log_event(sql, day_id, "requeued", filename,
               "hard re-split: artifacts deleted, status → parsed")
    return "requeued for split (artifacts deleted)"


def retry_sftp(day_id: str, filename: str) -> str:
    """failed/deferred → pending (picked up by the next job_deliver run)."""
    sql = get_sql()
    sql.execute(
        f"""UPDATE {_LOG}
            SET sftp_delivery_status = 'pending', sftp_delivery_error = NULL
            WHERE day_id = :day AND filename = :f
              AND sftp_delivery_status IN ('failed', 'deferred')""",
        parameters=[sql.str_param("day", day_id), sql.str_param("f", filename)],
    )
    _log_event(sql, day_id, "requeued", filename, "retry sftp: → pending")
    return "requeued for sftp"


def mark_manual(day_id: str, filename: str) -> str:
    """Terminal state: handled outside the pipeline.

    Guarded: a DELIVERED file must never be hijacked into the manual flow —
    flipping it would put it in the manual worklist, and a manual save then
    DELETEs its real split_results row. One mis-click in the bulk Errors-tab
    flow was enough to corrupt a delivered file's record."""
    sql = get_sql()
    sql.execute(
        f"""UPDATE {_LOG}
            SET status = 'manual'
            WHERE day_id = :day AND filename = :f
              AND {_NOT_DELIVERED}""",
        parameters=[sql.str_param("day", day_id), sql.str_param("f", filename)],
    )
    _log_event(sql, day_id, "marked_manual", filename, "handled manually")
    return "marked manual"


def mark_manual_bulk(day_id: str, filenames: list[str]) -> int:
    """Mark several files manual in 3 statements per chunk, not 2 per file.

    Was a loop over mark_manual: 2 round trips × N files, and at ~600 ms per
    statement the 94-file batch of 20260801 took over a minute and a half of
    wall clock. Now one SELECT (which files are actually eligible) + one
    UPDATE + one multi-row event INSERT, so any selection up to _BULK_CHUNK
    costs three round trips.

    Each file still gets its own 'marked_manual' event — the audit trail keeps
    exactly the shape it had. Better than before, in fact: files skipped by the
    delivered-guard no longer get an event claiming they were marked, and the
    returned count is the number of files actually flipped rather than the
    number requested.
    """
    total = 0
    sql = get_sql()
    for i in range(0, len(filenames), _BULK_CHUNK):
        total += _mark_manual_chunk(sql, day_id, filenames[i:i + _BULK_CHUNK])
    return total


def _mark_manual_chunk(sql, day_id: str, chunk: list[str]) -> int:
    """One chunk: resolve eligible files, flip them, log one event each."""
    if not chunk:
        return 0
    params = [sql.str_param("day", day_id)]
    placeholders = []
    for i, f in enumerate(chunk):
        params.append(sql.str_param(f"f{i}", f))
        placeholders.append(f":f{i}")
    in_list = ", ".join(placeholders)

    # Which of the requested files may actually be marked? Resolving first keeps
    # the event rows truthful and gives an exact count without relying on the
    # driver reporting affected rows.
    rows = sql.execute(
        f"""SELECT filename FROM {_LOG}
            WHERE day_id = :day AND filename IN ({in_list})
              AND {_NOT_DELIVERED}""",
        parameters=params,
    )
    eligible = sorted({r["filename"] for r in rows})
    if not eligible:
        return 0

    sql.execute(
        f"""UPDATE {_LOG}
            SET status = 'manual'
            WHERE day_id = :day AND filename IN ({in_list})
              AND {_NOT_DELIVERED}""",
        parameters=params,
    )
    _log_events_bulk(sql, day_id, "marked_manual", eligible, "handled manually")
    return len(eligible)


def _log_events_bulk(sql, day_id: str, event_type: str, filenames: list[str],
                     detail: str | None = None) -> None:
    """One INSERT with a VALUES tuple per file — same rows N _log_event calls
    would write, one round trip instead of N."""
    if not filenames:
        return
    params = [
        sql.str_param("day", day_id),
        sql.str_param("etype", event_type),
        sql.str_param("actor", actor()),
        sql.str_param("detail", detail or ""),
    ]
    tuples = []
    for i, f in enumerate(filenames):
        params.append(sql.str_param(f"eid{i}", str(uuid.uuid4())))
        params.append(sql.str_param(f"ef{i}", f))
        tuples.append(
            f"(:eid{i}, NULL, :day, 'dashboard', :etype, :ef{i}, NULL, "
            f"NULL, NULL, :detail, NULL, :actor, current_timestamp())"
        )
    sql.execute(
        f"""INSERT INTO {_EVENTS}
            (event_id, run_id, day_id, stage, event_type, filename, folder_id,
             old_status, new_status, detail, error_message, actor, event_ts)
            VALUES {", ".join(tuples)}""",
        parameters=params,
    )


def retry_pdf_split(day_id: str, filename: str) -> str:
    """Re-queue a file for the PHYSICAL split, keeping its boundaries.

    nb_pdf_split treats `sftp_delivery_status IS NOT NULL` as "already split",
    so a file whose split failed sits at 'failed' and is skipped forever. Only
    NULL brings it back into the candidate set — 'pending' does not, and
    retry_sftp (which sets 'pending') makes it worse: the upload then looks for
    output PDFs that were never produced and fails again.

    Deliberately does NOT delete split_results, unlike retry_split: on a
    hand-annotated file that row IS the human's work (boundary_source='manual')
    and deleting it would throw the annotation away.
    """
    sql = get_sql()
    sql.execute(
        f"""UPDATE {_LOG}
            SET sftp_delivery_status = NULL, sftp_delivery_error = NULL,
                error_message = NULL, error_stage = NULL
            WHERE day_id = :day AND filename = :f
              AND sftp_delivery_status = 'failed'""",
        parameters=[sql.str_param("day", day_id), sql.str_param("f", filename)],
    )
    _log_event(sql, day_id, "requeued", filename,
               "retry pdf split: sftp_delivery_status → NULL, boundaries kept")
    return "requeued for pdf split (boundaries kept)"


def approve_review(day_id: str, filename: str) -> str:
    """Approve an unsplit [1]-fallback for delivery as-is."""
    sql = get_sql()
    sql.execute(
        f"""UPDATE {_SPLIT}
            SET needs_review = false
            WHERE day_id = :day AND filename = :f""",
        parameters=[sql.str_param("day", day_id), sql.str_param("f", filename)],
    )
    _log_event(sql, day_id, "review_approved", filename,
               "unsplit delivery approved by reviewer")
    return "review approved"


def reset_deferred(day_id: str, filenames: list[str] | None) -> int:
    """deferred → pending, optionally only a subset. Returns rows targeted."""
    sql = get_sql()
    if filenames:
        for f in filenames:
            sql.execute(
                f"""UPDATE {_LOG}
                    SET sftp_delivery_status = 'pending', sftp_delivery_error = NULL
                    WHERE day_id = :day AND filename = :f
                      AND sftp_delivery_status = 'deferred'""",
                parameters=[sql.str_param("day", day_id), sql.str_param("f", f)],
            )
        n = len(filenames)
    else:
        sql.execute(
            f"""UPDATE {_LOG}
                SET sftp_delivery_status = 'pending', sftp_delivery_error = NULL
                WHERE day_id = :day AND sftp_delivery_status = 'deferred'""",
            parameters=[sql.str_param("day", day_id)],
        )
        n = -1  # all
    _log_event(sql, day_id, "requeued", None,
               f"re-deliver deferred: {n if n >= 0 else 'all'} files → pending")
    return n


ACTIONS = {
    "retry-parse": retry_parse,
    "retry-split": retry_split,
    "retry-sftp": retry_sftp,
    "retry-pdf-split": retry_pdf_split,
    "mark-manual": mark_manual,
    "approve-review": approve_review,
}
