"""Lakebase serving layer: param translation, StatementExecution-shaped
stringification, read dispatch, and the rq() name qualifier."""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core import db
from src.core.config import config
from src.core.pg import PgClient, pg_user, stringify, translate_params


class _FakeWorkspace:
    class _Me:
        user_name = "human@luxottica.com"

    class _CurrentUser:
        @staticmethod
        def me():
            return _FakeWorkspace._Me()

    current_user = _CurrentUser()


def test_pg_user_prefers_app_service_principal(monkeypatch):
    # In Databricks Apps the PG role is named after the SP client id.
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "fb442fb0-d35a-449d-be4f-1a82231af8ae")
    assert pg_user(_FakeWorkspace()) == "fb442fb0-d35a-449d-be4f-1a82231af8ae"


def test_pg_user_falls_back_to_me(monkeypatch):
    monkeypatch.delenv("DATABRICKS_CLIENT_ID", raising=False)
    assert pg_user(_FakeWorkspace()) == "human@luxottica.com"


def test_translate_named_params():
    assert translate_params("WHERE day_id = :day AND filename = :f") == \
        "WHERE day_id = %(day)s AND filename = %(f)s"
    # LIKE wildcards and quoted literals must survive untouched
    assert translate_params("WHERE status = 'manual' AND f LIKE :q LIMIT 30") == \
        "WHERE status = 'manual' AND f LIKE %(q)s LIMIT 30"


def test_stringify_matches_statement_execution_shape():
    assert stringify(None) is None
    assert stringify(True) == "true" and stringify(False) == "false"
    assert stringify(7) == "7"
    assert stringify(0.25) == "0.25"
    assert stringify([1, 5, 9]) == "[1, 5, 9]"
    ts = stringify(datetime(2026, 8, 1, 10, 23, 45))
    assert ts == "2026-08-01 10:23:45"
    assert ts[11:19] == "10:23:45"  # the JS event feed slices exactly this


def test_str_param_tuple_shape():
    assert PgClient.str_param("day", "20260801") == ("day", "20260801")
    assert PgClient.str_param("x", None) == ("x", None)
    assert PgClient.str_param("n", 5) == ("n", "5")


def test_get_reader_dispatch(monkeypatch):
    wh, pg = object(), object()
    monkeypatch.setattr(db, "_client", wh)
    monkeypatch.setattr(config, "LAKEBASE_ENABLED", False)
    assert db.get_reader() is wh

    import src.core.pg as pgmod
    monkeypatch.setattr(pgmod, "_client", pg)
    monkeypatch.setattr(config, "LAKEBASE_ENABLED", True)
    assert db.get_reader() is pg


def test_rq_follows_flag(monkeypatch):
    # rq() is a classmethod: patch the class, not the instance
    monkeypatch.setattr(type(config), "LAKEBASE_ENABLED", False)
    assert config.rq("v_funnel") == "`sbx-logistics`.`multidocument-prod`.`v_funnel`"
    monkeypatch.setattr(type(config), "LAKEBASE_ENABLED", True)
    assert config.rq("v_funnel") == "v_funnel"


def test_eval_array_literal_dialects():
    from src.annotate.annotation import _int_array_literal
    assert _int_array_literal([1, 5, 9]) == "array(1, 5, 9)"
    assert _int_array_literal([1, 5, 9], pg=True) == "ARRAY[1, 5, 9]"
    assert _int_array_literal([], pg=True) == "ARRAY[]::integer[]"
