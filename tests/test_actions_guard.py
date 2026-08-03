"""mark_manual must carry the delivered-guard in its UPDATE: a delivered file
stays delivered even if someone bulk-marks it manual from the Errors tab."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pipeline import actions


class FakeSql:
    def __init__(self):
        self.statements = []

    def execute(self, statement, parameters=None):
        self.statements.append(" ".join(statement.split()))
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


def test_bulk_reuses_guarded_single(monkeypatch):
    fake = FakeSql()
    monkeypatch.setattr(actions, "get_sql", lambda: fake)
    monkeypatch.setattr(actions, "actor", lambda: "tester@local")

    n = actions.mark_manual_bulk("20260707", ["a", "b"])

    assert n == 2
    updates = [s for s in fake.statements if "SET status = 'manual'" in s]
    assert len(updates) == 2
    assert all("<> 'delivered'" in u for u in updates)
