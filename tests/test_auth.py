"""can_operate(): empty OPERATOR_EMAILS keeps allow-all; a non-empty list
gates on the X-Forwarded-Email header (lowercased)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask

from src.core import auth
from src.core.config import config

app = Flask(__name__)


def _ctx(email=None):
    headers = {config.USER_HEADER: email} if email else {}
    return app.test_request_context("/", headers=headers)


def test_empty_list_allows_everyone(monkeypatch):
    monkeypatch.setattr(config, "OPERATOR_EMAILS", [])
    with _ctx("anyone@luxottica.com"):
        assert auth.can_operate() is True
    with _ctx():  # no header → DEFAULT_ANNOTATOR fallback
        assert auth.can_operate() is True


def test_listed_operator_allowed(monkeypatch):
    monkeypatch.setattr(config, "OPERATOR_EMAILS", ["ops@luxottica.com"])
    with _ctx("ops@luxottica.com"):
        assert auth.can_operate() is True


def test_header_matched_case_insensitively(monkeypatch):
    monkeypatch.setattr(config, "OPERATOR_EMAILS", ["ops@luxottica.com"])
    with _ctx("OPS@Luxottica.com"):
        assert auth.can_operate() is True


def test_unlisted_caller_denied(monkeypatch):
    monkeypatch.setattr(config, "OPERATOR_EMAILS", ["ops@luxottica.com"])
    with _ctx("intruder@luxottica.com"):
        assert auth.can_operate() is False


def test_missing_header_denied_when_gated(monkeypatch):
    monkeypatch.setattr(config, "OPERATOR_EMAILS", ["ops@luxottica.com"])
    monkeypatch.setattr(config, "DEFAULT_ANNOTATOR", "local@local")
    with _ctx():
        assert auth.can_operate() is False
