-- Lakebase Postgres serving layer — schema, native table, view twins.
-- Project laplace-multidocument-cockpit / branch production / db databricks_postgres.
--
-- The app READS here when LAKEBASE_ENABLED=1 (src/core/pg.py, search_path =
-- laplace,public). Source of truth stays Unity Catalog: the *_pg tables in
-- schema "multidocument-prod" are Databricks SYNCED TABLES (continuous, CDF)
-- fed from the Delta tables — read-only here. evaluation_results is the ONE
-- native Postgres table (app-only writer; notebooks never touch it).
--
-- ⚠ These views are the Postgres TWINS of sql/views.sql. When a view changes
--   there, change it here too — same columns, same semantics, PG dialect.
--   Dialect notes: bool_or() replaces MAX(CASE...true...), EXTRACT(EPOCH...)
--   replaces UNIX_TIMESTAMP, interval '2 hours' replaces INTERVAL 2 HOURS.

CREATE SCHEMA IF NOT EXISTS laplace;
SET search_path TO laplace;

-- ─────────────────────────────────────────────────────────────────────────────
-- evaluation_results — native (mirrors sql/ddl_evaluation_results.sql).
-- "precision" quoted in DDL only (type-name clash); app SQL uses it unquoted.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS laplace.evaluation_results (
    filename            text NOT NULL,
    folder_id           text,
    day_id              text,
    total_pages         integer,
    gt_starts           integer[],
    gt_n_documents      integer,
    gt_is_multidoc      boolean,
    model_starts        integer[],
    model_n_documents   integer,
    model_used          text,
    exact_match         boolean,
    n_true_positive     integer,
    n_false_positive    integer,
    n_false_negative    integer,
    "precision"         double precision,
    recall              double precision,
    f1                  double precision,
    n_offby1            integer,
    precision_tol       double precision,
    recall_tol          double precision,
    f1_tol              double precision,
    multidoc_correct    boolean,
    annotator           text,
    annotated_at        timestamptz
);
CREATE INDEX IF NOT EXISTS eval_day_idx ON laplace.evaluation_results (day_id, filename);

-- ─────────────────────────────────────────────────────────────────────────────
-- v_file_status — twin of views.sql. The synced PK (day_id, filename) already
-- dedups processing_log/split_results by upsert, but the ROW_NUMBER structure
-- is kept for semantic parity with the warehouse view.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW laplace.v_file_status AS
WITH ranked AS (
  SELECT *,
    ROW_NUMBER() OVER (
      PARTITION BY day_id, filename
      ORDER BY CASE status
                 WHEN 'done'    THEN 1
                 WHEN 'parsed'  THEN 2
                 WHEN 'manual'  THEN 3
                 WHEN 'parsing' THEN 4
                 WHEN 'error'   THEN 5
                 WHEN 'skipped' THEN 6
                 WHEN 'pending' THEN 7
                 ELSE 8 END,
               created_at DESC
    ) AS rn
  FROM "multidocument-prod".processing_log_pg
)
SELECT
  l.day_id, l.filename, l.folder_id, l.file_size_mb, l.status,
  l.error_message, l.error_stage, l.n_pages, l.n_documents_found,
  l.retry_count, l.created_at, l.started_at, l.completed_at, l.run_id,
  l.archived_path, l.sftp_delivered_at, l.sftp_target_folder,
  l.sftp_delivery_status, l.sftp_delivery_error,
  s.needs_review, s.boundary_source, s.n_documents, s.model_used,
  s.predicted_starts
FROM ranked l
LEFT JOIN (
  SELECT * FROM (
    SELECT s.*,
      ROW_NUMBER() OVER (
        PARTITION BY day_id, filename
        ORDER BY processing_timestamp DESC
      ) AS srn
    FROM "multidocument-prod".split_results_pg s
  ) d WHERE srn = 1
) s
  ON s.day_id = l.day_id AND s.filename = l.filename
WHERE l.rn = 1;

