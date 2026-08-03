"""run-deliver gate guard: an opened gate with a vanished sample must 409,
not launch the job (fail-closed for the volumes.list_stems NotFound swallow)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask

from src.pipeline import routes


def _client():
    app = Flask(__name__)
    app.register_blueprint(routes.bp)
    return app.test_client()


def _post(client, day="20260707"):
    return client.post("/api/run-deliver",
                       json={"day_id": day, "sftp_remote_base": "/Laplace/US/x"})


def _gate(n_sampled, n_annotated, complete):
    return {"day_id": "20260707", "n_sampled": n_sampled,
            "n_annotated": n_annotated,
            "missing": [], "complete": complete, "metrics": None}


def test_gate_opened_but_sample_vanished_is_409(monkeypatch):
    monkeypatch.setattr(routes.gate, "gate_state", lambda d: _gate(0, 0, False))
    monkeypatch.setattr(routes.queries, "batch_status",
                        lambda d=None: [{"day_id": d, "gate_opened": "true"}])
    launched = []
    monkeypatch.setattr(routes.jobs, "run_deliver",
                        lambda d, r: launched.append(d) or {"run_id": 1, "job": "deliver"})
    resp = _post(_client())
    assert resp.status_code == 409
    assert launched == []
    assert "refusing" in resp.get_json()["error"]


def test_never_sampled_batch_still_delivers(monkeypatch):
    monkeypatch.setattr(routes.gate, "gate_state", lambda d: _gate(0, 0, False))
    monkeypatch.setattr(routes.queries, "batch_status",
                        lambda d=None: [{"day_id": d, "gate_opened": None}])
    monkeypatch.setattr(routes.jobs, "run_deliver",
                        lambda d, r: {"run_id": 7, "job": "deliver"})
    resp = _post(_client())
    assert resp.status_code == 200
    assert resp.get_json()["launched"] is True


def test_incomplete_gate_still_409(monkeypatch):
    monkeypatch.setattr(routes.gate, "gate_state", lambda d: _gate(6, 4, False))
    resp = _post(_client())
    assert resp.status_code == 409
    assert "4/6" in resp.get_json()["error"]


def test_complete_gate_delivers(monkeypatch):
    monkeypatch.setattr(routes.gate, "gate_state", lambda d: _gate(6, 6, True))
    monkeypatch.setattr(routes.jobs, "run_deliver",
                        lambda d, r: {"run_id": 9, "job": "deliver"})
    resp = _post(_client())
    assert resp.status_code == 200
