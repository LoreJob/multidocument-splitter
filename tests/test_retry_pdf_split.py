"""retry-pdf-split: re-queue a file whose PHYSICAL split failed.

Born from 20260515, where four hand-annotated oversized files ended at
'sftp failed: pdf_split failed: no such file'. nb_pdf_split skips anything with
sftp_delivery_status IS NOT NULL, so 'failed' means "never retried"; and
retry_sftp ('pending') only moves it to an upload with no files to upload.
Only NULL brings the file back — with its hand-drawn boundaries intact.
"""
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


def _run(monkeypatch):
    fake = FakeSql()
    monkeypatch.setattr(actions, "get_sql", lambda: fake)
    monkeypatch.setattr(actions, "actor", lambda: "tester@local")
    msg = actions.retry_pdf_split("20260515", "BIM14234061_COMMERCIAL_INVOICE")
    return fake, msg


def test_resets_to_null_not_pending(monkeypatch):
    """'pending' would leave the file outside nb_pdf_split's candidate set."""
    fake, msg = _run(monkeypatch)
    upd = next(s for s in fake.statements if "UPDATE" in s and "sftp_delivery_status" in s)
    assert "SET sftp_delivery_status = NULL" in upd
    assert "= 'pending'" not in upd
    assert "requeued" in msg


def test_never_deletes_the_hand_drawn_boundaries(monkeypatch):
    """Unlike retry_split: on a manual file that row IS the human's work."""
    fake, _ = _run(monkeypatch)
    assert not any("DELETE" in s for s in fake.statements)
    assert not any("split_results" in s for s in fake.statements)


def test_only_touches_failed_rows(monkeypatch):
    """A delivered or in-flight file must never be dragged back."""
    fake, _ = _run(monkeypatch)
    upd = next(s for s in fake.statements if "UPDATE" in s)
    assert "sftp_delivery_status = 'failed'" in upd.split("WHERE", 1)[1]


def test_writes_an_audit_event(monkeypatch):
    fake, _ = _run(monkeypatch)
    ev = next(s for s in fake.statements if "INSERT INTO" in s)
    assert "pipeline_events" in ev


def test_is_registered_as_an_action():
    assert actions.ACTIONS["retry-pdf-split"] is actions.retry_pdf_split