-- ─────────────────────────────────────────────────────────────────────────────
-- v_funnel — includes n_review_blocked (fix(audit)-lifecycle-regression) and
-- the three Live Flow manual-lane columns. TWIN of v_funnel in sql/views.sql:
-- the app reads this one when LAKEBASE_ENABLED, so a column added there and
-- forgotten here shows up as an empty lane, not as an error.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW laplace.v_funnel AS
SELECT
  day_id,
  COUNT(*)                                                            AS n_files,
  SUM(CASE WHEN status = 'pending'  THEN 1 ELSE 0 END)                AS n_pending,
  SUM(CASE WHEN status = 'parsing'  THEN 1 ELSE 0 END)                AS n_parsing,
  SUM(CASE WHEN status = 'parsed'   THEN 1 ELSE 0 END)                AS n_parsed,
  SUM(CASE WHEN status = 'done'     THEN 1 ELSE 0 END)                AS n_predicted,
  SUM(CASE WHEN status = 'error'    THEN 1 ELSE 0 END)                AS n_error,
  SUM(CASE WHEN status = 'skipped'  THEN 1 ELSE 0 END)                AS n_skipped,
  SUM(CASE WHEN status = 'manual'   THEN 1 ELSE 0 END)                AS n_manual,
  SUM(CASE WHEN status = 'manual' AND boundary_source = 'manual'
           THEN 1 ELSE 0 END)                                         AS n_manual_deliverable,
  SUM(CASE WHEN sftp_delivery_status = 'pending'   THEN 1 ELSE 0 END) AS n_sftp_pending,
  SUM(CASE WHEN sftp_delivery_status = 'delivered' THEN 1 ELSE 0 END) AS n_delivered,
  SUM(CASE WHEN sftp_delivery_status = 'failed'    THEN 1 ELSE 0 END) AS n_sftp_failed,
  SUM(CASE WHEN sftp_delivery_status = 'deferred'  THEN 1 ELSE 0 END) AS n_deferred,
  SUM(CASE WHEN needs_review THEN 1 ELSE 0 END)                       AS n_needs_review,
  SUM(CASE WHEN needs_review AND sftp_delivery_status IS NULL
           THEN 1 ELSE 0 END)                                         AS n_review_blocked,
  -- ── Live Flow manual lane (twin of sql/views.sql) ────────────────────────
  -- The first two are BATCH TOTALS, not queues: they count on file_size_mb and
  -- error_stage, which mark-manual never touches (it only changes status).
  -- Counting them on status would drain them as files are taken in hand and
  -- the diagram's oversized + failed = manual arithmetic would stop adding up.
  -- retry_parse clears error_stage, so a recovered file drops out.
  -- 100 = MAX_FILE_SIZE_MB in algorithm-prod/nb_parse_documents.py.
  SUM(CASE WHEN file_size_mb >= 100 THEN 1 ELSE 0 END)                AS n_oversized,
  SUM(CASE WHEN error_stage = 'parsing' THEN 1 ELSE 0 END)            AS n_failed_parse,
  -- Union: everything that needs (or needed) human work. status='manual' is in
  -- the OR because a file can be marked manual from the Errors tab for reasons
  -- other than size or parse failure; without it, it would vanish from the load.
  -- boundary_source='manual' is in the OR for the same reason it discriminates
  -- for every delivery consumer: it is the ONLY trace that survives a hand
  -- UPDATE of the status. On 20260801 the 94 hand-annotated files carry
  -- status='done' (forced on 2026-08-03) and a NULL error_stage — without this
  -- arm the biggest batch's manual workload would read as zero.
  SUM(CASE WHEN file_size_mb >= 100 OR error_stage = 'parsing'
                OR status = 'manual' OR boundary_source = 'manual'
           THEN 1 ELSE 0 END)                                         AS n_manual_total,
  -- Boundaries actually drawn by hand. Distinct from n_manual_deliverable,
  -- which also requires status='manual' and feeds v_batch_status's CASE: that
  -- one stays put, or the 'delivered' threshold moves.
  SUM(CASE WHEN boundary_source = 'manual' THEN 1 ELSE 0 END)         AS n_manual_noted
