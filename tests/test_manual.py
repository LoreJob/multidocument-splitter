"""Unit tests for the manual (parse-failed) annotation path.

The contract: save_manual must (1) write a ground-truth JSON marked source=manual,
(2) insert a split_results row so the file becomes a delivery candidate, and
(3) NEVER touch evaluation_results — manual files are deliverable but unscored.

Run: python -m pytest tests/ (or python tests/test_manual.py).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.annotate import annotation, manual


class FakeSql:
    """Records every executed statement; str_param is a no-op placeholder."""
    def __init__(self):
        self.statements = []

    def str_param(self, name, value):
        return (name, value)

    def execute(self, stmt, parameters=None):
        self.statements.append(stmt)
        return []


class FakeVols:
    def __init__(self):
        self.uploaded = []

    @staticmethod
    def json_path(volume_path, filename):
        return f"{volume_path}/{filename}.json"

    def upload_json(self, path, payload):
        self.uploaded.append((path, payload))

    def list_json_stems(self, path):
        return set()


def _patch(monkeypatch):
    sql = FakeSql()
    vols = FakeVols()
    monkeypatch.setattr(manual, "get_sql", lambda: sql)
    monkeypatch.setattr(manual, "get_volumes", lambda: vols)
    # save_ground_truth lives in annotation and resolves volumes there.
    monkeypatch.setattr(annotation, "get_volumes", lambda: vols)
    # _log_event pulls actor() from a Flask request; stub it out here.
    monkeypatch.setattr(manual, "_log_event", lambda *a, **k: None)
    return sql, vols


def test_save_manual_writes_split_results(monkeypatch):
    sql, vols = _patch(monkeypatch)
    manual.save_manual(
        day_id="20260731", filename="ABC_123", starts=[1, 4],
        is_multidoc=True, total_pages=6, folder_id="ABC", annotator="me@x.com",
    )
    joined = "\n".join(sql.statements)
    assert "split_results" in joined
    assert any(s.strip().upper().startswith("INSERT INTO") and "split_results" in s
               for s in sql.statements)
    # delete-before-append keeps a re-save idempotent
    assert any(s.strip().upper().startswith("DELETE FROM") and "split_results" in s
               for s in sql.statements)


def test_save_manual_never_touches_evaluation(monkeypatch):
    sql, vols = _patch(monkeypatch)
    manual.save_manual(
        day_id="20260731", filename="ABC_123", starts=[1, 4],
        is_multidoc=True, total_pages=6, folder_id="ABC", annotator="me@x.com",
    )
    assert all("evaluation_results" not in s for s in sql.statements)


def test_save_manual_marks_gt_as_manual(monkeypatch):
    sql, vols = _patch(monkeypatch)
    manual.save_manual(
        day_id="20260731", filename="ABC_123", starts=[1, 4],
        is_multidoc=True, total_pages=6, folder_id="ABC", annotator="me@x.com",
    )
    assert len(vols.uploaded) == 1
    _, payload = vols.uploaded[0]
    assert payload["source"] == "manual"
    assert payload["predicted_starts"] == [1, 4]
    assert payload["annotator"] == "me@x.com"


def test_split_insert_uses_manual_boundary_source(monkeypatch):
    sql, vols = _patch(monkeypatch)
    manual.save_manual(
        day_id="20260731", filename="ABC_123", starts=[1, 4],
        is_multidoc=True, total_pages=6, folder_id="ABC", annotator="me@x.com",
    )
    insert = next(s for s in sql.statements
                  if s.strip().upper().startswith("INSERT INTO") and "split_results" in s)
    assert "'manual'" in insert          # boundary_source / model_used / run_id
    assert "array(1, 4)" in insert       # human starts as an int-literal array
    assert "FALSE" in insert             # needs_review=false → not blocked


if __name__ == "__main__":
    import types

    class _MP:
        """Minimal monkeypatch shim so the file runs without pytest too."""
        def __init__(self):
            self._undo = []

        def setattr(self, obj, name, value):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, old in reversed(self._undo):
                setattr(obj, name, old)

    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and isinstance(v, types.FunctionType)]
    for fn in fns:
        mp = _MP()
        try:
            fn(mp)
            print(f"ok  {fn.__name__}")
        finally:
            mp.undo()
    print(f"\n{len(fns)} passed")
