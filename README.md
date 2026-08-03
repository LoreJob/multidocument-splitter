# Laplace Multidocument — Control Tower + Ground Truth

One web app that drives the **multi-document splitting** pipeline end to end and
carries the human annotation step inside it.

Concatenated multi-document PDF "packages" arrive from logistics. The pipeline
parses them, an LLM predicts document boundaries, a human annotates a sample
(ground truth), the PDFs are physically split and delivered to SFTP. This app is
where an operator watches, annotates and drives all of that.

Runs as a **Databricks App** (Flask, port 8000). Vanilla JS, no framework, no
build step, no CDN. Light + dark theme.

**Live in production** since 2026-07-21 against `sbx-logistics`.`multidocument-prod`.
Batches delivered end to end so far: `20260721` (100 packages), `20260722` (100),
`20260801` (1480 packages → 2871 split PDFs, 94 of them hand-annotated).

> Until 2026-07-17 this was two apps — the annotator on :8000 and a control tower
> on :8001, linked only by a `window.open()` to the annotator's root. Because the
> annotator then picked its own batch, you could land on a different one than you
> clicked from. They are now a single app, and the topbar's batch selector drives
> every tab, annotation included.

## The eight tabs

| Tab | What |
|---|---|
| **Batches** | every `inbox/{day_id}/` folder with its lifecycle badge and contextual actions |
| **Live Flow** | animated funnel (inbox → parsed → predicted → ground truth → split → delivered), polls every 4s while a job runs; errors show as red counters on the failing stage |
| **Annotation Gate** | sample progress, model-vs-GT metrics, unlocks delivery |
| **Annotate** | the annotation UI, in two modes: **Sample** (scored) and **Manual** — see below |
| **Errors** | every failure, with its reason and a requeue button |
| **SFTP** | delivery board + deferred re-delivery to a different remote path |
| **Review** | packages where both LLMs failed and would ship UNSPLIT — approve or requeue |
| **Files** | search, per-file status, event timeline, raw LLM responses |

A batch flows: `uploaded → parsing → predicted → awaiting_annotation → annotated
→ delivering → delivered`. Every action is written to `pipeline_events` with an
actor — nothing the app does is silent or unattributed.

## The annotation gate

The sample lands in `validation/{day_id}/`. Every sampled file needs a
`ground_truth/{day_id}/{f}.json` before delivery is allowed. **The gate counts
those JSONs**, so annotating a file in the Annotate tab moves the gate counter,
the batch lifecycle, and the Split & upload button in the same page. Ask to
deliver early and the server answers `409` and drops you on the Gate tab.

## What the annotator does

1. Picks a PDF from the worklist (the batch comes from the topbar selector).
2. Clicks each page that is the **first page of a new document**. Page 1 is
   always a start.
3. Toggles **Multi-document** (auto-set from boundary count, overridable).
4. **Save & Evaluate** → writes the GT JSON and shows precision / recall / F1
   (exact and ±1-tolerant) plus exact-match and multidoc-correct flags.

**Operator-blind:** the model's predicted boundaries are never sent to the
annotation page, so the labelling is unbiased. The comparison happens
server-side at save time, and metrics appear only *after* the operator commits.

Pages are rendered server-side to JPEG by PyMuPDF and lazy-loaded as they scroll
into view — no PDF.js, no CDN. Unsaved marks are guarded against tab switches,
batch changes and page unload.

## Manual mode — rescuing files the pipeline can't handle

A file that fails `ai_parse_document` never gets a prediction, so it is never
sampled and would never ship. Mark it **manual** in the Errors tab and it appears
in the Annotate tab's **Manual** mode, rendered straight from the raw PDF in
`inbox/` (PyMuPDF renders fine even when the LLM parse failed). Saving there
writes the ground-truth JSON **and** a `split_results` row with
`boundary_source='manual'`, which is what makes the file deliverable. No
`evaluation_results` row is written — manual boundaries are never scored, so
they cannot flatter or dent the model's metrics.

`status='manual'` therefore means two different things, and only one of them ships:

| | marked manual in Errors, **not** annotated | marked manual **and** hand-annotated |
|---|---|---|
| `boundary_source='manual'` row | no | yes |
| split by `nb_pdf_split` | no | yes |
| uploaded by `nb_sftp_upload` | no | yes |
| meaning | handled outside the pipeline | deliverable, just not model-predicted |

Everything downstream keys off `boundary_source`, never off the status alone.
A hand-annotated file keeps `status='manual'` all the way through to
`delivered` — do not flip it to `done` to force a delivery, it only removes the
file from the manual worklist. Files marked manual also drop out of the
annotation gate's sample: their PDF stays in `validation/` (marking cannot
delete from a volume), so counting them would deadlock delivery at `409`.

## `/dashboard`

Aggregates `evaluation_results` into headline numbers — PDFs annotated, exact
split matches + rate, multidoc-correct rate, off-by-one count, and average
precision / recall / F1. Exports a **PNG** (vendored `html2canvas`, no CDN) and a
**standalone HTML** file for slides. It is a separate page rather than a tab
because html2canvas cannot resolve `prefers-color-scheme` — the export needs a
pinned colour context.

## Data produced

