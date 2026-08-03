"""POST /api/annotate/save + /manual/save input validation: malformed bodies
must 400 with a precise message, never 500, and never silently drop
out-of-range boundaries or path-hostile folder_ids."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask

from src.annotate import routes


def _client(monkeypatch):
    # Stub every side effect: validation failures must return before these,
    # and the happy path must not need a warehouse.
    monkeypatch.setattr(routes.annotation, "get_model_prediction",
                        lambda name, day=None: None)
    monkeypatch.setattr(routes.annotation, "save_ground_truth",
                        lambda payload: f"/gt/{payload['filename']}.json")
    monkeypatch.setattr(routes.annotation, "run_and_store_evaluation",
                        lambda payload, model: {"model_prediction_missing": True})
    monkeypatch.setattr(routes.manual, "save_manual",
                        lambda **kw: {"saved": True, "kw": {k: kw[k] for k in
                                                            ("starts", "folder_id")}})
    app = Flask(__name__)
    app.register_blueprint(routes.bp)
    return app.test_client()


BODY = {"filename": "F123_pkg", "day_id": "20260707",
        "total_pages": 10, "predicted_starts": [1, 5], "is_multidoc": True}


def _post(client, path="/api/annotate/save", **over):
    return client.post(path, json={**BODY, **over})


def test_valid_save_succeeds(monkeypatch):
    resp = _post(_client(monkeypatch))
    assert resp.status_code == 200
    assert resp.get_json()["saved"] is True


def test_non_numeric_start_is_400_not_500(monkeypatch):
    resp = _post(_client(monkeypatch), predicted_starts=[1, "abc"])
    assert resp.status_code == 400
    assert "predicted_starts" in resp.get_json()["error"]


def test_out_of_range_start_rejected_not_dropped(monkeypatch):
    resp = _post(_client(monkeypatch), predicted_starts=[1, 12])  # 10 pages
    assert resp.status_code == 400
    assert "between 1 and 10" in resp.get_json()["error"]


def test_non_numeric_total_pages_is_400(monkeypatch):
    resp = _post(_client(monkeypatch), total_pages="ten")
    assert resp.status_code == 400


def test_path_hostile_folder_id_rejected(monkeypatch):
    resp = _post(_client(monkeypatch), folder_id="../../etc")
    assert resp.status_code == 400
    assert "folder_id" in resp.get_json()["error"]


def test_manual_save_same_rules(monkeypatch):
    client = _client(monkeypatch)
    ok = _post(client, path="/api/annotate/manual/save")
    assert ok.status_code == 200
    bad = _post(client, path="/api/annotate/manual/save", predicted_starts=[0])
    assert bad.status_code == 400
    hostile = _post(client, path="/api/annotate/manual/save", folder_id="a/b")
    assert hostile.status_code == 400
