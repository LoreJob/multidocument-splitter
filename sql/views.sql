-- P3 — Control-tower SQL views. One definition, shared by the Flask dashboard
-- and nb_pipeline_status. All views expose day_id (batch identity).
-- Run after ddl_prod_schema.sql + ddl_pipeline_events.sql + ddl_evaluation_results.sql.

USE CATALOG `sbx-logistics`;
USE SCHEMA `multidocument-prod`;

-- ─────────────────────────────────────────────────────────────────────────────
-- v_file_status — one row per (day_id, filename): furthest-progressed log entry
-- joined with split prediction facts. The dedup ranking mirrors the old
-- nb_pipeline_status logic (done > parsed > parsing > error > skipped > pending).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_file_status AS
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
  FROM processing_log
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
-- Dedup split_results to the latest row per (day_id, filename): a non-idempotent
-- rerun can leave >1 prediction row per file, and a raw join would fan v_funnel's
-- counts out (100 files → 200). srn=1 keeps the join strictly 1:1.
LEFT JOIN (
  SELECT * FROM (
    SELECT s.*,
      ROW_NUMBER() OVER (
        PARTITION BY day_id, filename
        ORDER BY processing_timestamp DESC
      ) AS srn
    FROM split_results s
  ) WHERE srn = 1
) s
  ON s.day_id = l.day_id AND s.filename = l.filename
WHERE l.rn = 1;