FROM laplace.v_file_status
GROUP BY day_id;

-- ─────────────────────────────────────────────────────────────────────────────
-- v_batch_status
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW laplace.v_batch_status AS
SELECT
  f.*,
  g.last_event_ts,
  g.gate_opened,
  CASE
    WHEN f.n_delivered > 0
         AND f.n_delivered >= (f.n_files - f.n_skipped
                               - (f.n_manual - f.n_manual_deliverable) - f.n_error
                               - f.n_review_blocked)
      THEN 'delivered'
    WHEN f.n_sftp_pending + f.n_sftp_failed + f.n_deferred > 0
      THEN 'delivering'
    WHEN g.gate_opened AND f.n_delivered = 0
      THEN 'awaiting_annotation'
    WHEN f.n_predicted > 0 THEN 'predicted'
    WHEN f.n_parsing > 0 OR f.n_parsed > 0 THEN 'parsing'
    WHEN f.n_pending > 0 THEN 'uploaded'
    ELSE 'unknown'
  END AS lifecycle,
  (f.n_error + f.n_sftp_failed) > 0 AS has_errors
FROM laplace.v_funnel f
LEFT JOIN (
  SELECT day_id,
         MAX(event_ts) AS last_event_ts,
         BOOL_OR(event_type = 'awaiting_annotation') AS gate_opened
  FROM "multidocument-prod".pipeline_events_pg
  GROUP BY day_id
) g ON g.day_id = f.day_id;

-- ─────────────────────────────────────────────────────────────────────────────
-- v_run_summary
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW laplace.v_run_summary AS
SELECT
  run_id, day_id, stage,
  MIN(event_ts) AS started_at,
  MAX(event_ts) AS last_event_at,
  CAST(EXTRACT(EPOCH FROM (MAX(event_ts) - MIN(event_ts))) / 60.0 AS numeric(10,1))
    AS duration_min,
  SUM(CASE WHEN event_type = 'error' THEN 1 ELSE 0 END)         AS n_errors,
  SUM(CASE WHEN event_type = 'needs_review' THEN 1 ELSE 0 END)  AS n_needs_review,
  SUM(CASE WHEN event_type = 'delivered' THEN 1 ELSE 0 END)     AS n_delivered,
  SUM(CASE WHEN event_type = 'deferred' THEN 1 ELSE 0 END)      AS n_deferred,
  SUM(CASE WHEN event_type = 'quarantined' THEN 1 ELSE 0 END)   AS n_quarantined,
  MAX(CASE WHEN event_type = 'run_completed' THEN new_status END) AS run_outcome
FROM "multidocument-prod".pipeline_events_pg
GROUP BY run_id, day_id, stage;

-- ─────────────────────────────────────────────────────────────────────────────
-- v_stuck_files — includes the fix(audit)-oversized-window arms.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW laplace.v_stuck_files AS
SELECT *,
  CASE
    WHEN status = 'error'
      THEN CONCAT('error at ', COALESCE(error_stage, '?'), ': ', COALESCE(error_message, ''))
    -- Oversized: il parser LLM non li tocca (>100MB) e il PDF sta in
    -- oversized/{day}/. Vanno annotati a mano dal tab Manual come i parse
    -- failure. Gemello di sql/views.sql — cambiare sempre entrambi.
    WHEN status = 'skipped'
      THEN CONCAT('oversized (', COALESCE(ROUND(file_size_mb)::text, '?'),
                  ' MB) — annotare a mano dal tab Manual')
    WHEN sftp_delivery_status = 'failed'
      THEN CONCAT('sftp failed: ', COALESCE(sftp_delivery_error, ''))
    WHEN sftp_delivery_status = 'deferred'
      THEN CONCAT('deferred: ', COALESCE(sftp_delivery_error, 'remote folder missing'))
    WHEN status = 'parsing' AND started_at < now() - interval '2 hours'
      THEN 'stuck in parsing > 2h'
    WHEN status = 'parsed' AND completed_at < now() - interval '12 hours'
      THEN 'parsed but never split (> 12h)'
    WHEN sftp_delivery_status = 'pending' AND archived_path IS NULL
         AND completed_at < now() - interval '2 hours'
      THEN 'split but not archived (crash between passes?)'
    WHEN sftp_delivery_status = 'pending'
         AND completed_at < now() - interval '24 hours'
      THEN 'awaiting sftp > 24h'
    WHEN needs_review AND sftp_delivery_status IS NULL
      THEN CONCAT('needs review (', COALESCE(boundary_source, '?'), ') — delivery blocked')
    WHEN status = 'pending' AND created_at < now() - interval '2 hours'
      THEN 'stuck in pending > 2h (never picked up by parse)'
  END AS stuck_reason
