"""Caller identity and the operator gate.

One place decides who is asking and what they may do. Today every caller may
operate the pipeline; flipping that on is a one-line change here plus an
OPERATOR_EMAILS value in app.yaml — no route or template changes.
"""
from __future__ import annotations

from flask import request

from .config import config


def current_user() -> str:
    """The caller's email. Databricks Apps injects it as X-Forwarded-Email;
    locally the header is absent and we fall back to the configured default."""
    header = (request.headers.get(config.USER_HEADER) or "").strip().lower()
    return header or config.DEFAULT_ANNOTATOR


def actor() -> str:
    """Who to record in pipeline_events and in ground-truth JSON."""
    return current_user()


def can_operate() -> bool:
    """May this caller launch jobs and mutate pipeline state?

    Empty OPERATOR_EMAILS (the default) = everyone may operate — today's
    behavior. Setting OPERATOR_EMAILS in app.yaml (comma-separated, matched
    lowercase) turns enforcement on with no other change.

    Annotation is deliberately NOT gated by this — annotators are the wide
    audience, operators the narrow one.
    """
    return (not config.OPERATOR_EMAILS) or current_user() in config.OPERATOR_EMAILS
