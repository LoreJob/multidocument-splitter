"""One-time copy of evaluation_results: UC (warehouse) -> Lakebase Postgres.

Idempotent: skips rows already present by (day_id, filename, annotated_at).
Run from a machine with the `luxottica` profile:

    DATABRICKS_CONFIG_PROFILE=luxottica python deploy/migrate_evaluation_results.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

from src.core.config import config

WAREHOUSE_ID = "2663c9a13af5c078"

COLS = [
    "filename", "folder_id", "day_id", "total_pages",
    "gt_starts", "gt_n_documents", "gt_is_multidoc",
    "model_starts", "model_n_documents", "model_used",
    "exact_match",
    "n_true_positive", "n_false_positive", "n_false_negative",
    "precision", "recall", "f1",
    "n_offby1", "precision_tol", "recall_tol", "f1_tol",
    "multidoc_correct",
    "annotator", "annotated_at",
]
INTS = {"total_pages", "gt_n_documents", "model_n_documents",
        "n_true_positive", "n_false_positive", "n_false_negative", "n_offby1"}
FLOATS = {"precision", "recall", "f1", "precision_tol", "recall_tol", "f1_tol"}
BOOLS = {"gt_is_multidoc", "exact_match", "multidoc_correct"}
ARRAYS = {"gt_starts", "model_starts"}


def _coerce(col: str, v):
    if v is None or v == "":
        return None
    if col in INTS:
        return int(v)
    if col in FLOATS:
        return float(v)
    if col in BOOLS:
        return str(v).strip().lower() == "true"
    if col in ARRAYS:
        s = str(v).strip()
        try:
            parsed = json.loads(s)
            return [int(x) for x in parsed] if isinstance(parsed, list) else None
        except (ValueError, TypeError):
            s = s.lstrip("[").rstrip("]")
            return [int(x.strip().strip('"')) for x in s.split(",") if x.strip()] if s else []
    return v  # text / timestamp (PG casts the ISO string)


def main() -> None:
    import psycopg

    w = WorkspaceClient()
    resp = w.statement_execution.execute_statement(
        warehouse_id=WAREHOUSE_ID,
        statement=f"SELECT {', '.join(COLS)} FROM "
                  f"`{config.CATALOG}`.`{config.SCHEMA}`.`evaluation_results`",
        wait_timeout="50s",
    )
    while resp.status and resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
        resp = w.statement_execution.get_statement(resp.statement_id)
    assert resp.status.state == StatementState.SUCCEEDED, resp.status
    src_rows = resp.result.data_array if resp.result and resp.result.data_array else []
    print(f"UC rows: {len(src_rows)}")

    tok = w.postgres.generate_database_credential(endpoint=config.LAKEBASE_ENDPOINT).token
    conn = psycopg.connect(
        host=config.LAKEBASE_HOST, dbname=config.LAKEBASE_DB,
        user=w.current_user.me().user_name, password=tok,
        sslmode="require", autocommit=False,
    )
    quoted = [f'"{c}"' if c == "precision" else c for c in COLS]
    placeholders = ", ".join(["%s"] * len(COLS))
    inserted = skipped = 0
    with conn.cursor() as c:
        for raw in src_rows:
            row = [_coerce(col, v) for col, v in zip(COLS, raw)]
            c.execute(
                "SELECT 1 FROM laplace.evaluation_results "
                "WHERE day_id = %s AND filename = %s AND annotated_at = %s",
                (row[2], row[0], row[23]),
            )
            if c.fetchone():
                skipped += 1
                continue
            c.execute(
                f"INSERT INTO laplace.evaluation_results ({', '.join(quoted)}) "
                f"VALUES ({placeholders})",
                row,
            )
            inserted += 1
    conn.commit()
    with conn.cursor() as c:
        c.execute("SELECT COUNT(*) FROM laplace.evaluation_results")
        total = c.fetchone()[0]
    conn.close()
    print(f"inserted={inserted} skipped={skipped} pg_total={total}")


if __name__ == "__main__":
    main()
