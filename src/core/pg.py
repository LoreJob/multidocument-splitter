"""Lakebase Postgres serving-layer client (read path + evaluation_results).

Same execute()/str_param() contract as core.db.SqlClient so the query modules
work unchanged behind core.db.get_reader():

  * statements keep their `:name` parameter style — translated to psycopg's
    `%(name)s` here (the repo never uses `::` casts, so a plain regex is safe);
  * rows come back as list[dict] with every value STRINGIFIED exactly like
    StatementExecution returns them (bools 'true'/'false', timestamps
    'YYYY-MM-DD HH:MM:SS', arrays '[1, 5]'). The frontend (`r.needs_review ===
    "true"`, `event_ts.slice(11, 19)`) and the _to_int/_to_bool/_parse_int_array
    helpers all rely on that shape — native types would silently break them.

Auth: Lakebase native login is disabled on the project — the Postgres password
is a short-lived OAuth token from postgres.generate_database_credential().
Tokens last ~1h; we refresh after TOKEN_TTL_S and, as a belt, retry once with a
fresh connection when an OperationalError smells like an expired credential.

Pooling: a tiny lock-guarded free-list (gunicorn worker = 4 threads at most in
practice). Connections are autocommit — reads plus single-row INSERTs only.
"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import date, datetime

from .config import config

_PARAM = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)")
_TOKEN_TTL_S = 45 * 60          # refresh well before the ~1h expiry
_POOL_MAX = 4


def pg_user(workspace) -> str:
    """The Postgres role name for the current identity.

    Inside Databricks Apps the caller is a service principal and its Lakebase
    role is named after the SP's client id — which the platform injects as
    DATABRICKS_CLIENT_ID. Trust that first; me().user_name is the fallback for
    local runs (a human, whose role is their email).
    """
    sp = os.environ.get("DATABRICKS_CLIENT_ID", "").strip()
    if sp:
        return sp
    return workspace.current_user.me().user_name


def translate_params(statement: str) -> str:
    """`WHERE day_id = :day` -> `WHERE day_id = %(day)s`."""
    return _PARAM.sub(r"%(\1)s", statement)


def stringify(v):
    """Match StatementExecution's all-strings row shape."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, list):
        return "[" + ", ".join(str(x) for x in v) + "]"
    return str(v)


class PgClient:
    def __init__(self):
        import psycopg  # imported lazily: only needed when LAKEBASE_ENABLED

        from databricks.sdk import WorkspaceClient

        self._psycopg = psycopg
        self._w = WorkspaceClient()
        self._user = pg_user(self._w)
        self._lock = threading.Lock()
        self._pool: list = []
        self._token: str | None = None
        self._token_ts = 0.0

    @property
    def workspace(self):
        """Parity with SqlClient — jobs.py reaches the Jobs API through here."""
        return self._w

    # ── auth ───────────────────────────────────────────────────────────────
    def _fresh_token(self) -> str:
        with self._lock:
            if self._token and (time.time() - self._token_ts) < _TOKEN_TTL_S:
                return self._token
            cred = self._w.postgres.generate_database_credential(
                endpoint=config.LAKEBASE_ENDPOINT
            )
            self._token = cred.token
            self._token_ts = time.time()
            return self._token

    def _connect(self):
        return self._psycopg.connect(
            host=config.LAKEBASE_HOST,
            dbname=config.LAKEBASE_DB,
            user=self._user,
            password=self._fresh_token(),
            sslmode="require",
            connect_timeout=15,
            autocommit=True,
            options=f"-c search_path={config.LAKEBASE_SCHEMA},public",
        )

    # ── tiny pool ──────────────────────────────────────────────────────────
    def _acquire(self):
        with self._lock:
            if self._pool:
                return self._pool.pop()
        return self._connect()

    def _release(self, conn) -> None:
        with self._lock:
            if len(self._pool) < _POOL_MAX and not conn.closed:
                self._pool.append(conn)
                return
        try:
            conn.close()
        except Exception:  # noqa: BLE001 — closing a dead conn must not raise
            pass

    # ── executor (SqlClient-compatible) ────────────────────────────────────
    def execute(self, statement: str, parameters: list | None = None) -> list[dict]:
        stmt = translate_params(statement)
        args = {}
        for p in parameters or []:
            # accepts both this module's ('name', value) tuples and
            # StatementParameterListItem objects, so call sites can share params
            name = getattr(p, "name", None)
            if name is not None:
                args[name] = p.value
            else:
                args[p[0]] = p[1]

        last_err = None
        for attempt in (0, 1):
            conn = self._acquire()
            try:
                with conn.cursor() as cur:
                    cur.execute(stmt, args or None)
                    if cur.description is None:
                        self._release(conn)
                        return []
                    cols = [d.name for d in cur.description]
                    rows = [
                        {c: stringify(v) for c, v in zip(cols, row)}
                        for row in cur.fetchall()
                    ]
                self._release(conn)
                return rows
            except self._psycopg.OperationalError as e:
                # dead/expired connection: drop it, force a token refresh once
                last_err = e
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                with self._lock:
                    self._token = None
                if attempt == 1:
                    raise
            except Exception:
                self._release(conn)
                raise
        raise last_err  # pragma: no cover — loop always returns or raises

    @staticmethod
    def str_param(name: str, value):
        return (name, None if value is None else str(value))


_client: PgClient | None = None
_client_lock = threading.Lock()


def get_pg() -> PgClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = PgClient()
    return _client
