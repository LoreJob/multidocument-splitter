# Databricks Jobs — multidocument pipeline

Two jobs replace the old single 4-task job. The human annotation gate sits between them:
`job_ingest` produces predictions + the annotation sample; the operator annotates in the
Ground Truth app; the control tower unlocks `job_deliver` when the gate is complete.

Both jobs exist and have run in production since 2026-07-21 (ids and wiring in
[../deploy/README.md](../deploy/README.md)).

## job_ingest  (parse → split → check_export)

Tasks (each a notebook task, sequential dependency):
1. `nb_parse_documents`
2. `nb_split_documents`
3. `nb_check_export`

Job parameters (inherited by all tasks as widgets):
| name | value | note |
|------|-------|------|
| `run_id` | `{{job.run_id}}` | shared execution id — verify reference syntax in your Jobs UI version |
| `day_id` | (required, no default) | inbox/{day_id}/ batch — typed/selected in the control tower |
| `sample_pct` | `10` | annotation sample %, overridable per run from the control tower |

## job_deliver  (pdf_split → sftp_upload)

Tasks:
1. `nb_pdf_split`
2. `nb_sftp_upload`

Job parameters:
| name | value | note |
|------|-------|------|
| `run_id` | `{{job.run_id}}` | |
| `day_id` | (required, no default) | same batch as the ingest run |
| `sftp_remote_base` | (required, no default) | full remote path, typed by the user per delivery (e.g. `/Laplace/LAPLACE/US/20260711`). Preflight aborts if the path doesn't exist. Missing `{base}/{folder_id}/` subfolders → files marked `deferred`, re-deliverable later to another base. |

## What gets delivered

`nb_pdf_split` selects from `split_results`, `nb_sftp_upload` from
`processing_log` where `sftp_delivery_status='pending'`. Two kinds of file are
eligible, and the notebooks must agree on both or files sit `pending` forever:

- **the automatic flow** — `status='done'`;
- **hand-annotated files** — `status='manual'` **and** a `split_results` row with
  `boundary_source='manual'` (written by the app's Manual mode, never by a
  notebook). A ground-truth JSON always overrides the model's boundaries.

A file merely marked manual from the Errors tab has no such row: it is handled
outside the pipeline and is deliberately excluded from both notebooks, so it
never becomes a `pending` row nobody will upload. `needs_review` files (both LLMs
failed → `[1]`) stay blocked until approved in the dashboard or annotated.

> Regression to avoid (fixed 2026-08-03): `nb_sftp_upload` used to filter
> `AND status = 'done'` alone, which silently dropped every hand-annotated file
> after `nb_pdf_split` had already split it and marked it `pending` — 94 files on
> batch `20260801`. If you touch either query, keep the two in sync.

## Before first run (P0 — one-time)

Target schema: `sbx-logistics`.`multidocument-prod` (greenfield). `multidocument-us` is retired.
Steps 1-4 were done 2026-07-17, step 5 on 2026-07-21 — this section is kept as the
recipe for a new environment, not as an open to-do.

1. Run `sql/ddl_prod_schema.sql` (creates the 7 pipeline tables, day_id native).
2. Run `sql/ddl_pipeline_events.sql` (creates `pipeline_events`).
3. Run `sql/ddl_evaluation_results.sql` (creates `evaluation_results`).
4. Run `sql/views.sql` (creates the v_* views).
5. Sync this folder to the workspace (`databricks workspace import-dir algorithm-prod /path/...`)
   — `nb_helpers.py` must sit next to the task notebooks (they `%run ./nb_helpers`).
6. Uploader convention: PDFs are dropped in `inbox/{day_id}/` (e.g. `inbox/20260707/`).
   The pipeline propagates day_id to oversized/, quarantine/, validation/, ground_truth/,
   archive/, output/ — all created by code; only inbox/{day_id}/ is made by hand.

## Notes

- `nb_pipeline_status` is a read-only monitor; the control tower app supersedes it.
- Requeue semantics (used by the control tower) are implemented in
  [`src/pipeline/actions.py`](../src/pipeline/actions.py) — `retry-parse`,
  `retry-split`, `retry-sftp`, `mark-manual`, `approve-review`, each writing its
  own `pipeline_events` row. (The old `pipeline-dashboard/` folder was merged into
  the root app on 2026-07-17 and no longer exists.)
- After editing any notebook here, re-sync the workspace copy — a Git folder pull
  or `databricks workspace import-dir`. Nothing picks up a local edit on its own.
