"""run-deliver must refuse while files still need a human.

Marking a file manual is NOT enough to unblock it: it has to be split by hand
(boundary_source='manual'). A failed or deferred SFTP upload must never block,
or the retry that repairs it would be locked out for good.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pipeline import queries, routes


class FakeReader:
    """Captures the statement and replays canned rows."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.statements = []

    def execute(self, statement, parameters=None):
        self.statements.append(" ".join(statement.split()))
        return self.rows

    @staticmethod
    def str_param(name, value):
        return (name, value)


@pytest.fixture
def app():
    from flask import Flask

    a = Flask(__name__)
    a.register_blueprint(routes.bp)
    return a


def _post(app, monkeypatch, blockers, **stubs):
    monkeypatch.setattr(routes.queries, "delivery_blockers", lambda day, **k: blockers)
    monkeypatch.setattr(routes.gate, "gate_state",
                        lambda day: stubs.get("gate", {"n_sampled": 2, "n_annotated": 2,
                                                       "complete": True, "missing": []}))
    monkeypatch.setattr(routes.auth, "can_operate", lambda: True)
    monkeypatch.setattr(routes.jobs, "run_deliver",
                        lambda day, remote: {"run_id": 1, "job": "deliver"})
    with app.test_client() as c:
        return c.post("/api/run-deliver",
                      json={"day_id": "20260501", "sftp_remote_base": "/out"})


def test_blocked_when_files_need_hand_splitting(app, monkeypatch):
    r = _post(app, monkeypatch, [{"filename": "BIG_1", "status": "skipped"},
                                 {"filename": "BAD_2", "status": "error"}])
    assert r.status_code == 409
    body = r.get_json()
    assert body["blocked_files"] == ["BIG_1", "BAD_2"]
    assert "Manual" in body["error"]


def test_marking_manual_alone_does_not_unblock(app, monkeypatch):
    """The whole point of the stricter rule: 'manual' without hand-drawn
    boundaries still blocks."""
    r = _post(app, monkeypatch, [{"filename": "BIG_1", "status": "manual"}])
    assert r.status_code == 409


def test_delivers_once_nothing_is_blocking(app, monkeypatch):
    r = _post(app, monkeypatch, [])
    assert r.status_code == 200
    assert r.get_json()["launched"] is True


def test_blockers_query_ignores_delivery_status(monkeypatch):
    """A failed/deferred upload must not appear: blocking it would deadlock the
    retry. The predicate keys on status and boundary_source only."""
    fake = FakeReader()
    monkeypatch.setattr(queries, "get_reader", lambda: fake)
    queries.delivery_blockers("20260501")
    sql = fake.statements[0]
    assert "status IN ('error', 'skipped', 'manual')" in sql
    assert "boundary_source <> 'manual'" in sql
    assert "sftp_delivery_status" not in sql


def test_blockers_query_is_parameterised(monkeypatch):
    """day_id is bound, never interpolated — same rule as every other query."""
    fake = FakeReader()
    monkeypatch.setattr(queries, "get_reader", lambda: fake)
    queries.delivery_blockers("20260501")
    assert "20260501" not in fake.statements[0]
    assert ":day" in fake.statements[0]