FROM laplace.v_file_status
WHERE
     status = 'error'
  OR status = 'skipped'
  OR sftp_delivery_status IN ('failed', 'deferred')
  OR (status = 'parsing' AND started_at < now() - interval '2 hours')
  OR (status = 'parsed' AND completed_at < now() - interval '12 hours')
  OR (sftp_delivery_status = 'pending' AND archived_path IS NULL
      AND completed_at < now() - interval '2 hours')
  OR (sftp_delivery_status = 'pending' AND completed_at < now() - interval '24 hours')
  OR (needs_review AND sftp_delivery_status IS NULL)
  OR (status = 'pending' AND created_at < now() - interval '2 hours');

-- ─────────────────────────────────────────────────────────────────────────────
-- v_sftp_board
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW laplace.v_sftp_board AS
SELECT
  day_id, folder_id,
  SUM(COALESCE(n_documents, 0)) AS n_files,
  SUM(CASE WHEN sftp_delivery_status = 'delivered' THEN COALESCE(n_documents, 0) ELSE 0 END) AS n_delivered,
  SUM(CASE WHEN sftp_delivery_status = 'pending'   THEN COALESCE(n_documents, 0) ELSE 0 END) AS n_pending,
  SUM(CASE WHEN sftp_delivery_status = 'failed'    THEN COALESCE(n_documents, 0) ELSE 0 END) AS n_failed,
  SUM(CASE WHEN sftp_delivery_status = 'deferred'  THEN COALESCE(n_documents, 0) ELSE 0 END) AS n_deferred,
  MAX(sftp_delivered_at) AS last_delivered_at,
  MAX(sftp_target_folder) AS sftp_target_folder
FROM laplace.v_file_status
WHERE sftp_delivery_status IS NOT NULL
GROUP BY day_id, folder_id;

-- ─────────────────────────────────────────────────────────────────────────────
-- v_needs_review
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW laplace.v_needs_review AS
SELECT day_id, filename, folder_id, total_pages, predicted_starts, n_documents,
       model_used, boundary_source, processing_timestamp
FROM "multidocument-prod".split_results_pg
WHERE needs_review = true;

-- ─────────────────────────────────────────────────────────────────────────────
-- v_events_recent
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW laplace.v_events_recent AS
SELECT event_ts, day_id, run_id, stage, event_type, filename, folder_id,
       old_status, new_status, detail, error_message, actor
FROM "multidocument-prod".pipeline_events_pg
ORDER BY event_ts DESC;

-- Aliases so config.rq() bare names resolve for the two synced tables the app
-- queries directly (manual worklist / model prediction / file drawer).
CREATE OR REPLACE VIEW laplace.processing_log AS
  SELECT * FROM "multidocument-prod".processing_log_pg;
CREATE OR REPLACE VIEW laplace.split_results AS
  SELECT * FROM "multidocument-prod".split_results_pg;
CREATE OR REPLACE VIEW laplace.pipeline_events AS
  SELECT * FROM "multidocument-prod".pipeline_events_pg;
CREATE OR REPLACE VIEW laplace.gcs_llm_responses AS
  SELECT * FROM "multidocument-prod".gcs_llm_responses_pg;
