"""mark-manual: the delivered-guard must ride on every path, and the bulk path
must stay batched (3 statements per chunk, never 2 per file)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pipeline import actions


class FakeSql:
    """Records statements; answers the eligibility SELECT with `eligible`
    (default: every filename bound in the statement)."""

    def __init__(self, eligible=None):
        self.statements = []
        self.calls = []
        self._eligible = eligible

    def execute(self, statement, parameters=None):
        flat = " ".join(statement.split())
        self.statements.append(flat)
        self.calls.append((flat, list(parameters or [])))
        if flat.startswith("SELECT filename"):
            bound = [v for k, v in (parameters or []) if k.startswith("f")]
            names = bound if self._eligible is None else self._eligible
            return [{"filename": f} for f in names]
        return []

    @staticmethod
    def str_param(name, value):
        return (name, value)


def test_mark_manual_update_excludes_delivered(monkeypatch):
    fake = FakeSql()
    monkeypatch.setattr(actions, "get_sql", lambda: fake)
    monkeypatch.setattr(actions, "actor", lambda: "tester@local")

    msg = actions.mark_manual("20260707", "F1_pkg")

    assert msg == "marked manual"
    update = next(s for s in fake.statements if "SET status = 'manual'" in s)
    assert "sftp_delivery_status IS NULL" in update
    assert "<> 'delivered'" in update


def _patch(monkeypatch, fake):
    monkeypatch.setattr(actions, "get_sql", lambda: fake)
    monkeypatch.setattr(actions, "actor", lambda: "tester@local")
    return fake


def test_bulk_is_three_statements_regardless_of_size(monkeypatch):
    fake = _patch(monkeypatch, FakeSql())
    n = actions.mark_manual_bulk("20260707", [f"F{i}" for i in range(50)])

    assert n == 50
    # SELECT eligible + UPDATE + one multi-row event INSERT. Never 2 per file.
    assert len(fake.statements) == 3
    assert sum(1 for s in fake.statements if s.startswith("UPDATE")) == 1
    assert sum(1 for s in fake.statements if s.startswith("INSERT INTO")) == 1


def test_bulk_guard_on_select_and_update(monkeypatch):
    fake = _patch(monkeypatch, FakeSql())
    actions.mark_manual_bulk("20260707", ["a", "b"])

    sel = next(s for s in fake.statements if s.startswith("SELECT filename"))
    upd = next(s for s in fake.statements if s.startswith("UPDATE"))
    for stmt in (sel, upd):
        assert "sftp_delivery_status IS NULL" in stmt
        assert "<> 'delivered'" in stmt
        assert "filename IN (:f0, :f1)" in stmt      # bound, never interpolated


def test_bulk_counts_and_logs_only_eligible_files(monkeypatch):
    # 'b' is already delivered → the SELECT does not return it
    fake = _patch(monkeypatch, FakeSql(eligible=["a", "c"]))
    n = actions.mark_manual_bulk("20260707", ["a", "b", "c"])

    assert n == 2                                   # requested 3, flipped 2
    insert, params = next((s, p) for s, p in fake.calls if s.startswith("INSERT INTO"))
    assert insert.count("(:eid") == 2               # one event row per marked file
    logged = sorted(v for k, v in params if k.startswith("ef"))
    assert logged == ["a", "c"]                     # no event for the skipped file


def test_bulk_chunks_large_selections(monkeypatch):
    fake = _patch(monkeypatch, FakeSql())
    monkeypatch.setattr(actions, "_BULK_CHUNK", 20)

    n = actions.mark_manual_bulk("20260707", [f"F{i}" for i in range(45)])

    assert n == 45
    assert len(fake.statements) == 9                # 3 chunks × 3 statements


def test_bulk_no_eligible_files_writes_nothing(monkeypatch):
    fake = _patch(monkeypatch, FakeSql(eligible=[]))
    n = actions.mark_manual_bulk("20260707", ["already_delivered"])

    assert n == 0
    assert len(fake.statements) == 1                # only the SELECT ran
    assert not any(s.startswith("UPDATE") for s in fake.statements)
