"""500 responses must not echo exception text: generic message + correlation
id to the client, full detail only in the server log."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask

from src.annotate import routes as annotate_routes
from src.pipeline import routes as pipeline_routes


def _client():
    app = Flask(__name__)
    app.register_blueprint(pipeline_routes.bp)
    app.register_blueprint(annotate_routes.bp)
    return app.test_client()


SECRET = "SECRET-SQL-DETAIL-42"


def _boom(*a, **k):
    raise RuntimeError(SECRET)


def test_pipeline_500_hides_exception_text(monkeypatch):
    monkeypatch.setattr(pipeline_routes.queries, "batch_status", _boom)
    resp = _client().get("/api/days")
    assert resp.status_code == 500
    err = resp.get_json()["error"]
    assert SECRET not in err
    assert re.search(r"internal error \[[0-9a-f]{8}\]", err)


def test_annotate_500_hides_exception_text(monkeypatch):
    monkeypatch.setattr(annotate_routes.annotation, "build_worklist", _boom)
    resp = _client().get("/api/annotate/worklist?day_id=20260707")
    assert resp.status_code == 500
    err = resp.get_json()["error"]
    assert SECRET not in err
    assert "internal error [" in err


def test_stack_trace_lands_in_log(monkeypatch, caplog):
    monkeypatch.setattr(pipeline_routes.queries, "stuck_files", _boom)
    client = _client()
    with caplog.at_level("ERROR"):
        resp = client.get("/api/errors")
    assert resp.status_code == 500
    eid = re.search(r"\[([0-9a-f]{8})\]", resp.get_json()["error"]).group(1)
    joined = "\n".join(r.getMessage() for r in caplog.records) + "".join(
        str(r.exc_text) for r in caplog.records if r.exc_text)
    assert eid in joined       # correlation id in the log line
    assert SECRET in joined    # full detail preserved server-side


def test_validation_400s_keep_precise_messages():
    resp = _client().get("/api/file/bad..name?day_id=20260707")
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid filename"
