"""Repository layer: worklist, model predictions, ground-truth persistence, eval.

Ties together the SQL warehouse (tables), the Files API (volumes), and the
evaluation logic into the operations the Flask routes call.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import fitz  # PyMuPDF — server-side page rendering

from ..core.config import config
from ..core.db import get_sql
from ..core.volumes import get_volumes
from .evaluation import evaluate, aggregate_stats_grouped


# ─────────────────────────────────────────────────────────────────────────────
#  Worklist
#
#  There is no day list here: batches come from /api/days (inbox/ ∪ validation/
#  ∪ tables), which is a superset of what validation/ alone can see.
# ─────────────────────────────────────────────────────────────────────────────
def build_worklist(day_id: str) -> dict:
    """PDFs in validation/{day_id}/ split into pending (no GT yet) and done (GT exists)."""
    vols = get_volumes()
    validation_pdfs = vols.list_pdfs(config.validation_path(day_id))
    done = vols.list_json_stems(config.ground_truth_path(day_id))

    pending = [f for f in validation_pdfs if f not in done]
    completed = [f for f in validation_pdfs if f in done]
    return {
        "day_id": day_id,
        "pending": pending,
        "completed": completed,
        "n_total": len(validation_pdfs),
        "n_pending": len(pending),
        "n_completed": len(completed),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Model prediction (split_results)
# ─────────────────────────────────────────────────────────────────────────────
def get_model_prediction(filename: str, day_id: str | None = None) -> dict | None:
    """Latest split_results row for (day_id, filename). Same filename may exist
    in several day batches — day_id disambiguates; None falls back to latest."""
    sql = get_sql()
    day_filter = "AND day_id = :day" if day_id else ""
    params = [sql.str_param("fname", filename)]
    if day_id:
        params.append(sql.str_param("day", day_id))
    rows = sql.execute(
        f"""
        SELECT filename, folder_id, total_pages, predicted_starts,
               n_documents, model_used
        FROM {config.fq(config.TABLE_SPLIT_RESULTS)}
        WHERE filename = :fname {day_filter}
        ORDER BY processing_timestamp DESC
        LIMIT 1
        """,
        parameters=params,
    )
    if not rows:
        return None
    r = rows[0]
    return {
        "filename": r["filename"],
        "folder_id": r.get("folder_id"),
        "total_pages": _to_int(r.get("total_pages")),
        "predicted_starts": _parse_int_array(r.get("predicted_starts")),
        "n_documents": _to_int(r.get("n_documents")),
        "model_used": r.get("model_used"),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Server-side PDF rendering (download once, render pages on demand)
# ─────────────────────────────────────────────────────────────────────────────
PAGE_ZOOM = 1.6          # ~115 DPI
JPEG_QUALITY = 80

# Rendering is expensive twice over: the PDF is downloaded from the Volume and
# every page is rasterised. Two caches sit in front of it:
#   * _DOC_CACHE — open fitz.Document per worker (avoids re-download while a file
#     is open). Small, in-process.
#   * a shared on-disk JPEG cache — each rendered page is written once and served
#     by ANY of the gunicorn workers (they share the container filesystem), so a
#     page is rasterised once ever, not per session / per worker / per tab.
# _RENDER_LOCK makes both safe when a background pre-warm thread renders while a
# request thread also renders. It is an RLock because render_page_jpeg holds it
# and calls _get_doc, which acquires it too. Disk-cache HITS never take the lock.
_RENDER_LOCK = threading.RLock()

_DOC_CACHE: "OrderedDict[str, fitz.Document]" = OrderedDict()
_DOC_CACHE_MAX = 4       # keep the last few PDFs open (bytes can be large)

# Shared page-image cache on the container filesystem.
_CACHE_DIR = Path(os.environ.get("RENDER_CACHE_DIR")
                  or (Path(tempfile.gettempdir()) / "gt-render-cache"))
_CACHE_MAX_MB = int(os.environ.get("RENDER_CACHE_MAX_MB", "512"))
_PREWARM_ON = os.environ.get("RENDER_PREWARM", "1") != "0"
try:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass  # unwritable cache dir → we degrade to render-without-cache below

_WARMING: set[str] = set()   # cache-bases currently being pre-warmed
_WARMING_LOCK = threading.Lock()
_EVICT_EVERY = 200           # sweep the cache only once every N writes
_write_count = 0


def _base_for(day_id: str, base_path: str | None) -> str:
    # base_path defaults to validation/ (the sample). Manual annotation passes
    # inbox/ so parse-failed files (never staged into validation/) can be rendered.
    return base_path or config.validation_path(day_id)


def _get_doc(day_id: str, filename: str, base_path: str | None = None) -> fitz.Document:
    base = _base_for(day_id, base_path)
    key = f"{base}/{filename}"
    with _RENDER_LOCK:
        if key in _DOC_CACHE:
            _DOC_CACHE.move_to_end(key)
            return _DOC_CACHE[key]
        vols = get_volumes()
        data = vols.download_bytes(vols.pdf_path(base, filename))
        doc = fitz.open(stream=data, filetype="pdf")
        _DOC_CACHE[key] = doc
        if len(_DOC_CACHE) > _DOC_CACHE_MAX:
            _, old = _DOC_CACHE.popitem(last=False)
            try:
                old.close()
            except Exception:
                pass
        return doc


def page_count(day_id: str, filename: str, base_path: str | None = None) -> int:
    return _get_doc(day_id, filename, base_path).page_count


def _cache_path(base: str, filename: str, n: int) -> Path:
    # Keyed on base (validation vs inbox), page, and the render settings, so a
    # zoom/quality change never serves stale bytes. PDF pages are immutable.
    key = f"{base}|{filename}|{n}|z{PAGE_ZOOM}|q{JPEG_QUALITY}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return _CACHE_DIR / f"{digest}.jpg"


def _render_raw(day_id: str, filename: str, n: int, base_path: str | None) -> bytes:
    """Rasterise one page with fitz (no caching). Holds the render lock."""
    with _RENDER_LOCK:
        page = _get_doc(day_id, filename, base_path).load_page(n - 1)
        pix = page.get_pixmap(matrix=fitz.Matrix(PAGE_ZOOM, PAGE_ZOOM),
                              colorspace=fitz.csRGB, alpha=False)
        return pix.tobytes("jpeg", jpg_quality=JPEG_QUALITY)


def render_page_jpeg(day_id: str, filename: str, n: int, base_path: str | None = None) -> bytes:
    """1-based page `n` as an RGB JPEG (csRGB avoids CMYK/colorspace glitches).

    Served from the shared disk cache when present; otherwise rendered once,
    written atomically, and cached. A missing/unwritable cache silently degrades
    to plain rendering — never a 500.
    """
    base = _base_for(day_id, base_path)
    path = _cache_path(base, filename, n)

    try:
        if path.exists():
            return path.read_bytes()          # hot path — no lock, no fitz
    except OSError:
        pass

    with _RENDER_LOCK:
        try:                                   # double-check: a peer may have won
            if path.exists():
                return path.read_bytes()
        except OSError:
            pass
        data = _render_raw(day_id, filename, n, base_path)

    _write_cache(path, data)
    return data


def _write_cache(path: Path, data: bytes) -> None:
    """Atomic write (temp + replace) so a reader never sees a half file."""
    global _write_count
    try:
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError:
        return  # cache unwritable — the caller already has the bytes
    _write_count += 1
    if _write_count % _EVICT_EVERY == 0:
        _evict_if_needed()


def _evict_if_needed() -> None:
    """Cap the cache: delete oldest-by-mtime files until under ~80% of the cap."""
    try:
        files = [(p, p.stat()) for p in _CACHE_DIR.glob("*.jpg")]
    except OSError:
        return
    total = sum(st.st_size for _, st in files)
    cap = _CACHE_MAX_MB * 1024 * 1024
    if total <= cap:
        return
    target = int(cap * 0.8)
    for p, st in sorted(files, key=lambda t: t[1].st_mtime):
        try:
            p.unlink()
            total -= st.st_size
        except OSError:
            continue
        if total <= target:
            break


def prewarm(day_id: str, filename: str, total_pages: int, base_path: str | None = None) -> None:
    """Render every page into the disk cache on a background thread, so the pages
    are ready before the operator scrolls to them. No-op if disabled or already
    warming this file. The per-page lock in render_page_jpeg lets interactive
    requests interleave instead of waiting for the whole warm."""
    if not _PREWARM_ON or total_pages < 1:
        return
    base = _base_for(day_id, base_path)
    warm_key = f"{base}/{filename}"
    with _WARMING_LOCK:
        if warm_key in _WARMING:
            return
        _WARMING.add(warm_key)

    def _run():
        try:
            for n in range(1, total_pages + 1):
                try:
                    render_page_jpeg(day_id, filename, n, base_path)
                except Exception:
                    pass  # one bad page must not abort the warm
        finally:
            with _WARMING_LOCK:
                _WARMING.discard(warm_key)

    threading.Thread(target=_run, name=f"prewarm:{warm_key}", daemon=True).start()


# ─────────────────────────────────────────────────────────────────────────────
#  Dashboard stats (evaluation_results aggregate)
# ─────────────────────────────────────────────────────────────────────────────
def get_eval_stats() -> dict:
    sql = get_sql()
    rows = sql.execute(
        f"""
        SELECT model_starts, exact_match, multidoc_correct,
               precision, recall, f1, f1_tol, n_offby1, gt_is_multidoc
        FROM {config.fq(config.TABLE_EVALUATION)}
        """
    )
    norm = []
    for r in rows:
        norm.append({
            "model_starts": r.get("model_starts"),  # None when no model row
            "exact_match": _to_bool(r.get("exact_match")),
            "multidoc_correct": _to_bool(r.get("multidoc_correct")),
            "precision": _to_float(r.get("precision")),
            "recall": _to_float(r.get("recall")),
            "f1": _to_float(r.get("f1")),
            "f1_tol": _to_float(r.get("f1_tol")),
            "n_offby1": _to_int(r.get("n_offby1")) or 0,
            "gt_is_multidoc": _to_bool(r.get("gt_is_multidoc")),
        })
    return aggregate_stats_grouped(norm)


# ─────────────────────────────────────────────────────────────────────────────
#  Ground truth read / build
# ─────────────────────────────────────────────────────────────────────────────
def load_ground_truth(day_id: str, filename: str) -> dict | None:
    vols = get_volumes()
    path = vols.json_path(config.ground_truth_path(day_id), filename)
    return vols.read_json(path)


def build_gt_payload(
    filename: str,
    folder_id: str | None,
    total_pages: int,
    gt_starts: list[int],
    is_multidoc: bool,
    annotator: str,
    day_id: str | None = None,
) -> dict:
    """Canonical ground-truth JSON. `documents` is derived from starts so future
    versions can attach a `type` per segment without changing the boundary model."""
    starts = sorted(set(int(p) for p in gt_starts if 1 <= int(p) <= total_pages))
    if 1 not in starts:
        starts = [1] + starts
    documents = _starts_to_documents(starts, total_pages)
    return {
        "filename": filename,
        "folder_id": folder_id,
        "day_id": day_id,
        "total_pages": total_pages,
        "is_multidoc": is_multidoc,
        "predicted_starts": starts,        # human ground-truth boundaries
        "n_documents": len(starts),
        "documents": documents,            # [{start, end, type=None}]  type reserved for v2
        "annotator": annotator,
        "annotated_at": _now_iso(),
        "schema_version": 1,
    }


def _starts_to_documents(starts: list[int], total_pages: int) -> list[dict]:
    docs = []
    for i, s in enumerate(starts):
        end = (starts[i + 1] - 1) if i + 1 < len(starts) else total_pages
        docs.append({"start": s, "end": end, "type": None})
    return docs


def save_ground_truth(payload: dict) -> str:
    vols = get_volumes()
    path = vols.json_path(config.ground_truth_path(payload.get("day_id")),
                          payload["filename"])
    vols.upload_json(path, payload)
    return path


# ─────────────────────────────────────────────────────────────────────────────
#  Evaluation + persistence
# ─────────────────────────────────────────────────────────────────────────────
def run_and_store_evaluation(gt_payload: dict, model: dict | None) -> dict:
    """Evaluate GT vs model prediction and append a row to evaluation_results.

    If no model prediction exists (file not yet split), persists the GT-side facts
    with null model metrics so the row still records that this file was annotated.
    """
    gt_starts = gt_payload["predicted_starts"]
    model_starts = model["predicted_starts"] if model else []

    ev = evaluate(gt_starts, model_starts) if model else None
    _insert_evaluation_row(gt_payload, model, ev)
    return ev.as_dict() if ev else {"model_prediction_missing": True}


def _insert_evaluation_row(gt: dict, model: dict | None, ev) -> None:
    """Append one row to evaluation_results.

    Every string goes in as a bound parameter. The numbers do NOT: named
    parameters have no ARRAY type in StatementExecution, so gt_starts /
    model_starts must be `array(1, 5, 9)` literals — which is safe only because
    _int_array_literal() int()-coerces each element. The scalar numbers are
    coerced here for the same reason, rather than trusting a distant caller.
    """
    sql = get_sql()
    table = config.fq(config.TABLE_EVALUATION)
    params = []

    def p(name: str, value) -> str:
        """Bind a string as :name, or emit a literal NULL. Never interpolates."""
        if value is None:
            return "NULL"
        params.append(sql.str_param(name, str(value)))
        return f":{name}"

    # Strings — bound. `annotator` especially: it comes from a request header
    # (X-Forwarded-Email) and is the one field no regex upstream validates.
    fname = p("fname", gt["filename"])
    folder = p("folder", gt.get("folder_id"))
    day = p("day", gt.get("day_id"))
    model_used = p("model_used", model.get("model_used") if model else None)
    annotator = p("annotator", gt["annotator"])

    # Numbers + arrays — literals, coerced at the point of use.
    gt_starts = _int_array_literal(gt["predicted_starts"])
    gt_n = int(gt["n_documents"])
    total_pages = int(gt["total_pages"])
    gt_multi = "TRUE" if gt["is_multidoc"] else "FALSE"
    model_starts = _int_array_literal(model["predicted_starts"]) if model else "NULL"
    model_n = int(model["n_documents"]) if model else "NULL"

    if ev:
        exact_match = "TRUE" if ev.exact_match else "FALSE"
        metrics = (
            f"{int(ev.n_true_positive)}, {int(ev.n_false_positive)}, {int(ev.n_false_negative)}, "
            f"{float(ev.precision)}, {float(ev.recall)}, {float(ev.f1)}, "
            f"{int(ev.n_offby1)}, {float(ev.precision_tol)}, {float(ev.recall_tol)}, {float(ev.f1_tol)}, "
            f"{'TRUE' if ev.multidoc_correct else 'FALSE'}"
        )
    else:
        exact_match = "NULL"
        metrics = "NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL"

    stmt = f"""
        INSERT INTO {table} (
            filename, folder_id, day_id, total_pages,
            gt_starts, gt_n_documents, gt_is_multidoc,
            model_starts, model_n_documents, model_used,
            exact_match,
            n_true_positive, n_false_positive, n_false_negative,
            precision, recall, f1,
            n_offby1, precision_tol, recall_tol, f1_tol,
            multidoc_correct,
            annotator, annotated_at
        ) VALUES (
            {fname}, {folder}, {day}, {total_pages},
            {gt_starts}, {gt_n}, {gt_multi},
            {model_starts}, {model_n}, {model_used},
            {exact_match},
            {metrics},
            {annotator}, current_timestamp()
        )
    """
    sql.execute(stmt, parameters=params)


# ─────────────────────────────────────────────────────────────────────────────
#  helpers
# ─────────────────────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_int(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_bool(v):
    """StatementExecution returns booleans as the strings 'true'/'false'."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() == "true"


def _parse_int_array(v) -> list[int]:
    """StatementExecution returns ARRAY<INT> as a JSON-ish string — sometimes with
    quoted elements ('["1","5"]'), sometimes bare ('[1, 5]'). Coerce every element
    through int() so a quoted element never crashes with `int('"1"')`."""
    if v is None:
        return []
    if isinstance(v, list):
        return [int(x) for x in v]
    s = str(v).strip()
    if not s:
        return []
    try:
        parsed = json.loads(s)
        if isinstance(parsed, list):
            return [int(x) for x in parsed]
    except (ValueError, TypeError):
        pass
    # Fallback for non-JSON serialisations: strip brackets + per-element quotes.
    s = s.lstrip("[").rstrip("]")
    if not s:
        return []
    return [int(x.strip().strip('"').strip("'")) for x in s.split(",") if x.strip()]


def _int_array_literal(arr: list[int]) -> str:
    """`array(1, 5, 9)`. A literal because StatementExecution has no ARRAY
    parameter type; safe because every element is int()-coerced here."""
    return "array(" + ", ".join(str(int(x)) for x in arr) + ")"