-- ─────────────────────────────────────────────────────────────────────────────
-- v_funnel — per-batch per-stage counts. Poll source for the live flow view.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_funnel AS
SELECT
  day_id,
  COUNT(*)                                                          AS n_files,
  SUM(CASE WHEN status = 'pending'  THEN 1 ELSE 0 END)              AS n_pending,
  SUM(CASE WHEN status = 'parsing'  THEN 1 ELSE 0 END)              AS n_parsing,
  SUM(CASE WHEN status = 'parsed'   THEN 1 ELSE 0 END)              AS n_parsed,
  SUM(CASE WHEN status = 'done'     THEN 1 ELSE 0 END)              AS n_predicted,
  SUM(CASE WHEN status = 'error'    THEN 1 ELSE 0 END)              AS n_error,
  SUM(CASE WHEN status = 'skipped'  THEN 1 ELSE 0 END)              AS n_skipped,
  SUM(CASE WHEN status = 'manual'   THEN 1 ELSE 0 END)              AS n_manual,
  -- 'manual' vale due cose: annotato a mano nel tab Manual (boundary_source
  -- ='manual' → consegnabile come ogni altro file) e marcato manual dal tab
  -- Errors (gestito fuori dalla pipeline, mai consegnato). Solo il secondo va
  -- escluso dal denominatore di 'delivered' in v_batch_status.
  SUM(CASE WHEN status = 'manual' AND boundary_source = 'manual'
           THEN 1 ELSE 0 END)                                       AS n_manual_deliverable,
  SUM(CASE WHEN sftp_delivery_status = 'pending'   THEN 1 ELSE 0 END) AS n_sftp_pending,
  SUM(CASE WHEN sftp_delivery_status = 'delivered' THEN 1 ELSE 0 END) AS n_delivered,
  SUM(CASE WHEN sftp_delivery_status = 'failed'    THEN 1 ELSE 0 END) AS n_sftp_failed,
  SUM(CASE WHEN sftp_delivery_status = 'deferred'  THEN 1 ELSE 0 END) AS n_deferred,
  SUM(CASE WHEN needs_review THEN 1 ELSE 0 END)                     AS n_needs_review,
  -- Review-blocked: needs_review senza esito di consegna (nb_pdf_split li
  -- salta finché non c'è GT o approvazione). Vanno tolti dal denominatore di
  -- 'delivered' in v_batch_status, altrimenti un batch consegnato con un file
  -- bloccato regredisce a 'predicted' per sempre.
  SUM(CASE WHEN needs_review AND sftp_delivery_status IS NULL
           THEN 1 ELSE 0 END)                                       AS n_review_blocked,
  -- ── corsia manuale del Live Flow ────────────────────────────────────────
  -- Le prime due sono TOTALI DI BATCH, non code: contano su file_size_mb ed
  -- error_stage, che mark-manual non tocca (cambia solo lo status). Contarle
  -- sullo status le farebbe svuotare man mano che i file vengono presi in
  -- carico, e nel diagramma la somma oversized+falliti=manuali non tornerebbe
  -- più. retry_parse azzera error_stage, quindi un file recuperato esce.
  -- 100 = MAX_FILE_SIZE_MB in algorithm-prod/nb_parse_documents.py.
  SUM(CASE WHEN file_size_mb >= 100 THEN 1 ELSE 0 END)              AS n_oversized,
  SUM(CASE WHEN error_stage = 'parsing' THEN 1 ELSE 0 END)          AS n_failed_parse,
  -- Unione: tutto ciò che richiede (o ha richiesto) lavoro umano. Lo status
  -- 'manual' è nell'OR perché un file può essere marcato manual dal tab Errors
  -- per motivi diversi da taglia/parse: senza, sparirebbe dal carico.
  -- boundary_source='manual' è nell'OR per la stessa ragione per cui è il
  -- discriminante di ogni consumer della consegna: è l'UNICA traccia che
  -- sopravvive a un UPDATE a mano dello status. Su 20260801 i 94 file
  -- annotati a mano hanno status='done' (forzato il 2026-08-03) ed
  -- error_stage NULL: senza questo ramo il carico manuale del batch più
  -- grosso risulterebbe zero.
  SUM(CASE WHEN file_size_mb >= 100 OR error_stage = 'parsing'
                OR status = 'manual' OR boundary_source = 'manual'
           THEN 1 ELSE 0 END)                                       AS n_manual_total,
  -- Confini davvero disegnati a mano. Distinto da n_manual_deliverable, che
  -- richiede ANCHE status='manual' ed entra nel CASE di v_batch_status: quello
  -- non si tocca, o si sposta la soglia di 'delivered'.
  SUM(CASE WHEN boundary_source = 'manual' THEN 1 ELSE 0 END)       AS n_manual_noted,
  -- File che BLOCCANO la consegna: hanno bisogno di un umano e non hanno
  -- ancora i confini disegnati a mano. Marcare manual non basta a sbloccare —
  -- serve l'annotazione vera (decisione utente 2026-08-04).
  -- NB: si basa sullo status, non su sftp_delivery_status, apposta. Una
  -- consegna 'failed'/'deferred' NON deve bloccare, o il ritentativo che la
  -- risolve resterebbe fuori per sempre.
  SUM(CASE WHEN status IN ('error', 'skipped', 'manual')
                AND (boundary_source IS NULL OR boundary_source <> 'manual')
           THEN 1 ELSE 0 END)                                       AS n_delivery_blocked
FROM v_file_status
GROUP BY day_id;

-- ─────────────────────────────────────────────────────────────────────────────
-- v_batch_status — coarse lifecycle per day_id. The app refines
-- 'awaiting_annotation' with the gate counts (volume listings).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_batch_status AS
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
FROM v_funnel f
LEFT JOIN (
  SELECT day_id,
         MAX(event_ts) AS last_event_ts,
         MAX(CASE WHEN event_type = 'awaiting_annotation' THEN true ELSE false END) AS gate_opened
  FROM pipeline_events
  GROUP BY day_id
) g ON g.day_id = f.day_id;

-- ─────────────────────────────────────────────────────────────────────────────
-- v_run_summary — per run/stage: window + event counts (durations for the UI).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_run_summary AS
SELECT
  run_id, day_id, stage,
  MIN(event_ts) AS started_at,
  MAX(event_ts) AS last_event_at,
  CAST((UNIX_TIMESTAMP(MAX(event_ts)) - UNIX_TIMESTAMP(MIN(event_ts))) / 60.0 AS DECIMAL(10,1))
    AS duration_min,
  SUM(CASE WHEN event_type = 'error' THEN 1 ELSE 0 END)         AS n_errors,
  SUM(CASE WHEN event_type = 'needs_review' THEN 1 ELSE 0 END)  AS n_needs_review,
  SUM(CASE WHEN event_type = 'delivered' THEN 1 ELSE 0 END)     AS n_delivered,
  SUM(CASE WHEN event_type = 'deferred' THEN 1 ELSE 0 END)      AS n_deferred,
  SUM(CASE WHEN event_type = 'quarantined' THEN 1 ELSE 0 END)   AS n_quarantined,
  MAX(CASE WHEN event_type = 'run_completed' THEN new_status END) AS run_outcome
FROM pipeline_events
GROUP BY run_id, day_id, stage;

-- ─────────────────────────────────────────────────────────────────────────────
-- v_stuck_files — every file that needs attention, with WHY.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_stuck_files AS
SELECT *,
  CASE
    WHEN status = 'error'
      THEN CONCAT('error at ', COALESCE(error_stage, '?'), ': ', COALESCE(error_message, ''))
    -- Oversized: il parser LLM non li tocca (>100MB) e il PDF sta in
    -- oversized/{day}/. Vanno annotati a mano dal tab Manual come i parse
    -- failure: senza questo ramo non comparivano da nessuna parte e restavano
    -- 'skipped' per sempre, invisibili.
    WHEN status = 'skipped'
      THEN CONCAT('oversized (', COALESCE(CAST(ROUND(file_size_mb, 0) AS STRING), '?'),
                  ' MB) — annotare a mano dal tab Manual')
    WHEN sftp_delivery_status = 'failed'
      THEN CONCAT('sftp failed: ', COALESCE(sftp_delivery_error, ''))
    WHEN sftp_delivery_status = 'deferred'
      THEN CONCAT('deferred: ', COALESCE(sftp_delivery_error, 'remote folder missing'))
    WHEN status = 'parsing' AND started_at < current_timestamp() - INTERVAL 2 HOURS
      THEN 'stuck in parsing > 2h'
    WHEN status = 'parsed' AND completed_at < current_timestamp() - INTERVAL 12 HOURS
      THEN 'parsed but never split (> 12h)'
    -- Before the generic 24h arm, or it would shadow this more specific label.
    WHEN sftp_delivery_status = 'pending' AND archived_path IS NULL
         AND completed_at < current_timestamp() - INTERVAL 2 HOURS
      THEN 'split but not archived (crash between passes?)'
    WHEN sftp_delivery_status = 'pending'
         AND completed_at < current_timestamp() - INTERVAL 24 HOURS
      THEN 'awaiting sftp > 24h'
    -- Marcato manual ma senza confini disegnati: blocca la consegna del batch
    -- (v_funnel.n_delivery_blocked) e senza questo ramo bloccherebbe restando
    -- invisibile — 'manual' non compare in nessun altro arm.
    WHEN status = 'manual' AND (boundary_source IS NULL OR boundary_source <> 'manual')
      THEN 'marcato manual, in attesa di annotazione — blocca la consegna'
    WHEN needs_review AND sftp_delivery_status IS NULL
      THEN CONCAT('needs review (', COALESCE(boundary_source, '?'), ') — delivery blocked')
    WHEN status = 'pending' AND created_at < current_timestamp() - INTERVAL 2 HOURS
      THEN 'stuck in pending > 2h (never picked up by parse)'
  END AS stuck_reason,
  -- stuck_kind — la stessa scala di stuck_reason, arm per arm, nello stesso
  -- ordine, ma etichettata invece che raccontata. Il tab Errori ci mappa sopra
  -- l'azione da offrire: prima erano condizioni indipendenti nel JS, quindi su
  -- una riga comparivano insieme tutti i bottoni che matchavano — compreso
  -- quello che fa danno (retry_sftp su uno split fallito porta
  -- sftp_delivery_status a 'pending', non-NULL, e nb_pdf_split esclude per
  -- sempre tutto ciò che non è NULL).
  -- Tenere l'ordine allineato a stuck_reason: è ciò che garantisce che etichetta
  -- e frase non possano descrivere due cose diverse.
  -- Gemello di sql/lakebase_ddl.sql — cambiare sempre entrambi.
  CASE
    WHEN status = 'error' AND error_stage = 'parsing'   THEN 'parse_error'
    WHEN status = 'error' AND error_stage = 'pdf_split' THEN 'split_error'
    WHEN status = 'error'                               THEN 'error_other'
    WHEN status = 'skipped'                             THEN 'oversized'
    -- Il prefisso lo scrive nb_pdf_split quando il MERGE devia l'errore sul
    -- canale sftp per non far perdere lo status 'manual' al file annotato a
    -- mano. È l'unica cosa che distingue uno split fisico fallito da un upload
    -- fallito: entrambi sono sftp_delivery_status='failed'.
    WHEN sftp_delivery_status = 'failed'
         AND sftp_delivery_error LIKE 'pdf_split failed:%' THEN 'pdf_split_failed'
    WHEN sftp_delivery_status = 'failed'                THEN 'sftp_failed'
    WHEN sftp_delivery_status = 'deferred'              THEN 'sftp_deferred'
    WHEN status = 'parsing' AND started_at < current_timestamp() - INTERVAL 2 HOURS
      THEN 'stuck_parsing'
    WHEN status = 'parsed' AND completed_at < current_timestamp() - INTERVAL 12 HOURS
      THEN 'stuck_parsed'
    WHEN sftp_delivery_status = 'pending' AND archived_path IS NULL
         AND completed_at < current_timestamp() - INTERVAL 2 HOURS
      THEN 'not_archived'
    WHEN sftp_delivery_status = 'pending'
         AND completed_at < current_timestamp() - INTERVAL 24 HOURS
      THEN 'sftp_stale'
    WHEN status = 'manual' AND (boundary_source IS NULL OR boundary_source <> 'manual')
      THEN 'awaiting_manual'
    WHEN needs_review AND sftp_delivery_status IS NULL  THEN 'needs_review'
    WHEN status = 'pending' AND created_at < current_timestamp() - INTERVAL 2 HOURS
      THEN 'stuck_pending'
  END AS stuck_kind
FROM v_file_status
WHERE
     status = 'error'
  OR status = 'skipped'
  OR (status = 'manual' AND (boundary_source IS NULL OR boundary_source <> 'manual'))
  OR sftp_delivery_status IN ('failed', 'deferred')
  OR (status = 'parsing' AND started_at < current_timestamp() - INTERVAL 2 HOURS)
  OR (status = 'parsed' AND completed_at < current_timestamp() - INTERVAL 12 HOURS)
  OR (sftp_delivery_status = 'pending' AND archived_path IS NULL
      AND completed_at < current_timestamp() - INTERVAL 2 HOURS)
  OR (sftp_delivery_status = 'pending' AND completed_at < current_timestamp() - INTERVAL 24 HOURS)
  OR (needs_review AND sftp_delivery_status IS NULL)
  OR (status = 'pending' AND created_at < current_timestamp() - INTERVAL 2 HOURS);

-- ─────────────────────────────────────────────────────────────────────────────
-- v_sftp_board — delivery completeness per (day_id, folder_id).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_sftp_board AS
-- Counts are in PHYSICAL PDF units, not packages: each package (one row here)
-- produces n_documents split PDFs under {base}/{folder_id}/, and every PDF of a
-- package shares that package's delivery status. Weighting each package by
-- n_documents makes n_files = split PDFs in the folder and keeps the status
-- columns reconciling (delivered + pending + failed + deferred = n_files).
SELECT
  day_id, folder_id,
  SUM(COALESCE(n_documents, 0)) AS n_files,
  SUM(CASE WHEN sftp_delivery_status = 'delivered' THEN COALESCE(n_documents, 0) ELSE 0 END) AS n_delivered,
  SUM(CASE WHEN sftp_delivery_status = 'pending'   THEN COALESCE(n_documents, 0) ELSE 0 END) AS n_pending,
  SUM(CASE WHEN sftp_delivery_status = 'failed'    THEN COALESCE(n_documents, 0) ELSE 0 END) AS n_failed,
  SUM(CASE WHEN sftp_delivery_status = 'deferred'  THEN COALESCE(n_documents, 0) ELSE 0 END) AS n_deferred,
  MAX(sftp_delivered_at) AS last_delivered_at,
  MAX(sftp_target_folder) AS sftp_target_folder
FROM v_file_status
WHERE sftp_delivery_status IS NOT NULL
GROUP BY day_id, folder_id;

-- ─────────────────────────────────────────────────────────────────────────────
-- v_needs_review — [1]-fallback queue for the review page.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_needs_review AS
SELECT day_id, filename, folder_id, total_pages, predicted_starts, n_documents,
       model_used, boundary_source, processing_timestamp
FROM split_results
WHERE needs_review = true;

-- ─────────────────────────────────────────────────────────────────────────────
-- v_events_recent — activity feed (newest first, capped by the caller's LIMIT).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_events_recent AS
SELECT event_ts, day_id, run_id, stage, event_type, filename, folder_id,
       old_status, new_status, detail, error_message, actor
FROM pipeline_events
ORDER BY event_ts DESC;
