"""Oversized files (>100MB, moved to oversized/{day}/) must be annotatable in
the Manual tab exactly like parse failures that stayed in inbox/."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from databricks.sdk.errors import NotFound

from src.annotate import manual
from src.core.config import config

DAY = "20260501"
BIG = "E7J01631190_COMMERCIAL_INVOICE"     # 464 MB → oversized/
SMALL = "PARSE_FAILED_DOC"                 # parse failure → still in inbox/

INBOX = config.inbox_path(DAY)
OVERSIZED = config.volume_path(config.OVERSIZED_VOLUME, DAY)


class FakeVols:
    """Only SMALL exists in inbox/; BIG only in oversized/."""

    def __init__(self):
        self.probes = []

    def exists(self, path):
        self.probes.append(path)
        return path == f"{INBOX}/{SMALL}.pdf"

    @staticmethod
    def pdf_path(base, filename):
        return f"{base}/{filename}.pdf"


def _patch(monkeypatch, vols=None):
    vols = vols or FakeVols()
    monkeypatch.setattr(manual, "get_volumes", lambda: vols)
    manual._BASE_CACHE.clear()
    return vols


def test_oversized_file_resolves_to_oversized_volume(monkeypatch):
    _patch(monkeypatch)
    assert manual.source_base(DAY, BIG) == OVERSIZED


def test_parse_failure_still_resolves_to_inbox(monkeypatch):
    _patch(monkeypatch)
    assert manual.source_base(DAY, SMALL) == INBOX


def test_location_is_cached_not_probed_per_page(monkeypatch):
    vols = _patch(monkeypatch)
    for _ in range(5):
        manual.source_base(DAY, BIG)
    assert len(vols.probes) == 1          # one metadata call, not one per page


def test_forget_source_forces_a_new_probe(monkeypatch):
    vols = _patch(monkeypatch)
    manual.source_base(DAY, BIG)
    manual.forget_source(DAY, BIG)
    manual.source_base(DAY, BIG)
    assert len(vols.probes) == 2


def test_render_and_prewarm_use_the_resolved_base(monkeypatch):
    _patch(monkeypatch)
    seen = {}
    monkeypatch.setattr(manual.annotation, "render_page_jpeg",
                        lambda d, f, n, base: seen.setdefault("render", base) or b"jpg")
    monkeypatch.setattr(manual.annotation, "prewarm",
                        lambda d, f, total, base: seen.setdefault("prewarm", base))

    manual.render_page_jpeg(DAY, BIG, 3)
    manual.prewarm(DAY, BIG, 10)

    assert seen["render"] == OVERSIZED
    assert seen["prewarm"] == OVERSIZED


def test_page_count_retries_once_when_the_file_moved(monkeypatch):
    """A cached location that went stale (file archived after delivery) must be
    re-resolved instead of failing forever."""
    vols = _patch(monkeypatch)
    calls = []

    def flaky(day, name, base):
        calls.append(base)
        if len(calls) == 1:
            raise NotFound("gone")
        return 42

    monkeypatch.setattr(manual.annotation, "page_count", flaky)
    assert manual.page_count(DAY, BIG) == 42
    assert len(calls) == 2                # first attempt failed, second resolved
    assert len(vols.probes) == 2          # cache was dropped in between