**`ground_truth/{day_id}/{filename}.json`**
```json
{
  "filename": "34572_PACKAGE",
  "folder_id": "34572",
  "day_id": "20260714",
  "total_pages": 12,
  "is_multidoc": true,
  "predicted_starts": [1, 5, 9],
  "n_documents": 3,
  "documents": [
    {"start": 1, "end": 4, "type": null},
    {"start": 5, "end": 8, "type": null},
    {"start": 9, "end": 12, "type": null}
  ],
  "annotator": "lorenzo.muscillo@luxottica.com",
  "annotated_at": "2026-06-25T10:00:00+00:00",
  "schema_version": 1
}
```
`documents[].type` is reserved `null` — v2 will let annotators tag document types
(Invoice, AWB, …) without changing the boundary model.

**`evaluation_results`** — one row per save. See [sql/ddl_evaluation_results.sql](sql/ddl_evaluation_results.sql).

## Evaluation metric

A boundary = first page of a document. Page 1 is excluded from precision/recall/F1
(it is always a trivial boundary both sides agree on); `exact_match` compares the
full sets including page 1.

- **Exact**: a predicted boundary is correct only if its page is in the GT set.
- **Tolerant (±1)**: a predicted boundary within one page of an unused GT boundary
  counts as a match (greedy nearest). `n_offby1` = how many matches were off-by-one.
- **multidoc_correct**: `(gt_n_docs > 1) == (model_n_docs > 1)`.

## Layout

```
app.py                      registers two blueprints, serves / and /dashboard
app.yaml                    Databricks Apps config (env + resources)
src/core/       config.py   ONE Config: fq(), volume_path(), every volume NAME
                db.py       SQL Warehouse access (SDK StatementExecution)
                volumes.py  UC Volume access (SDK Files API)
                auth.py     caller identity + can_operate() (the roles hook)
                http.py     shared request validation
src/pipeline/   routes.py   /api/*  — batches, jobs, gate, errors, sftp, review
                queries.py  thin SELECTs over the shared sql/views.sql
                actions.py  mutations, each one an audited pipeline_events row
                jobs.py     job_ingest / job_deliver launch + poll
                gate.py     the annotation gate
src/annotate/   routes.py   /api/annotate/* — worklist, page JPEGs, save
                annotation.py  worklist, model lookup, page render, GT, eval
                evaluation.py  boundary metrics (pure, unit-tested)
                manual.py      manual mode: GT + the boundary_source='manual'
                               split row that makes a file deliverable
templates/      control_tower.html   the SPA shell (8 tabs)
                dashboard.html       standalone export page
static/         css/control_tower.css   one theme, light + dark
                js/control_tower.js     shell: tabs, setDay, polling
                js/annotate.js          the annotate tab (IIFE)
                js/dashboard.js         stats + PNG/HTML export
sql/            table DDL + the shared v_* views
algorithm-prod/ the Databricks notebooks (see JOBS.md)
```

## Setup

1. **Create the tables** (once): run `sql/ddl_prod_schema.sql` first, then
   `sql/ddl_pipeline_events.sql`, `sql/ddl_evaluation_results.sql`, `sql/views.sql`.
2. **Configure** [app.yaml](app.yaml): `DATABRICKS_WAREHOUSE_ID`, `UC_CATALOG` /
   `UC_SCHEMA`, `JOB_INGEST_ID`, `JOB_DELIVER_ID`, `DASHBOARD_ACTOR`.
3. **Grant the app's service principal**:
   - `CAN_USE` on the SQL Warehouse,
   - `SELECT` on the pipeline tables, `INSERT`/`UPDATE` where the app writes,
   - `READ VOLUME` on `inbox/` `validation/` `archive/`, `READ/WRITE VOLUME` on `ground_truth/`,
   - **`CAN_MANAGE_RUN` on `job_ingest` and `job_deliver`** — the app launches
     them as itself, not as you.
4. **Deploy**: `databricks apps deploy` (or the Apps UI pointing at this folder).

## Local development

```bash
export DATABRICKS_HOST=https://<workspace>.azuredatabricks.net
export DATABRICKS_TOKEN=<pat>
export DATABRICKS_WAREHOUSE_ID=<warehouse-id>
python app.py            # http://127.0.0.1:8000
```

`python -m pytest tests/` covers the boundary metrics (`test_evaluation`), the
manual path and the annotation gate (`test_manual`), and the page render cache
(`test_render_cache`). Each file also runs standalone: `python tests/test_manual.py`.

**After changing anything under `src/`, redeploy the app** — re-syncing the
notebooks does not cover the Flask side. After changing `sql/views.sql`, re-run
it against the warehouse: the views are the one definition shared by the app and
`nb_pipeline_status`.

## Notes / future work

- **Roles**: `src/core/auth.can_operate()` returns `True` today and gates the four
  mutating pipeline routes. Set `OPERATOR_EMAILS` and flip its one-line body to
  restrict job launching to operators; annotation stays open.
- **Types per document** (Invoice/AWB/…) — the schema already carries a `type` slot.
- **Concurrency**: GT files are keyed by `(day_id, filename)`; last save wins.
  Fine for a small review team; add optimistic locking if reviewers share a file.
- Page rendering (`PAGE_ZOOM` / `JPEG_QUALITY` in `src/annotate/annotation.py`)
  keeps a bounded per-worker PDF cache. It is not thread-safe — safe under
  gunicorn's sync workers, but `--threads` would need a render lock first.
