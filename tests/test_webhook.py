"""The outbound webhook (F11), and the guarantee that matters more than delivery.

The review asked for "an optional outbound webhook - roughly five lines". These tests
exist because the five obvious lines would be a synchronous POST inside the ingester's
flush, and the ingester is a single-threaded loop: a slow or black-holed endpoint
would stall the write path while MQTT keeps redelivering. Losing an alert is a
nuisance and the anomaly is in the database anyway; losing readings is data loss.

So what is pinned here is mostly what the webhook must NOT do - block, raise, retry,
or grow without bound.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from gemp.ingest.webhook import Notification, Webhook

T0 = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def notification(severity: str = "critical", kind: str = "spike") -> Notification:
    return Notification(
        building_id="b001", ts=T0, observed_kw=150.0, expected_kw=100.0,
        robust_z=19.4, severity=severity, kind=kind,
    )


class Collector(BaseHTTPRequestHandler):
    received: list[dict] = []
    status = 200
    delay = 0.0

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's naming
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        Collector.received.append(json.loads(body))
        if Collector.delay:
            time.sleep(Collector.delay)
        self.send_response(Collector.status)
        self.end_headers()

    def log_message(self, *_args):
        pass


@pytest.fixture
def endpoint():
    Collector.received = []
    Collector.status = 200
    Collector.delay = 0.0

    server = HTTPServer(("127.0.0.1", 0), Collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/hook"
    server.shutdown()
    server.server_close()


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# --- delivery ---------------------------------------------------------------


def test_a_critical_anomaly_is_posted(endpoint):
    hook = Webhook(endpoint, min_severity="critical")
    try:
        hook.notify(notification())
        assert wait_for(lambda: len(Collector.received) == 1)
    finally:
        hook.stop()

    payload = Collector.received[0]
    assert payload["event"] == "anomaly.detected"
    assert payload["building_id"] == "b001"
    assert payload["severity"] == "critical"
    assert payload["kind"] == "spike"
    assert hook.sent == 1


def test_anomalies_below_the_threshold_are_suppressed(endpoint):
    """Eleven thousand alerts over sixteen months is not something to forward."""
    hook = Webhook(endpoint, min_severity="critical")
    try:
        hook.notify(notification(severity="medium"))
        hook.notify(notification(severity="high"))
        hook.notify(notification(severity="critical"))
        assert wait_for(lambda: len(Collector.received) == 1)
        time.sleep(0.2)                       # nothing else should arrive
    finally:
        hook.stop()

    assert len(Collector.received) == 1
    assert hook.suppressed == 2


def test_a_lower_threshold_forwards_more(endpoint):
    hook = Webhook(endpoint, min_severity="medium")
    try:
        hook.notify(notification(severity="medium"))
        hook.notify(notification(severity="critical"))
        assert wait_for(lambda: len(Collector.received) == 2)
    finally:
        hook.stop()


# --- what it must not do ----------------------------------------------------


def test_no_url_means_the_path_is_inert():
    """The demonstration default. Nobody reads email during a presentation."""
    hook = Webhook(None)
    assert not hook.enabled
    hook.notify(notification())               # must not raise, must not start a thread
    assert hook.sent == 0
    hook.stop()


def test_notify_does_not_block_on_a_slow_endpoint(endpoint):
    """The guarantee the whole design exists for.

    A synchronous POST here would hold up the ingester's flush, and MQTT would keep
    redelivering into a queue that cannot drain.
    """
    Collector.delay = 1.0
    hook = Webhook(endpoint, min_severity="medium")
    try:
        started = time.perf_counter()
        for _ in range(5):
            hook.notify(notification())
        elapsed = time.perf_counter() - started
    finally:
        hook.stop(timeout=0.5)

    assert elapsed < 0.2, f"notify blocked for {elapsed:.2f}s"


def test_an_unreachable_endpoint_is_counted_not_raised():
    """A dead notification target must not be able to break ingestion."""
    hook = Webhook("http://127.0.0.1:9/nothing-listens-here", min_severity="medium")
    try:
        hook.notify(notification())
        assert wait_for(lambda: hook.failed == 1)
    finally:
        hook.stop()

    assert hook.sent == 0


def test_a_rejecting_endpoint_is_counted_not_raised(endpoint):
    Collector.status = 500
    hook = Webhook(endpoint, min_severity="medium")
    try:
        hook.notify(notification())
        assert wait_for(lambda: hook.failed == 1)
    finally:
        hook.stop()


def test_the_queue_is_bounded_and_drops_rather_than_growing(endpoint):
    """Memory is not allowed to be the thing that fails."""
    from gemp.ingest import webhook as module

    Collector.delay = 0.5
    hook = Webhook(endpoint, min_severity="medium")
    try:
        for _ in range(module.QUEUE_DEPTH + 50):
            hook.notify(notification())
        assert hook.dropped > 0
        assert hook._queue.qsize() <= module.QUEUE_DEPTH
    finally:
        hook.stop(timeout=0.5)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",       # urlopen would READ this
    "ftp://example.internal/x",
    "notaurl",
    "https://",                 # a scheme and no host
])
def test_a_target_that_is_not_an_http_url_disables_the_webhook(url):
    """`urlopen` opens more than http, and a `file://` target turns a notifier into
    a local file reader. The URL is operator configuration rather than user input,
    so this is not request forgery - but a typo should disable the path with a
    logged reason at startup, not surface as a stack trace in a worker thread the
    first time a fault is detected."""
    assert Webhook(url).enabled is False


def test_an_http_target_still_works():
    """The guard must not be a way of never notifying anything."""
    assert Webhook("https://hooks.example.internal/gemp").enabled is True
    assert Webhook("http://localhost:9000/hook").enabled is True
