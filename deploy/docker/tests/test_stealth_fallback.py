"""Behavioral tests for the /md nodriver stealth fallback (api.py).

These tests exercise the detector and the worker client directly (no browser,
no running worker): they pin down which failure shapes trigger a stealth
retry, that non-bot-wall failures (SSRF 400, DNS, 404) never do, and that the
worker client maps success/failure/down to (markdown, title) / None / None.
"""

import asyncio
import sys
from pathlib import Path

import pytest

DOCKER_DIR = Path(__file__).resolve().parents[1]
if str(DOCKER_DIR) not in sys.path:
    sys.path.insert(0, str(DOCKER_DIR))

import api  # noqa: E402


def test_bot_wall_detector_triggers():
    d = api._looks_like_bot_wall
    # explicit bot-wall statuses
    assert d(403, None) is True
    assert d(429, None) is True
    assert d(503, None) is True
    # 200-with-challenge-text (the classic CF shape)
    assert d(None, "Please enable cookies.\nJust a moment...") is True
    assert d(None, "Attention Required! | Cloudflare") is True
    assert d(None, '<div id="challenge-platform"></div>') is True
    assert d(None, "Pardon Our Interruption") is True
    assert d(None, "Robot Check — enter the characters") is True
    # success but empty markdown
    assert d(None, "") is True
    assert d(None, "   \n\t") is True
    # 500 whose error text is a bot wall
    assert d(500, None, "HTTP 403: Just a moment") is True
    # this stack's own engine failure text for a CF trip (patchright)
    assert d(500, None, "Blocked by anti-bot protection: Cloudflare JS challenge") is True
    assert d(500, None, "Akamai request blocked") is True
    # marker buried past the 4000-char head is NOT checked (and does not fire)
    assert d(None, ("x" * 5000) + " captcha") is False


def test_bot_wall_detector_passes_through():
    d = api._looks_like_bot_wall
    # ordinary content is untouched
    assert d(None, "# Hanabi\nA cooperative card game about signaling.") is False
    # non-bot-wall failures: no stealth retry
    assert d(400, None, "URL blocked: private address") is False
    assert d(404, None, "Not Found") is False
    assert d(500, None, "net::ERR_NAME_NOT_RESOLVED at https://nope.example") is False
    assert d(500, None, "page.goto: Timeout 30000ms exceeded") is False
    assert d(500, None, None) is False


class _FakeResp:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self):
        return self._payload


def _fake_client(payload: dict, calls: list):
    class _Client:
        def __init__(self, *a, **k):
            calls.append(k)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            calls.append((url, json))
            return _FakeResp(payload)

    return _Client


def test_worker_fetch_success(monkeypatch):
    calls = []
    import httpx
    monkeypatch.setattr(
        httpx, "AsyncClient",
        _fake_client(
            {"url": "https://example.com", "markdown": "REAL CONTENT",
             "title": "Example", "success": True},
            calls,
        ),
    )
    monkeypatch.delenv("NODRIVER_STEALTH_ENABLED", raising=False)
    monkeypatch.setenv("NODRIVER_WORKER_URL", "http://127.0.0.1:8001/md")
    md, title = asyncio.run(api._stealth_worker_fetch("https://example.com"))
    assert md == "REAL CONTENT"
    assert title == "Example"
    # client used the 95s budget and posted the url contract
    assert calls and calls[-1][0] == "http://127.0.0.1:8001/md"
    assert calls[-1][1] == {"url": "https://example.com"}
    assert calls[0].get("timeout") == 95.0


def test_worker_fetch_reports_failure_as_none(monkeypatch):
    import httpx
    monkeypatch.setattr(
        httpx, "AsyncClient",
        _fake_client(
            {"url": "https://example.com", "markdown": "", "title": None,
             "success": False, "error": "cloudflare challenge still present"},
            [],
        ),
    )
    assert asyncio.run(api._stealth_worker_fetch("https://example.com")) is None


def test_worker_fetch_disabled(monkeypatch):
    import httpx

    def _boom(*a, **k):
        raise AssertionError("worker must not be contacted when disabled")

    monkeypatch.setattr(httpx, "AsyncClient", _boom)
    monkeypatch.setenv("NODRIVER_STEALTH_ENABLED", "false")
    assert asyncio.run(api._stealth_worker_fetch("https://example.com")) is None


def test_worker_down_returns_none(monkeypatch):
    import httpx

    class _Down:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise ConnectionError("refused")

    monkeypatch.setattr(httpx, "AsyncClient", _Down)
    monkeypatch.delenv("NODRIVER_STEALTH_ENABLED", raising=False)
    assert asyncio.run(api._stealth_worker_fetch("https://example.com")) is None
