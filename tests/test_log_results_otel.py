"""Tests for the OTel OTLP push path in eval-mlflow/scripts/log_results.py."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add skills/eval-mlflow/scripts to path so log_results.py is importable
_SCRIPTS = Path(__file__).parent.parent / "skills" / "eval-mlflow" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from log_results import _push_otel_spans


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_otel_path(tmp_path, resource_spans=None):
    """Write a minimal otel_spans.json and return the path."""
    if resource_spans is None:
        resource_spans = [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "claude-code"}},
                    ],
                },
                "scopeSpans": [{
                    "spans": [{
                        "traceId": "a" * 32,
                        "spanId": "b" * 16,
                        "name": "claude_code.interaction",
                        "startTimeUnixNano": "1000000000",
                        "endTimeUnixNano": "5000000000",
                    }],
                }],
            }
        ]
    path = tmp_path / "otel_spans.json"
    path.write_text(json.dumps({"resourceSpans": resource_spans}))
    return path


def _mock_response(status_code=200):
    """Build a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        import requests
        resp.raise_for_status.side_effect = requests.HTTPError(
            f"HTTP {status_code}", response=resp)
    return resp


# ── HTTP request correctness ──────────────────────────────────────────────────

class TestPushOtelSpansRequest:

    def test_posts_to_v1_traces(self, tmp_path):
        """POST is sent to <tracking_uri>/v1/traces."""
        otel = _make_otel_path(tmp_path)
        with patch("requests.post", return_value=_mock_response()) as mock_post:
            _push_otel_spans(otel, "http://localhost:5000", "exp-42")
        url = mock_post.call_args[0][0]
        assert url == "http://localhost:5000/v1/traces"

    def test_strips_trailing_slash_from_uri(self, tmp_path):
        """Trailing slash on tracking_uri is stripped before /v1/traces."""
        otel = _make_otel_path(tmp_path)
        with patch("requests.post", return_value=_mock_response()) as mock_post:
            _push_otel_spans(otel, "http://localhost:5000/", "exp-42")
        url = mock_post.call_args[0][0]
        assert url == "http://localhost:5000/v1/traces"

    def test_content_type_header_set(self, tmp_path):
        """Content-Type: application/json is always sent."""
        otel = _make_otel_path(tmp_path)
        with patch("requests.post", return_value=_mock_response()) as mock_post:
            _push_otel_spans(otel, "http://localhost:5000", "exp-42")
        headers = mock_post.call_args.kwargs["headers"]
        assert headers["Content-Type"] == "application/json"

    def test_experiment_id_header_set(self, tmp_path):
        """x-mlflow-experiment-id header is always sent."""
        otel = _make_otel_path(tmp_path)
        with patch("requests.post", return_value=_mock_response()) as mock_post:
            _push_otel_spans(otel, "http://localhost:5000", "exp-99")
        headers = mock_post.call_args.kwargs["headers"]
        assert headers["x-mlflow-experiment-id"] == "exp-99"

    def test_run_id_header_included_when_provided(self, tmp_path):
        """x-mlflow-run-id is sent when run_id is given."""
        otel = _make_otel_path(tmp_path)
        with patch("requests.post", return_value=_mock_response()) as mock_post:
            _push_otel_spans(otel, "http://localhost:5000", "exp-1",
                             run_id="run-abc-123")
        headers = mock_post.call_args.kwargs["headers"]
        assert headers["x-mlflow-run-id"] == "run-abc-123"

    def test_run_id_header_absent_when_none(self, tmp_path):
        """x-mlflow-run-id is NOT sent when run_id is None."""
        otel = _make_otel_path(tmp_path)
        with patch("requests.post", return_value=_mock_response()) as mock_post:
            _push_otel_spans(otel, "http://localhost:5000", "exp-1", run_id=None)
        headers = mock_post.call_args.kwargs["headers"]
        assert "x-mlflow-run-id" not in headers

    def test_json_body_matches_file_content(self, tmp_path):
        """The request body is the parsed JSON from otel_spans.json."""
        resource_spans = [{"resource": {}, "scopeSpans": []}]
        otel = _make_otel_path(tmp_path, resource_spans=resource_spans)
        with patch("requests.post", return_value=_mock_response()) as mock_post:
            _push_otel_spans(otel, "http://localhost:5000", "exp-1")
        body = mock_post.call_args.kwargs["json"]
        assert body == {"resourceSpans": resource_spans}

    def test_timeout_is_set(self, tmp_path):
        """A 30-second timeout is passed to requests.post."""
        otel = _make_otel_path(tmp_path)
        with patch("requests.post", return_value=_mock_response()) as mock_post:
            _push_otel_spans(otel, "http://localhost:5000", "exp-1")
        assert mock_post.call_args.kwargs["timeout"] == 30

    def test_returns_response_object(self, tmp_path):
        """The raw response is returned to the caller."""
        otel = _make_otel_path(tmp_path)
        fake_resp = _mock_response()
        with patch("requests.post", return_value=fake_resp):
            result = _push_otel_spans(otel, "http://localhost:5000", "exp-1")
        assert result is fake_resp


# ── Error handling ────────────────────────────────────────────────────────────

class TestPushOtelSpansErrors:

    def test_raises_on_http_error(self, tmp_path):
        """HTTP 4xx/5xx raises via raise_for_status()."""
        import requests
        otel = _make_otel_path(tmp_path)
        with patch("requests.post", return_value=_mock_response(500)):
            with pytest.raises(requests.HTTPError):
                _push_otel_spans(otel, "http://localhost:5000", "exp-1")

    def test_raises_on_missing_file(self, tmp_path):
        """FileNotFoundError propagates when otel_spans.json doesn't exist."""
        missing = tmp_path / "otel_spans.json"
        with pytest.raises(FileNotFoundError):
            _push_otel_spans(missing, "http://localhost:5000", "exp-1")

    def test_raises_on_invalid_json(self, tmp_path):
        """json.JSONDecodeError propagates when file contains invalid JSON."""
        bad = tmp_path / "otel_spans.json"
        bad.write_text("not valid json")
        with pytest.raises(json.JSONDecodeError):
            _push_otel_spans(bad, "http://localhost:5000", "exp-1")

    def test_empty_resource_spans_still_posts(self, tmp_path):
        """Empty resourceSpans list is a valid payload — posts without error."""
        otel = _make_otel_path(tmp_path, resource_spans=[])
        with patch("requests.post", return_value=_mock_response()) as mock_post:
            _push_otel_spans(otel, "http://localhost:5000", "exp-1")
        body = mock_post.call_args.kwargs["json"]
        assert body == {"resourceSpans": []}
