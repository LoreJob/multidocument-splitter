"""Unit tests for the shared on-disk page-render cache.

Contract: a page is rasterised once ever — the second request for the same page
(same zoom/quality) is served from disk without touching fitz; a different page
or a changed setting renders again; pre-warm fills the cache for every page; the
cache stays under its size cap.

Run: python -m pytest tests/test_render_cache.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.annotate import annotation


def _fake_renderer():
    """Replacement for _render_raw that counts calls and returns stable bytes."""
    calls = []

    def render(day_id, filename, n, base_path):
        calls.append((filename, n))
        return f"IMG:{filename}:{n}".encode()

    return render, calls


def _use_tmp_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(annotation, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(annotation, "_PREWARM_ON", True)


def test_second_render_is_cache_hit(monkeypatch, tmp_path):
    _use_tmp_cache(monkeypatch, tmp_path)
    render, calls = _fake_renderer()
    monkeypatch.setattr(annotation, "_render_raw", render)

    a = annotation.render_page_jpeg("20260801", "FILE_A", 3, "/vol/inbox/20260801")
    b = annotation.render_page_jpeg("20260801", "FILE_A", 3, "/vol/inbox/20260801")

    assert a == b == b"IMG:FILE_A:3"
    assert len(calls) == 1                      # rendered once, then cache hit
    assert list(tmp_path.glob("*.jpg"))          # a file landed on disk


def test_different_page_renders_again(monkeypatch, tmp_path):
    _use_tmp_cache(monkeypatch, tmp_path)
    render, calls = _fake_renderer()
    monkeypatch.setattr(annotation, "_render_raw", render)

    annotation.render_page_jpeg("d", "FILE_A", 3, "/base")
    annotation.render_page_jpeg("d", "FILE_A", 4, "/base")
    assert len(calls) == 2


def test_changed_quality_busts_cache(monkeypatch, tmp_path):
    _use_tmp_cache(monkeypatch, tmp_path)
    render, calls = _fake_renderer()
    monkeypatch.setattr(annotation, "_render_raw", render)

    annotation.render_page_jpeg("d", "FILE_A", 1, "/base")
    monkeypatch.setattr(annotation, "JPEG_QUALITY", 50)   # different key
    annotation.render_page_jpeg("d", "FILE_A", 1, "/base")
    assert len(calls) == 2


def test_prewarm_populates_all_pages(monkeypatch, tmp_path):
    _use_tmp_cache(monkeypatch, tmp_path)
    render, calls = _fake_renderer()
    monkeypatch.setattr(annotation, "_render_raw", render)

    # Run the warm thread synchronously so the assertion is deterministic.
    class _SyncThread:
        def __init__(self, target=None, **kw):
            self._t = target
        def start(self):
            self._t()
    monkeypatch.setattr(annotation.threading, "Thread", _SyncThread)

    annotation.prewarm("d", "FILE_A", 5, "/base")
    assert sorted(n for _, n in calls) == [1, 2, 3, 4, 5]
    assert len(list(tmp_path.glob("*.jpg"))) == 5


def test_evict_if_needed_caps_size(monkeypatch, tmp_path):
    monkeypatch.setattr(annotation, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(annotation, "_CACHE_MAX_MB", 1)   # 1 MB cap
    # 4 files × 400 KB = 1.6 MB > cap; oldest should be evicted to ≤ 80% (0.8 MB).
    import os
    for i in range(4):
        p = tmp_path / f"{i:040d}.jpg"
        p.write_bytes(b"x" * (400 * 1024))
        os.utime(p, (i, i))                              # ascending mtime
    annotation._evict_if_needed()

    remaining = sorted(int(p.stem) for p in tmp_path.glob("*.jpg"))
    total = sum(p.stat().st_size for p in tmp_path.glob("*.jpg"))
    assert total <= int(1 * 1024 * 1024 * 0.8)
    assert remaining and remaining[-1] == 3              # newest kept
    assert 0 not in remaining                            # oldest evicted first
