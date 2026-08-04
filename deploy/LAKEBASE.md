# Lakebase serving layer — architecture, setup log, operations

Since 2026-08-03 the app READS from **Lakebase Postgres 17** instead of the SQL
warehouse. Unity Catalog stays the **source of truth**: the pipeline notebooks
are untouched and keep writing Delta. Rollback = `LAKEBASE_ENABLED: "0"` in
app.yaml + redeploy (nothing else changes).

## Why

Measured 2026-08-03 (LOGISTICS warehouse, warm): every app query cost
450–850 ms flat via StatementExecution on ~23k total rows — pure API overhead.
Same queries on Lakebase through the app's own read layer: **33–39 ms p50**
(`/api/progress` SQL share: ~2.4 s → <200 ms).

## Topology

| Piece | Value |
|---|---|
| Project | `laplace-multidocument-cockpit` (uid 0e76fc13-433f-4be7-bd5a-324b2c2ac046) |
| Branch / endpoint | `production` / `primary` |
| Host | `ep-broad-flower-e29e9vb7.database.westeurope.azuredatabricks.net` |
| Database | `databricks_postgres` |
| Schemas | `"multidocument-prod"` = synced tables (read-only) · `laplace` = app views + native `evaluation_results` |

**Synced tables** (continuous, CDF on the Delta sources, PK upsert):

| PG table | Source (UC) | Primary key |
|---|---|---|
| `"multidocument-prod".processing_log_pg` | processing_log | (day_id, filename) |
| `"multidocument-prod".split_results_pg` | split_results | (day_id, filename) |
| `"multidocument-prod".pipeline_events_pg` | pipeline_events | (event_id) |
| `"multidocument-prod".gcs_llm_responses_pg` | gcs_llm_responses | (day_id, filename, stage, processing_timestamp) |

`laplace.evaluation_results` is **native Postgres** — the app is its only
writer (GT saves). The UC copy is frozen history as of the migration
(91 rows copied via `deploy/migrate_evaluation_results.py`, idempotent).

## How the app decides where to read

- `src/core/db.py::get_reader()` → `src/core/pg.py::PgClient` when
  `LAKEBASE_ENABLED=1`, else the warehouse `SqlClient`. Same
  `execute()/str_param()` interface; `:name` params auto-translated; every
  value stringified to StatementExecution's shape ('true'/'false',
  '[1, 5]', 'YYYY-MM-DD HH:MM:SS') so frontend + coercion helpers see no
  difference.
- Table names via `config.rq(name)`: bare (search_path=laplace) on PG,
  backticked UC name on the warehouse.
- **Writes to UC-owned tables NEVER move**: `actions.py`, `_log_event`,
  `manual._upsert_manual_split` stay on `get_sql()` + `config.fq()` — the
  notebooks read those rows from Delta (nb_pdf_split needs the manual
  split_results row in UC, not in Postgres).
- Auth: Lakebase native login is disabled; password = OAuth token from
  `postgres.generate_database_credential()`, cached 45 min, refreshed on
  OperationalError (see PgClient).

## What was provisioned (2026-08-03, all reproducible)

1. `ALTER TABLE ... SET TBLPROPERTIES (delta.enableChangeDataFeed=true)` on the
   4 source tables.
2. 4 synced tables via `w.postgres.create_synced_table` (CONTINUOUS,
   `synced_table_id = sbx-logistics.multidocument-prod.<table>_pg`).
3. `sql/lakebase_ddl.sql` applied: schema `laplace`, `evaluation_results`,
   8 view twins + 4 bare-name alias views.
4. 91 evaluation rows migrated (`deploy/migrate_evaluation_results.py`).
5. Roles for the app service principals (LAKEBASE_OAUTH_V1) + grants: USAGE on
   both schemas, SELECT on all tables, INSERT on `laplace.evaluation_results`,
   plus ALTER DEFAULT PRIVILEGES so future objects are covered:

   | PG role id | SP client id | Databricks App |
   |---|---|---|
   | `app-cockpit` | `fb442fb0-d35a-449d-be4f-1a82231af8ae` | **laplace-multidocument-cockpit** (the one actually running) |
   | `app-laplace-prod` | `13c04e3e-bf8b-4306-b012-047e7f7b3a1f` | laplace-prod-v4 |
   | `app-laplace-dev` | `64af4581-8f63-47d5-babf-7b860f662fd7` | laplace-dev-v4 |

   ⚠ **Every app that serves this repo needs its own PG role named after its SP
   client id.** A missing role is the classic first-deploy failure: the app
   cannot authenticate and every tab returns `internal error [id]` — the log
   line shows a psycopg OperationalError. The Postgres username used by the app
   is `DATABRICKS_CLIENT_ID` (see `pg_user()` in src/core/pg.py), so the role
   name must match that value exactly.

## Operations

- **View change**: `sql/views.sql` (warehouse) and `sql/lakebase_ddl.sql` (PG)
  are TWINS — change both, re-run each on its engine. The PG file is safe to
  re-run wholesale (CREATE OR REPLACE / IF NOT EXISTS).
- **Recreating a synced table drops its grants** — re-run the GRANT block for
  the SP roles afterwards (bottom of this file's provisioning list, or just
  re-run `GRANT SELECT ON ALL TABLES IN SCHEMA "multidocument-prod" TO "<sp>"`).
- **Sync lag**: seconds. A dashboard action (retry, mark-manual) writes UC and
  the next poll reads PG — a briefly stale status is normal, not a bug.
- **Rollback**: `LAKEBASE_ENABLED: "0"` + redeploy. Caveat: evaluations saved
  while enabled live ONLY in Postgres — after a rollback the gate metrics /
  dashboard won't see them until copied back (reverse of the migration script).
- **Cost knob**: the `primary` endpoint autoscales 8–16 CU with a 24h suspend
  timeout — generous for this workload; consider lowering min CU in the
  project settings if the bill says so.

## Adding a new synced table later

```python
w.postgres.create_synced_table(
    synced_table=SyncedTable(spec=SyncedTableSyncedTableSpec(
        branch="projects/laplace-multidocument-cockpit/branches/production",
        postgres_database="databricks_postgres",
        source_table_full_name="sbx-logistics.multidocument-prod.<table>",
        primary_key_columns=[...],
        scheduling_policy=CONTINUOUS,
        create_database_objects_if_missing=True)),
    synced_table_id="sbx-logistics.multidocument-prod.<table>_pg")
```
Remember: CDF on the source first, grants after.
