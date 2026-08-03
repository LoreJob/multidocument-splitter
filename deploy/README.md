# deploy/ — create the two pipeline jobs and wire them to the app

Makes the control tower's **run-ingest / run-deliver** buttons actually launch the
pipeline. Three separate surfaces: Git folder (notebooks) · Jobs (the two jobs) ·
Databricks Apps (the running Flask app). Do them in order.

## 1. Import the repo as a Git folder (UI)

Databricks → Workspace → your user folder → **Create → Git folder** →
`https://github.com/loreluxottica/gt-app-laplace`, branch `main`.

Confirm where it landed and note the path to `algorithm-prod/`. Common defaults:
- `/Workspace/Repos/lorenzo.muscillo@luxottica.com/gt-app-laplace/algorithm-prod`
- `/Workspace/Users/lorenzo.muscillo@luxottica.com/gt-app-laplace/algorithm-prod`

The JSON in this folder assumes the **Repos** path. If yours differs, fix the
`notebook_path` values in `job_ingest.json` / `job_deliver.json` (find/replace the
prefix). `nb_helpers` must sit beside the task notebooks — the Git folder guarantees
that. `CLAUDE.md` / `run_local.py` are gitignored, so they won't appear (expected).

## 2. Create the jobs

Two options — both fine, serverless either way (notebook tasks with no cluster block
run on serverless; the notebooks pin their own env via the `/// script` header).

**A. Paste the JSON (CLI, needs a valid profile):**
```
export DATABRICKS_CONFIG_PROFILE=luxottica   # re-auth first: databricks auth login --profile luxottica
databricks jobs create --json @deploy/job_ingest.json
databricks jobs create --json @deploy/job_deliver.json
```
Each prints a `job_id`.

**B. Jobs UI (no CLI):** create two jobs matching the JSON —
- **job_ingest**: 3 serverless notebook tasks, sequential
  `nb_parse_documents → nb_split_documents → nb_check_export`.
  Job params: `run_id={{job.run_id}}`, `day_id` (empty), `sample_pct=10`.
- **job_deliver**: 2 serverless notebook tasks, sequential
  `nb_pdf_split → nb_sftp_upload`.
  Job params: `run_id={{job.run_id}}`, `day_id` (empty), `sftp_remote_base` (empty).

After creating in the UI, export each job's JSON back over these files so the repo
stays the source of truth (`databricks jobs get <id>` → the `settings` block).

Job ids (created 2026-07-21):
- job_ingest  = `909853340536600`
- job_deliver = `775787615266557`

## 3. Bind the jobs to the app (UI, then repo)

Nothing hardcoded — bind by resource, not id:
1. Apps UI → app → **Edit → App resources → Add → Job** → job_ingest, resource name
   `job-ingest`, permission **CAN_MANAGE_RUN**. Repeat for `job-deliver`.
   (Without CAN_MANAGE_RUN the app SP's `run_now` fails as a generic 500.)
2. In `app.yaml`, uncomment the two `valueFrom` env lines already prepared in the
   `JOB_INGEST_ID` / `JOB_DELIVER_ID` block.
3. Redeploy the app.

## Verify

- Jobs UI lists job_ingest + job_deliver.
- run-ingest on a real `day_id` → `{run_id, job:"ingest"}`, a run appears in the Jobs
  UI and the flow tab; no "not set" toast.
- Smoke: small batch in `inbox/{day_id}/`, `sample_pct=100`,
  ingest → annotate → gate unlocks → deliver.

Done: steps 1-3 completed 2026-07-21, verified by three batches delivered end to
end (`20260721`, `20260722`, `20260801`).

## 4. Keeping the workspace in sync (every change after the first deploy)

The three surfaces move independently — a merged PR changes none of them by itself.

| Changed | What to do | Who picks it up |
|---|---|---|
| `algorithm-prod/*.py` | Git folder → **Pull** (or `databricks workspace import-dir`) | the next job run |
| `src/`, `templates/`, `static/`, `app.yaml` | redeploy the app (`databricks apps deploy`) | the running app |
| `sql/views.sql` | re-run the whole file against the warehouse — every statement is `CREATE OR REPLACE VIEW`, so it is safe to re-run and touches no data | app **and** `nb_pipeline_status`, they share the views |
| `sql/ddl_*.sql` | run only in a new environment — the tables already exist | — |

Two traps: a notebook fix looks deployed after a merge but is not until the Pull,
and the app is *not* covered by the Pull. If a change spans both — as the
2026-08-03 manual-delivery fix did — do both, or the notebooks and the app will
disagree about which files are deliverable.
